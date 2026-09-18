"""Mock clinical-trials registry.

Stands in for a real public registry so the project is self-contained and CI is
deterministic. It is deliberately *not* a perfect service: `/api/trials/search`
supports a `?simulate=` parameter that produces the failure modes the agent's
HTTP client must survive — timeouts, 500s, 429s, malformed bodies.

That is what the resilience tests drive. A retry policy that has only ever been
exercised against a healthy dependency is an untested retry policy.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "sample" / "clinical_trials.json"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _load()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Mock Clinical Trials Registry",
    description=(
        "Stand-in for an external trials registry. Supports deliberate failure "
        "injection via ?simulate= so the agent's retry and degradation paths can "
        "be tested."
    ),
    version="1.0.0",
)

_TRIALS: list[dict[str, Any]] = []


def _load() -> list[dict[str, Any]]:
    global _TRIALS
    if not _TRIALS and DATA_FILE.exists():
        _TRIALS = json.loads(DATA_FILE.read_text())
    return _TRIALS


# =============================================================================
# Auth
# =============================================================================
VALID_KEYS = {"demo-clinical-api-key", "ci-test-key"}


async def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str:
    """Simple API-key auth, so the client's 401/403 handling is real."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header is required")
    if x_api_key not in VALID_KEYS:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return x_api_key


# =============================================================================
# Schemas
# =============================================================================
class Trial(BaseModel):
    registry_id: str
    title: str
    condition: str
    intervention: str | None = None
    phase: str
    status: str
    sponsor: str | None = None
    enrollment: int | None = None
    start_date: dt.date | None = None
    completion_date: dt.date | None = None
    primary_outcome: str | None = None
    summary: str | None = None
    locations: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    total: int
    count: int
    results: list[Trial]


SimulateMode = Literal[
    "ok", "timeout", "server_error", "bad_gateway", "rate_limit", "malformed", "flaky"
]


async def _apply_simulation(mode: SimulateMode, request: Request) -> Response | None:
    """Return a failure response when the caller asked for one."""
    if mode == "ok":
        return None
    if mode == "timeout":
        await asyncio.sleep(30)  # longer than any sane client timeout
        return None
    if mode == "server_error":
        return JSONResponse({"detail": "Internal registry error"}, status_code=500)
    if mode == "bad_gateway":
        return JSONResponse({"detail": "Upstream unavailable"}, status_code=502)
    if mode == "rate_limit":
        return JSONResponse(
            {"detail": "Too many requests"},
            status_code=429,
            headers={"Retry-After": "1"},
        )
    if mode == "malformed":
        return Response(content="<html>not json</html>", media_type="application/json")
    if mode == "flaky":
        # Fails roughly half the time: exercises "succeeds on retry".
        if random.random() < 0.5:  # noqa: S311 - test fixture, not crypto
            return JSONResponse({"detail": "Transient failure"}, status_code=503)
        return None
    return None


# =============================================================================
# Routes
# =============================================================================
@app.get("/health", tags=["ops"])
async def health() -> dict[str, Any]:
    return {"status": "ok", "trials_loaded": len(_load())}


@app.get(
    "/api/trials/search",
    response_model=SearchResponse,
    tags=["trials"],
    summary="Search clinical trials",
)
async def search_trials(
    request: Request,
    _key: Annotated[str, Depends(require_api_key)],
    condition: Annotated[str, Query(min_length=2, max_length=255)],
    phase: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    intervention: Annotated[str | None, Query(max_length=255)] = None,
    min_enrollment: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    simulate: Annotated[SimulateMode, Query(description="Inject a failure mode")] = "ok",
) -> Any:
    failure = await _apply_simulation(simulate, request)
    if failure is not None:
        return failure

    needle = condition.lower().strip()
    matches = [
        t
        for t in _load()
        if needle in t["condition"].lower()
        or needle in t["title"].lower()
        or any(needle in word for word in t["condition"].lower().split())
    ]
    if phase:
        matches = [t for t in matches if t["phase"].lower() == phase.lower()]
    if status:
        matches = [t for t in matches if t["status"].lower() == status.lower()]
    if intervention:
        needle_i = intervention.lower()
        matches = [t for t in matches if needle_i in (t.get("intervention") or "").lower()]
    if min_enrollment is not None:
        matches = [t for t in matches if (t.get("enrollment") or 0) >= min_enrollment]

    matches.sort(key=lambda t: t.get("start_date") or "", reverse=True)
    page = matches[:limit]
    return SearchResponse(total=len(matches), count=len(page), results=[Trial(**t) for t in page])


@app.get(
    "/api/trials/{registry_id}",
    response_model=Trial,
    tags=["trials"],
    summary="Get one trial",
    responses={404: {"description": "No such trial"}},
)
async def get_trial(
    request: Request,
    registry_id: str,
    _key: Annotated[str, Depends(require_api_key)],
    simulate: Annotated[SimulateMode, Query()] = "ok",
) -> Any:
    failure = await _apply_simulation(simulate, request)
    if failure is not None:
        return failure

    for trial in _load():
        if trial["registry_id"].lower() == registry_id.lower():
            return Trial(**trial)
    raise HTTPException(status_code=404, detail=f"No trial with id {registry_id}")


@app.get("/api/trials", response_model=SearchResponse, tags=["trials"])
async def list_trials(
    _key: Annotated[str, Depends(require_api_key)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    trials = _load()
    page = trials[offset : offset + limit]
    return SearchResponse(total=len(trials), count=len(page), results=[Trial(**t) for t in page])
