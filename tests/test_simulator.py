"""The simulator on made-up prices: invariants hold, edge cases behave, output is reproducible."""

import numpy as np
import pandas as pd
import pytest

from portfolio.accounting import daily_nav
from portfolio.invariants import check_portfolio
from portfolio.mandates import BOOK, MANDATES, Mandate
from portfolio.simulator import simulate

CALENDAR = pd.bdate_range("2018-01-01", "2021-12-31")
SYMBOLS = ["AAA", "BBB", "CCC", "LATE", "GONE", "ALT1", "ALT2"]
MANDATE = Mandate(
    name="Test",
    benchmark="IDX",
    targets={"AAA": 0.3, "BBB": 0.25, "CCC": 0.2, "LATE": 0.15, "GONE": 0.1},
    alternates=("ALT1", "ALT2"),
)
LATE_LISTING = pd.Timestamp("2019-06-03")
GONE_LAST_DAY = pd.Timestamp("2020-09-30")


@pytest.fixture(scope="module")
def market():
    rng = np.random.default_rng(0)
    steps = rng.normal(0.0004, 0.015, size=(len(CALENDAR), len(SYMBOLS)))
    closes = pd.DataFrame(100 * np.exp(steps.cumsum(axis=0)), index=CALENDAR, columns=SYMBOLS)
    closes.loc[closes.index < LATE_LISTING, "LATE"] = np.nan
    closes.loc[closes.index > GONE_LAST_DAY, "GONE"] = np.nan
    closes.iloc[300:305, 0] = np.nan  # a short data gap in AAA
    listing = {s: (CALENDAR[0], CALENDAR[-1]) for s in SYMBOLS}
    listing["LATE"] = (LATE_LISTING, CALENDAR[-1])
    listing["GONE"] = (CALENDAR[0], GONE_LAST_DAY)
    return closes, listing


def run(market, seed=1, inception=CALENDAR[10], cash=1_000_000):
    closes, listing = market
    trades, flows = simulate(
        7, MANDATE, closes, listing, inception, cash, np.random.default_rng(seed)
    )
    t, f = pd.DataFrame(trades), pd.DataFrame(flows)
    return t, f, daily_nav(t, f, closes, inception)


def test_all_invariants_hold(market):
    closes, listing = market
    t, f, nav = run(market)
    assert check_portfolio(t, f, nav, closes, listing, MANDATE, CALENDAR[10], 1_000_000) == []


def test_late_listing_is_bought_only_after_it_lists(market):
    t, _, _ = run(market)
    late_buys = t[(t.symbol == "LATE") & (t.side == "BUY")]
    assert not late_buys.empty
    assert late_buys.trade_date.min() >= LATE_LISTING


def test_stock_whose_data_ends_is_sold_on_its_last_day(market):
    t, _, _ = run(market)
    gone = t[t.symbol == "GONE"]
    held = gone.quantity.where(gone.side == "BUY", -gone.quantity).sum()
    assert held == 0
    assert gone.trade_date.max() == GONE_LAST_DAY


def test_cash_buffer_is_kept(market):
    _, _, nav = run(market)
    cash_share = (nav.cash / nav.nav).iloc[1:]
    assert cash_share.min() > 0.005  # the 1% buffer, give or take price moves
    assert cash_share.median() < 0.03  # but cash isn't piling up either


def test_life_cycle_events_happen(market):
    t, f, _ = run(market)
    assert {"BUY", "SELL"} <= set(t.side)
    assert {"DEPOSIT", "WITHDRAWAL"} <= set(f.type)
    assert set(t.symbol) & {"ALT1", "ALT2"}, "expected at least one discretionary swap"


def test_same_seed_same_book(market):
    a, _, _ = run(market, seed=5)
    b, _, _ = run(market, seed=5)
    c, _, _ = run(market, seed=6)
    pd.testing.assert_frame_equal(a, b)
    assert not a.equals(c)


def test_checker_catches_a_bad_trade(market):
    closes, listing = market
    t, f, nav = run(market)
    t.loc[t.index[3], "price"] *= 1.01
    problems = check_portfolio(t, f, nav, closes, listing, MANDATE, CALENDAR[10], 1_000_000)
    assert any("not traded at the close" in p for p in problems)


def test_book_config_is_consistent():
    for _, _, _, key, cash in BOOK:
        m = MANDATES[key]
        assert sum(m.targets.values()) == pytest.approx(1.0), key
        assert not set(m.targets) & set(m.alternates), key
        assert cash > 0
