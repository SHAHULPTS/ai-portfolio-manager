"""The formulas, as plain functions of daily return series. No database, no LLM.

Conventions, used everywhere:
- `r` is a series of daily simple returns indexed by date. `rf` is the daily risk-free rate
  and `td` the number of trading days per year (252).
- Portfolio returns are time-weighted (cash flows removed), so deposits can't look like
  gains and withdrawals can't look like losses.
- Each definition matches empyrical's (the reference library the tests compare against),
  except where a docstring says otherwise.
"""

import math
from statistics import NormalDist

import numpy as np
import pandas as pd


def twr_returns(nav: pd.DataFrame) -> pd.Series:
    """Daily time-weighted returns from a NAV frame: (NAV_t - flow_t) / NAV_{t-1} - 1.

    Flows land at the end of the day (see portfolio/accounting.py), so day t's flow is
    taken out of NAV_t before comparing it with the previous close. The first day has no
    previous close, so it has no return.
    """
    r = (nav["nav"] - nav["net_flow"]) / nav["nav"].shift(1) - 1
    return r.iloc[1:].rename("return")


def price_returns(close: pd.Series) -> pd.Series:
    """Daily returns of a price series, e.g. a benchmark index."""
    return close.pct_change(fill_method=None).iloc[1:].rename("return")


