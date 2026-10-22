"""The executor's own defences, with the guard deliberately skipped."""

import psycopg
import pytest

from agent.sql.executor import QueryError, execute


@pytest.mark.parametrize(
    "sql, message",
    [
        ("SELECT 1; DELETE FROM core.transactions", "multiple commands"),
        ("DELETE FROM core.transactions", "not permitted"),
        ("SELECT * FROM core.transactions", "not permitted"),
        ("SELECT pg_sleep(5)", "longer than"),
    ],
)
def test_database_layer_refuses_even_without_the_guard(metrics_db, sql, message):
    _, agent = metrics_db
    with pytest.raises(QueryError, match=message):
        execute(agent, 1, sql, timeout_ms=500)


def test_expensive_plan_is_refused_before_running(metrics_db):
    _, agent = metrics_db
    cartesian = "SELECT count(*) FROM analytics.v_prices a, analytics.v_prices b"
    with pytest.raises(QueryError, match="too expensive"):
        execute(agent, 1, cartesian, max_cost=10_000)


def test_nothing_was_written(metrics_db):
    admin, agent = metrics_db
    with psycopg.connect(admin) as conn:
        before = conn.execute("SELECT count(*) FROM core.transactions").fetchone()[0]
    for sql in ["DELETE FROM core.transactions", "SELECT 1; DELETE FROM core.transactions"]:
        with pytest.raises(QueryError):
            execute(agent, 1, sql)
    with psycopg.connect(admin) as conn:
        assert conn.execute("SELECT count(*) FROM core.transactions").fetchone()[0] == before


def test_rls_scopes_results_to_the_manager(metrics_db):
    _, agent = metrics_db
    sql = "SELECT DISTINCT portfolio_id FROM analytics.v_positions ORDER BY 1"
    assert execute(agent, 1, sql).rows == [{"portfolio_id": 11}]
    assert execute(agent, 2, sql).rows == [{"portfolio_id": 21}]


def test_rows_are_capped_and_plain(metrics_db):
    _, agent = metrics_db
    r = execute(
        agent, 1, "SELECT date, nav FROM analytics.v_portfolio_nav ORDER BY date", row_limit=3
    )
    assert r.row_count == 3 and r.truncated
    assert isinstance(r.rows[0]["nav"], float) and isinstance(r.rows[0]["date"], str)
