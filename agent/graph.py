"""The agent as a LangGraph state machine.

    start_turn -> input_guard -> plan --+--> run_metrics --+--> explain -> END
                       |                 +--> run_sql -----+
                       |                 +--> clarify (interrupt) -> plan
                       +-----------------+--> refuse -> END

- `plan` routes to run_metrics, run_sql, or both. "Both" runs the two nodes in the same
  step (in parallel), and `explain` runs once after both have finished.
- `clarify` pauses the graph with interrupt() and asks the manager a question. Their reply
  resumes it (Command(resume=...)), and the planner tries again with that context. Only one
  clarification per turn, so the agent can't loop asking questions.
- With a checkpointer, each thread_id is one conversation: `history` carries earlier turns,
  so follow-ups like "and since 2020?" work. A thread belongs to the manager who started it.
  A different manager_id on the same thread is refused.

Every node gets its LLMs and settings through `Deps`, so the tests can swap in scripted fakes
and run the whole graph without an API key.
"""

import json
import operator
from dataclasses import asdict, dataclass
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent.formatting import display
from agent.input_guard import check_input
from agent.output_guard import DISCLAIMER, unsupported_numbers
from agent.planner import Plan, PlannerError, sanitize
from agent.sql.pipeline import answer_with_sql
from agent.sql.schema import SchemaInfo
from agent.sql.writer import WriterError, WriterRefused
from db.session import agent_session
from metrics import engine
from metrics.base import MetricConfig
from metrics.loaders import PortfolioNotFound, list_portfolios

PAYLOAD_TABLE_ROWS = 20
PAYLOAD_SQL_ROWS = 50


def add_or_reset(current: list | None, new: list | None) -> list:
    """List reducer that a node can clear by returning None (used for the per-turn trace)."""
    if new is None:
        return []
    return (current or []) + new


class AgentState(TypedDict, total=False):
    manager_id: int
    owner_id: int  # the manager who started this thread
    question: str
    portfolios: list[dict]
    as_of: str
    flags: list[str]
    clarification: str
    plan: dict | None
    sql: dict | None
    metrics: list[dict] | None
    answer: str
    status: str  # answered | out_of_scope | error
    history: Annotated[list[dict], operator.add]
    trace: Annotated[list[dict], add_or_reset]


@dataclass
class Deps:
    agent_url: str
    planner: Any  # .plan(question, portfolios, as_of, history, clarification) -> Plan
    sql_writer: Any  # .write(...) -> SqlDraft
    explainer: Any  # .explain(question, payload, feedback="") -> str
    schema: SchemaInfo
    metric_config: MetricConfig
    row_limit: int = 500
    timeout_ms: int = 3000
    max_cost: float = 2_000_000


REFUSALS = {
    "investment_advice": (
        "I can't recommend buying, selling or holding anything. I can show you the facts behind "
        "a decision, though: for example a stock's weight and P&L in your portfolio, its risk "
        "contribution, or how a portfolio has performed against its benchmark."
    ),
    "other_manager": "I can only see and analyse your own portfolios.",
    "write_request": (
        "I can only read your portfolio data; I can't change, add or delete anything."
    ),
    "off_topic": (
        "I can answer questions about your portfolios: holdings, trades, cash flows, returns, "
        "risk and comparisons with the benchmark."
    ),
    "harmful": "I can't help with that. I can answer questions about your portfolios.",
}


