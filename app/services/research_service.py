"""Read service for the structured research domain.

Shared by three callers: the REST endpoints, the agent's SQL tools and the MCP
server. Keeping the queries here is what stops MCP becoming a parallel
implementation that drifts from the API.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Text, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import Permission, Principal
from app.core.errors import NotFoundError, ResourceAccessDeniedError
from app.models.enums import AccessLevel
from app.models.research import (
    ClinicalTrial,
    Compound,
    Experiment,
    ExperimentResult,
    ResearchTopic,
)

MAX_ROWS = 50


def visible_levels(principal: Principal) -> list[AccessLevel]:
    if principal.has_permission(Permission.EXPERIMENTS_READ_RESTRICTED):
        return [AccessLevel.PUBLIC, AccessLevel.INTERNAL, AccessLevel.RESTRICTED]
    return [AccessLevel.PUBLIC, AccessLevel.INTERNAL]


class ResearchService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------------------------------------------------- experiments
    async def _resolve_experiment(self, identifier: str, principal: Principal) -> Experiment | None:
        stmt = select(Experiment)
        try:
            stmt = stmt.where(Experiment.id == uuid.UUID(identifier))
        except (ValueError, AttributeError):
            stmt = stmt.where(func.upper(Experiment.code) == identifier.strip().upper())
        return (await self._session.execute(stmt)).unique().scalar_one_or_none()

    async def get_experiment(self, identifier: str, principal: Principal) -> Experiment:
        """Fetch one experiment, enforcing resource-level access.

        Note the two distinct outcomes: a row that does not exist is a 404, a
        row the caller may not see is a 403. Collapsing both into 404 hides the
        existence of restricted records - sometimes desirable, but it also
        hides genuine permission misconfiguration from the user. Here the
        record's *existence* is not itself sensitive, so we are explicit.
        """
        experiment = await self._resolve_experiment(identifier, principal)
        if experiment is None:
            raise NotFoundError(f"No experiment matches {identifier!r}")
        if not principal.can_read_experiment_level(experiment.access_level):
            raise ResourceAccessDeniedError(
                "You do not have access to this experiment",
                details={"required": "experiments:read_restricted"},
            )
        return experiment

    async def get_experiment_results(self, experiment_id: uuid.UUID) -> list[ExperimentResult]:
        stmt = (
            select(ExperimentResult)
            .where(ExperimentResult.experiment_id == experiment_id)
            .order_by(ExperimentResult.metric_name)
            .limit(MAX_ROWS)
        )
        return list((await self._session.execute(stmt)).unique().scalars().all())

    async def get_experiment_detail(
        self, identifier: str, principal: Principal
    ) -> dict[str, Any] | None:
        """Flattened experiment + results, for MCP and tool consumers."""
        experiment = await self._resolve_experiment(identifier, principal)
        if experiment is None or not principal.can_read_experiment_level(experiment.access_level):
            return None
        results = await self.get_experiment_results(experiment.id)
        return {
            "code": experiment.code,
            "title": experiment.title,
            "hypothesis": experiment.hypothesis,
            "methodology": experiment.methodology,
            "status": experiment.status.value,
            "model_system": experiment.model_system,
            "sample_size": experiment.sample_size,
            "compound": experiment.compound.name if experiment.compound else None,
            "results": [
                {
                    "metric": r.metric_name,
                    "value": r.metric_value,
                    "unit": r.unit,
                    "p_value": r.p_value,
                    "confidence_interval": r.confidence_interval,
                    "evidence_strength": r.evidence_strength.value,
                    "conclusion": r.conclusion,
                }
                for r in results
            ],
        }

    async def search_experiments_summary(
        self, query: str, principal: Principal, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        term = f"%{query.strip()}%"
        stmt = (
            select(Experiment)
            .where(
                Experiment.access_level.in_(visible_levels(principal)),
                or_(
                    Experiment.title.ilike(term),
                    Experiment.code.ilike(term),
                    Experiment.hypothesis.ilike(term),
                ),
            )
            .order_by(Experiment.code)
            .limit(min(limit, MAX_ROWS))
        )
        rows = (await self._session.execute(stmt)).unique().scalars().all()
        return [
            {
                "code": e.code,
                "title": e.title,
                "status": e.status.value,
                "compound": e.compound.name if e.compound else None,
                "model_system": e.model_system,
                "sample_size": e.sample_size,
            }
            for e in rows
        ]

    # ------------------------------------------------------------- compounds
    async def get_compound(self, identifier: str) -> Compound:
        stmt = select(Compound)
        try:
            stmt = stmt.where(Compound.id == uuid.UUID(identifier))
        except (ValueError, AttributeError):
            stmt = stmt.where(func.upper(Compound.code) == identifier.strip().upper())
        compound = (await self._session.execute(stmt)).unique().scalar_one_or_none()
        if compound is None:
            raise NotFoundError(f"No compound matches {identifier!r}")
        return compound

    async def search_compounds(self, query: str, *, limit: int = 10) -> list[Compound]:
        term = f"%{query.strip()}%"
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
            .limit(min(limit, MAX_ROWS))
        )
        return list((await self._session.execute(stmt)).unique().scalars().all())

    async def search_compounds_summary(
        self, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        return [
            {
                "code": c.code,
                "name": c.name,
                "therapeutic_area": c.therapeutic_area,
                "mechanism_of_action": c.mechanism_of_action,
                "targets": c.targets,
                "development_phase": c.development_phase,
            }
            for c in await self.search_compounds(query, limit=limit)
        ]

    # ---------------------------------------------------------------- trials
    async def get_trial(self, registry_id: str) -> dict[str, Any] | None:
        stmt = select(ClinicalTrial).where(ClinicalTrial.registry_id == registry_id)
        trial = (await self._session.execute(stmt)).unique().scalar_one_or_none()
        if trial is None:
            return None
        return {
            "registry_id": trial.registry_id,
            "title": trial.title,
            "condition": trial.condition,
            "intervention": trial.intervention,
            "phase": trial.phase.value,
            "status": trial.status.value,
            "sponsor": trial.sponsor,
            "enrollment": trial.enrollment,
            "primary_outcome": trial.primary_outcome,
            "summary": trial.summary,
        }

    # ---------------------------------------------------------------- topics
    async def get_topic(self, identifier: str) -> ResearchTopic:
        stmt = select(ResearchTopic)
        try:
            stmt = stmt.where(ResearchTopic.id == uuid.UUID(identifier))
        except (ValueError, AttributeError):
            stmt = stmt.where(ResearchTopic.slug == identifier.strip().lower())
        topic = (await self._session.execute(stmt)).unique().scalar_one_or_none()
        if topic is None:
            raise NotFoundError(f"No research topic matches {identifier!r}")
        return topic

    async def list_topics(self, *, limit: int = 100) -> list[ResearchTopic]:
        rows = (
            (
                await self._session.execute(
                    select(ResearchTopic).order_by(ResearchTopic.name).limit(limit)
                )
            )
            .unique()
            .scalars()
            .all()
        )
        return list(rows)

    async def list_topic_experiments(
        self, topic_id: uuid.UUID, principal: Principal, *, limit: int = 25
    ) -> list[Experiment]:
        stmt = (
            select(Experiment)
            .where(
                Experiment.topic_id == topic_id,
                Experiment.access_level.in_(visible_levels(principal)),
            )
            .order_by(Experiment.code)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).unique().scalars().all())
