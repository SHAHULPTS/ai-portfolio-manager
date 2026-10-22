"""Question -> SQL -> guard -> execute, retrying with the error when a draft is rejected.

Phase 5 turns these steps into LangGraph nodes. Keeping the loop here as plain code lets the
retry behaviour and the security properties be tested without a graph or an LLM: any object
with a `.write(...)` method returning a SqlDraft can stand in for the writer.
"""

from dataclasses import dataclass, field
from typing import Protocol

from agent.sql import guard
from agent.sql.executor import QueryError, QueryResult, execute
from agent.sql.schema import SchemaInfo
from agent.sql.writer import SqlDraft

MAX_ATTEMPTS = 3


class Writer(Protocol):
    def write(
        self, question, portfolios, as_of, previous_sql=None, previous_error=None
    ) -> SqlDraft: ...


@dataclass
class SqlOutcome:
    status: str  # "ok", "unanswerable", "failed"
    result: QueryResult | None = None
    explanation: str = ""
    attempts: list[dict] = field(default_factory=list)  # every draft and why it failed, for the UI


def answer_with_sql(
    question: str,
    *,
    writer: Writer,
    schema: SchemaInfo,
    agent_url: str,
    manager_id: int,
    portfolios: list[dict],
    as_of: str,
    row_limit: int = 500,
    timeout_ms: int = 3000,
    max_cost: float = 2_000_000,
) -> SqlOutcome:
    outcome = SqlOutcome(status="failed")
    previous_sql = previous_error = None
    for _ in range(MAX_ATTEMPTS):
        draft = writer.write(question, portfolios, as_of, previous_sql, previous_error)
        attempt = {"sql": draft.sql, "explanation": draft.explanation}
        outcome.attempts.append(attempt)
        if not draft.answerable:
            outcome.status, outcome.explanation = "unanswerable", draft.explanation
            return outcome

        checked = guard.check(draft.sql, schema, row_limit)
        if not checked.ok:
            attempt["error"] = f"guard: {checked.reason}"
            previous_sql, previous_error = draft.sql, checked.reason
            continue
        try:
            result = execute(agent_url, manager_id, checked.sql, row_limit, timeout_ms, max_cost)
        except QueryError as e:
            attempt["error"] = f"database: {e}"
            previous_sql, previous_error = checked.sql, str(e)
            continue
        outcome.status, outcome.result, outcome.explanation = "ok", result, draft.explanation
        return outcome

    outcome.explanation = f"no valid query after {MAX_ATTEMPTS} attempts"
    return outcome
