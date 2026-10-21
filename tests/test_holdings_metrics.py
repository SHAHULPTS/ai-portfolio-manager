import numpy as np
import pandas as pd
import pytest

from metrics.holdings import concentration, pnl_by_stock, sector_allocation
from portfolio.accounting import daily_nav
from portfolio.mandates import Mandate
from portfolio.simulator import simulate


def test_sector_allocation_includes_cash_and_sums_to_one():
    pos = pd.DataFrame(
        {
            "sector": ["IT", "IT", "Banks"],
            "market_value": [30.0, 20.0, 40.0],
            "symbol": ["A", "B", "C"],
            "weight": [0.3, 0.2, 0.4],
        }
    )
    table = sector_allocation(pos, nav=100.0)
    assert dict(zip(table.sector, table.weight, strict=True)) == pytest.approx(
        {"IT": 0.5, "Banks": 0.4, "Cash": 0.1}
    )


def test_concentration():
    equal = pd.DataFrame({"symbol": list("ABCD"), "market_value": [25.0] * 4, "weight": [0.24] * 4})
    c = concentration(equal, top_n=2)
    assert c["hhi"] == pytest.approx(0.25)
    assert c["effective_holdings"] == pytest.approx(4)
    assert c["top_n_weight"] == pytest.approx(0.48)


@pytest.fixture(scope="module")
def book():
    cal = pd.bdate_range("2022-01-03", periods=400)
    rng = np.random.default_rng(3)
    syms = ["A", "B", "C", "D"]
    closes = pd.DataFrame(
        100 * np.exp(rng.normal(0.0003, 0.015, (400, 4)).cumsum(0)), index=cal, columns=syms
    )
    listing = {s: (cal[0], cal[-1]) for s in syms}
    mandate = Mandate("T", "IDX", {"A": 0.4, "B": 0.3, "C": 0.3}, ("D",))
    trades, flows = simulate(
        1, mandate, closes, listing, cal[0], 1_000_000, np.random.default_rng(9)
    )
    t, fl = pd.DataFrame(trades), pd.DataFrame(flows)
    return t, fl, closes, daily_nav(t, fl, closes, cal[0])


@pytest.mark.parametrize("start_i, end_i", [(0, 399), (120, 300), (250, 399)])
def test_pnl_by_stock_adds_up_to_the_portfolio_gain(book, start_i, end_i):
    t, flows, closes, nav = book
    start, end = closes.index[start_i], closes.index[end_i]
    table = pnl_by_stock(t, closes, start, end)

    base_nav = nav["nav"].iloc[start_i - 1] if start_i > 0 else 0.0
    flows_in = flows[(flows.date >= start) & (flows.date <= end)]["amount"].sum()
    gain = nav["nav"].iloc[end_i] - base_nav - flows_in
    # Cash earns nothing, so every rupee of gain is some stock's P&L (fees included).
    assert table["pnl"].sum() == pytest.approx(gain, abs=0.01)
