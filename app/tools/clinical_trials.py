"""Clinical-trials tool: external REST API with a database fallback.

Graceful degradation is the point. If the registry is unreachable the agent
does not fail the whole run; it answers from the locally cached
`clinical_trials` table and marks the result `degraded`, which the synthesis
prompt turns into an explicit caveat in the answer. The user learns that the
data may be stale instead of receiving either a 502 or a confidently stale
answer with no warning.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.auth.permissions import Permission
from app.core.errors import (
    AuthenticationError,
    ExternalNotFoundError,
    ExternalServiceError,
    ToolTimeoutError,
)
from app.core.logging import get_logger
from app.models.research import ClinicalTrial
from app.schemas.agent import EvidenceItem
from app.schemas.search import ClinicalTrialSearchRequest
from app.tools.base import Tool, ToolContext, ToolResult
from app.tools.external.clinical_client import ClinicalTrialsClient, get_clinical_client

log = get_logger(__name__)


def _trial_evidence(trial: dict, origin: str) -> EvidenceItem:
    return EvidenceItem(
        source_type="clinical_trial",
        source_id=f"trial:{trial.get('registry_id')}",
        title=trial.get("title", "Untitled trial"),
        snippet=(
            f"{trial.get('title')} - {trial.get('phase')}, status "
            f"{trial.get('status')}, condition {trial.get('condition')}, "
            f"intervention {trial.get('intervention') or 'n/a'}, "
            f"enrolment {trial.get('enrollment') or 'n/a'}. "
            f"Primary outcome: {trial.get('primary_outcome') or 'n/a'}. "
            f"{trial.get('summary') or ''}"
        )[:1200],
        origin_tool=origin,
        publication_date=(
            dt.date.fromisoformat(trial["start_date"])
            if isinstance(trial.get("start_date"), str)
            else trial.get("start_date")
        ),
    )


class SearchClinicalTrialsTool(Tool):
    name = "search_clinical_trials"
    description = (
        "Search the clinical-trials registry by condition, with optional phase, "
        "status, intervention and minimum enrolment filters. Returns registry "
        "ids, phases, status, enrolment and primary outcomes."
    )
    input_model = ClinicalTrialSearchRequest
    required_permission = Permission.SEARCH_TRIALS
    #: Cacheable: registry data is public and identical for every caller, so a
    #: shared cache entry cannot leak anything. TTL keeps it reasonably fresh.
    cacheable = True
    cache_ttl_seconds = 600
    timeout_seconds = 20.0

    def __init__(self, client: ClinicalTrialsClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> ClinicalTrialsClient:
        return self._client or get_clinical_client()

    async def run(self, args: ClinicalTrialSearchRequest, ctx: ToolContext) -> ToolResult:
        retries = 0
        try:
            results, total, retries = await self.client.search_trials(
                args.condition,
                phase=args.phase.value if args.phase else None,
                status=args.status.value if args.status else None,
                intervention=args.intervention,
                min_enrollment=args.min_enrollment,
                limit=args.limit,
            )
        except ExternalNotFoundError:
            return ToolResult(
                tool=self.name,
                ok=True,
                summary=f"The registry has no trials for {args.condition!r}.",
                data=[],
                count=0,
                retry_count=retries,
            )
        except AuthenticationError as exc:
            # Never silently degrade an auth failure: it is an ops problem.
            log.error("tool.external_auth_failed", tool=self.name, error=exc.message)
            return ToolResult.failure(
                self.name,
                "external_auth_failed",
                "The clinical-trials API rejected our credentials",
            )
        except (ExternalServiceError, ToolTimeoutError) as exc:
            log.warning("tool.degrading_to_cache", tool=self.name, error=exc.message)
            return await self._fallback_to_database(args, ctx, reason=exc.message)

        evidence = [_trial_evidence(t, self.name) for t in results]
        return ToolResult(
            tool=self.name,
            ok=True,
            summary=f"{len(results)} of {total} registry trial(s) for {args.condition!r}.",
            data=results,
            evidence=evidence,
            count=len(results),
            retry_count=retries,
        )

    async def _fallback_to_database(
        self, args: ClinicalTrialSearchRequest, ctx: ToolContext, *, reason: str
    ) -> ToolResult:
        """Serve the locally mirrored registry table, clearly marked stale."""
        async with ctx.session_factory() as session:
            stmt = (
                select(ClinicalTrial)
                .where(ClinicalTrial.condition.ilike(f"%{args.condition}%"))
                .order_by(ClinicalTrial.start_date.desc())
                .limit(args.limit)
            )
            if args.phase:
                stmt = stmt.where(ClinicalTrial.phase == args.phase)
            if args.status:
                stmt = stmt.where(ClinicalTrial.status == args.status)
            rows = (await session.execute(stmt)).unique().scalars().all()

        data = [
            {
                "registry_id": t.registry_id,
                "title": t.title,
                "condition": t.condition,
                "intervention": t.intervention,
                "phase": t.phase.value,
                "status": t.status.value,
                "sponsor": t.sponsor,
                "enrollment": t.enrollment,
                "start_date": t.start_date.isoformat() if t.start_date else None,
                "completion_date": t.completion_date.isoformat() if t.completion_date else None,
                "primary_outcome": t.primary_outcome,
                "summary": t.summary,
                "locations": t.locations,
            }
            for t in rows
        ]
        return ToolResult(
            tool=self.name,
            ok=True,
            summary=(
                f"Registry unavailable ({reason}); answered from the local mirror: "
                f"{len(rows)} trial(s). Data may be out of date."
            ),
            data=data,
            evidence=[_trial_evidence(t, self.name) for t in data],
            count=len(rows),
            degraded=True,
        )
