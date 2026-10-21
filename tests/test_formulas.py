"""Every formula against numbers worked out by hand, then against empyrical."""

import math

import empyrical as ep
import numpy as np
import pandas as pd
import pytest

from metrics import formulas as f

TD = 252
RF = (1.065) ** (1 / TD) - 1  # 6.5% a year, daily


def series(values, start="2024-01-01"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)


# --- by hand ------------------------------------------------------------------------------


def test_twr_takes_the_deposit_out():
    nav = pd.DataFrame(
        {"nav": [100.0, 110.0, 160.0, 154.0], "net_flow": [100.0, 0.0, 50.0, 0.0]},
        index=pd.bdate_range("2024-01-01", periods=4),
    )
    # Day 3: NAV jumps 110 -> 160 but 50 of that is a deposit, so the return is 0.
    assert f.twr_returns(nav).tolist() == pytest.approx([0.10, 0.0, 154 / 160 - 1])


def test_cumulative_and_annualised_return():
    r = series([0.001] * TD)
    assert f.cumulative_return(r) == pytest.approx(1.001**TD - 1)
    assert f.annualized_return(r, TD) == pytest.approx(1.001**TD - 1)  # exactly one year
    half = series([0.001] * (TD // 2))
    assert f.annualized_return(half, TD) == pytest.approx(1.001**TD - 1)


def test_cagr_uses_calendar_time():
    # 21% over exactly two calendar years is 10% a year, however many days the market traded.
    days = pd.bdate_range("2024-01-02", "2025-12-31")
    r = pd.Series(1.21 ** (1 / len(days)) - 1, index=days)
    base = pd.Timestamp("2023-12-31")
    assert f.cagr(r, base) == pytest.approx(0.10, abs=1e-3)  # 731 days vs 730.5: within 0.1bp


def test_volatility():
    r = series([0.01, -0.01, 0.01, -0.01])
    assert f.annualized_volatility(r, TD) == pytest.approx(math.sqrt(0.0004 / 3) * math.sqrt(TD))


def test_sharpe_and_sortino():
    r = series([0.02, 0.0, 0.02, 0.0]) + RF  # excess: mean 0.01, sample std 0.011547
    assert f.sharpe_ratio(r, RF, TD) == pytest.approx(0.01 / math.sqrt(0.0004 / 3) * math.sqrt(TD))

    r = series([0.02, -0.01, 0.02, -0.01]) + RF
    # mean excess 0.005 -> 1.26 a year; shortfalls 0, -0.01, 0, -0.01 -> sqrt(0.00005) a day
    expected = (0.005 * TD) / (math.sqrt(0.00005) * math.sqrt(TD))
    assert f.sortino_ratio(r, RF, TD) == pytest.approx(expected)


def test_max_drawdown_with_dates():
    r = series([0.10, -0.20, 0.125, 0.0])  # wealth 1.10, 0.88, 0.99, 0.99
    dd = f.max_drawdown(r)
    assert dd["max_drawdown"] == pytest.approx(0.88 / 1.10 - 1)  # -20%
    assert dd["peak_date"] == r.index[0]
    assert dd["trough_date"] == r.index[1]
    assert dd["recovery_date"] is None

    recovered = series([0.10, -0.20, 0.125, 0.0, 0.12])  # 0.99 x 1.12 = 1.1088 > 1.10
    assert f.max_drawdown(recovered)["recovery_date"] == recovered.index[4]


def test_drawdown_from_the_very_start():
    dd = f.max_drawdown(series([-0.10, 0.05]))
    assert dd["max_drawdown"] == pytest.approx(-0.10)
    assert dd["peak_date"] is None  # the peak was the starting wealth


def test_var_and_cvar():
    r = series(np.linspace(-0.05, 0.05, 101))  # -5.0%, -4.9%, ..., +5.0%
    assert f.var_historical(r, 0.95) == pytest.approx(0.045)
    # worst int(100 x 0.05) + 1 = 6 days: -5.0% ... -4.5%, average -4.75%
    assert f.cvar(r, 0.95) == pytest.approx(0.0475)


def test_parametric_var():
    r = series([0.01, -0.01] * 50)  # mean 0
    assert f.var_parametric(r, 0.95) == pytest.approx(1.6448536 * r.std(ddof=1), rel=1e-6)


def test_two_times_the_benchmark():
    rb = series([0.01, -0.02, 0.015, 0.0, -0.005, 0.02])
    rp = 2 * rb
    assert f.beta(rp, rb) == pytest.approx(2.0)
    assert f.correlation(rp, rb) == pytest.approx(1.0)
    assert f.tracking_error(rp, rb, TD) == pytest.approx(rb.std(ddof=1) * math.sqrt(TD))
    # rp - rf - 2 (rb - rf) = rf every day: 2x leverage with no borrowing cost earns rf of alpha.
    assert f.alpha(rp, rb, RF, TD) == pytest.approx(0.065)


def test_information_ratio():
    rb = series([0.01, -0.02, 0.015, 0.0])
    rp = rb + series([0.002, 0.0, 0.002, 0.0])  # active mean 0.001, sample std 0.0011547
    assert f.information_ratio(rp, rb, TD) == pytest.approx(
        0.001 / math.sqrt(4e-6 / 3) * math.sqrt(TD)
    )


def test_relative_metrics_use_common_days_only():
    rb = series([0.01, -0.02, 0.015, 0.0])
    rp = (2 * rb).drop(rb.index[2])
    assert f.beta(rp, rb) == pytest.approx(2.0)


def test_monthly_returns():
    r = pd.Series(
        [0.10, 0.10, -0.50], index=pd.to_datetime(["2024-01-30", "2024-01-31", "2024-02-01"])
    )
    m = f.monthly_returns(r)
    assert m.tolist() == pytest.approx([0.21, -0.50])


def test_risk_contributions():
    rng = np.random.default_rng(1)
    rets = pd.DataFrame(rng.normal(0, 0.01, size=(5000, 2)), columns=["A", "B"])
    equal = f.risk_contributions(pd.Series([0.5, 0.5], index=["A", "B"]), rets, TD)
    assert equal.tolist() == pytest.approx([0.5, 0.5], abs=0.03)
    tilted = f.risk_contributions(pd.Series([0.8, 0.2], index=["A", "B"]), rets, TD)
    assert tilted.sum() == pytest.approx(1.0)
    assert tilted["A"] > 0.8  # uncorrelated: risk share ~ w^2, so the big position dominates


# --- against empyrical ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def pair():
    """Fat-tailed, correlated daily returns, four years long."""
    rng = np.random.default_rng(42)
    rb = rng.standard_t(4, size=1000) * 0.008 + 0.0004
    rp = 1.1 * rb + rng.standard_t(4, size=1000) * 0.004 + 0.0002
    return series(rp, "2020-01-01"), series(rb, "2020-01-01")


@pytest.mark.parametrize(
    "ours, theirs",
    [
        (lambda r, b: f.annualized_return(r, TD), lambda r, b: ep.annual_return(r)),
        (lambda r, b: f.annualized_volatility(r, TD), lambda r, b: ep.annual_volatility(r)),
        (lambda r, b: f.sharpe_ratio(r, RF, TD), lambda r, b: ep.sharpe_ratio(r, risk_free=RF)),
        (
            lambda r, b: f.sortino_ratio(r, RF, TD),
            lambda r, b: ep.sortino_ratio(r, required_return=RF),
        ),
        (
            lambda r, b: f.downside_deviation(r, RF, TD),
            lambda r, b: ep.downside_risk(r, required_return=RF),
        ),
        (lambda r, b: f.max_drawdown(r)["max_drawdown"], lambda r, b: ep.max_drawdown(r)),
        (
            lambda r, b: f.calmar_ratio(f.annualized_return(r, TD), r),
            lambda r, b: ep.calmar_ratio(r),
        ),
        (lambda r, b: -f.var_historical(r, 0.95), lambda r, b: ep.value_at_risk(r, cutoff=0.05)),
        (lambda r, b: -f.cvar(r, 0.95), lambda r, b: ep.conditional_value_at_risk(r, cutoff=0.05)),
        (lambda r, b: f.beta(r, b), lambda r, b: ep.beta(r, b)),
        (lambda r, b: f.alpha(r, b, RF, TD), lambda r, b: ep.alpha(r, b, risk_free=RF)),
        (lambda r, b: f.cumulative_return(r), lambda r, b: ep.cum_returns_final(r)),
    ],
    ids=[
        "cagr",
        "volatility",
        "sharpe",
        "sortino",
        "downside",
        "max_drawdown",
        "calmar",
        "var",
        "cvar",
        "beta",
        "alpha",
        "total_return",
    ],
)
def test_matches_empyrical(pair, ours, theirs):
    rp, rb = pair
    assert ours(rp, rb) == pytest.approx(float(theirs(rp, rb)), rel=1e-9)
