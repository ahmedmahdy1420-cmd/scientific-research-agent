"""The text-to-SQL safety gate.

Every one of these is an attack that a naive `if "drop" not in sql` check would
miss. The gate is defence in depth; the read-only Postgres role is the control
that actually holds.
"""

from __future__ import annotations

import pytest

from app.core.errors import UnsafeQueryError
from app.tools.text_to_sql import ALLOWED_TABLES, MAX_ROWS, validate_sql

pytestmark = pytest.mark.unit


class TestAccepted:
    def test_plain_select(self):
        safe = validate_sql("SELECT code, title FROM experiments WHERE status = 'completed'")
        assert safe.tables == ["experiments"]

    def test_join_between_allowlisted_tables(self):
        safe = validate_sql(
            "SELECT e.code, c.name FROM experiments e JOIN compounds c ON c.id = e.compound_id"
        )
        assert set(safe.tables) == {"experiments", "compounds"}

    def test_limit_injected_when_absent(self):
        safe = validate_sql("SELECT * FROM compounds")
        assert safe.limit_applied
        assert f"LIMIT {MAX_ROWS}" in safe.sql

    def test_existing_limit_respected(self):
        safe = validate_sql("SELECT * FROM compounds LIMIT 5")
        assert not safe.limit_applied
        assert safe.sql.count("LIMIT") == 1

    def test_trailing_semicolon_tolerated(self):
        assert validate_sql("SELECT 1 FROM compounds;").sql.count(";") == 0

    def test_line_comment_is_stripped_and_its_payload_neutralised(self):
        """`-- ; DROP TABLE users` is a comment, so stripping it is correct.

        The validated SQL that actually executes contains no second statement,
        which is the point: we run the *stripped* text, not the original.
        """
        safe = validate_sql("SELECT * FROM compounds -- ; DROP TABLE users")
        assert "DROP" not in safe.sql.upper()
        assert safe.tables == ["compounds"]


class TestRejected:
    @pytest.mark.parametrize(
        "sql,reason",
        [
            ("DELETE FROM documents", "write"),
            ("UPDATE users SET hashed_password = 'x'", "write"),
            ("INSERT INTO roles (name) VALUES ('admin')", "write"),
            ("DROP TABLE documents", "ddl"),
            ("TRUNCATE experiments", "ddl"),
            ("GRANT ALL ON documents TO research_ro", "privilege"),
            ("SELECT * FROM compounds; DROP TABLE users", "stacked statement"),
            ("SELECT * FROM users", "identity table not allowlisted"),
            ("SELECT * FROM tool_audit_logs", "audit table not allowlisted"),
            ("SELECT * FROM roles", "identity table not allowlisted"),
            ("SELECT * FROM pg_catalog.pg_user", "system catalog"),
            ("SELECT * FROM information_schema.tables", "system catalog"),
            ("SELECT pg_sleep(30) FROM compounds", "denial of service"),
            ("SELECT pg_read_file('/etc/passwd') FROM compounds", "file read"),
            ("SELECT * FROM compounds /* hidden */ ; DELETE FROM documents", "comment smuggling"),
            ("", "empty"),
            ("   ", "empty"),
            ("EXPLAIN ANALYZE SELECT * FROM compounds", "not a plain select"),
        ],
    )
    def test_unsafe_query_rejected(self, sql, reason):
        with pytest.raises(UnsafeQueryError):
            validate_sql(sql)

    def test_select_without_a_table_rejected(self):
        with pytest.raises(UnsafeQueryError, match="allowlisted table"):
            validate_sql("SELECT 1")


class TestAllowlist:
    def test_identity_and_audit_tables_are_excluded(self):
        for table in (
            "users",
            "roles",
            "user_roles",
            "tool_audit_logs",
            "agent_runs",
            "agent_messages",
            "approval_requests",
        ):
            assert table not in ALLOWED_TABLES

    def test_research_tables_are_included(self):
        for table in ("documents", "experiments", "compounds", "clinical_trials"):
            assert table in ALLOWED_TABLES
