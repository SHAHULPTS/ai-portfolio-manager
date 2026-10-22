"""The metric catalogue: every metric the agent can compute, by name.

The planner LLM may only ask for names in CATALOG. Each entry says what it needs loaded
("nav", "benchmark", "holdings"), and its function turns MetricInputs into a MetricResult
whose `formula` and `assumptions` the explainer quotes back to the manager.
"""

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from metrics import formulas as f
from metrics.base import MetricConfig, MetricError, MetricInputs, MetricResult
from metrics.holdings import concentration, pnl_by_stock, sector_allocation


@dataclass(frozen=True)
class Spec:
    name: str
    label: str
    category: str
    needs: frozenset[str]
    fn: Callable[[MetricInputs, MetricConfig], MetricResult]
    description: str


RETURNS_NOTE = "daily time-weighted price returns; dividends not included"


def _returns(inp: MetricInputs, cfg: MetricConfig) -> pd.Series:
    r = f.in_period(f.twr_returns(inp.nav), inp.start, inp.end)
    if len(r) < cfg.min_observations:
        raise MetricError(
            f"only {len(r)} trading days in the period; need at least {cfg.min_observations}"
        )
    return r


def _cagr(inp: MetricInputs, r: pd.Series) -> float:
    base = inp.nav.index[inp.nav.index < r.index[0]][-1]
    return f.cagr(r, base)


def _benchmark_returns(inp: MetricInputs, r: pd.Series) -> pd.Series:
    rb = f.in_period(f.price_returns(inp.benchmark_close), inp.start, inp.end)
    return rb.reindex(r.index)


def _result(inp, cfg, r, name, label, value, unit, formula, **extra) -> MetricResult:
    assumptions = {"returns": RETURNS_NOTE, "trading_days_per_year": cfg.trading_days}
    assumptions.update(extra.pop("assumptions", {}))
    return MetricResult(
        name=name,
        label=label,
        value=value,
        unit=unit,
        formula=formula,
        period_start=(r.index[0] if r is not None else inp.start).date(),
        period_end=(r.index[-1] if r is not None else inp.end).date(),
        n_observations=len(r) if r is not None else None,
        assumptions=assumptions,
        **extra,
    )


def _rf(cfg):
    return {"risk_free_rate_annual": cfg.risk_free_rate}


# --- returns ------------------------------------------------------------------------------


def total_return(inp, cfg):
    r = _returns(inp, cfg)
    return _result(
        inp,
        cfg,
        r,
        "total_return",
        "Total return",
        f.cumulative_return(r),
        "%",
        "prod(1 + daily return) - 1",
    )


def cagr(inp, cfg):
    r = _returns(inp, cfg)
    return _result(
        inp,
        cfg,
        r,
        "cagr",
        "Annualised return (CAGR)",
        _cagr(inp, r),
        "%",
        "(1 + total return) ^ (365.25 / calendar days) - 1",
    )


def monthly_returns(inp, cfg):
    r = _returns(inp, cfg)
    m = f.monthly_returns(r)
    table = [{"month": str(p), "return": float(v)} for p, v in m.items()]
    return _result(
        inp,
        cfg,
        r,
        "monthly_returns",
        "Monthly returns",
        None,
        "table",
        "compounded daily returns within each calendar month",
        table=table,
        details={"best_month": str(m.idxmax()), "worst_month": str(m.idxmin())},
    )


def excess_return(inp, cfg):
    r = _returns(inp, cfg)
    rb = _benchmark_returns(inp, r)
    p_total, b_total = f.cumulative_return(r), f.cumulative_return(rb.dropna())
    p_ann, b_ann = _cagr(inp, r), _cagr(inp, rb.dropna())
    return _result(
        inp,
        cfg,
        r,
        "excess_return",
        f"Return vs {inp.benchmark}",
        p_total - b_total,
        "%",
        "portfolio total return - benchmark total return",
        details={
            "portfolio_total": p_total,
            "benchmark_total": b_total,
            "portfolio_cagr": p_ann,
            "benchmark_cagr": b_ann,
            "cagr_difference": p_ann - b_ann,
            "benchmark": inp.benchmark,
        },
        assumptions={
            "benchmark": f"{inp.benchmark} price index (no dividends)",
            "caveat": "the stock universe is today's index members (survivorship "
            "bias), which flatters any comparison with the index",
        },
    )


# --- risk -------------------------------------------------------------------------------


