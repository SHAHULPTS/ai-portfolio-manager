"""The one entry point the agent calls: compute named metrics for one portfolio and period.

    run(agent_url, manager_id=1, portfolio_id=101, metrics=["sharpe_ratio", "max_drawdown"],
        period="3y")

Loads only what the requested metrics need, through the RLS-protected agent role, then runs
each formula. A metric that can't be computed (too little data, say) comes back with
value=None and the reason in details["error"]; it never sinks the other metrics.
"""

import pandas as pd

from config import load_settings
from metrics import loaders
from metrics.base import MetricConfig, MetricError, MetricInputs, MetricResult
from metrics.catalog import CATALOG
from metrics.periods import resolve
from portfolio.accounting import positions


def config_from_settings() -> MetricConfig:
    s = load_settings()
    return MetricConfig(risk_free_rate=s.risk_free_rate, trading_days=s.trading_days)


def run(
    agent_url: str,
    manager_id: int,
    portfolio_id: int,
    metrics: list[str],
    period: str | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    benchmark: str | None = None,
    cfg: MetricConfig | None = None,
) -> list[MetricResult]:
    unknown = [m for m in metrics if m not in CATALOG]
    if unknown:
        raise ValueError(f"unknown metrics {unknown}; choose from {sorted(CATALOG)}")
    cfg = cfg or config_from_settings()
    needs = set().union(*(CATALOG[m].needs for m in metrics))

    with loaders.agent_session(agent_url, manager_id) as conn:
        meta = loaders.portfolio_meta(conn, portfolio_id)
        if benchmark and not loaders.benchmark_exists(conn, benchmark):
            raise ValueError(f"{benchmark} is not an available benchmark index")
        bench = benchmark or meta["benchmark"]
        p_start, p_end = resolve(period, meta["as_of"], meta["inception"], start, end)
        nav = loaders.load_nav(conn, portfolio_id, p_end)
        inp = MetricInputs(
            portfolio_id=portfolio_id,
            portfolio_name=meta["name"],
            benchmark=bench,
            inception=meta["inception"],
            start=p_start,
            end=p_end,
            nav=nav,
        )
        if "benchmark" in needs:
            inp.benchmark_close = loaders.load_closes(conn, [bench], nav.index)[bench]
        if "holdings" in needs:
            txns = loaders.load_transactions(conn, portfolio_id, p_end)
            inp.transactions = txns
            inp.closes = loaders.load_closes(conn, sorted(txns["symbol"].unique()), nav.index)
            inp.holdings, inp.realized = build_holdings(txns, inp.closes, nav, p_end)

    results = []
    for name in metrics:
        spec = CATALOG[name]
        try:
            results.append(spec.fn(inp, cfg))
        except MetricError as e:
            results.append(
                MetricResult(
                    name=name,
                    label=spec.label,
                    value=None,
                    unit="",
                    formula="",
                    period_start=p_start.date(),
                    period_end=p_end.date(),
                    details={"error": str(e)},
                )
            )
    return results


def build_holdings(
    txns: pd.DataFrame, closes: pd.DataFrame, nav: pd.DataFrame, end: pd.Timestamp
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Open positions and realised P&L as of `end`, rebuilt from the trades.

    Uses the same accounting code that filled the database, so for the latest date this
    matches analytics.v_positions exactly (a test checks that).
    """
    pos = positions(txns)
    info = txns.drop_duplicates("symbol").set_index("symbol")[["name", "sector"]]
    pos = pos.join(info, on="symbol")
    marks = closes.ffill().loc[end]
    nav_end = float(nav["nav"].loc[:end].iloc[-1])

    open_ = pos[pos["quantity"] > 0].copy()
    open_["last_price"] = open_["symbol"].map(marks)
    open_["market_value"] = open_["quantity"] * open_["last_price"]
    open_["unrealized_pnl"] = open_["quantity"] * (open_["last_price"] - open_["avg_cost"])
    open_["weight"] = open_["market_value"] / nav_end
    return open_.reset_index(drop=True), pos[["symbol", "name", "sector", "realized_pnl"]]
