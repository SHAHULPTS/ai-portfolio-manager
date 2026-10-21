"""Guardrail layer 1: what the database itself refuses the agent role, whatever the SQL says."""

import psycopg
import pytest
from psycopg import errors, sql


def run_as_agent(agent_url, query, manager_id=None):
    """Run a query the way the phase-4 executor will: one transaction, manager set by SET LOCAL."""
    with psycopg.connect(agent_url) as conn, conn.transaction():
        if manager_id is not None:
            conn.execute(
                sql.SQL("SET LOCAL app.manager_id = {}").format(sql.Literal(str(manager_id)))
            )
        return conn.execute(query).fetchall()


def test_agent_can_read_analytics_views(db_urls):
    _, agent = db_urls
    rows = run_as_agent(agent, "SELECT symbol FROM analytics.v_securities ORDER BY symbol")
    assert rows == [("NIFTY50",), ("TCS",)]


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM core.transactions",
        "SELECT * FROM core.portfolios",
        "SELECT * FROM core.prices_daily",
    ],
)
def test_agent_cannot_read_base_tables(db_urls, query):
    _, agent = db_urls
    with pytest.raises(errors.InsufficientPrivilege):
        run_as_agent(agent, query, manager_id=1)


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM core.transactions",
        "DROP TABLE core.transactions",
        "UPDATE core.portfolios SET initial_cash = 0",
        "INSERT INTO core.managers VALUES (99, 'x')",
        "CREATE TABLE analytics.evil (x int)",
        "CREATE TEMP TABLE evil (x int)",
    ],
)
def test_agent_cannot_write_or_create(db_urls, query):
    _, agent = db_urls
    # Read-only transaction or missing privilege: either way, refused.
    with pytest.raises((errors.InsufficientPrivilege, errors.ReadOnlySqlTransaction)):
        run_as_agent(agent, query, manager_id=1)


def test_grants_hold_even_if_read_only_mode_is_switched_off(db_urls):
    _, agent = db_urls
    with pytest.raises(errors.InsufficientPrivilege):
        with psycopg.connect(agent) as conn, conn.transaction():
            conn.execute("SET LOCAL transaction_read_only = off")
            conn.execute("DELETE FROM core.transactions")


def test_rls_shows_only_own_portfolios(db_urls):
    _, agent = db_urls
    assert run_as_agent(agent, "SELECT portfolio_id FROM analytics.v_portfolios", 1) == [(10,)]
    assert run_as_agent(agent, "SELECT portfolio_id FROM analytics.v_portfolios", 2) == [(20,)]


def test_rls_fails_closed_when_manager_unset(db_urls):
    _, agent = db_urls
    assert run_as_agent(agent, "SELECT portfolio_id FROM analytics.v_portfolios") == []


def test_query_cannot_switch_manager_with_set_config(db_urls):
    _, agent = db_urls
    attack = (
        "WITH x AS (SELECT set_config('app.manager_id', '2', false)) "
        "SELECT portfolio_id FROM x, analytics.v_portfolios"
    )
    with pytest.raises(errors.InsufficientPrivilege):
        run_as_agent(agent, attack, manager_id=1)


def test_rls_applies_to_child_tables(db_urls):
    admin, _ = db_urls
    # The agent can't read core.transactions directly, so check the policy as analytics_owner,
    # the role the phase-2 transaction views will run as.
    with psycopg.connect(admin) as conn, conn.transaction():
        conn.execute("SET LOCAL ROLE analytics_owner")
        conn.execute("SET LOCAL app.manager_id = '1'")
        txns = conn.execute("SELECT portfolio_id FROM core.transactions").fetchall()
        flows = conn.execute("SELECT portfolio_id FROM core.cash_flows").fetchall()
    assert txns == [(10,)]
    assert flows == [(10,)]


def test_long_queries_are_cancelled(db_urls):
    _, agent = db_urls
    with pytest.raises(errors.QueryCanceled):
        run_as_agent(agent, "SELECT pg_sleep(5)", manager_id=1)


@pytest.mark.parametrize(
    "view",
    [
        "v_portfolios",
        "v_portfolio_nav",
        "v_transactions",
        "v_cash_flows",
        "v_positions",
        "v_realized_pnl",
    ],
)
def test_rls_isolates_every_portfolio_view(db_urls, view):
    _, agent = db_urls
    query = f"SELECT DISTINCT portfolio_id FROM analytics.{view} ORDER BY 1"
    if view == "v_realized_pnl":  # nothing has been sold in the seed data
        assert run_as_agent(agent, query, 1) == []
        return
    assert run_as_agent(agent, query, 1) == [(10,)]
    assert run_as_agent(agent, query, 2) == [(20,)]
    assert run_as_agent(agent, query) == []


def test_every_analytics_view_is_owned_by_analytics_owner(db_urls):
    # If the admin role owned a view, RLS would silently stop applying to it.
    admin, _ = db_urls
    with psycopg.connect(admin) as conn:
        owners = conn.execute(
            "SELECT viewname, viewowner FROM pg_views WHERE schemaname = 'analytics'"
        ).fetchall()
    assert owners
    assert all(owner == "analytics_owner" for _, owner in owners), owners


def test_positions_view_values(db_urls):
    _, agent = db_urls
    row = run_as_agent(
        agent,
        "SELECT symbol, quantity::int, market_value::float8, weight::float8"
        " FROM analytics.v_positions",
        1,
    )
    # 100 TCS at a 100.5 close, in a 1,000,000 portfolio (no fees in the seed).
    assert row == [("TCS", 100, 10050.0, pytest.approx(10050 / 1_000_000))]