def volatility(inp, cfg):
    r = _returns(inp, cfg)
    return _result(
        inp,
        cfg,
        r,
        "volatility",
        "Annualised volatility",
        f.annualized_volatility(r, cfg.trading_days),
        "%",
        "standard deviation of daily returns x sqrt(252)",
    )


def max_drawdown(inp, cfg):
    r = _returns(inp, cfg)
    dd = f.max_drawdown(r)

    def day(x):
        return x.date().isoformat() if x is not None else None

    return _result(
        inp,
        cfg,
        r,
        "max_drawdown",
        "Maximum drawdown",
        dd["max_drawdown"],
        "%",
        "largest fall from a running peak of the time-weighted wealth index",
        details={
            "peak_date": day(dd["peak_date"]) or "start of period",
            "trough_date": day(dd["trough_date"]),
            "recovery_date": day(dd["recovery_date"]) or "not yet recovered",
        },
    )


def downside_deviation(inp, cfg):
    r = _returns(inp, cfg)
    return _result(
        inp,
        cfg,
        r,
        "downside_deviation",
        "Downside deviation",
        f.downside_deviation(r, cfg.daily_risk_free, cfg.trading_days),
        "%",
        "sqrt(mean(min(daily return - daily risk-free, 0)^2)) x sqrt(252)",
        assumptions=_rf(cfg),
    )


def _var_details(inp, loss, confidence):
    nav_end = float(inp.nav["nav"].loc[: inp.end].iloc[-1])
    # "95%" in everyday terms. Computed here so the explainer can say "about 1 trading day in
    # 20" as a checked fact rather than deriving it (the output guard rejects derived numbers).
    one_day_in = round(1 / (1 - confidence))
    return {
        "loss_inr_on_current_nav": loss * nav_end,
        "nav_inr": nav_end,
        "exceeded_about_one_trading_day_in": one_day_in,
    }


def var_historical(inp, cfg):
    r = _returns(inp, cfg)
    v = f.var_historical(r, cfg.var_confidence)
    return _result(
        inp,
        cfg,
        r,
        "var_historical",
        f"1-day VaR {cfg.var_confidence:.0%} (historical)",
        v,
        "%",
        "5th percentile of daily returns, as a positive loss",
        details=_var_details(inp, v, cfg.var_confidence),
        assumptions={"confidence": cfg.var_confidence},
    )


def var_parametric(inp, cfg):
    r = _returns(inp, cfg)
    v = f.var_parametric(r, cfg.var_confidence)
    return _result(
        inp,
        cfg,
        r,
        "var_parametric",
        f"1-day VaR {cfg.var_confidence:.0%} (normal)",
        v,
        "%",
        "-(mean + z x std) of daily returns, z = -1.645",
        details=_var_details(inp, v, cfg.var_confidence),
        assumptions={
            "confidence": cfg.var_confidence,
            "distribution": "normal (understates fat tails)",
        },
    )


def cvar(inp, cfg):
    r = _returns(inp, cfg)
    v = f.cvar(r, cfg.var_confidence)
    return _result(
        inp,
        cfg,
        r,
        "cvar",
        f"1-day CVaR {cfg.var_confidence:.0%} (expected shortfall)",
        v,
        "%",
        "average loss on the worst 5% of days",
        details=_var_details(inp, v, cfg.var_confidence),
        assumptions={"confidence": cfg.var_confidence},
    )


# --- risk-adjusted ------------------------------------------------------------------------


def sharpe_ratio(inp, cfg):
    r = _returns(inp, cfg)
    return _result(
        inp,
        cfg,
        r,
        "sharpe_ratio",
        "Sharpe ratio",
        f.sharpe_ratio(r, cfg.daily_risk_free, cfg.trading_days),
        "ratio",
        "mean(daily return - daily risk-free) / std(same) x sqrt(252)",
        assumptions=_rf(cfg),
    )


def sortino_ratio(inp, cfg):
    r = _returns(inp, cfg)
    return _result(
        inp,
        cfg,
        r,
        "sortino_ratio",
        "Sortino ratio",
        f.sortino_ratio(r, cfg.daily_risk_free, cfg.trading_days),
        "ratio",
        "annualised mean excess return / annualised downside deviation",
        assumptions=_rf(cfg),
    )


def calmar_ratio(inp, cfg):
    r = _returns(inp, cfg)
    if f.max_drawdown(r)["max_drawdown"] == 0:
        raise MetricError("no drawdown in the period, so the Calmar ratio is undefined")
    return _result(
        inp,
        cfg,
        r,
        "calmar_ratio",
        "Calmar ratio",
        f.calmar_ratio(_cagr(inp, r), r),
        "ratio",
        "CAGR / |maximum drawdown|",
    )


