"""Average-cost positions and daily NAV, checked against numbers worked out by hand."""

import numpy as np
import pandas as pd
import pytest

from portfolio.accounting import daily_nav, positions

D = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])


def txns(*rows):
    cols = ["portfolio_id", "symbol", "trade_date", "side", "quantity", "price", "fees"]
    frame = pd.DataFrame(rows, columns=cols)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame


def test_average_cost_and_realized_pnl():
    pos = positions(
        txns(
            (1, "ABC", "2024-01-01", "BUY", 10, 100.0, 1.0),  # cost 1001
            (1, "ABC", "2024-01-02", "BUY", 10, 110.0, 1.0),  # cost 2102, avg 105.10
            (1, "ABC", "2024-01-03", "SELL", 5, 120.0, 1.0),  # realised 600 - 1 - 525.5 = 73.5
        )
    ).iloc[0]
    assert pos.quantity == 15
    assert pos.avg_cost == pytest.approx(105.10)
    assert pos.realized_pnl == pytest.approx(73.5)


def test_closed_position_keeps_its_realized_pnl():
    pos = positions(
        txns(
            (1, "ABC", "2024-01-01", "BUY", 10, 100.0, 0.0),
            (1, "ABC", "2024-01-02", "SELL", 10, 90.0, 0.0),
        )
    ).iloc[0]
    assert pos.quantity == 0
    assert pos.realized_pnl == pytest.approx(-100.0)


def test_short_sale_is_rejected():
    with pytest.raises(ValueError, match="sells 5"):
        positions(txns((1, "ABC", "2024-01-01", "SELL", 5, 100.0, 0.0)))


def test_daily_nav_by_hand():
    closes = pd.DataFrame({"ABC": [100.0, np.nan, 120.0, 130.0]}, index=D)
    t = txns(
        (1, "ABC", "2024-01-01", "BUY", 10, 100.0, 1.0),
        (1, "ABC", "2024-01-03", "SELL", 5, 120.0, 0.6),
    )
    flows = pd.DataFrame(
        {
            "portfolio_id": 1,
            "date": pd.to_datetime(["2024-01-01", "2024-01-04"]),
            "amount": [10_000.0, -500.0],
        }
    )
    nav = daily_nav(t, flows, closes, D[0])

    # Day 1: deposit 10,000, buy 10 @ 100 + 1 fee -> cash 8,999, holdings 1,000.
    # Day 2: no ABC bar, so the last price (100) carries forward.
    # Day 3: sell 5 @ 120 - 0.6 fee -> cash 9,598.4, holdings 5 x 120.
    # Day 4: withdraw 500 -> cash 9,098.4, holdings 5 x 130.
    assert nav["cash"].tolist() == pytest.approx([8999.0, 8999.0, 9598.4, 9098.4])
    assert nav["holdings_value"].tolist() == pytest.approx([1000.0, 1000.0, 600.0, 650.0])
    assert nav["nav"].tolist() == pytest.approx([9999.0, 9999.0, 10198.4, 9748.4])
    assert nav["net_flow"].tolist() == pytest.approx([10_000.0, 0.0, 0.0, -500.0])


def test_trade_on_a_non_trading_day_is_rejected():
    closes = pd.DataFrame({"ABC": [100.0, 101.0]}, index=D[:2])
    t = txns((1, "ABC", "2024-01-06", "BUY", 1, 100.0, 0.0))
    empty_flows = pd.DataFrame(columns=["portfolio_id", "date", "amount"])
    with pytest.raises(ValueError, match="non-trading"):
        daily_nav(t, empty_flows, closes, D[0])
