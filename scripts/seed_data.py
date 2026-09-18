#!/usr/bin/env python
"""Seed the database with users, research data and the document corpus.

Idempotent: safe to run repeatedly. Every entity is upserted on its natural key
(email, code, slug, registry id, content hash), so `docker compose up` a second
time does not duplicate anything.

With `--with-pdfs` it also runs the real ingestion pipeline over the generated
PDFs — extraction, chunking and embedding — so the corpus is genuinely
searchable rather than hand-inserted.

Run:
    python -m scripts.seed_data --with-pdfs
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth.permissions import (  # noqa: E402
    ROLE_DESCRIPTIONS,
    ROLE_PERMISSIONS,
)
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.database.session import sync_session_scope  # noqa: E402
from app.documents.ingestion import DocumentIngestionPipeline  # noqa: E402
from app.documents.storage import build_key, get_storage, sha256_of  # noqa: E402
from app.evaluation.dataset import sync_dataset  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.enums import (  # noqa: E402
    AccessLevel,
    DocumentType,
    EvidenceStrength,
    ExperimentStatus,
    IngestionStatus,
    RoleName,
    TrialPhase,
    TrialStatus,
)
from app.models.research import (  # noqa: E402
    ClinicalTrial,
    Compound,
    Experiment,
    ExperimentResult,
    ResearchTopic,
)
from app.models.user import Role, User  # noqa: E402

settings = get_settings()
configure_logging(settings.log_level, "console")
log = get_logger("seed")

SAMPLE = ROOT / "data" / "sample"
PDF_DIR = ROOT / "data" / "pdfs"

DEMO_USERS = [
    {
        "email": "researcher@example.com",
        "full_name": "Rita Researcher",
        "role": RoleName.RESEARCHER,
        "department": "oncology",
    },
    {
        "email": "senior@example.com",
        "full_name": "Sam Senior",
        "role": RoleName.SENIOR_RESEARCHER,
        "department": "oncology",
    },
    {
        "email": "admin@example.com",
        "full_name": "Ada Admin",
        "role": RoleName.ADMIN,
        "department": "platform",
    },
]


def _load(name: str) -> list[dict[str, Any]]:
    path = SAMPLE / name
    if not path.exists():
        raise SystemExit(f"{path} is missing. Run `python -m scripts.generate_sample_data` first.")
    return json.loads(path.read_text())


# =============================================================================
# Roles and users
# =============================================================================
def seed_roles(session: Session) -> dict[str, Role]:
    existing = {r.name: r for r in session.execute(select(Role)).scalars().all()}
    for name, permissions in ROLE_PERMISSIONS.items():
        role = existing.get(name)
        if role is None:
            role = Role(
                name=name,
                description=ROLE_DESCRIPTIONS.get(name, ""),
                permissions=list(permissions),
            )
            session.add(role)
            existing[name] = role
        else:
            # Keep permissions in sync with the code when they change.
            role.permissions = list(permissions)
            role.description = ROLE_DESCRIPTIONS.get(name, role.description)
    session.flush()
    log.info("seed.roles", count=len(existing))
    return existing


def seed_users(session: Session, roles: dict[str, Role]) -> dict[str, User]:
    password = settings.seed_default_password
    users: dict[str, User] = {}
    for spec in DEMO_USERS:
        user = (
            session.execute(select(User).where(User.email == spec["email"]))
            .unique()
            .scalar_one_or_none()
        )
        if user is None:
            user = User(
                email=spec["email"],
                full_name=spec["full_name"],
                hashed_password=hash_password(password),
                department=spec["department"],
                is_active=True,
            )
            session.add(user)
            session.flush()
        role = roles[spec["role"]]
        if role not in user.roles:
            user.roles.append(role)
        users[spec["role"]] = user
    session.flush()
    log.info("seed.users", count=len(users), password_hint=password)
    return users


# =============================================================================
# Research domain
# =============================================================================
def seed_topics(session: Session) -> dict[str, ResearchTopic]:
    records = _load("research_topics.json")
    existing = {t.slug: t for t in session.execute(select(ResearchTopic)).scalars().all()}
    for record in records:
        topic = existing.get(record["slug"])
        if topic is None:
            topic = ResearchTopic(**record)
            session.add(topic)
            existing[record["slug"]] = topic
        else:
            for key, value in record.items():
                setattr(topic, key, value)
    session.flush()
    log.info("seed.topics", count=len(existing))
    return existing


def seed_compounds(session: Session) -> dict[str, Compound]:
    records = _load("compounds.json")
    existing = {c.code: c for c in session.execute(select(Compound)).scalars().all()}
    for record in records:
        compound = existing.get(record["code"])
        if compound is None:
            compound = Compound(**record)
            session.add(compound)
            existing[record["code"]] = compound
        else:
            for key, value in record.items():
                setattr(compound, key, value)
    session.flush()
    log.info("seed.compounds", count=len(existing))
    return existing


def seed_experiments(
    session: Session,
    compounds: dict[str, Compound],
    topics: dict[str, ResearchTopic],
) -> dict[str, Experiment]:
    records = _load("experiments.json")
    existing = {e.code: e for e in session.execute(select(Experiment)).scalars().all()}
    for record in records:
        payload = dict(record)
        compound = compounds.get(payload.pop("compound_code", ""))
        topic = topics.get(payload.pop("topic_slug", ""))
        payload["status"] = ExperimentStatus(payload["status"])
        payload["access_level"] = AccessLevel(payload["access_level"])
        for field in ("started_on", "completed_on"):
            if payload.get(field):
                payload[field] = dt.date.fromisoformat(payload[field])

        experiment = existing.get(payload["code"])
        if experiment is None:
            experiment = Experiment(**payload)
            session.add(experiment)
            existing[payload["code"]] = experiment
        else:
            for key, value in payload.items():
                setattr(experiment, key, value)
        experiment.compound_id = compound.id if compound else None
        experiment.topic_id = topic.id if topic else None
    session.flush()
    log.info("seed.experiments", count=len(existing))
    return existing


def seed_experiment_results(session: Session, experiments: dict[str, Experiment]) -> int:
    records = _load("experiment_results.json")
    existing = {
        (r.experiment_id, r.metric_name): r
        for r in session.execute(select(ExperimentResult)).scalars().all()
    }
    written = 0
    for record in records:
        experiment = experiments.get(record["experiment_code"])
        if experiment is None:
            continue
        key = (experiment.id, record["metric_name"])
        payload = {
            "metric_name": record["metric_name"],
            "metric_value": record["metric_value"],
            "unit": record["unit"],
            "p_value": record["p_value"],
            "confidence_interval": record["confidence_interval"],
            "evidence_strength": EvidenceStrength(record["evidence_strength"]),
            "conclusion": record["conclusion"],
        }
        row = existing.get(key)
        if row is None:
            session.add(ExperimentResult(experiment_id=experiment.id, **payload))
            written += 1
        else:
            for field, value in payload.items():
                setattr(row, field, value)
    session.flush()
    log.info("seed.experiment_results", inserted=written, total=len(records))
    return written


def seed_trials(session: Session, compounds: dict[str, Compound]) -> int:
    records = _load("clinical_trials.json")
    existing = {t.registry_id: t for t in session.execute(select(ClinicalTrial)).scalars().all()}
    for record in records:
        payload = dict(record)
        compound = compounds.get(payload.pop("compound_code", ""))
        payload["phase"] = TrialPhase(payload["phase"])
        payload["status"] = TrialStatus(payload["status"])
        for field in ("start_date", "completion_date"):
            if payload.get(field):
                payload[field] = dt.date.fromisoformat(payload[field])

        trial = existing.get(payload["registry_id"])
        if trial is None:
            trial = ClinicalTrial(**payload)
            session.add(trial)
            existing[payload["registry_id"]] = trial
        else:
            for key, value in payload.items():
                setattr(trial, key, value)
        trial.compound_id = compound.id if compound else None
    session.flush()
    log.info("seed.clinical_trials", count=len(existing))
    return len(existing)


# =============================================================================
# Documents
# =============================================================================
def seed_documents(session: Session, owner: User, ingest: bool) -> int:
    """Store the generated PDFs and (optionally) run the real pipeline."""
    records = _load("documents.json")
    if not PDF_DIR.exists():
        log.warning("seed.pdfs_missing", hint="run python -m scripts.generate_pdfs")
        return 0

    storage = get_storage(settings)
    created = 0
    to_ingest: list[Any] = []

    for record in records:
        pdf_path = PDF_DIR / record["filename"]
        if not pdf_path.exists():
            continue
        data = pdf_path.read_bytes()
        content_hash = sha256_of(data)

        document = (
            session.execute(select(Document).where(Document.content_sha256 == content_hash))
            .unique()
            .scalar_one_or_none()
        )

        if document is None:
            key = build_key(content_hash, record["filename"])
            storage.put(key, data, "application/pdf")
            document = Document(
                title=record["title"][:512],
                filename=record["filename"],
                storage_key=key,
                content_sha256=content_hash,
                size_bytes=len(data),
                document_type=DocumentType(record["document_type"]),
                access_level=AccessLevel(record["access_level"]),
                authors=record.get("authors") or [],
                source=record.get("source"),
                doi=record.get("doi"),
                journal=record.get("journal"),
                publication_date=(
                    dt.date.fromisoformat(record["publication_date"])
                    if record.get("publication_date")
                    else None
                ),
                research_area=record.get("research_area"),
                abstract=record.get("abstract"),
                keywords=record.get("keywords") or [],
                owner_id=owner.id,
                ingestion_status=IngestionStatus.UPLOADED,
            )
            session.add(document)
            created += 1

        session.flush()
        if ingest and document.ingestion_status != IngestionStatus.COMPLETED:
            to_ingest.append(document.id)

    session.commit()
    log.info("seed.documents", created=created, queued_for_ingestion=len(to_ingest))

    if ingest and to_ingest:
        pipeline = DocumentIngestionPipeline(session, storage, settings)
        for index, document_id in enumerate(to_ingest, start=1):
            try:
                outcome = pipeline.process(document_id)
                log.info(
                    "seed.ingested",
                    progress=f"{index}/{len(to_ingest)}",
                    chunks=outcome.chunk_count,
                    pages=outcome.page_count,
                )
            except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop seeding
                log.error("seed.ingestion_failed", document_id=str(document_id), error=str(exc))

    return created


# =============================================================================
# Entry point
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the database with sample data")
    parser.add_argument(
        "--with-pdfs",
        action="store_true",
        help="Also run the full ingestion pipeline over the generated PDFs",
    )
    parser.add_argument(
        "--skip-generate",
        action="store_true",
        help="Do not regenerate the JSON dataset or PDFs first",
    )
    args = parser.parse_args()

    if not args.skip_generate:
        if not (SAMPLE / "documents.json").exists():
            from scripts.generate_sample_data import main as generate_data

            generate_data()
        if args.with_pdfs and not any(PDF_DIR.glob("*.pdf")):
            from scripts.generate_pdfs import main as generate_pdfs

            generate_pdfs()

    with sync_session_scope() as session:
        roles = seed_roles(session)
        users = seed_users(session, roles)
        topics = seed_topics(session)
        compounds = seed_compounds(session)
        experiments = seed_experiments(session, compounds, topics)
        seed_experiment_results(session, experiments)
        seed_trials(session, compounds)
        sync_dataset(session)
        session.commit()

        seed_documents(session, users[RoleName.ADMIN], ingest=args.with_pdfs)

        from app.models.document import DocumentChunk

        chunk_count = session.scalar(select(func.count()).select_from(DocumentChunk)) or 0
        embedded = (
            session.scalar(
                select(func.count())
                .select_from(DocumentChunk)
                .where(DocumentChunk.embedding.is_not(None))
            )
            or 0
        )

    print("\n" + "=" * 70)
    print("Seed complete.")
    print(f"  users        : {len(users)}  (password: {settings.seed_default_password})")
    for spec in DEMO_USERS:
        print(f"                 {spec['email']:28s} -> {spec['role']}")
    print(f"  topics       : {len(topics)}")
    print(f"  compounds    : {len(compounds)}")
    print(f"  experiments  : {len(experiments)}")
    print(f"  chunks       : {chunk_count} ({embedded} embedded)")
    if not args.with_pdfs:
        print("\n  Documents were not ingested. Re-run with --with-pdfs for RAG.")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
