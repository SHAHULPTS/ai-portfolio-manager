import os

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from config import load_settings
from db.migrate import migrate
from etl.refresh_derived import refresh

TEST_DB = "portfolio_test"


@pytest.fixture(scope="session")
def db_urls():
    """Fresh, migrated, seeded test database. Yields (admin_url, agent_url).

    Skips when Postgres is unreachable, unless REQUIRE_DB=1 (set in CI) makes that a failure.
    """
    s = load_settings()
    server_url = make_conninfo(s.database_url, dbname="postgres")
    try:
        server = psycopg.connect(server_url, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as e:
        if os.environ.get("REQUIRE_DB") == "1":
            raise
        pytest.skip(f"Postgres not reachable: {e}")

    with server:
        server.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        server.execute(f"CREATE DATABASE {TEST_DB}")

    admin_url = make_conninfo(s.database_url, dbname=TEST_DB)
    agent_url = make_conninfo(s.agent_database_url, dbname=TEST_DB)
    migrate(admin_url, agent_url)
    _seed(admin_url)
    yield admin_url, agent_url

    with psycopg.connect(server_url, autocommit=True) as server:
        server.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


def _seed(admin_url: str) -> None:
    """Two managers with one portfolio each, so isolation can be tested."""
    with psycopg.connect(admin_url) as conn:
        conn.execute(
            "INSERT INTO core.securities (symbol, name, sector, industry, is_index) VALUES"
            " ('NIFTY50', 'NIFTY 50 Index', 'Index', 'Broad Market Index', true),"
            " ('TCS', 'Tata Consultancy Services', 'Information Technology', 'IT Services', false)"
        )
        conn.execute(
            "INSERT INTO core.prices_daily VALUES"
            " ('TCS', '2024-01-01', 100, 101, 99, 100.5, 1000),"
            " ('NIFTY50', '2024-01-01', 21000, 21100, 20900, 21050, 0)"
        )
        conn.execute("INSERT INTO core.managers VALUES (1, 'Asha'), (2, 'Ravi')")
        conn.execute(
            "INSERT INTO core.portfolios VALUES"
            " (10, 1, 'Asha Tech', 'IT growth', 'NIFTY50', '2024-01-01', 1000000),"
            " (20, 2, 'Ravi Value', 'Balanced', 'NIFTY50', '2024-01-01', 2000000)"
        )
        conn.execute(
            "INSERT INTO core.transactions"
            " (portfolio_id, symbol, trade_date, side, quantity, price)"
            " VALUES (10, 'TCS', '2024-01-01', 'BUY', 100, 100.5),"
            "        (20, 'TCS', '2024-01-01', 'BUY', 500, 100.5)"
        )
        conn.execute(
            "INSERT INTO core.cash_flows (portfolio_id, date, amount, type)"
            " VALUES (10, '2024-01-01', 1000000, 'DEPOSIT'), (20, '2024-01-01', 2000000, 'DEPOSIT')"
        )
        refresh(conn)
