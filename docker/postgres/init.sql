-- Runs once, on first boot of the postgres volume.
-- 1) the vector extension, 2) a least-privilege role for the SQL tool layer.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy name lookup for compounds
CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- gen_random_uuid()

-- The role the agent's SQL tools connect as. It can read and nothing else.
-- This is the database *enforcing* the read-only guarantee; the application
-- merely asks nicely.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'research_ro') THEN
        CREATE ROLE research_ro LOGIN PASSWORD 'research_ro';
    END IF;
END
$$;

REVOKE ALL ON SCHEMA public FROM research_ro;
GRANT CONNECT ON DATABASE research TO research_ro;
GRANT USAGE ON SCHEMA public TO research_ro;

-- Existing objects (none on first boot) and, crucially, everything Alembic
-- creates later: default privileges apply to tables created by `research`.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO research_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE research IN SCHEMA public
    GRANT SELECT ON TABLES TO research_ro;

-- Hard caps so a runaway tool query cannot pin a connection.
ALTER ROLE research_ro SET statement_timeout = '5s';
ALTER ROLE research_ro SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE research_ro SET default_transaction_read_only = on;
