"""Guardrail layer 2: the SQL guard blocks every attack and passes every worked example."""

import psycopg
import pytest

from agent.sql.executor import execute
from agent.sql.fewshots import FEWSHOTS
from agent.sql.guard import check
from agent.sql.schema import load_schema

ATTACKS = {
    "drop": "DROP TABLE core.transactions",
    "stacked delete": "SELECT 1; DELETE FROM core.transactions",
    "comment-hidden drop": "SELECT 1 /* harmless */; DROP TABLE core.managers -- ",
    "mixed-case delete": "DeLeTe FROM analytics.v_positions",
    "update": "UPDATE core.portfolios SET initial_cash = 0",
    "insert": "INSERT INTO core.managers VALUES (9, 'x')",
    "select into": "SELECT * INTO evil FROM analytics.v_prices",
    "row lock": "SELECT * FROM analytics.v_positions FOR UPDATE",
    "copy to file": "COPY (SELECT 1) TO '/tmp/x'",
    "grant": "GRANT SELECT ON core.transactions TO agent_ro",
    "transaction": "BEGIN; DELETE FROM core.transactions; COMMIT",
    "explain analyze delete": "EXPLAIN ANALYZE DELETE FROM core.transactions",
    "set manager": "SET app.manager_id = '2'",
    "base table": "SELECT * FROM core.transactions",
    "catalog": "SELECT * FROM pg_catalog.pg_user",
    "password hashes": "SELECT usename, passwd FROM pg_shadow",
    "information schema": "SELECT * FROM information_schema.tables",
    "union into catalog": "SELECT symbol, name FROM analytics.v_securities "
    "UNION SELECT usename, passwd FROM pg_shadow",
    "subquery on base table": "SELECT * FROM analytics.v_prices WHERE symbol IN "
    "(SELECT symbol FROM core.positions)",
    "cross database": "SELECT * FROM other_db.analytics.v_prices",
    "sleep": "SELECT pg_sleep(10)",
    "sleep in where": "SELECT * FROM analytics.v_prices WHERE pg_sleep(5) IS NULL",
    "set_config": "SELECT set_config('app.manager_id', '2', false)",
    "set_config in cte": "WITH x AS (SELECT set_config('app.manager_id', '2', false)) "
    "SELECT * FROM x, analytics.v_portfolios",
    "read setting": "SELECT current_setting('app.manager_id')",
    "read file": "SELECT pg_read_file('/etc/passwd')",
    "sequence write": "SELECT nextval('core.transactions_txn_id_seq')",
    "dblink": "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)",
    "xml exfiltration": "SELECT query_to_xml('select * from core.transactions', true, true, '')",
    "recursive cte": "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) "
    "SELECT * FROM r",
    "non-literal limit": "SELECT * FROM analytics.v_prices LIMIT (SELECT 10)",
    "unknown column": "SELECT secret FROM analytics.v_prices",
    "empty": "   ;  ",
    "too long": "SELECT 1 AS x" + " , 1 AS x" * 1000,
}


@pytest.fixture(scope="module")
def schema(metrics_db):
    _, agent = metrics_db
    with psycopg.connect(agent) as conn:
        return load_schema(conn)


@pytest.mark.parametrize("sql", ATTACKS.values(), ids=ATTACKS.keys())
def test_attack_is_blocked(schema, sql):
    result = check(sql, schema)
    assert not result.ok
    assert result.reason


@pytest.mark.parametrize("question, sql", FEWSHOTS, ids=[q[:40] for q, _ in FEWSHOTS])
def test_worked_example_passes_and_runs(metrics_db, schema, question, sql):
    _, agent = metrics_db
    result = check(sql, schema)
    assert result.ok, result.reason
    execute(agent, 1, result.sql)  # must not raise


def test_limit_is_added_capped_or_kept(schema):
    assert check("SELECT symbol FROM analytics.v_prices", schema, 500).sql.endswith("LIMIT 500")
    assert check("SELECT symbol FROM analytics.v_prices LIMIT 9999", schema, 500).sql.endswith(
        "LIMIT 500"
    )
    assert check("SELECT symbol FROM analytics.v_prices LIMIT 7", schema, 500).sql.endswith(
        "LIMIT 7"
    )


def test_unqualified_view_is_qualified(schema):
    assert "analytics.v_prices" in check("SELECT symbol FROM v_prices", schema).sql


def test_reasons_help_the_writer_fix_the_query(schema):
    assert "secret" in check("SELECT secret FROM analytics.v_prices", schema).reason
    assert "analytics.v_positions" in check("SELECT * FROM core.positions", schema).reason


def test_examples_never_use_current_date():
    # The data ends before today, so relative dates must come from the "data as of" date.
    # The rule says so; the examples must not teach the opposite.
    assert not [q for q, sql in FEWSHOTS if "current_date" in sql.lower() or "now()" in sql.lower()]


def test_ctes_and_window_functions_are_fine(schema):
    sql = """WITH ranked AS (
        SELECT portfolio_id, symbol, weight,
               rank() OVER (PARTITION BY portfolio_id ORDER BY weight DESC) AS r
        FROM analytics.v_positions)
      SELECT * FROM ranked WHERE r <= 3"""
    assert check(sql, schema).ok
