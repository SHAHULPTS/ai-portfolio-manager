"""The Streamlit page: view helpers, and the real page script run headless with a fake agent."""

from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from agent.output_guard import DISCLAIMER
from app.views import chart_for, metrics_table, receipt

PAGE = str(Path(__file__).parents[1] / "app" / "streamlit_app.py")


def metric(name="cagr", pid=201, value=0.12, unit="%", display="12.00%", table=None, **kw):
    return {
        "name": name,
        "label": name.upper(),
        "portfolio_id": pid,
        "portfolio_name": f"P{pid}",
        "value": value,
        "unit": unit,
        "display": display,
        "period_start": "2020-01-01",
        "period_end": "2026-04-08",
        "formula": "f",
        "table": table,
        **kw,
    }


def answered(metrics=None, sql=None, answer="CAGR was 12.00%.", trace=None):
    return {
        "type": "answer",
        "status": "answered",
        "answer": answer + DISCLAIMER,
        "plan": {"intent": "metric"},
        "metrics": metrics,
        "sql": sql,
        "trace": trace or [],
    }


# --- view helpers -------------------------------------------------------------------------


def test_receipt_describes_metrics_and_checked_figures():
    text = receipt(answered(metrics=[metric()]))
    assert text == (
        "1 metric computed for portfolio 201. All 1 figures checked against the computed results."
    )


def test_receipt_mentions_sql_retries_and_fallback():
    sql = {"status": "ok", "row_count": 4, "attempts": [{}, {}]}
    trace = [{"step": "explain", "notes": ["still unsupported; used the plain summary"]}]
    text = receipt(answered(sql=sql, trace=trace))
    assert "4 rows read with a checked SQL query (after 2 attempts)" in text
    assert "plain summary" in text


def test_receipt_for_refusals():
    assert "no data was read" in receipt({"status": "out_of_scope", "answer": "x"})


def test_metrics_table_shows_errors_and_skips_pure_tables():
    rows = metrics_table(
        [
            metric(),
            metric("sharpe_ratio", value=None, display=None, error="only 5 trading days"),
            metric("holdings", unit="table", value=None, display=None, table=[{"a": 1}]),
        ]
    )
    assert list(rows["Metric"]) == ["CAGR", "SHARPE_RATIO"]
    assert rows.iloc[1]["Value"] == "not computed: only 5 trading days"


def chart_title(chart):
    return chart.to_dict()["title"]


def test_chart_choice():
    pnl = [{"symbol": "A", "pnl": 10.0}, {"symbol": "B", "pnl": -5.0}]
    assert (
        chart_title(chart_for(answered([metric("pnl_contribution", unit="INR", table=pnl)])))
        == "P&L by stock (INR)"
    )
    across = [
        metric("pnl_contribution", pid=1, unit="INR", value=5.0),
        metric("pnl_contribution", pid=2, unit="INR", value=-3.0),
    ]
    assert chart_title(chart_for(answered(across))) == "Total P&L by portfolio (INR)"
    assert chart_for(answered([metric()])) is None  # a single number needs no chart


def test_sql_chart_needs_a_date_series():
    rows = [{"date": f"2025-0{i}-01", "nav": 100.0 + i} for i in range(1, 6)]
    assert chart_for(answered(sql={"status": "ok", "rows": rows})) is not None
    assert chart_for(answered(sql={"status": "ok", "rows": rows[:2]})) is None


# --- the page itself ----------------------------------------------------------------------


class FakeAgent:
    def __init__(self):
        self.calls = []
        self.next = answered(metrics=[metric()])

    def ask(self, thread, manager_id, question):
        self.calls.append(("ask", manager_id, question))
        return self.next

    def resume(self, thread, manager_id, answer):
        self.calls.append(("resume", manager_id, answer))
        return answered(metrics=[metric()])


@pytest.fixture
def page(monkeypatch):
    fake = FakeAgent()

    @contextmanager
    def fake_open_agent(*a, **k):
        yield fake

    @contextmanager
    def fake_session(url, manager_id):
        yield None

    portfolios = pd.DataFrame(
        [
            {
                "portfolio_id": 201,
                "name": "Ravi Tech",
                "mandate": "IT",
                "benchmark": "NIFTY50",
                "inception": date(2016, 2, 26),
            }
        ]
    )
    # cache_resource outlives a single AppTest run: without this, the agent cached by an
    # earlier test would answer instead of this test's fake.
    st.cache_resource.clear()
    st.cache_data.clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("agent.app.open_agent", fake_open_agent)
    monkeypatch.setattr("db.session.agent_session", fake_session)
    monkeypatch.setattr("metrics.loaders.list_portfolios", lambda conn: portfolios)
    at = AppTest.from_file(PAGE, default_timeout=30)
    at.run()
    return at, fake


def test_empty_state_and_sidebar(page):
    at, _ = page
    assert not at.exception
    assert at.title[0].value == "Ask about your portfolios"
    assert at.sidebar.selectbox[0].value == 2  # Ravi Iyer
    assert any("Ravi Tech" in m.value for m in at.sidebar.markdown)


def test_asking_renders_answer_and_receipt(page):
    at, fake = page
    at.chat_input[0].set_value("What's my CAGR?").run()
    assert not at.exception
    assert fake.calls == [("ask", 2, "What's my CAGR?")]
    text = " ".join(m.value for m in at.markdown)
    assert "CAGR was 12.00%." in text
    assert "checked against the computed results" in text
    assert not at.title  # the empty-state heading is gone once there's a conversation


def test_clarify_then_resume(page):
    at, fake = page
    fake.next = {"type": "clarify", "question": "Which period?"}
    at.chat_input[0].set_value("How did it do?").run()
    assert at.chat_input[0].placeholder == "Reply to the question above"
    at.chat_input[0].set_value("last year").run()
    assert fake.calls[-1] == ("resume", 2, "last year")


def test_agent_errors_are_shown_not_raised(page):
    at, fake = page
    fake.ask = lambda *a: (_ for _ in ()).throw(RuntimeError("database is down"))
    at.chat_input[0].set_value("Anything?").run()
    assert not at.exception
    assert "database is down" in at.error[0].value


def test_missing_api_key_is_explained(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr("config.load_dotenv", lambda: None)
    at = AppTest.from_file(PAGE, default_timeout=30)
    at.run()
    assert "ANTHROPIC_API_KEY" in at.error[0].value
