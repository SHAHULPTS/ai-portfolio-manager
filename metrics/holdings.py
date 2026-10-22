"""Holdings-based numbers: what the portfolio owns now, and which stocks made or lost money.

`positions` is the v_positions frame: one row per open holding, with symbol, sector,
quantity, market_value, unrealized_pnl and weight (share of NAV including cash).
"""

import pandas as pd


def sector_allocation(positions: pd.DataFrame, nav: float) -> pd.DataFrame:
    """Weight of NAV in each sector, with cash as its own line. Sums to 1."""
    by_sector = positions.groupby("sector", as_index=False)["market_value"].sum()
    cash = nav - positions["market_value"].sum()
    table = pd.concat(
        [by_sector, pd.DataFrame({"sector": ["Cash"], "market_value": [cash]})],
        ignore_index=True,
    )
    table["weight"] = table["market_value"] / nav
    return table.sort_values("weight", ascending=False, ignore_index=True)


def concentration(positions: pd.DataFrame, top_n: int) -> dict:
    """Top-N weight, Herfindahl index and effective number of holdings.

    HHI = sum of squared weights *within the stock holdings* (cash excluded, weights
    rescaled to sum to 1). 1/HHI is the number of equal-sized holdings that would be
    equally concentrated: 10 stocks with a 1/HHI of 6 behave like 6 equal positions.
    """
    w = positions["market_value"] / positions["market_value"].sum()
    hhi = float((w**2).sum())
    top = positions.nlargest(top_n, "weight")
    return {
        "top_n_weight": float(top["weight"].sum()),
        "top_n_symbols": top["symbol"].tolist(),
        "hhi": hhi,
        "effective_holdings": 1 / hhi,
        "n_holdings": len(positions),
    }


def pnl_by_stock(
    transactions: pd.DataFrame,
    closes: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Money each stock made over [start, end], net of fees.

    pnl_i = value_i(end) - value_i(base) - net purchases_i during the period,
    where `base` is the last trading day before `start` and
    net purchases = buys (value + fees) - sells (value - fees).

    Summed over all stocks this equals NAV(end) - NAV(base) - deposits + withdrawals:
    the portfolio's investment gain, with nothing lost or double-counted.
    """
    calendar = closes.index
    base = calendar[calendar < start][-1] if (calendar < start).any() else None
    marks = closes.ffill()

    t = transactions.copy()
    signed_qty = t["quantity"].where(t["side"] == "BUY", -t["quantity"])
    gross = t["quantity"] * t["price"]
    t["cash_out"] = (gross + t["fees"]).where(t["side"] == "BUY", -(gross - t["fees"]))
    t["signed_qty"] = signed_qty

    held_base = (
        t[t["trade_date"] <= base].groupby("symbol")["signed_qty"].sum()
        if base is not None
        else pd.Series(dtype=float)
    )
    held_end = t[t["trade_date"] <= end].groupby("symbol")["signed_qty"].sum()
    in_window = t[(t["trade_date"] >= start) & (t["trade_date"] <= end)]
    net_purchases = in_window.groupby("symbol")["cash_out"].sum()

    symbols = sorted(set(held_base.index) | set(held_end.index) | set(net_purchases.index))
    rows = []
    for s in symbols:
        q0, q1 = held_base.get(s, 0.0), held_end.get(s, 0.0)
        v0 = q0 * marks.at[base, s] if base is not None and q0 else 0.0
        v1 = q1 * marks.at[end, s] if q1 else 0.0
        bought = net_purchases.get(s, 0.0)
        if q0 == 0 and q1 == 0 and bought == 0:
            continue
        rows.append(
            {
                "symbol": s,
                "start_value": v0,
                "end_value": v1,
                "net_purchases": bought,
                "pnl": v1 - v0 - bought,
            }
        )
    table = pd.DataFrame(
        rows, columns=["symbol", "start_value", "end_value", "net_purchases", "pnl"]
    )
    return table.sort_values("pnl", ascending=False, ignore_index=True)
