-- Guardrail layer 1: the database itself limits what the agent can do.
--
-- analytics_owner owns the analytics views. It is NOT a superuser and does NOT own the core
-- tables, so row-level security applies when its views read them. (Views run with their
-- owner's rights, and superusers and table owners bypass RLS. If the admin role owned the
-- views, RLS would silently never apply.)
--
-- agent_ro is the login role for the agent. It can read the analytics views and nothing else.
-- Its password is set by db/migrate.py from AGENT_DATABASE_URL, never stored in SQL.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analytics_owner') THEN
        CREATE ROLE analytics_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_ro') THEN
        CREATE ROLE agent_ro LOGIN;
    END IF;
END
$$;

-- analytics_owner: read the core tables, own the views.
GRANT USAGE ON SCHEMA core TO analytics_owner;
GRANT SELECT ON ALL TABLES IN SCHEMA core TO analytics_owner;
GRANT USAGE, CREATE ON SCHEMA analytics TO analytics_owner;
ALTER VIEW analytics.v_securities OWNER TO analytics_owner;
ALTER VIEW analytics.v_prices OWNER TO analytics_owner;
ALTER VIEW analytics.v_portfolios OWNER TO analytics_owner;

-- agent_ro: connect, read analytics views, nothing else.
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO agent_ro', current_database());
    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
END
$$;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA core FROM PUBLIC;
GRANT USAGE ON SCHEMA analytics TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO agent_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE analytics_owner IN SCHEMA analytics GRANT SELECT ON TABLES TO agent_ro;

-- RLS below trusts the app.manager_id setting, so a query must not be able to change it.
-- set_config() is callable from inside a SELECT (e.g. in a CTE), which would let one query
-- switch to another manager. Revoking it leaves the SET command as the only way in, and SET is
-- a separate statement that the single-SELECT rule (phase 4) never lets through.
REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) FROM PUBLIC;

-- These role defaults are a backstop, not the wall: a session can override them with SET.
-- The real barrier is the grants above (agent_ro has no write privilege anywhere).
-- Even if a write slipped past every other check, the session is read-only and short-lived.
ALTER ROLE agent_ro SET default_transaction_read_only = on;
ALTER ROLE agent_ro SET statement_timeout = '3s';
ALTER ROLE agent_ro SET idle_in_transaction_session_timeout = '10s';

-- Row-level security: a manager sees only their own portfolios and what hangs off them.
-- The app sets app.manager_id per transaction. If it is unset, NULLIF makes the comparison
-- NULL, so no rows match: the policy fails closed.
ALTER TABLE core.portfolios ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.cash_flows ENABLE ROW LEVEL SECURITY;

CREATE POLICY manager_isolation ON core.portfolios
    USING (manager_id = NULLIF(current_setting('app.manager_id', true), '')::integer);

CREATE POLICY manager_isolation ON core.transactions
    USING (portfolio_id IN (SELECT portfolio_id FROM core.portfolios));

CREATE POLICY manager_isolation ON core.cash_flows
    USING (portfolio_id IN (SELECT portfolio_id FROM core.portfolios));
