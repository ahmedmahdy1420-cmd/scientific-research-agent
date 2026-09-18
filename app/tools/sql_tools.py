"""Controlled database tools.

The LLM never writes SQL on the production path. It picks a *named* tool and
supplies typed parameters; the SQL itself is written by us, parameterised
through SQLAlchemy, bounded by a LIMIT, and executed as a read-only role.

Each tool also re-applies the caller's access rules. A restricted experiment is
filtered out in the WHERE clause, so it is never loaded, never summarised and
never citable — regardless of what the model asked for.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field
from sqlalchemy import Text, func, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import Permission, Principal
from app.core.logging import get_logger
from app.models.enums import AccessLevel, ExperimentStatus
from app.models.research import Compound, Experiment, ExperimentResult
from app.schemas.agent import EvidenceItem
from app.tools.base import Tool, ToolContext, ToolResult

log = get_logger(__name__)

#: Hard cap on rows any SQL tool may return, independent of what was requested.
MAX_ROWS = 50


def _visible_experiment_levels(principal: Principal) -> list[AccessLevel]:
    if principal.has_permission(Permission.EXPERIMENTS_READ_RESTRICTED):
        return [AccessLevel.PUBLIC, AccessLevel.INTERNAL, AccessLevel.RESTRICTED]
    return [AccessLevel.PUBLIC, AccessLevel.INTERNAL]


async def _load_experiment(
    session: AsyncSession, identifier: str, principal: Principal
) -> Experiment | None:
    """Resolve an experiment by UUID or by human-readable code (EXP-0001)."""
    stmt = select(Experiment).where(
        Experiment.access_level.in_(_visible_experiment_levels(principal))
    )
    try:
        stmt = stmt.where(Experiment.id == uuid.UUID(identifier))
    except (ValueError, AttributeError):
        stmt = stmt.where(func.upper(Experiment.code) == identifier.strip().upper())
    return (await session.execute(stmt)).unique().scalar_one_or_none()


# =============================================================================
# get_experiment
# =============================================================================
class GetExperimentRequest(BaseModel):
    experiment_id_or_code: str = Field(
        min_length=1, max_length=64, description="Experiment UUID or code such as EXP-0007"
    )


class GetExperimentTool(Tool):
    name = "get_experiment"
    description = (
        "Fetch one internal experiment by UUID or code (e.g. EXP-0007): "
        "hypothesis, methodology, model system, sample size and linked compound."
    )
    input_model = GetExperimentRequest
    required_permission = Permission.EXPERIMENTS_READ
    timeout_seconds = 10.0

    async def run(self, args: GetExperimentRequest, ctx: ToolContext) -> ToolResult:
        async with ctx.session_factory() as session:
            experiment = await _load_experiment(session, args.experiment_id_or_code, ctx.principal)
            if experiment is None:
                return ToolResult(
                    tool=self.name,
                    ok=True,  # "not found" is an answer, not a failure
                    summary=f"No accessible experiment matches {args.experiment_id_or_code!r}.",
                    data=None,
                    count=0,
                )

            payload = {
                "code": experiment.code,
                "title": experiment.title,
                "hypothesis": experiment.hypothesis,
                "methodology": experiment.methodology,
                "status": experiment.status.value,
                "model_system": experiment.model_system,
                "sample_size": experiment.sample_size,
                "started_on": experiment.started_on.isoformat() if experiment.started_on else None,
                "completed_on": experiment.completed_on.isoformat()
                if experiment.completed_on
                else None,
                "lead_researcher": experiment.lead_researcher,
                "compound": (
                    {"code": experiment.compound.code, "name": experiment.compound.name}
                    if experiment.compound
                    else None
                ),
            }
            evidence = EvidenceItem(
                source_type="experiment",
                source_id=f"exp:{experiment.code}",
                title=experiment.title,
                snippet=(
                    f"{experiment.hypothesis} Methodology: {experiment.methodology} "
                    f"Model system: {experiment.model_system or 'n/a'}, "
                    f"n={experiment.sample_size or 'n/a'}."
                )[:1200],
                origin_tool=self.name,
            )
            return ToolResult(
                tool=self.name,
                ok=True,
                summary=f"Experiment {experiment.code}: {experiment.title}",
                data=payload,
                evidence=[evidence],
                count=1,
            )


# =============================================================================
# get_experiment_results
# =============================================================================
class GetExperimentResultsRequest(BaseModel):
    experiment_id_or_code: str = Field(min_length=1, max_length=64)


class GetExperimentResultsTool(Tool):
    name = "get_experiment_results"
    description = (
        "Measured results for one experiment: metric name, value, unit, p-value, "
        "confidence interval, evidence strength and the recorded conclusion."
    )
    input_model = GetExperimentResultsRequest
    required_permission = Permission.EXPERIMENTS_READ
    timeout_seconds = 10.0

    async def run(self, args: GetExperimentResultsRequest, ctx: ToolContext) -> ToolResult:
        async with ctx.session_factory() as session:
            experiment = await _load_experiment(session, args.experiment_id_or_code, ctx.principal)
            if experiment is None:
                return ToolResult(
                    tool=self.name,
                    ok=True,
                    summary=f"No accessible experiment matches {args.experiment_id_or_code!r}.",
                    data=[],
                    count=0,
                )

            rows = (
                (
                    await session.execute(
                        select(ExperimentResult)
                        .where(ExperimentResult.experiment_id == experiment.id)
                        .order_by(ExperimentResult.metric_name)
                        .limit(MAX_ROWS)
                    )
                )
                .unique()
                .scalars()
                .all()
            )

            data = [
                {
                    "metric": r.metric_name,
                    "value": r.metric_value,
                    "unit": r.unit,
                    "p_value": r.p_value,
                    "confidence_interval": r.confidence_interval,
                    "evidence_strength": r.evidence_strength.value,
                    "conclusion": r.conclusion,
                }
                for r in rows
            ]
            evidence = [
                EvidenceItem(
                    source_type="experiment",
                    source_id=f"exp:{experiment.code}:{r.metric_name}",
                    title=f"{experiment.code} - {r.metric_name}",
                    snippet=(
                        f"{r.metric_name} = {r.metric_value}{' ' + r.unit if r.unit else ''}"
                        f"{f', p={r.p_value}' if r.p_value is not None else ''}"
                        f"{f', 95% CI {r.confidence_interval}' if r.confidence_interval else ''}. "
                        f"Evidence strength: {r.evidence_strength.value}. {r.conclusion}"
                    )[:1200],
                    origin_tool=self.name,
                )
                for r in rows
            ]
            return ToolResult(
                tool=self.name,
                ok=True,
                summary=f"{len(rows)} result(s) for experiment {experiment.code}.",
                data=data,
                evidence=evidence,
                count=len(rows),
            )


# =============================================================================
# search_compounds
# =============================================================================
class SearchCompoundsRequest(BaseModel):
    query: str = Field(min_length=1, max_length=255, description="Compound name, code or target")
    therapeutic_area: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=10, ge=1, le=25)


class SearchCompoundsTool(Tool):
    name = "search_compounds"
    description = (
        "Search the internal compound catalogue by name, code, target or mechanism of action."
    )
    input_model = SearchCompoundsRequest
    required_permission = Permission.COMPOUNDS_READ
    #: Safe to cache: the compound catalogue is not access-level filtered.
    cacheable = True
    timeout_seconds = 10.0

    async def run(self, args: SearchCompoundsRequest, ctx: ToolContext) -> ToolResult:
        term = f"%{args.query.strip()}%"
        async with ctx.session_factory() as session:
            stmt = (
                select(Compound)
                .where(
                    or_(
                        Compound.name.ilike(term),
                        Compound.code.ilike(term),
                        Compound.mechanism_of_action.ilike(term),
                        Compound.targets.cast(Text).ilike(term),
                    )
                )
                .order_by(Compound.name)
                .limit(min(args.limit, MAX_ROWS))
            )
            if args.therapeutic_area:
                stmt = stmt.where(Compound.therapeutic_area.ilike(f"%{args.therapeutic_area}%"))
            rows = (await session.execute(stmt)).unique().scalars().all()

        data = [
            {
                "code": c.code,
                "name": c.name,
                "therapeutic_area": c.therapeutic_area,
                "mechanism_of_action": c.mechanism_of_action,
                "development_phase": c.development_phase,
                "targets": c.targets,
                "molecular_weight": c.molecular_weight,
            }
            for c in rows
        ]
        evidence = [
            EvidenceItem(
                source_type="compound",
                source_id=f"cmp:{c.code}",
                title=f"{c.name} ({c.code})",
                snippet=(
                    f"{c.name} targets {', '.join(c.targets) or 'n/a'}. "
                    f"Mechanism: {c.mechanism_of_action or 'n/a'}. "
                    f"Therapeutic area: {c.therapeutic_area or 'n/a'}. "
                    f"Development phase: {c.development_phase or 'n/a'}."
                )[:1200],
                origin_tool=self.name,
            )
            for c in rows
        ]
        return ToolResult(
            tool=self.name,
            ok=True,
            summary=f"{len(rows)} compound(s) matched {args.query!r}.",
            data=data,
            evidence=evidence,
            count=len(rows),
        )


# =============================================================================
# search_experiments_by_compound
# =============================================================================
class SearchExperimentsByCompoundRequest(BaseModel):
    compound_query: str = Field(
        min_length=1,
        max_length=255,
        description="Compound name or code, e.g. 'CMP-0042'",
    )
    status: ExperimentStatus | None = None
    limit: int = Field(default=10, ge=1, le=25)


class SearchExperimentsByCompoundTool(Tool):
    name = "search_experiments_by_compound"
    description = (
        "Find internal experiments associated with a compound, resolved by name "
        "or code. Returns each experiment with its headline results. This is the "
        "right tool for 'which experiments used compound X'."
    )
    input_model = SearchExperimentsByCompoundRequest
    required_permission = Permission.EXPERIMENTS_READ
    timeout_seconds = 12.0

    async def run(self, args: SearchExperimentsByCompoundRequest, ctx: ToolContext) -> ToolResult:
        term = f"%{args.compound_query.strip()}%"
        async with ctx.session_factory() as session:
            stmt = (
                select(Experiment)
                .join(Compound, Compound.id == Experiment.compound_id)
                .where(
                    or_(Compound.name.ilike(term), Compound.code.ilike(term)),
                    Experiment.access_level.in_(_visible_experiment_levels(ctx.principal)),
                )
                .order_by(nulls_last(Experiment.started_on.desc()))
                .limit(min(args.limit, MAX_ROWS))
            )
            if args.status:
                stmt = stmt.where(Experiment.status == args.status)
            experiments = (await session.execute(stmt)).unique().scalars().all()

            if not experiments:
                return ToolResult(
                    tool=self.name,
                    ok=True,
                    summary=(
                        f"No accessible experiments are linked to a compound matching "
                        f"{args.compound_query!r}."
                    ),
                    data=[],
                    count=0,
                )

            result_rows = (
                (
                    await session.execute(
                        select(ExperimentResult)
                        .where(ExperimentResult.experiment_id.in_([e.id for e in experiments]))
                        .limit(MAX_ROWS * 4)
                    )
                )
                .unique()
                .scalars()
                .all()
            )

        by_experiment: dict[uuid.UUID, list[ExperimentResult]] = {}
        for row in result_rows:
            by_experiment.setdefault(row.experiment_id, []).append(row)

        data = []
        evidence = []
        for exp in experiments:
            results = by_experiment.get(exp.id, [])
            headline = ", ".join(
                f"{r.metric_name}={r.metric_value}{r.unit or ''}"
                f"{f' (p={r.p_value})' if r.p_value is not None else ''}"
                for r in results[:4]
            )
            data.append(
                {
                    "code": exp.code,
                    "title": exp.title,
                    "status": exp.status.value,
                    "compound": exp.compound.name if exp.compound else None,
                    "compound_code": exp.compound.code if exp.compound else None,
                    "model_system": exp.model_system,
                    "sample_size": exp.sample_size,
                    "started_on": exp.started_on.isoformat() if exp.started_on else None,
                    "headline_results": headline or None,
                }
            )
            evidence.append(
                EvidenceItem(
                    source_type="experiment",
                    source_id=f"exp:{exp.code}",
                    title=f"{exp.code}: {exp.title}",
                    snippet=(
                        f"{exp.title}. Compound: {exp.compound.name if exp.compound else 'n/a'}. "
                        f"Model system: {exp.model_system or 'n/a'}, n={exp.sample_size or 'n/a'}. "
                        f"Status: {exp.status.value}. Results: {headline or 'none recorded'}."
                    )[:1200],
                    origin_tool=self.name,
                )
            )

        return ToolResult(
            tool=self.name,
            ok=True,
            summary=(
                f"{len(experiments)} experiment(s) linked to a compound matching "
                f"{args.compound_query!r}."
            ),
            data=data,
            evidence=evidence,
            count=len(experiments),
        )
