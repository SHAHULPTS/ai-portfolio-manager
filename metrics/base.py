"""What every metric returns, and what it is computed from.

A MetricResult carries everything the explainer needs to state the number honestly: the
value, its unit, the formula in words, the exact period, and the assumptions behind it.
"""

from dataclasses import dataclass, field
from datetime import date

import pandas as pd


class MetricError(Exception):
    """The metric can't be computed for this input (e.g. too few days of data)."""


@dataclass(frozen=True)
class MetricConfig:
    risk_free_rate: float = 0.065  # annual
    trading_days: int = 252
    var_confidence: float = 0.95
    min_observations: int = 20  # daily returns needed before risk numbers mean anything
    top_n: int = 5

    @property
    def daily_risk_free(self) -> float:
        return (1 + self.risk_free_rate) ** (1 / self.trading_days) - 1


@dataclass
class MetricResult:
    name: str
    label: str
    value: float | None
    unit: str  # "%", "INR", "ratio", "count", "date", "table"
    formula: str
    period_start: date | None = None
    period_end: date | None = None
    n_observations: int | None = None
    assumptions: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)  # extra numbers, e.g. drawdown dates
    table: list[dict] | None = None  # for metrics whose answer is a list


@dataclass
class MetricInputs:
    """Everything a metric might need, loaded once per request by the engine.

    Only what the requested metrics declare gets loaded; the rest stays None.
    """

    portfolio_id: int
    portfolio_name: str
    benchmark: str
    inception: pd.Timestamp
    start: pd.Timestamp
    end: pd.Timestamp
    nav: pd.DataFrame | None = None  # date index; holdings_value, cash, nav, net_flow
    benchmark_close: pd.Series | None = None
    transactions: pd.DataFrame | None = None  # every trade up to `end`
    closes: pd.DataFrame | None = None  # date x symbol, on the portfolio's trading calendar
    holdings: pd.DataFrame | None = None  # open positions as of `end` (built by the engine)
    realized: pd.DataFrame | None = None  # realised P&L per stock, inception to `end`