def in_period(r: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    return r[(r.index >= start) & (r.index <= end)].dropna()


def cumulative_return(r: pd.Series) -> float:
    """Growth over the period: prod(1 + r) - 1."""
    return float(np.prod(1 + r.to_numpy()) - 1)


def annualized_return(r: pd.Series, td: int) -> float:
    """Annualised on a trading-day count: (1 + cumulative) ^ (td / n) - 1 (empyrical's).

    Not used for reported CAGR: NSE trades about 248 days a year, not 252, so this overstates
    a 20% CAGR by about 0.4 points. See `cagr`.
    """
    return (1 + cumulative_return(r)) ** (td / len(r)) - 1


def cagr(r: pd.Series, base_date: pd.Timestamp) -> float:
    """CAGR on calendar time: (1 + cumulative) ^ (365.25 / days) - 1.

    `base_date` is the close the first return is measured from (the trading day before the
    period starts), so a period from one 31 December close to the next is exactly one year.
    """
    years = (r.index[-1] - base_date).days / 365.25
    return (1 + cumulative_return(r)) ** (1 / years) - 1


def annualized_volatility(r: pd.Series, td: int) -> float:
    """Sample standard deviation of daily returns x sqrt(td)."""
    return float(r.std(ddof=1) * math.sqrt(td))


def sharpe_ratio(r: pd.Series, rf: float, td: int) -> float:
    """mean(r - rf) / std(r - rf) x sqrt(td)."""
    excess = r - rf
    return float(excess.mean() / excess.std(ddof=1) * math.sqrt(td))


def downside_deviation(r: pd.Series, target: float, td: int) -> float:
    """sqrt(mean(min(r - target, 0)^2)) x sqrt(td). Only shortfalls below the target count."""
    shortfall = np.minimum(r.to_numpy() - target, 0.0)
    return float(math.sqrt(np.mean(shortfall**2)) * math.sqrt(td))


def sortino_ratio(r: pd.Series, rf: float, td: int) -> float:
    """Annualised mean excess return / annualised downside deviation below rf."""
    return float((r - rf).mean() * td / downside_deviation(r, rf, td))


def drawdowns(r: pd.Series) -> pd.Series:
    """Wealth relative to its running peak, minus 1. The starting wealth of 1 counts as a peak."""
    wealth = (1 + r).cumprod()
    peak = np.maximum(wealth.cummax(), 1.0)
    return wealth / peak - 1


def max_drawdown(r: pd.Series) -> dict:
    """Worst peak-to-trough fall, with its dates. `recovered` is None if not yet recovered."""
    dd = drawdowns(r)
    trough = dd.idxmin()
    wealth = (1 + r).cumprod()
    before = wealth.loc[:trough]
    peak_level = max(before.max(), 1.0)
    peak = before.idxmax() if before.max() >= 1.0 else None  # None: peak was the start
    after = wealth.loc[trough:]
    recovered = after[after >= peak_level]
    return {
        "max_drawdown": float(dd.min()),
        "peak_date": peak,
        "trough_date": trough,
        "recovery_date": recovered.index[0] if not recovered.empty else None,
    }


def calmar_ratio(annual_return: float, r: pd.Series) -> float:
    """Annual return / |max drawdown of r|."""
    return annual_return / abs(max_drawdown(r)["max_drawdown"])


def var_historical(r: pd.Series, confidence: float) -> float:
    """1-day loss not exceeded on `confidence` of days, from the actual distribution.

    Returned as a positive loss (0.02 = a 2% loss), the 5th percentile of returns negated.
    """
    return float(-np.percentile(r.to_numpy(), 100 * (1 - confidence)))


def var_parametric(r: pd.Series, confidence: float) -> float:
    """Same, assuming normal returns: -(mean + z x std), z = the (1 - confidence) quantile.

    Not in empyrical; checked against the closed form in the tests.
    """
    z = NormalDist().inv_cdf(1 - confidence)
    return float(-(r.mean() + z * r.std(ddof=1)))


def cvar(r: pd.Series, confidence: float) -> float:
    """Expected shortfall: the average loss on the worst (1 - confidence) of days, positive."""
    values = r.to_numpy()
    k = int((len(values) - 1) * (1 - confidence))
    return float(-np.mean(np.partition(values, k)[: k + 1]))


def _aligned(rp: pd.Series, rb: pd.Series) -> tuple[pd.Series, pd.Series]:
    both = pd.concat([rp, rb], axis=1, join="inner").dropna()
    return both.iloc[:, 0], both.iloc[:, 1]


def beta(rp: pd.Series, rb: pd.Series) -> float:
    """cov(rp, rb) / var(rb), on days both have a return."""
    p, b = _aligned(rp, rb)
    return float(np.cov(p, b, ddof=1)[0, 1] / np.var(b, ddof=1))


def alpha(rp: pd.Series, rb: pd.Series, rf: float, td: int) -> float:
    """Jensen's alpha, annualised: (1 + mean((rp - rf) - beta x (rb - rf))) ^ td - 1."""
    p, b = _aligned(rp, rb)
    daily = ((p - rf) - beta(p, b) * (b - rf)).mean()
    return float((1 + daily) ** td - 1)


def tracking_error(rp: pd.Series, rb: pd.Series, td: int) -> float:
    """std(rp - rb) x sqrt(td): how far the portfolio's path strays from the benchmark's."""
    p, b = _aligned(rp, rb)
    return float((p - b).std(ddof=1) * math.sqrt(td))


def information_ratio(rp: pd.Series, rb: pd.Series, td: int) -> float:
    """mean(rp - rb) / std(rp - rb) x sqrt(td): excess return per unit of tracking error."""
    p, b = _aligned(rp, rb)
    active = p - b
    return float(active.mean() / active.std(ddof=1) * math.sqrt(td))


def correlation(rp: pd.Series, rb: pd.Series) -> float:
    p, b = _aligned(rp, rb)
    return float(np.corrcoef(p, b)[0, 1])


def monthly_returns(r: pd.Series) -> pd.Series:
    """Compounded return for each calendar month in the series."""
    return (1 + r).groupby(r.index.to_period("M")).prod() - 1


def risk_contributions(weights: pd.Series, holding_returns: pd.DataFrame, td: int) -> pd.Series:
    """Each holding's share of portfolio variance: w_i x (Sigma w)_i / (w' Sigma w). Sums to 1.

    Uses the current weights and the sample covariance of daily returns over the period.
    """
    cov = holding_returns[weights.index].cov() * td
    w = weights.to_numpy()
    marginal = cov.to_numpy() @ w
    return pd.Series(w * marginal / (w @ marginal), index=weights.index)
