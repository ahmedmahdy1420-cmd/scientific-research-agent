"""HTTP client for the external clinical-trials registry.

External APIs are unreliable; that is a design input, not an incident. The
policy here is explicit per status class:

    timeout            -> retry (transient)
    connection error   -> retry (transient)
    429                -> retry, honouring Retry-After
    500 / 502/503/504  -> retry with exponential backoff + jitter
    400 / 422          -> do NOT retry: the request is wrong, repeating it
                          wastes the budget and the upstream's capacity
    401 / 403          -> STOP immediately, do not retry, surface loudly:
                          credentials are broken and retrying looks like an
                          attack
    404                -> a controlled "not found" result, not an exception

Jitter matters: without it, every worker that failed at the same moment retries
at the same moment, and the upstream gets a synchronised stampede exactly when
it is least able to cope.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import (
    AuthenticationError,
    ExternalNotFoundError,
    ExternalServiceError,
    ToolTimeoutError,
)
from app.core.logging import get_logger

log = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
FATAL_STATUS = frozenset({401, 403})


class ClinicalTrialsClient:
    """Async client with bounded retries. One instance per process."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._settings.clinical_trials_api_url,
            timeout=httpx.Timeout(
                self._settings.external_http_timeout_seconds,
                connect=min(5.0, self._settings.external_http_timeout_seconds),
            ),
            headers={
                "X-API-Key": self._settings.clinical_trials_api_key,
                "Accept": "application/json",
                "User-Agent": f"{self._settings.app_name}/agent",
            },
            # Bounded pool: an agent fan-out must not open unlimited sockets.
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ core
    async def _request(self, method: str, url: str, **kwargs: Any) -> tuple[dict[str, Any], int]:
        """Perform a request with retry. Returns (json, retry_count)."""
        attempts = max(1, self._settings.external_http_max_retries)
        delay = 0.4
        retries = 0
        last_error: str = "unknown"

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {exc}"
                if attempt == attempts:
                    raise ToolTimeoutError(
                        "Clinical-trials API timed out", details={"attempts": attempts}
                    ) from exc
            except httpx.ConnectError as exc:
                last_error = f"connect: {exc}"
                if attempt == attempts:
                    raise ExternalServiceError(
                        "Cannot reach the clinical-trials API",
                        details={"attempts": attempts},
                    ) from exc
            except httpx.HTTPError as exc:
                last_error = f"http: {exc}"
                if attempt == attempts:
                    raise ExternalServiceError(
                        f"Clinical-trials API transport error: {exc}"
                    ) from exc
            else:
                status = response.status_code

                if status in FATAL_STATUS:
                    # Never retry an auth failure.
                    log.error("external.auth_failed", service="clinical_trials", status=status)
                    raise AuthenticationError(
                        "Clinical-trials API rejected our credentials",
                        details={"status": status},
                    )

                if status == 404:
                    raise ExternalNotFoundError(
                        "No such record in the clinical-trials registry",
                        details={"url": url},
                    )

                if 400 <= status < 500 and status not in RETRYABLE_STATUS:
                    body = response.text[:300]
                    log.warning("external.client_error", status=status, url=url, body=body)
                    raise ExternalServiceError(
                        f"Clinical-trials API rejected the request ({status})",
                        details={"status": status, "body": body},
                    )

                if status in RETRYABLE_STATUS:
                    last_error = f"status {status}"
                    if attempt == attempts:
                        raise ExternalServiceError(
                            f"Clinical-trials API failed after {attempts} attempts ({last_error})",
                            details={"status": status},
                        )
                    # Honour Retry-After when the server sends one.
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        delay = min(float(retry_after), 10.0)
                else:
                    try:
                        return response.json(), retries
                    except ValueError as exc:
                        raise ExternalServiceError(
                            "Clinical-trials API returned a non-JSON body"
                        ) from exc

            retries += 1
            sleep_for = delay + random.uniform(0, delay * 0.5)  # noqa: S311 - jitter, not crypto
            log.warning(
                "external.retry",
                service="clinical_trials",
                url=url,
                attempt=attempt,
                max_attempts=attempts,
                reason=last_error,
                sleep_s=round(sleep_for, 2),
            )
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, 8.0)

        raise ExternalServiceError(
            f"Clinical-trials API failed after {attempts} attempts ({last_error})"
        )

    # ------------------------------------------------------------------- api
    async def search_trials(
        self,
        condition: str,
        *,
        phase: str | None = None,
        status: str | None = None,
        intervention: str | None = None,
        min_enrollment: int | None = None,
        limit: int = 10,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Search trials. Returns (results, total, retry_count)."""
        params: dict[str, Any] = {"condition": condition, "limit": limit}
        if phase:
            params["phase"] = phase
        if status:
            params["status"] = status
        if intervention:
            params["intervention"] = intervention
        if min_enrollment is not None:
            params["min_enrollment"] = min_enrollment

        started = time.perf_counter()
        payload, retries = await self._request("GET", "/api/trials/search", params=params)
        log.info(
            "external.call",
            service="clinical_trials",
            op="search",
            latency_ms=int((time.perf_counter() - started) * 1000),
            retries=retries,
            count=len(payload.get("results", [])),
        )
        return payload.get("results", []), int(payload.get("total", 0)), retries

    async def get_trial(self, registry_id: str) -> dict[str, Any]:
        payload, _ = await self._request("GET", f"/api/trials/{registry_id}")
        return payload

    async def health(self) -> bool:
        try:
            await self._request("GET", "/health")
            return True
        except Exception:  # noqa: BLE001 - health probe never raises
            return False


_client: ClinicalTrialsClient | None = None


def get_clinical_client() -> ClinicalTrialsClient:
    global _client
    if _client is None:
        _client = ClinicalTrialsClient()
    return _client


def set_clinical_client(client: ClinicalTrialsClient | None) -> None:
    """Test hook."""
    global _client
    _client = client


async def close_clinical_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
