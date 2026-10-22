"""The eval graders must fail what's wrong, not just pass what's right."""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import yaml

from evals import scoring
from metrics.catalog import CATALOG


def test_rows_match_ignores_column_names_and_order():
    golden = [(201, Decimal("30482.11")), (202, Decimal("8259.30"))]
    agent = [{"pid": 202, "total_fees": 8259.3}, {"pid": 201, "total_fees": 30482.11}]
    assert scoring.rows_match(golden, agent) == (True, "")


def test_rows_match_allows_extra_columns_and_normalises_dates():
    golden = [(dt.date(2025, 8, 11), Decimal("-4917000.00"))]
    agent = [{"date": "2025-08-11", "amount": -4917000.0, "portfolio_name": "Ravi Consumer"}]
    assert scoring.rows_match(golden, agent)[0]


def test_rows_match_fails_on_wrong_counts_or_values():
    golden = [(201, 10.0), (202, 20.0)]
    assert not scoring.rows_match(golden, [{"a": 201, "b": 10.0}])[0]
    ok, why = scoring.rows_match(golden, [{"a": 201, "b": 10.0}, {"a": 202, "b": 21.0}])
    assert not ok and "202" in why
    # One agent row can't satisfy two golden rows.
    assert not scoring.rows_match([(1,), (1,)], [{"a": 1}, {"a": 2}])[0]


def test_mentions_allows_formatting_but_not_other_numbers():
    assert scoring.mentions("CAGR was 11.4% a year.", 0.11372)
    assert scoring.mentions("NAV is ₹8.16 Cr.", 81_610_000)
    assert scoring.mentions("You withdrew ₹49.17 lakh.", -4_917_000)
    assert not scoring.mentions("CAGR was 12.4% a year.", 0.11372)


def test_route_and_args():
    case = {
        "intents": ["metric"],
        "metrics": ["sharpe_ratio"],
        "portfolios": [202],
        "period": "ytd",
    }
    good = {
        "intent": "metric",
        "metrics": ["sharpe_ratio", "volatility"],
        "portfolio_ids": [202],
        "period": "ytd",
    }
    assert scoring.check_route(case, good, "answer")[0]
    assert scoring.check_args(case, good, [201, 202, 203])[0]
    ok, why = scoring.check_args(
        case, {**good, "period": "1y", "portfolio_ids": [201]}, [201, 202, 203]
    )
    assert not ok and "period" in why and "portfolios" in why
    assert not scoring.check_route(case, {"intent": "data_lookup"}, "answer")[0]
    assert scoring.check_route({"intents": ["clarify"]}, None, "clarify")[0]


def test_empty_portfolio_list_means_all():
    case = {"intents": ["metric"], "portfolios": []}
    assert scoring.check_args(case, {"portfolio_ids": [1, 2]}, [1, 2])[0]
    assert scoring.check_args(
        {"intents": ["metric"], "portfolios": [1, 2]}, {"portfolio_ids": []}, [1, 2]
    )[0]


def test_values_must_match_and_be_stated():
    results = [{"portfolio_id": 1, "name": "cagr", "value": 0.1137, "display": "11.37%"}]
    assert scoring.check_values({(1, "cagr"): 0.1137}, results, "CAGR 11.37%.")[0]
    assert not scoring.check_values({(1, "cagr"): 0.1137}, results, "It did well.")[0]
    assert not scoring.check_values({(1, "cagr"): 0.2}, results, "CAGR 11.37%.")[0]
    assert not scoring.check_values({(2, "cagr"): 0.1}, results, "")[0]


def test_leak_detection():
    reply = {
        "metrics": [{"portfolio_id": 201}],
        "sql": {"rows": [{"portfolio_id": 301}]},
        "answer": "Ravi Tech Opportunities did well.",
    }
    found = scoring.leaks(reply, {201, 301}, ["Ravi Tech Opportunities"])
    assert len(found) == 3
    assert scoring.leaks({"answer": "Only your portfolios."}, {201}, ["Ravi Tech"]) == []


def test_case_files_are_well_formed():
    here = Path(__file__).parents[1] / "evals"
    golden = yaml.safe_load((here / "golden.yaml").read_text())
    red = yaml.safe_load((here / "redteam.yaml").read_text())
    ids = [c["id"] for c in golden + red]
    assert len(ids) == len(set(ids))
    for c in golden:
        assert c["manager"] in (1, 2, 3) and c["intents"], c["id"]
        for name in c.get("metrics", []) + c.get("any_metric", []):
            assert name in CATALOG, (c["id"], name)
