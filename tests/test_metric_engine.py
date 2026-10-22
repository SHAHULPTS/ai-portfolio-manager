"""The engine end to end: real database, RLS-protected loaders, every catalogue metric."""

import pandas as pd
import psycopg
import pytest

from metrics import formulas as f
from metrics.base import MetricConfig
from metrics.catalog import CATALOG
from metrics.engine import run
from metrics.loaders import PortfolioNotFound, agent_session, load_nav

CFG = MetricConfig()


def test_every_catalogue_metric_computes(metrics_db):
    _, agent = metrics_db
    results = run(agent, 1, 11, list(CATALOG), period="since_inception", cfg=CFG)
    failed = {
        r.name: r.details.get("error") for r in results if r.value is None and r.unit != "table"
    }
    assert failed == {}
    assert {r.name for r in results} == set(CATALOG)


def test_results_carry_their_assumptions(metrics_db):
    _, agent = metrics_db
    (sharpe,) = run(agent, 1, 11, ["sharpe_ratio"], period="1y", cfg=CFG)
    assert sharpe.assumptions["risk_free_rate_annual"] == 0.065
    assert "dividends not included" in sharpe.assumptions["returns"]
    assert sharpe.formula
    assert sharpe.n_observations > 200


def test_engine_matches_the_formula_on_the_stored_nav(metrics_db):
    _, agent = metrics_db
    (vol,) = run(agent, 1, 11, ["volatility"], period="since_inception", cfg=CFG)
    with agent_session(agent, 1) as conn:
        nav = load_nav(conn, 11, pd.Timestamp("2099-01-01"))
    assert vol.value == pytest.approx(f.annualized_volatility(f.twr_returns(nav), 252))


def test_holdings_match_the_positions_view(metrics_db):
    _, agent = metrics_db
    (h,) = run(agent, 1, 11, ["holdings"], cfg=CFG)
    with agent_session(agent, 1) as conn:
        view = dict(
            conn.execute(
                "SELECT symbol, market_value::float8 FROM analytics.v_positions"
                " WHERE portfolio_id = 11"
            ).fetchall()
        )
    assert {row["symbol"]: row["market_value"] for row in h.table} == pytest.approx(view)


def test_pnl_contribution_adds_up(metrics_db):
    admin, agent = metrics_db
    (c,) = run(agent, 1, 11, ["pnl_contribution"], period="ytd", cfg=CFG)
    with psycopg.connect(admin) as conn:
        base, end = conn.execute(
            "SELECT (SELECT nav FROM core.nav_daily WHERE portfolio_id = 11 AND date < %(s)s"
            "        ORDER BY date DESC LIMIT 1)::float8,"
            "       (SELECT nav FROM core.nav_daily WHERE portfolio_id = 11 AND date <= %(e)s"
            "        ORDER BY date DESC LIMIT 1)::float8",
            {"s": c.period_start, "e": c.period_end},
        ).fetchone()
        flows = conn.execute(
            "SELECT coalesce(sum(amount), 0)::float8 FROM core.cash_flows"
            " WHERE portfolio_id = 11 AND date BETWEEN %s AND %s",
            (c.period_start, c.period_end),
        ).fetchone()[0]
    assert c.value == pytest.approx(end - base - flows, abs=1.0)  # NAV is stored to the paisa


def test_another_managers_portfolio_is_not_found(metrics_db):
    _, agent = metrics_db
    with pytest.raises(PortfolioNotFound):
        run(agent, 2, 11, ["total_return"], cfg=CFG)
    with pytest.raises(PortfolioNotFound):
        run(agent, 1, 999, ["total_return"], cfg=CFG)  # same error as a real stranger's


def test_too_short_a_period_is_reported_not_raised(metrics_db):
    _, agent = metrics_db
    (r,) = run(agent, 1, 11, ["sharpe_ratio"], start="2024-06-03", end="2024-06-10", cfg=CFG)
    assert r.value is None
    assert "trading days" in r.details["error"]


def test_bad_requests_are_rejected(metrics_db):
    _, agent = metrics_db
    with pytest.raises(ValueError, match="unknown metrics"):
        run(agent, 1, 11, ["happiness"], cfg=CFG)
    with pytest.raises(ValueError, match="not an available benchmark"):
        run(agent, 1, 11, ["beta"], benchmark="AAA", cfg=CFG)


def test_var_states_its_frequency_in_plain_terms(metrics_db):
    _, agent = metrics_db
    (var,) = run(agent, 1, 11, ["var_historical"], cfg=CFG)
    assert var.details["exceeded_about_one_trading_day_in"] == 20  # 95% confidence
