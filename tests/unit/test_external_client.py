"""Retry, backoff and error classification for the external REST dependency.

Driven with respx rather than the live mock service so the timing is
deterministic and the assertions are about *policy*: which statuses retry,
which stop immediately, and how many attempts are made.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.core.errors import (
    AuthenticationError,
    ExternalNotFoundError,
    ExternalServiceError,
    ToolTimeoutError,
)
from app.tools.external.clinical_client import ClinicalTrialsClient

pytestmark = pytest.mark.unit

BASE = "http://clinical.test"
SEARCH = f"{BASE}/api/trials/search"


@pytest.fixture
def client():
    settings = get_settings().model_copy(
        update={
            "clinical_trials_api_url": BASE,
            "external_http_max_retries": 3,
            "external_http_timeout_seconds": 1.0,
        }
    )
    return ClinicalTrialsClient(settings)


@respx.mock
async def test_success_makes_exactly_one_call(client):
    route = respx.get(SEARCH).mock(
        return_value=httpx.Response(200, json={"results": [{"registry_id": "NCT1"}], "total": 1})
    )
    results, total, retries = await client.search_trials("melanoma")
    assert len(results) == 1
    assert total == 1
    assert retries == 0
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_500_is_retried_then_surfaces(client):
    route = respx.get(SEARCH).mock(return_value=httpx.Response(500, json={"detail": "boom"}))
    with pytest.raises(ExternalServiceError):
        await client.search_trials("melanoma")
    assert route.call_count == 3  # the configured attempt budget
    await client.aclose()


@respx.mock
async def test_transient_failure_then_success(client):
    route = respx.get(SEARCH).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"results": [{"registry_id": "NCT2"}], "total": 1}),
        ]
    )
    results, _total, retries = await client.search_trials("melanoma")
    assert len(results) == 1
    assert retries == 1  # recovered on the second attempt
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_timeout_is_retried_then_classified(client):
    route = respx.get(SEARCH).mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(ToolTimeoutError):
        await client.search_trials("melanoma")
    assert route.call_count == 3
    await client.aclose()


@respx.mock
async def test_connection_error_is_retried(client):
    route = respx.get(SEARCH).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ExternalServiceError):
        await client.search_trials("melanoma")
    assert route.call_count == 3
    await client.aclose()


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_stops_immediately(client, status):
    """Never retry an auth failure: it will not fix itself and repeated
    attempts look like an attack to the upstream."""
    route = respx.get(SEARCH).mock(return_value=httpx.Response(status))
    with pytest.raises(AuthenticationError):
        await client.search_trials("melanoma")
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_400_is_not_retried(client):
    """A malformed request fails identically every time."""
    route = respx.get(SEARCH).mock(return_value=httpx.Response(400, json={"detail": "bad"}))
    with pytest.raises(ExternalServiceError):
        await client.search_trials("melanoma")
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_404_is_a_controlled_not_found(client):
    route = respx.get(f"{BASE}/api/trials/NCT404").mock(return_value=httpx.Response(404))
    with pytest.raises(ExternalNotFoundError):
        await client.get_trial("NCT404")
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_non_json_body_is_rejected_not_guessed(client):
    respx.get(SEARCH).mock(
        return_value=httpx.Response(
            200, text="<html>not json</html>", headers={"Content-Type": "application/json"}
        )
    )
    with pytest.raises(ExternalServiceError, match="non-JSON"):
        await client.search_trials("melanoma")
    await client.aclose()


@respx.mock
async def test_api_key_header_is_sent(client):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"results": [], "total": 0})

    respx.get(SEARCH).mock(side_effect=handler)
    await client.search_trials("melanoma")
    assert captured.get("x-api-key")
    await client.aclose()


@respx.mock
async def test_filters_are_passed_through_as_query_params(client):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"results": [], "total": 0})

    respx.get(SEARCH).mock(side_effect=handler)
    await client.search_trials("melanoma", phase="Phase 3", status="recruiting", limit=7)
    assert captured["condition"] == "melanoma"
    assert captured["phase"] == "Phase 3"
    assert captured["status"] == "recruiting"
    assert captured["limit"] == "7"
    await client.aclose()