def build_graph(deps: Deps, checkpointer=None):
    def start_turn(state: AgentState) -> dict:
        owner = state.get("owner_id")
        reset = {
            "plan": None,
            "sql": None,
            "metrics": None,
            "answer": "",
            "flags": [],
            "clarification": "",
            "status": "",
            "trace": None,
        }
        if owner is not None and owner != state["manager_id"]:
            return {
                **reset,
                "status": "error",
                "answer": "This conversation belongs to a different manager.",
            }
        with agent_session(deps.agent_url, state["manager_id"]) as conn:
            portfolios = list_portfolios(conn)
            as_of = conn.execute("SELECT max(date) FROM analytics.v_portfolio_nav").fetchone()[0]
        portfolios["inception"] = portfolios["inception"].astype(str)
        return {
            **reset,
            "owner_id": state["manager_id"],
            "portfolios": portfolios.to_dict("records"),
            "as_of": str(as_of),
        }

    def input_guard(state: AgentState) -> dict:
        result = check_input(state["question"])
        update = {
            "question": result.question,
            "flags": result.flags,
            "trace": [{"step": "input_guard", "blocked": result.blocked, "flags": result.flags}],
        }
        if result.blocked:
            update["plan"] = {"intent": "out_of_scope", "out_of_scope_reason": result.reason}
        return update

    def plan(state: AgentState) -> dict:
        ids = [p["portfolio_id"] for p in state["portfolios"]]
        try:
            raw = deps.planner.plan(
                state["question"],
                state["portfolios"],
                state["as_of"],
                state.get("history", []),
                state.get("clarification", ""),
            )
        except PlannerError as e:
            return {
                "status": "error",
                "answer": f"Sorry, I couldn't work out that question ({e}).",
                "trace": [{"step": "plan", "error": str(e)}],
            }
        planned, notes = sanitize(raw, ids)
        if planned.intent == "clarify" and state.get("clarification"):
            notes.append("planner asked to clarify twice; answering for all portfolios instead")
            planned = planned.model_copy(
                update={"intent": "out_of_scope", "out_of_scope_reason": "off_topic"}
            )
        return {
            "plan": planned.model_dump(),
            "trace": [{"step": "plan", "plan": planned.model_dump(), "notes": notes}],
        }

    def clarify(state: AgentState) -> dict:
        question = state["plan"].get("clarification_question") or (
            "Could you say which portfolio and period you mean?"
        )
        reply = interrupt({"type": "clarify", "question": question})
        return {
            "clarification": str(reply),
            "trace": [{"step": "clarify", "asked": question, "reply": str(reply)}],
        }

    def run_metrics(state: AgentState) -> dict:
        p = Plan.model_validate(state["plan"])
        ids = p.portfolio_ids or [x["portfolio_id"] for x in state["portfolios"]]
        names = {x["portfolio_id"]: x["name"] for x in state["portfolios"]}
        period = None if p.period == "custom" else p.period
        out = []
        for pid in ids:
            try:
                results = engine.run(
                    deps.agent_url,
                    state["manager_id"],
                    pid,
                    list(p.metrics),
                    period=period,
                    start=p.start_date or None,
                    end=p.end_date or None,
                    benchmark=None if p.benchmark == "default" else p.benchmark,
                    cfg=deps.metric_config,
                )
            except (PortfolioNotFound, ValueError) as e:
                out.append({"portfolio_id": pid, "error": str(e)})
                continue
            for r in results:
                item = json.loads(json.dumps(asdict(r), default=str))
                item.update(
                    portfolio_id=pid,
                    portfolio_name=names.get(pid, str(pid)),
                    display=display(r.value, r.unit),
                )
                if "error" in item.get("details", {}):
                    item["error"] = item["details"]["error"]
                out.append(item)
        return {
            "metrics": out,
            "trace": [
                {
                    "step": "run_metrics",
                    "portfolios": ids,
                    "metrics": list(p.metrics),
                    "results": len(out),
                }
            ],
        }

    def run_sql(state: AgentState) -> dict:
        p = state["plan"]
        question = p.get("data_question") or state["question"]
        try:
            outcome = answer_with_sql(
                question,
                writer=deps.sql_writer,
                schema=deps.schema,
                agent_url=deps.agent_url,
                manager_id=state["manager_id"],
                portfolios=state["portfolios"],
                as_of=state["as_of"],
                row_limit=deps.row_limit,
                timeout_ms=deps.timeout_ms,
                max_cost=deps.max_cost,
            )
        except (WriterRefused, WriterError) as e:
            sql = {"status": "failed", "explanation": str(e), "attempts": []}
        else:
            r = outcome.result
            sql = {
                "status": outcome.status,
                "explanation": outcome.explanation,
                "attempts": outcome.attempts,
                "sql": r.sql if r else "",
                "columns": r.columns if r else [],
                "rows": r.rows if r else [],
                "row_count": r.row_count if r else 0,
                "truncated": r.truncated if r else False,
            }
        return {
            "sql": sql,
            "trace": [
                {
                    "step": "run_sql",
                    "question": question,
                    "status": sql["status"],
                    "attempts": len(sql.get("attempts", [])),
                }
            ],
        }

    def explain(state: AgentState) -> dict:
        payload = build_payload(state)
        notes = []
        try:
            answer = deps.explainer.explain(state["question"], payload)
            bad = unsupported_numbers(answer, payload, state["question"])
            if bad:
                notes.append(f"unsupported numbers on first draft: {bad}")
                answer = deps.explainer.explain(
                    state["question"],
                    payload,
                    feedback=(
                        f"Your previous answer used numbers that are not in the results: {bad}. "
                        "Rewrite it using only numbers from the results; describe comparisons "
                        "in words instead of computing them."
                    ),
                )
                bad = unsupported_numbers(answer, payload, state["question"])
            if bad:
                notes.append(f"still unsupported after retry: {bad}; used the plain summary")
                answer = fallback_answer(payload)
        except Exception as e:  # an explainer failure must not lose the computed results
            notes.append(f"explainer failed: {type(e).__name__}: {e}")
            answer = fallback_answer(payload)
        answer += DISCLAIMER
        return {
            "answer": answer,
            "status": "answered",
            "history": [{"question": state["question"], "answer": answer, "plan": state["plan"]}],
            "trace": [{"step": "explain", "notes": notes}],
        }

    def refuse(state: AgentState) -> dict:
        if state.get("status") == "error":
            answer = state.get("answer") or "Something went wrong."
        else:
            reason = (state.get("plan") or {}).get("out_of_scope_reason", "off_topic")
            answer = REFUSALS.get(reason, REFUSALS["off_topic"])
        return {
            "answer": answer,
            "status": state.get("status") or "out_of_scope",
            "history": [
                {"question": state["question"], "answer": answer, "plan": state.get("plan")}
            ],
            "trace": [{"step": "refuse"}],
        }

    def after_start(state: AgentState) -> str:
        return "refuse" if state.get("status") == "error" else "input_guard"

    def after_guard(state: AgentState) -> str:
        return "refuse" if state.get("plan") else "plan"

    def after_plan(state: AgentState) -> str | list[str]:
        if state.get("status") == "error":
            return "refuse"
        intent = state["plan"]["intent"]
        return {
            "metric": ["run_metrics"],
            "data_lookup": ["run_sql"],
            "both": ["run_metrics", "run_sql"],
            "clarify": "clarify",
        }.get(intent, "refuse")

    g = StateGraph(AgentState)
    for name, fn in [
        ("start_turn", start_turn),
        ("input_guard", input_guard),
        ("plan", plan),
        ("clarify", clarify),
        ("run_metrics", run_metrics),
        ("run_sql", run_sql),
        ("explain", explain),
        ("refuse", refuse),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "start_turn")
    g.add_conditional_edges("start_turn", after_start, ["input_guard", "refuse"])
    g.add_conditional_edges("input_guard", after_guard, ["plan", "refuse"])
    g.add_conditional_edges("plan", after_plan, ["run_metrics", "run_sql", "clarify", "refuse"])
    g.add_edge("clarify", "plan")
    g.add_edge("run_metrics", "explain")
    g.add_edge("run_sql", "explain")
    g.add_edge("explain", END)
    g.add_edge("refuse", END)
    return g.compile(checkpointer=checkpointer)