# --- benchmark-relative -------------------------------------------------------------------


def _relative(name, label, unit, formula, compute, uses_rf=False):
    def fn(inp, cfg):
        r = _returns(inp, cfg)
        rb = _benchmark_returns(inp, r)
        extra = {"benchmark": f"{inp.benchmark} price index"}
        if uses_rf:
            extra.update(_rf(cfg))
        return _result(
            inp,
            cfg,
            r,
            name,
            f"{label} vs {inp.benchmark}",
            compute(r, rb, cfg),
            unit,
            formula,
            assumptions=extra,
        )

    return fn


beta = _relative(
    "beta",
    "Beta",
    "ratio",
    "cov(portfolio, benchmark) / var(benchmark)",
    lambda r, rb, cfg: f.beta(r, rb),
)
alpha = _relative(
    "alpha",
    "Jensen's alpha",
    "%",
    "(1 + mean(excess return - beta x benchmark excess return)) ^ 252 - 1",
    lambda r, rb, cfg: f.alpha(r, rb, cfg.daily_risk_free, cfg.trading_days),
    uses_rf=True,
)
tracking_error = _relative(
    "tracking_error",
    "Tracking error",
    "%",
    "std(portfolio - benchmark daily returns) x sqrt(252)",
    lambda r, rb, cfg: f.tracking_error(r, rb, cfg.trading_days),
)
information_ratio = _relative(
    "information_ratio",
    "Information ratio",
    "ratio",
    "mean(active return) / std(active return) x sqrt(252)",
    lambda r, rb, cfg: f.information_ratio(r, rb, cfg.trading_days),
)
correlation = _relative(
    "correlation",
    "Correlation",
    "ratio",
    "Pearson correlation of daily returns",
    lambda r, rb, cfg: f.correlation(r, rb),
)


# --- holdings -----------------------------------------------------------------------------


def _nav_at_end(inp) -> pd.Series:
    return inp.nav.loc[: inp.end].iloc[-1]


def _holdings_result(inp, cfg, name, label, value, unit, formula, **extra):
    extra.setdefault("assumptions", {})["as_of"] = "closing prices on the period end date"
    res = _result(inp, cfg, None, name, label, value, unit, formula, **extra)
    res.period_start = None  # a snapshot, not a period
    return res


def _holdings_table(h: pd.DataFrame) -> list[dict]:
    cols = [
        "symbol",
        "name",
        "sector",
        "quantity",
        "avg_cost",
        "last_price",
        "market_value",
        "weight",
        "unrealized_pnl",
    ]
    return h.sort_values("weight", ascending=False)[cols].to_dict("records")


def holdings(inp, cfg):
    return _holdings_result(
        inp,
        cfg,
        "holdings",
        "Holdings",
        float(len(inp.holdings)),
        "count",
        "open positions at the period end, by weight",
        table=_holdings_table(inp.holdings),
    )


def market_value(inp, cfg):
    row = _nav_at_end(inp)
    return _holdings_result(
        inp,
        cfg,
        "market_value",
        "Portfolio value (NAV)",
        float(row["nav"]),
        "INR",
        "market value of holdings + cash",
        details={
            "holdings_value": float(row["holdings_value"]),
            "cash": float(row["cash"]),
            "cash_weight": float(row["cash"] / row["nav"]),
        },
    )


def sector_allocation_metric(inp, cfg):
    table = sector_allocation(inp.holdings, float(_nav_at_end(inp)["nav"]))
    top = table.iloc[0]
    return _holdings_result(
        inp,
        cfg,
        "sector_allocation",
        "Sector allocation",
        float(top["weight"]),
        "%",
        "market value per sector / NAV; value = the largest sector's weight",
        table=table.to_dict("records"),
        details={"largest_sector": top["sector"]},
    )


def concentration_metric(inp, cfg):
    c = concentration(inp.holdings, cfg.top_n)
    return _holdings_result(
        inp,
        cfg,
        "concentration",
        f"Top-{cfg.top_n} concentration",
        c["top_n_weight"],
        "%",
        f"weight of the {cfg.top_n} largest holdings; HHI = sum of squared stock weights",
        details={k: v for k, v in c.items() if k != "top_n_weight"},
    )


