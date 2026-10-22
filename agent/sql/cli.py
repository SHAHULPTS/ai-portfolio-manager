"""Try the SQL path on the real database from a terminal (needs Anthropic credentials).

Usage: python -m agent.sql.cli "Which of my holdings are at a loss?" --manager 2
"""

import argparse
import json

import psycopg

from agent.sql.pipeline import answer_with_sql
from agent.sql.schema import load_schema
from agent.sql.writer import SqlWriter
from config import load_settings
from db.session import agent_session
from metrics.loaders import list_portfolios


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--manager", type=int, default=1)
    args = parser.parse_args()

    s = load_settings()
    with psycopg.connect(s.agent_database_url) as conn:
        schema = load_schema(conn)
    with agent_session(s.agent_database_url, args.manager) as conn:
        portfolios = list_portfolios(conn).to_dict("records")
        as_of = conn.execute("SELECT max(date) FROM analytics.v_portfolio_nav").fetchone()[0]

    writer = SqlWriter(schema=schema, model=s.reasoning_model, effort=s.sql_writer_effort)
    outcome = answer_with_sql(
        args.question,
        writer=writer,
        schema=schema,
        agent_url=s.agent_database_url,
        manager_id=args.manager,
        portfolios=portfolios,
        as_of=str(as_of),
        row_limit=s.sql_row_limit,
        timeout_ms=s.sql_timeout_ms,
        max_cost=s.sql_max_cost,
    )
    for i, a in enumerate(outcome.attempts, 1):
        print(f"--- attempt {i}\n{a['sql']}\n{a.get('error', 'ok')}")
    print(f"--- {outcome.status}: {outcome.explanation}")
    if outcome.result:
        print(json.dumps(outcome.result.rows[:20], indent=2, default=str))
        if outcome.result.truncated:
            print(f"(truncated at {s.sql_row_limit} rows)")


if __name__ == "__main__":
    main()
