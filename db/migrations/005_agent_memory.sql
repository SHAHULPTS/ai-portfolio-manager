-- Conversation memory for the agent (LangGraph checkpoints).
--
-- The checkpointer must write, but nothing that writes may touch portfolio data. So it gets
-- its own login role, agent_memory, which owns one schema (agent_memory) and has no rights on
-- core or analytics at all. agent_ro stays strictly read-only, and the admin role is never
-- handed to the app. Its password is set by db/migrate.py from MEMORY_DATABASE_URL.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_memory') THEN
        CREATE ROLE agent_memory LOGIN;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO agent_memory', current_database());
END
$$;

CREATE SCHEMA IF NOT EXISTS agent_memory AUTHORIZATION agent_memory;
ALTER ROLE agent_memory SET search_path = agent_memory;
ALTER ROLE agent_memory SET statement_timeout = '10s';