def unrealized_pnl(inp, cfg):
    h = inp.holdings
    total = float(h["unrealized_pnl"].sum())
    cost = float((h["quantity"] * h["avg_cost"]).sum())
    table = h.sort_values("unrealized_pnl", ascending=False)[
        ["symbol", "unrealized_pnl", "market_value"]
    ].to_dict("records")
    return _holdings_result(
        inp,
        cfg,
        "unrealized_pnl",
        "Unrealised P&L",
        total,
        "INR",
        "sum of quantity x (last price - average cost) over open holdings",
        details={"cost_basis": cost, "return_on_cost": total / cost},
        table=table,
        assumptions={"cost_method": "average cost incl. buy fees"},
    )


def realized_pnl(inp, cfg):
    rz = inp.realized[inp.realized["realized_pnl"] != 0]
    return _holdings_result(
        inp,
        cfg,
        "realized_pnl",
        "Realised P&L since inception",
        float(rz["realized_pnl"].sum()),
        "INR",
        "sum over sales of (sale value - fees - quantity x average cost)",
        table=rz.sort_values("realized_pnl", ascending=False).to_dict("records"),
        assumptions={"cost_method": "average cost incl. buy fees"},
    )


def pnl_contribution(inp, cfg):
    table = pnl_by_stock(inp.transactions, inp.closes, inp.start, inp.end)
    r = f.in_period(f.twr_returns(inp.nav), inp.start, inp.end)
    return _result(
        inp,
        cfg,
        r,
        "pnl_contribution",
        "P&L by stock",
        float(table["pnl"].sum()),
        "INR",
        "per stock: end value - start value - net purchases (buys incl. fees - sales net of fees)",
        table=table.to_dict("records"),
        details={
            "top_contributor": table.iloc[0]["symbol"] if len(table) else None,
            "worst_contributor": table.iloc[-1]["symbol"] if len(table) else None,
        },
    )


def _holding_returns(inp, cfg) -> pd.DataFrame:
    symbols = inp.holdings["symbol"].tolist()
    px = inp.closes[symbols].loc[: inp.end]
    rets = px.pct_change(fill_method=None)
    rets = rets[(rets.index >= inp.start)].dropna(how="all")
    if len(rets) < cfg.min_observations:
        raise MetricError(f"only {len(rets)} trading days in the period")
    return rets


def correlation_matrix(inp, cfg):
    rets = _holding_returns(inp, cfg)
    corr = rets.corr()
    pairs = corr.where(
        ~pd.DataFrame(
            [[i >= j for j in range(len(corr))] for i in range(len(corr))],
            index=corr.index,
            columns=corr.columns,
        )
    ).stack()
    return _result(
        inp,
        cfg,
        f.in_period(f.twr_returns(inp.nav), inp.start, inp.end),
        "correlation_matrix",
        "Correlation between holdings",
        float(pairs.mean()),
        "ratio",
        "Pearson correlation of daily returns for each pair of current holdings; "
        "value = the average pair",
        table=[
            {"a": a, "b": b, "correlation": float(c)}
            for (a, b), c in pairs.sort_values(ascending=False).items()
        ],
        details={"most_correlated": list(pairs.idxmax()), "least_correlated": list(pairs.idxmin())},
    )


def risk_contribution(inp, cfg):
    rets = _holding_returns(inp, cfg).dropna(axis=1, how="any")
    h = inp.holdings.set_index("symbol")
    w = h.loc[rets.columns, "market_value"]
    w = w / w.sum()
    rc = f.risk_contributions(w, rets, cfg.trading_days).sort_values(ascending=False)
    table = [{"symbol": s, "weight": float(w[s]), "risk_share": float(v)} for s, v in rc.items()]
    return _result(
        inp,
        cfg,
        f.in_period(f.twr_returns(inp.nav), inp.start, inp.end),
        "risk_contribution",
        "Risk contribution by holding",
        float(rc.iloc[0]),
        "%",
        "w_i x (covariance x w)_i / portfolio variance, current weights; value = the largest share",
        table=table,
        details={"largest_risk_contributor": rc.index[0]},
        assumptions={"weights": "current stock weights (cash excluded)"},
    )


NAV, BENCH, HOLD = (
    frozenset({"nav"}),
    frozenset({"nav", "benchmark"}),
    frozenset({"nav", "holdings"}),
)

