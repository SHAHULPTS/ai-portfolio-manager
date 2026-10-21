"""Recompute core.positions and core.nav_daily from transactions, cash flows and prices.

Usage: python -m etl.refresh_derived

Runs as the admin role (which bypasses RLS) so it sees every portfolio. It runs automatically
at the end of etl.generate_portfolios. Run it alone after changing transactions by hand.
"""

import pandas as pd
import psycopg

from config import load_settings
from portfolio.accounting import daily_nav, positions


def load_closes(conn: psycopg.Connection, symbols: list[str]) -> pd.DataFrame:
    """Wide close-price frame on the NIFTY50 trading calendar: rows = dates, columns = symbols."""
    calendar = pd.to_datetime(
        [
            r[0]
            for r in conn.execute(
                "SELECT date FROM core.prices_daily WHERE symbol = 'NIFTY50' ORDER BY date"
            )
        ]
    )
    rows = conn.execute(
        "SELECT date, symbol, close::float8 FROM core.prices_daily WHERE symbol = ANY(%s)",
        (symbols,),
    ).fetchall()
    frame = pd.DataFrame(rows, columns=["date", "symbol", "close"])
    frame["date"] = pd.to_datetime(frame["date"])
    wide = frame.pivot(index="date", columns="symbol", values="close")
    return wide.reindex(index=calendar, columns=symbols)


def load_book(conn: psycopg.Connection) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    portfolios = pd.DataFrame(
        conn.execute("SELECT portfolio_id, inception_date FROM core.portfolios").fetchall(),
        columns=["portfolio_id", "inception_date"],
    )
    txns = pd.DataFrame(
        conn.execute(
            "SELECT portfolio_id, symbol, trade_date, side, quantity::float8, price::float8,"
            " fees::float8 FROM core.transactions ORDER BY txn_id"
        ).fetchall(),
        columns=["portfolio_id", "symbol", "trade_date", "side", "quantity", "price", "fees"],
    )
    flows = pd.DataFrame(
        conn.execute(
            "SELECT portfolio_id, date, amount::float8 FROM core.cash_flows ORDER BY cash_flow_id"
        ).fetchall(),
        columns=["portfolio_id", "date", "amount"],
    )
    for frame, col in [(portfolios, "inception_date"), (txns, "trade_date"), (flows, "date")]:
        frame[col] = pd.to_datetime(frame[col])
    return portfolios, txns, flows


def refresh(conn: psycopg.Connection) -> tuple[int, int]:
    """Rebuild both derived tables inside the caller's transaction. Returns row counts."""
    portfolios, txns, flows = load_book(conn)
    closes = load_closes(conn, sorted(txns["symbol"].unique()))

    pos = positions(txns)
    navs = []
    for pid, inception in portfolios.itertuples(index=False):
        nav = daily_nav(
            txns[txns.portfolio_id == pid], flows[flows.portfolio_id == pid], closes, inception
        )
        navs.append(nav.reset_index().assign(portfolio_id=pid))
    nav_all = pd.concat(navs) if navs else pd.DataFrame()

    conn.execute("TRUNCATE core.positions, core.nav_daily")
    with conn.cursor().copy(
        "COPY core.positions (portfolio_id, symbol, quantity, avg_cost, realized_pnl) FROM STDIN"
    ) as copy:
        for row in pos.itertuples(index=False):
            copy.write_row(row)
    with conn.cursor().copy(
        "COPY core.nav_daily (portfolio_id, date, holdings_value, cash, nav, net_flow) FROM STDIN"
    ) as copy:
        cols = ["portfolio_id", "date", "holdings_value", "cash", "nav", "net_flow"]
        for pid, d, hv, cash, nav, flow in nav_all[cols].itertuples(index=False):
            copy.write_row(
                (pid, d.date(), round(hv, 2), round(cash, 2), round(nav, 2), round(flow, 2))
            )
    return len(pos), len(nav_all)


if __name__ == "__main__":
    with psycopg.connect(load_settings().database_url) as conn, conn.transaction():
        n_pos, n_nav = refresh(conn)
    print(f"Refreshed {n_pos} positions and {n_nav:,} NAV rows")
