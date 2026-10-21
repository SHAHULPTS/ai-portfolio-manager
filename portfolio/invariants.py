"""Rules every simulated portfolio must obey. Returns a list of violations (empty = all good).

The generator refuses to write a book with violations, and the tests run the same checks.
"""

import pandas as pd

from portfolio.mandates import Mandate

CASH_TOLERANCE = 0.01  # INR; rounding noise


def check_portfolio(
    trades: pd.DataFrame,
    flows: pd.DataFrame,
    nav: pd.DataFrame,
    closes: pd.DataFrame,
    listing: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    mandate: Mandate,
    inception: pd.Timestamp,
    initial_cash: float,
) -> list[str]:
    problems = []

    if (nav["cash"] < -CASH_TOLERANCE).any():
        first = nav.index[nav["cash"] < -CASH_TOLERANCE][0].date()
        problems.append(f"cash goes negative on {first}")

    expected_days = closes.index[closes.index >= inception]
    if not nav.index.equals(expected_days):
        problems.append("NAV is not one row per trading day from inception")

    day_one_fees = trades.loc[trades["trade_date"] == inception, "fees"].sum()
    if abs(nav["nav"].iloc[0] - (initial_cash - day_one_fees)) > CASH_TOLERANCE:
        problems.append(
            f"day-one NAV {nav['nav'].iloc[0]:.2f} != initial cash minus fees "
            f"{initial_cash - day_one_fees:.2f}"
        )

    outside = set(trades["symbol"]) - set(mandate.universe)
    if outside:
        problems.append(f"trades outside the mandate universe: {sorted(outside)}")

    for row in trades.itertuples(index=False):
        first, last = listing[row.symbol]
        if not first <= row.trade_date <= last:
            problems.append(f"{row.symbol} traded on {row.trade_date.date()} outside its listing")
        close = closes.at[row.trade_date, row.symbol]
        if pd.isna(close) or abs(close - row.price) > 1e-6:
            problems.append(f"{row.symbol} on {row.trade_date.date()} not traded at the close")
        if row.quantity != int(row.quantity) or row.quantity <= 0:
            problems.append(f"{row.symbol} on {row.trade_date.date()}: bad quantity {row.quantity}")

    signed = trades["quantity"].where(trades["side"] == "BUY", -trades["quantity"])
    running = signed.groupby(trades["symbol"]).cumsum()
    if (running < 0).any():
        problems.append("a position goes short")

    held_after = trades.assign(running=running).groupby("symbol").last()
    for symbol, row in held_after.iterrows():
        last = listing[symbol][1]
        if last < closes.index[-1] and row["running"] > 0:
            problems.append(f"{symbol} still held after its data ends on {last.date()}")

    if not flows.empty and (flows.loc[flows["type"] == "DEPOSIT", "amount"] <= 0).any():
        problems.append("a deposit is not positive")
    return problems