CATALOG: dict[str, Spec] = {
    s.name: s
    for s in [
        Spec(
            "total_return",
            "Total return",
            "returns",
            NAV,
            total_return,
            "growth over the period, deposits and withdrawals removed",
        ),
        Spec("cagr", "Annualised return", "returns", NAV, cagr, "compound annual growth rate"),
        Spec(
            "monthly_returns",
            "Monthly returns",
            "returns",
            NAV,
            monthly_returns,
            "return for each calendar month",
        ),
        Spec(
            "excess_return",
            "Return vs benchmark",
            "relative",
            BENCH,
            excess_return,
            "portfolio return minus benchmark return",
        ),
        Spec(
            "volatility",
            "Volatility",
            "risk",
            NAV,
            volatility,
            "annualised standard deviation of returns",
        ),
        Spec(
            "max_drawdown",
            "Maximum drawdown",
            "risk",
            NAV,
            max_drawdown,
            "worst peak-to-trough fall, with dates",
        ),
        Spec(
            "downside_deviation",
            "Downside deviation",
            "risk",
            NAV,
            downside_deviation,
            "volatility of returns below the risk-free rate",
        ),
        Spec(
            "var_historical",
            "Value at Risk (historical)",
            "risk",
            NAV,
            var_historical,
            "1-day loss exceeded on only 5% of days",
        ),
        Spec(
            "var_parametric",
            "Value at Risk (normal)",
            "risk",
            NAV,
            var_parametric,
            "1-day 95% VaR assuming normal returns",
        ),
        Spec(
            "cvar",
            "Expected shortfall (CVaR)",
            "risk",
            NAV,
            cvar,
            "average loss on the worst 5% of days",
        ),
        Spec(
            "sharpe_ratio",
            "Sharpe ratio",
            "risk_adjusted",
            NAV,
            sharpe_ratio,
            "excess return per unit of volatility",
        ),
        Spec(
            "sortino_ratio",
            "Sortino ratio",
            "risk_adjusted",
            NAV,
            sortino_ratio,
            "excess return per unit of downside volatility",
        ),
        Spec(
            "calmar_ratio",
            "Calmar ratio",
            "risk_adjusted",
            NAV,
            calmar_ratio,
            "annual return per unit of maximum drawdown",
        ),
        Spec("beta", "Beta", "relative", BENCH, beta, "sensitivity to benchmark moves"),
        Spec(
            "alpha",
            "Jensen's alpha",
            "relative",
            BENCH,
            alpha,
            "return beyond what beta to the benchmark explains",
        ),
        Spec(
            "tracking_error",
            "Tracking error",
            "relative",
            BENCH,
            tracking_error,
            "volatility of the difference from the benchmark",
        ),
        Spec(
            "information_ratio",
            "Information ratio",
            "relative",
            BENCH,
            information_ratio,
            "excess return per unit of tracking error",
        ),
        Spec(
            "correlation",
            "Correlation with benchmark",
            "relative",
            BENCH,
            correlation,
            "how closely daily returns move with the benchmark",
        ),
        Spec(
            "holdings",
            "Holdings",
            "holdings",
            HOLD,
            holdings,
            "open positions with weights and P&L",
        ),
        Spec(
            "market_value",
            "Portfolio value",
            "holdings",
            HOLD,
            market_value,
            "NAV split into holdings and cash",
        ),
        Spec(
            "sector_allocation",
            "Sector allocation",
            "holdings",
            HOLD,
            sector_allocation_metric,
            "weight in each sector, including cash",
        ),
        Spec(
            "concentration",
            "Concentration",
            "holdings",
            HOLD,
            concentration_metric,
            "top-5 weight, HHI and effective number of holdings",
        ),
        Spec(
            "unrealized_pnl",
            "Unrealised P&L",
            "holdings",
            HOLD,
            unrealized_pnl,
            "paper gains and losses on open holdings",
        ),
        Spec(
            "realized_pnl",
            "Realised P&L",
            "holdings",
            HOLD,
            realized_pnl,
            "gains and losses locked in by sales",
        ),
        Spec(
            "pnl_contribution",
            "P&L by stock",
            "holdings",
            HOLD,
            pnl_contribution,
            "how much each stock made or lost over the period",
        ),
        Spec(
            "correlation_matrix",
            "Correlation between holdings",
            "holdings",
            HOLD,
            correlation_matrix,
            "how the current holdings move together",
        ),
        Spec(
            "risk_contribution",
            "Risk contribution",
            "holdings",
            HOLD,
            risk_contribution,
            "each holding's share of portfolio volatility",
        ),
    ]
}
