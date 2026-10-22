"""Fixed, parameterised queries that feed the metric engine. No LLM writes any of these.

They run as agent_ro inside a transaction with app.manager_id set, exactly like the
LLM-written SQL will in phase 4. A manager asking about someone else's portfolio gets
PortfolioNotFound, the same as for a portfolio that doesn't exist, so the error itself
leaks nothing.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pandas as pd
import psycopg
from psycopg import sql


class PortfolioNotFound(LookupError):
    pass


@contextmanager
def agent_session(agent_url: str, manager_id: int) -> Iterator[psycopg.Connection]:
    with psycopg.connect(agent_url) as conn, conn.transaction():
        conn.execute(sql.SQL("SET LOCAL app.manager_id = {}").format(sql.Literal(str(manager_id))))
        yield conn


def list_portfolios(conn: psycopg.Connection) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT portfolio_id, name, mandate, benchmark_symbol, inception_date"
        " FROM analytics.v_portfolios ORDER BY portfolio_id"
    ).fetchall()
    return pd.DataFrame(rows, columns=["portfolio_id", "name", "mandate", "benchmark", "inception"])


def portfolio_meta(conn: psycopg.Connection, portfolio_id: int) -> dict:
    row = conn.execute(
        "SELECT p.name, p.benchmark_symbol, p.inception_date, max(n.date)"
        " FROM analytics.v_portfolios p JOIN analytics.v_portfolio_nav n USING (portfolio_id)"
        " WHERE p.portfolio_id = %s GROUP BY 1, 2, 3",
        (portfolio_id,),
    ).fetchone()
    if row is None:
        raise PortfolioNotFound(f"no portfolio {portfolio_id} for this manager")
    name, benchmark, inception, as_of = row
    return {
        "name": name,
        "benchmark": benchmark,
        "inception": pd.Timestamp(inception),
        "as_of": pd.Timestamp(as_of),
    }


def load_nav(conn, portfolio_id: int, end: pd.Timestamp) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT date, holdings_value::float8, cash::float8, nav::float8, net_flow::float8"
        " FROM analytics.v_portfolio_nav WHERE portfolio_id = %s AND date <= %s ORDER BY date",
        (portfolio_id, end.date()),
    ).fetchall()
    nav = pd.DataFrame(rows, columns=["date", "holdings_value", "cash", "nav", "net_flow"])
    nav["date"] = pd.to_datetime(nav["date"])
    return nav.set_index("date")


def load_closes(conn, symbols: list[str], calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """Close prices for `symbols`, on `calendar` (NaN where a stock has no bar that day)."""
    rows = conn.execute(
        "SELECT date, symbol, close::float8 FROM analytics.v_prices"
        " WHERE symbol = ANY(%s) AND date BETWEEN %s AND %s",
        (symbols, calendar[0].date(), calendar[-1].date()),
    ).fetchall()
    frame = pd.DataFrame(rows, columns=["date", "symbol", "close"])
    frame["date"] = pd.to_datetime(frame["date"])
    wide = frame.pivot(index="date", columns="symbol", values="close")
    return wide.reindex(index=calendar, columns=symbols)


def load_transactions(conn, portfolio_id: int, end: pd.Timestamp) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT trade_date, symbol, name, sector, side, quantity::float8, price::float8,"
        " fees::float8"
        " FROM analytics.v_transactions WHERE portfolio_id = %s AND trade_date <= %s"
        " ORDER BY txn_id",
        (portfolio_id, end.date()),
    ).fetchall()
    t = pd.DataFrame(
        rows,
        columns=["trade_date", "symbol", "name", "sector", "side", "quantity", "price", "fees"],
    )
    t["trade_date"] = pd.to_datetime(t["trade_date"])
    return t.assign(portfolio_id=portfolio_id)


def benchmark_exists(conn, symbol: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM analytics.v_securities WHERE symbol = %s AND is_index", (symbol,)
        ).fetchone()
        is not None
    )
