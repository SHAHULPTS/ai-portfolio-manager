"""Generate the synthetic book on real prices and write it to Postgres.

Usage: python -m etl.generate_portfolios

Deterministic: the same SEED always gives the same managers, trades and NAVs. Replaces any
existing book (managers, portfolios, trades, cash flows) in one transaction, then rebuilds the
derived positions and NAV tables. Refuses to write if any invariant fails.
"""

import sys

import numpy as np
import pandas as pd
import psycopg

from config import load_settings
from etl.refresh_derived import load_closes, refresh
from portfolio.accounting import daily_nav
from portfolio.invariants import check_portfolio
from portfolio.mandates import BOOK, MANAGERS, MANDATES
from portfolio.simulator import simulate

SEED = 2026
INCEPTION_WINDOW = ("2016-01-01", "2021-12-31")


def load_listing(conn: psycopg.Connection) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    rows = conn.execute("SELECT symbol, first_date, last_date FROM core.securities").fetchall()
    return {s: (pd.Timestamp(f), pd.Timestamp(last)) for s, f, last in rows}


def build_book(closes, listing):
    """Simulate every portfolio. Returns (portfolios, trades, flows, problems)."""
    rng = np.random.default_rng(SEED)
    window = closes.index[
        (closes.index >= INCEPTION_WINDOW[0]) & (closes.index <= INCEPTION_WINDOW[1])
    ]
    portfolios, all_trades, all_flows, problems = [], [], [], []
    for pid, manager_id, name, key, cash in BOOK:
        mandate = MANDATES[key]
        inception = pd.Timestamp(rng.choice(window))
        trades, flows = simulate(
            pid,
            mandate,
            closes[list(mandate.universe)],
            listing,
            inception,
            cash,
            np.random.default_rng([SEED, pid]),
        )
        t, f = pd.DataFrame(trades), pd.DataFrame(flows)
        nav = daily_nav(t, f, closes, inception)
        problems += [
            f"{pid}: {p}"
            for p in check_portfolio(t, f, nav, closes, listing, mandate, inception, cash)
        ]
        portfolios.append(
            (pid, manager_id, name, mandate.name, mandate.benchmark, inception.date(), cash)
        )
        all_trades += trades
        all_flows += flows
    return portfolios, all_trades, all_flows, problems


def write_book(conn, portfolios, trades, flows) -> None:
    conn.execute(
        "TRUNCATE core.nav_daily, core.positions, core.transactions, core.cash_flows,"
        " core.portfolios, core.managers RESTART IDENTITY"
    )
    conn.cursor().executemany("INSERT INTO core.managers VALUES (%s, %s)", list(MANAGERS.items()))
    conn.cursor().executemany(
        "INSERT INTO core.portfolios VALUES (%s, %s, %s, %s, %s, %s, %s)", portfolios
    )
    with conn.cursor().copy(
        "COPY core.transactions (portfolio_id, symbol, trade_date, side, quantity, price, fees)"
        " FROM STDIN"
    ) as copy:
        for t in trades:
            copy.write_row(
                (
                    t["portfolio_id"],
                    t["symbol"],
                    t["trade_date"].date(),
                    t["side"],
                    t["quantity"],
                    t["price"],
                    t["fees"],
                )
            )
    with conn.cursor().copy(
        "COPY core.cash_flows (portfolio_id, date, amount, type) FROM STDIN"
    ) as copy:
        for f in flows:
            copy.write_row((f["portfolio_id"], f["date"].date(), f["amount"], f["type"]))


def main() -> int:
    with psycopg.connect(load_settings().database_url) as conn:
        listing = load_listing(conn)
        universe = sorted({s for m in MANDATES.values() for s in m.universe})
        closes = load_closes(conn, universe)
        portfolios, trades, flows, problems = build_book(closes, listing)
        if problems:
            print("Invariant violations, nothing written:", *problems, sep="\n  ")
            return 1
        with conn.transaction():
            write_book(conn, portfolios, trades, flows)
            n_pos, n_nav = refresh(conn)
    print(
        f"Wrote {len(MANAGERS)} managers, {len(portfolios)} portfolios, {len(trades):,} trades,"
        f" {len(flows)} cash flows, {n_pos} positions, {n_nav:,} NAV rows"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
