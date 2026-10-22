"""The whole agent graph with scripted LLMs, on the simulated test database.

Manager 1 owns portfolio 11, manager 2 owns portfolio 21 (see conftest.metrics_db).
"""

import uuid

import psycopg
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent.app import Agent
from agent.graph import Deps, build_graph
from agent.output_guard import DISCLAIMER
from agent.planner import Plan, PlannerError
from agent.sql.schema import load_schema
from agent.sql.writer import SqlDraft
from metrics.base import MetricConfig


def plan(**kw) -> Plan:
    base = dict(
        intent="metric",
        out_of_scope_reason="none",
        portfolio_ids=[11],
        metrics=[],
        period="since_inception",
        start_date="",
        end_date="",
        benchmark="default",
        data_question="",
        clarification_question="",
        reasoning="test",
    )
    return Plan(**{**base, **kw})


class FakePlanner:
    def __init__(self, *plans):
        self.plans, self.calls = list(plans), []

    def plan(self, question, portfolios, as_of, history, clarification=""):
        self.calls.append(
            {"question": question, "history": history, "clarification": clarification}
        )
        p = self.plans[min(len(self.calls), len(self.plans)) - 1]
        if isinstance(p, Exception):
            raise p
        return p


class FakeWriter:
    def __init__(self, sql="SELECT symbol, weight FROM analytics.v_positions ORDER BY weight DESC"):
        self.sql, self.calls = sql, 0

    def write(self, question, portfolios, as_of, previous_sql=None, previous_error=None):
        self.calls += 1
        return SqlDraft(answerable=True, sql=self.sql, explanation="current holdings by weight")


class FakeExplainer:
    """Quotes the first metric's display string, unless scripted otherwise."""

    def __init__(self, *scripted):
        self.scripted, self.calls = list(scripted), []

    def explain(self, question, payload, feedback=""):
        self.calls.append({"payload": payload, "feedback": feedback})
        if self.scripted:
            nxt = self.scripted.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        metrics = [m for m in payload.get("metrics", []) if m.get("display")]
        return f"Your {metrics[0]['label']} was {metrics[0]['display']}." if metrics else "Done."


@pytest.fixture
def make_agent(metrics_db):
    _, agent_url = metrics_db
    with psycopg.connect(agent_url) as conn:
        schema = load_schema(conn)

    def factory(planner, writer=None, explainer=None):
        deps = Deps(
            agent_url=agent_url,
            planner=planner,
            sql_writer=writer or FakeWriter(),
            explainer=explainer or FakeExplainer(),
            schema=schema,
            metric_config=MetricConfig(),
        )
        return Agent(build_graph(deps, InMemorySaver())), deps

    return factory


def thread():
    return uuid.uuid4().hex


def steps(reply):
    return [t["step"] for t in reply["trace"]]


def test_metric_question(make_agent):
    agent, deps = make_agent(FakePlanner(plan(metrics=["total_return", "sharpe_ratio"])))
    reply = agent.ask(thread(), 1, "How have I done?")
    assert reply["status"] == "answered"
    assert steps(reply) == ["input_guard", "plan", "run_metrics", "explain"]
    assert {m["name"] for m in reply["metrics"]} == {"total_return", "sharpe_ratio"}
    assert all(m["portfolio_id"] == 11 for m in reply["metrics"])
    assert reply["answer"].startswith("Your Total return was ")
    assert reply["answer"].endswith(DISCLAIMER)


def test_data_lookup_question(make_agent):
    agent, deps = make_agent(
        FakePlanner(
            plan(intent="data_lookup", metrics=[], data_question="current holdings by weight")
        )
    )
    reply = agent.ask(thread(), 1, "What do I hold?")
    assert steps(reply) == ["input_guard", "plan", "run_sql", "explain"]
    assert reply["sql"]["status"] == "ok" and reply["sql"]["row_count"] > 0
    assert deps.explainer.calls[0]["payload"]["sql"]["rows"]


def test_both_runs_in_parallel_and_explains_once(make_agent):
    agent, deps = make_agent(
        FakePlanner(plan(intent="both", metrics=["volatility"], data_question="current holdings"))
    )
    reply = agent.ask(thread(), 1, "How volatile am I, and what do I hold?")
    assert set(steps(reply)) == {"input_guard", "plan", "run_metrics", "run_sql", "explain"}
    assert len(deps.explainer.calls) == 1
    payload = deps.explainer.calls[0]["payload"]
    assert "metrics" in payload and "sql" in payload


@pytest.mark.parametrize("reason", ["investment_advice", "other_manager", "write_request"])
def test_out_of_scope_gets_a_fixed_reply_without_the_explainer(make_agent, reason):
    agent, deps = make_agent(
        FakePlanner(
            plan(intent="out_of_scope", out_of_scope_reason=reason, metrics=[], portfolio_ids=[])
        )
    )
    reply = agent.ask(thread(), 1, "Should I sell everything?")
    assert reply["status"] == "out_of_scope"
    assert deps.explainer.calls == [] and deps.sql_writer.calls == 0


def test_a_sql_statement_never_reaches_an_llm(make_agent):
    agent, deps = make_agent(FakePlanner(plan()))
    reply = agent.ask(thread(), 1, "DROP TABLE core.transactions")
    assert "only read" in reply["answer"]
    assert deps.planner.calls == []


