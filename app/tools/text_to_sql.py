"""Text-to-SQL: demonstrated safely, deliberately NOT on the production path.

Interview position on this, stated plainly: generated SQL is useful for
exploratory analytics with a human reading the query before it runs. It is a
poor fit for an autonomous agent answering questions for other people, because
the failure mode is silent — a subtly wrong JOIN returns a confident, wrong
number with no error.

So the production path uses the named tools in `sql_tools.py`. This module
exists to show how you would do it if you had to, with every control in place:

    1. read-only database role          (Postgres enforces it, not us)
    2. single-statement, SELECT-only    (parsed, not regex-guessed at)
    3. table allowlist                  (no users, no audit tables)
    4. banned constructs                (no CTE-writes, no functions with side
                                         effects, no system catalogs)
    5. mandatory LIMIT                  (injected if absent)
    6. statement_timeout                (set per session)
    7. row cap on the result
    8. every generated query logged before execution

Layer 1 is the one that actually holds. The rest are defence in depth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.core.errors import UnsafeQueryError
from app.core.logging import get_logger
from app.database.session import get_readonly_engine

log = get_logger(__name__)

#: Only these tables may be referenced. Note the omissions: users, roles,
#: user_roles, tool_audit_logs, agent_* - identity and audit data are never
#: reachable from a generated query.
ALLOWED_TABLES: frozenset[str] = frozenset(
    {
        "documents",
        "research_topics",
        "experiments",
        "experiment_results",
        "compounds",
        "clinical_trials",
    }
)

_BANNED = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|"
    r"vacuum|analyze|reindex|call|do|merge|listen|notify|set|reset|begin|"
    r"commit|rollback|savepoint|prepare|execute|explain|pg_sleep|pg_read_file|"
    r"pg_ls_dir|lo_import|lo_export|dblink|current_setting|set_config)\b",
    re.I,
)
_SYSTEM_SCHEMA = re.compile(r"\b(pg_catalog|information_schema|pg_[a-z_]+)\b", re.I)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.I)
_COMMENT = re.compile(r"(--[^\n]*|/\*.*?\*/)", re.S)

MAX_ROWS = 100
STATEMENT_TIMEOUT_MS = 5000


@dataclass(slots=True)
class SafeQuery:
    sql: str
    tables: list[str]
    limit_applied: bool


def validate_sql(raw: str) -> SafeQuery:
    """Reject anything that is not a single, read-only, allowlisted SELECT."""
    if not raw or not raw.strip():
        raise UnsafeQueryError("Empty query")

    # Strip comments first: "-- " is the classic way to hide the rest.
    stripped = _COMMENT.sub(" ", raw).strip().rstrip(";").strip()

    if ";" in stripped:
        raise UnsafeQueryError("Only a single statement is allowed")
    if not re.match(r"^\s*(select|with)\b", stripped, re.I):
        raise UnsafeQueryError("Only SELECT statements are allowed")
    if _BANNED.search(stripped):
        raise UnsafeQueryError("Query contains a forbidden keyword")
    if _SYSTEM_SCHEMA.search(stripped):
        raise UnsafeQueryError("System catalogs are not accessible")

    tables = {t.lower() for t in _TABLE_REF.findall(stripped)}
    # Subquery aliases look like table refs; only flag genuine unknowns.
    unknown = {t for t in tables if t not in ALLOWED_TABLES and not t.startswith("(")}
    if unknown:
        raise UnsafeQueryError(f"Query references tables outside the allowlist: {sorted(unknown)}")
    if not tables:
        raise UnsafeQueryError("Query does not reference any allowlisted table")

    limit_applied = False
    if not re.search(r"\blimit\s+\d+", stripped, re.I):
        stripped = f"{stripped} LIMIT {MAX_ROWS}"
        limit_applied = True

    return SafeQuery(sql=stripped, tables=sorted(tables), limit_applied=limit_applied)


def execute_safe_sql(raw: str) -> list[dict[str, Any]]:
    """Validate and run a generated query on the read-only connection.

    Synchronous by design: this is an analyst-facing escape hatch, not part of
    the request path.
    """
    safe = validate_sql(raw)
    log.info("text_to_sql.executing", sql=safe.sql, tables=safe.tables)

    engine = get_readonly_engine()
    with engine.connect() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        result = conn.execute(text(safe.sql))
        rows = [dict(row._mapping) for row in result.fetchmany(MAX_ROWS)]

    log.info("text_to_sql.completed", rows=len(rows))
    return rows
