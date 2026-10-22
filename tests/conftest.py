import os
from collections.abc import Iterator
from contextlib import contextmanager

import numpy as np
import pandas as pd
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from config import load_settings
from db.migrate import migrate
from etl.generate_portfolios import write_book
from etl.refresh_derived import refresh
from portfolio.mandates import Mandate
from portfolio.simulator import simulate


@contextmanager
def fresh_database(name: str) -> Iterator[tuple[str, str]]:
    """Create, migrate and finally drop a throwaway database. Yields (admin_url, agent_url).

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
        server.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        server.execute(f"CREATE DATABASE {name}")

    admin_url = make_conninfo(s.database_url, dbname=name)
    agent_url = make_conninfo(s.agent_database_url, dbname=name)
    migrate(admin_url, agent_url)
    try:
        yield admin_url, agent_url
    finally:
        with psycopg.connect(server_url, autocommit=True) as server:
            server.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(scope="session")
def db_urls():
    """Tiny hand-written book: two managers, one portfolio and one trade each."""
    with fresh_database("portfolio_test") as urls:
        _seed(urls[0])
        yield urls


@pytest.fixture(scope="session")
def metrics_db():
    """A year and a half of synthetic prices and a simulated book, for the metric engine.

    Manager 1 owns portfolio 11, manager 2 owns portfolio 21. Built with the real simulator
    and refresh code, so the engine is tested on data shaped exactly like production's.
    """
    with fresh_database("portfolio_metrics_test") as urls:
        _seed_simulated(urls[0])
        yield urls


SIM_SYMBOLS = {
    "AAA": "Information Technology",
    "BBB": "Information Technology",
    "CCC": "Financial Services",
    "DDD": "FMCG",
    "EEE": "Healthcare",
}


def _seed_simulated(admin_url: str) -> None:
    calendar = pd.bdate_range("2023-01-02", periods=380)
    rng = np.random.default_rng(11)
    market = rng.normal(0.0004, 0.01, len(calendar))
    closes = pd.DataFrame(
        {
            s: 100 * np.exp((0.9 * market + rng.normal(0, 0.008, len(calendar))).cumsum())
            for s in SIM_SYMBOLS
        },
        index=calendar,
    )
    closes["NIFTY50"] = 10_000 * np.exp(market.cumsum())
    listing = {s: (calendar[0], calendar[-1]) for s in closes.columns}
    mandate = Mandate(
        "Synthetic", "NIFTY50", {"AAA": 0.3, "BBB": 0.2, "CCC": 0.3, "DDD": 0.2}, ("EEE",)
    )

    portfolios, trades, flows = [], [], []
    for pid, manager_id, cash in [(11, 1, 1_000_000), (21, 2, 3_000_000)]:
        t, fl = simulate(
            pid, mandate, closes, listing, calendar[5], cash, np.random.default_rng(pid)
        )
        portfolios.append(
            (pid, manager_id, f"P{pid}", mandate.name, "NIFTY50", calendar[5].date(), cash)
        )
        trades += t
        flows += fl

    with psycopg.connect(admin_url) as conn:
        conn.cursor().executemany(
            "INSERT INTO core.securities (symbol, name, sector, industry, is_index)"
            " VALUES (%s, %s, %s, %s, %s)",
            [(s, f"{s} Ltd", sector, sector, False) for s, sector in SIM_SYMBOLS.items()]
            + [("NIFTY50", "NIFTY 50 Index", "Index", "Broad Market Index", True)],
        )
        with conn.cursor().copy(
            "COPY core.prices_daily (symbol, date, open, high, low, close, volume) FROM STDIN"
        ) as copy:
            for s in closes.columns:
                for d, px in closes[s].items():
                    px = round(float(px), 4)
                    copy.write_row((s, d.date(), px, px, px, px, 0))
        write_book(conn, portfolios, trades, flows)
        refresh(conn)


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
