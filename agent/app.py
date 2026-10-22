"""Run the agent: build its dependencies from settings and handle ask / clarify / resume.

    with open_agent() as agent:
        reply = agent.ask(thread_id, manager_id=2, question="How risky is my tech fund?")
        if reply["type"] == "clarify":
            reply = agent.resume(thread_id, manager_id=2, answer="the last 3 years")

Conversation memory lives in Postgres (schema agent_memory, role agent_memory). Pass
memory="inmemory" for a throwaway session.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from agent.explainer import Explainer
from agent.graph import Deps, build_graph
from agent.planner import Planner
from agent.sql.schema import load_schema
from agent.sql.writer import SqlWriter
from config import Settings, load_settings
from metrics.base import MetricConfig


def make_deps(s: Settings) -> Deps:
    with psycopg.connect(s.agent_database_url) as conn:
        schema = load_schema(conn)
    return Deps(
        agent_url=s.agent_database_url,
        planner=Planner(model=s.router_model),
        sql_writer=SqlWriter(schema=schema, model=s.reasoning_model, effort=s.sql_writer_effort),
        explainer=Explainer(model=s.reasoning_model),
        schema=schema,
        metric_config=MetricConfig(risk_free_rate=s.risk_free_rate, trading_days=s.trading_days),
        row_limit=s.sql_row_limit,
        timeout_ms=s.sql_timeout_ms,
        max_cost=s.sql_max_cost,
    )


@dataclass
class Agent:
    graph: object

    def _config(self, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    def _reply(self, result: dict) -> dict:
        if result.get("__interrupt__"):
            payload = result["__interrupt__"][0].value
            return {"type": "clarify", "question": payload["question"]}
        return {
            "type": "answer",
            "answer": result["answer"],
            "status": result["status"],
            "plan": result.get("plan"),
            "metrics": result.get("metrics"),
            "sql": result.get("sql"),
            "trace": result.get("trace", []),
        }

    def ask(self, thread_id: str, manager_id: int, question: str) -> dict:
        result = self.graph.invoke(
            {"manager_id": manager_id, "question": question}, self._config(thread_id)
        )
        return self._reply(result)

    def resume(self, thread_id: str, manager_id: int, answer: str) -> dict:
        # The paused turn must belong to this manager; resuming skips start_turn's check.
        state = self.graph.get_state(self._config(thread_id)).values
        if state.get("owner_id") != manager_id:
            raise PermissionError("this conversation belongs to a different manager")
        return self._reply(self.graph.invoke(Command(resume=answer), self._config(thread_id)))


@contextmanager
def open_agent(settings: Settings | None = None, memory: str = "postgres") -> Iterator[Agent]:
    s = settings or load_settings()
    deps = make_deps(s)
    if memory == "inmemory":
        yield Agent(build_graph(deps, InMemorySaver()))
        return
    with PostgresSaver.from_conn_string(s.memory_database_url) as saver:
        saver.setup()
        yield Agent(build_graph(deps, saver))