def build_payload(state: AgentState) -> dict:
    """What the explainer sees: results only, with long tables trimmed."""
    # Only the portfolios the question is about: listing all of them invites the explainer
    # to comment on the ones that aren't in the results.
    in_scope = set((state.get("plan") or {}).get("portfolio_ids") or [])
    payload: dict = {
        "data_as_of": state["as_of"],
        "portfolios": [
            {"portfolio_id": p["portfolio_id"], "name": p["name"], "benchmark": p["benchmark"]}
            for p in state["portfolios"]
            if not in_scope or p["portfolio_id"] in in_scope
        ],
    }
    if state.get("metrics") is not None:
        items = []
        for m in state["metrics"]:
            item = {k: v for k, v in m.items() if k != "table"}
            if m.get("table"):
                item["table"] = m["table"][:PAYLOAD_TABLE_ROWS]
                item["table_rows_total"] = len(m["table"])
            items.append(item)
        payload["metrics"] = items
    if state.get("sql") is not None:
        s = state["sql"]
        payload["sql"] = {
            k: s.get(k)
            for k in ("status", "explanation", "columns", "row_count", "truncated", "sql")
        }
        payload["sql"]["rows"] = (s.get("rows") or [])[:PAYLOAD_SQL_ROWS]
        if s.get("status") == "unanswerable":
            payload["sql"]["unanswerable"] = s.get("explanation")
    return payload


def fallback_answer(payload: dict) -> str:
    """A plain, template-built summary. Used when the explanation can't be verified."""
    lines = ["Here are the computed results (shown as a plain summary):", ""]
    for m in payload.get("metrics", []):
        if m.get("error"):
            lines.append(
                f"- {m.get('portfolio_name', m['portfolio_id'])}: "
                f"{m.get('label', m.get('name', 'metric'))} could not be computed "
                f"({m['error']})"
            )
        elif m.get("display") is not None:
            lines.append(
                f"- {m['portfolio_name']}, {m['label']} "
                f"({m['period_start'] or 'as of'} to {m['period_end']}): {m['display']}"
            )
        else:
            lines.append(f"- {m['portfolio_name']}, {m['label']}: see the table")
    sql = payload.get("sql")
    if sql:
        if sql["status"] == "ok":
            lines.append(
                f"- {sql['explanation']} ({sql['row_count']} rows"
                f"{', truncated' if sql['truncated'] else ''})"
            )
            rows = sql["rows"][:10]
            if rows:
                cols = list(rows[0])
                lines += ["", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
                lines += ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows]
        else:
            lines.append(f"- The data question couldn't be answered: {sql['explanation']}")
    return "\n".join(lines)