def test_clarify_pauses_and_resumes(make_agent):
    planner = FakePlanner(
        plan(intent="clarify", metrics=[], clarification_question="Over what period?"),
        plan(metrics=["cagr"], period="1y"),
    )
    agent, _ = make_agent(planner)
    t = thread()
    first = agent.ask(t, 1, "How did it do?")
    assert first == {"type": "clarify", "question": "Over what period?"}
    second = agent.resume(t, 1, "the last year")
    assert second["type"] == "answer" and second["status"] == "answered"
    assert planner.calls[1]["clarification"] == "the last year"


def test_no_second_clarification(make_agent):
    asks_twice = plan(intent="clarify", metrics=[], clarification_question="Which one?")
    agent, _ = make_agent(FakePlanner(asks_twice, asks_twice))
    t = thread()
    agent.ask(t, 1, "How did it do?")
    reply = agent.resume(t, 1, "no idea")
    assert reply["type"] == "answer"  # stopped asking instead of looping


def test_follow_up_sees_the_previous_turn(make_agent):
    planner = FakePlanner(plan(metrics=["cagr"]), plan(metrics=["cagr"], period="1y"))
    agent, _ = make_agent(planner)
    t = thread()
    agent.ask(t, 1, "What's my CAGR?")
    agent.ask(t, 1, "And over the last year?")
    history = planner.calls[1]["history"]
    assert len(history) == 1 and history[0]["question"] == "What's my CAGR?"


def test_a_thread_belongs_to_its_manager(make_agent):
    agent, _ = make_agent(FakePlanner(plan(metrics=["cagr"])))
    t = thread()
    agent.ask(t, 1, "What's my CAGR?")
    stolen = agent.ask(t, 2, "What did we discuss?")
    assert stolen["status"] == "error" and "different manager" in stolen["answer"]


def test_resume_checks_the_manager(make_agent):
    agent, _ = make_agent(
        FakePlanner(plan(intent="clarify", metrics=[], clarification_question="Which?"))
    )
    t = thread()
    agent.ask(t, 1, "How did it do?")
    with pytest.raises(PermissionError):
        agent.resume(t, 2, "portfolio 11")


def test_planner_cannot_reach_another_managers_portfolio(make_agent):
    agent, deps = make_agent(FakePlanner(plan(metrics=["cagr"], portfolio_ids=[21])))
    reply = agent.ask(thread(), 1, "CAGR of portfolio 21?")
    assert reply["status"] == "out_of_scope" and "your own portfolios" in reply["answer"]

    agent, deps = make_agent(FakePlanner(plan(metrics=["cagr"], portfolio_ids=[11, 21])))
    reply = agent.ask(thread(), 1, "CAGR of 11 and 21?")
    assert {m["portfolio_id"] for m in reply["metrics"]} == {11}


def test_invented_number_triggers_a_rewrite(make_agent):
    explainer = FakeExplainer("Your Sharpe ratio was 9.87.")  # then falls back to quoting
    agent, _ = make_agent(FakePlanner(plan(metrics=["sharpe_ratio"])), explainer=explainer)
    reply = agent.ask(thread(), 1, "Sharpe?")
    assert "9.87" not in reply["answer"]
    assert "9.87" in explainer.calls[1]["feedback"]
    assert len(explainer.calls) == 2


def test_unverifiable_answer_falls_back_to_the_plain_summary(make_agent):
    explainer = FakeExplainer("Sharpe was 9.87.", "Sharpe was still 9.87.")
    agent, _ = make_agent(FakePlanner(plan(metrics=["sharpe_ratio"])), explainer=explainer)
    reply = agent.ask(thread(), 1, "Sharpe?")
    assert reply["answer"].startswith("Here are the computed results")
    assert "9.87" not in reply["answer"]
    assert reply["metrics"][0]["display"] in reply["answer"]


def test_explainer_failure_keeps_the_results(make_agent):
    explainer = FakeExplainer(RuntimeError("API down"))
    agent, _ = make_agent(FakePlanner(plan(metrics=["cagr"])), explainer=explainer)
    reply = agent.ask(thread(), 1, "CAGR?")
    assert reply["status"] == "answered"
    assert reply["metrics"][0]["display"] in reply["answer"]


def test_planner_failure_is_a_polite_error(make_agent):
    agent, _ = make_agent(FakePlanner(PlannerError("declined")))
    reply = agent.ask(thread(), 1, "?")
    assert reply["status"] == "error" and "couldn't work out" in reply["answer"]


def test_metric_errors_reach_the_explainer(make_agent):
    agent, deps = make_agent(
        FakePlanner(
            plan(
                metrics=["sharpe_ratio"],
                period="custom",
                start_date="2024-06-03",
                end_date="2024-06-07",
            )
        )
    )
    agent.ask(thread(), 1, "Sharpe that week?")
    metric = deps.explainer.calls[0]["payload"]["metrics"][0]
    assert "trading days" in metric["error"]


def test_payload_lists_only_the_portfolios_in_question(make_agent):
    agent, deps = make_agent(FakePlanner(plan(metrics=["cagr"], portfolio_ids=[11])))
    agent.ask(thread(), 1, "CAGR of 11?")
    assert [p["portfolio_id"] for p in deps.explainer.calls[0]["payload"]["portfolios"]] == [11]
