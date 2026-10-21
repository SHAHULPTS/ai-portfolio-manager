"""Turn trades and cash flows into positions and a daily NAV.

The simulator and the database refresh both use this module, so the holdings a simulated
manager trades on are exactly the holdings that get reported later.

Conventions:
- Trades happen at the day's close, and cash flows land at the end of the day. So the NAV on
  day t already includes that day's trades and flows. Phase 3's time-weighted return takes
  the flow back out: r_t = (NAV_t - flow_t) / NAV_{t-1} - 1.
- Average-cost accounting: buy fees are added to cost basis, sell fees reduce realised P&L.
"""

import numpy as np
import pandas as pd

EPS = 1e-9


def positions(txns: pd.DataFrame) -> pd.DataFrame:
    """Average-cost position per (portfolio_id, symbol), including closed ones.

    `txns` needs portfolio_id, symbol, trade_date, side, quantity, price, fees, in the order
    they were executed. Raises ValueError if any sale exceeds the quantity held (a short).
    """
    rows = []
    ordered = txns.sort_values("trade_date", kind="stable")
    for (pid, sym), g in ordered.groupby(["portfolio_id", "symbol"], sort=True):
        qty = cost = realized = 0.0
        for side, q, px, fee in g[["side", "quantity", "price", "fees"]].itertuples(index=False):
            if side == "BUY":
                qty += q
                cost += q * px + fee
            else:
                if q > qty + EPS:
                    raise ValueError(f"portfolio {pid} sells {q} {sym} but holds {qty}")
                avg = cost / qty
                realized += q * px - fee - q * avg
                cost -= q * avg
                qty -= q
        avg_cost = cost / qty if qty > EPS else 0.0
        rows.append((pid, sym, qty, avg_cost, realized))
    return pd.DataFrame(
        rows, columns=["portfolio_id", "symbol", "quantity", "avg_cost", "realized_pnl"]
    )


def daily_nav(
    txns: pd.DataFrame, flows: pd.DataFrame, closes: pd.DataFrame, start: pd.Timestamp
) -> pd.DataFrame:
    """Daily holdings value, cash and NAV for ONE portfolio, every trading day from `start`.

    `closes` is wide: one row per trading day (the calendar), one column per symbol, NaN where
    a stock has no bar. Missing prices are carried forward, so a held stock with a gap in its
    data keeps its last known value rather than dropping to zero.
    """
    calendar = closes.index[closes.index >= start]
    prices = closes.ffill().loc[calendar]

    unknown_dates = set(txns["trade_date"]) - set(calendar)
    if unknown_dates:
        raise ValueError(f"trades on non-trading days: {sorted(unknown_dates)[:5]}")

    signed_qty = np.where(txns["side"] == "BUY", txns["quantity"], -txns["quantity"])
    value = txns["quantity"] * txns["price"]
    cash_effect = np.where(txns["side"] == "BUY", -(value + txns["fees"]), value - txns["fees"])
    t = txns.assign(signed_qty=signed_qty, cash_effect=cash_effect)

    held = (
        t.pivot_table(index="trade_date", columns="symbol", values="signed_qty", aggfunc="sum")
        .reindex(calendar)
        .fillna(0.0)
        .cumsum()
    )
    held_prices = prices[held.columns]
    if (held_prices.isna() & (held > EPS)).any().any():
        raise ValueError("a stock is held on a day before it has any price")
    holdings_value = (held * held_prices.fillna(0.0)).sum(axis=1)

    trade_cash = t.groupby("trade_date")["cash_effect"].sum().reindex(calendar, fill_value=0.0)
    net_flow = flows.groupby("date")["amount"].sum().reindex(calendar, fill_value=0.0)
    cash = (net_flow + trade_cash).cumsum()

    return pd.DataFrame(
        {
            "holdings_value": holdings_value,
            "cash": cash,
            "nav": holdings_value + cash,
            "net_flow": net_flow,
        },
        index=calendar,
    ).rename_axis("date")
