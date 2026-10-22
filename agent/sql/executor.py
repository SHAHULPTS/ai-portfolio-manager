"""Run a guarded query as agent_ro, under the signed-in manager's row-level security.

Defences here, on top of the guard's:
- The query is sent as a prepared statement (extended protocol), and Postgres refuses more
  than one command in a prepared statement. So even a guard bug can't smuggle in a second
  statement.
- EXPLAIN runs first, and a plan whose estimated cost exceeds `max_cost` is refused before
  execution: a cartesian join over the price history is stopped before it starts.
- statement_timeout is set per transaction (the agent_ro role default is a backstop).
- At most `row_limit` rows come back; `truncated` says whether there were more.

Database errors become QueryError with a short, plain message the SQL writer can use to fix
its query. Raw driver text, which can include internal details, isn't passed on unedited.
"""

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

import psycopg
from psycopg import errors, sql

from db.session import agent_session


class QueryError(Exception):
    pass


@dataclass
class QueryResult:
    sql: str
    columns: list[str]
    rows: list[dict] = field(default_factory=list)
    truncated: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _plain(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return value


def execute(
    agent_url: str,
    manager_id: int,
    query: str,
    row_limit: int = 500,
    timeout_ms: int = 3000,
    max_cost: float = 2_000_000,
) -> QueryResult:
    try:
        with agent_session(agent_url, manager_id) as conn:
            conn.execute(
                sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(int(timeout_ms)))
            )
            plan = conn.execute(f"EXPLAIN (FORMAT JSON) {query}", prepare=True).fetchone()[0]
            cost = plan[0]["Plan"]["Total Cost"]
            if cost > max_cost:
                raise QueryError(
                    f"query is too expensive (estimated cost {cost:,.0f} > {max_cost:,.0f}); "
                    "filter by portfolio, symbol or date, or aggregate"
                )
            cur = conn.execute(query, prepare=True)
            columns = [d.name for d in cur.description]
            fetched = cur.fetchmany(row_limit + 1)
    except QueryError:
        raise
    except errors.QueryCanceled as e:
        raise QueryError(f"query took longer than {timeout_ms} ms; simplify or filter it") from e
    except (errors.InsufficientPrivilege, errors.ReadOnlySqlTransaction) as e:
        raise QueryError("not permitted: the agent can only read the analytics views") from e
    except (
        errors.UndefinedColumn,
        errors.UndefinedTable,
        errors.UndefinedFunction,
        errors.SyntaxError,
        errors.DatatypeMismatch,
        errors.InvalidTextRepresentation,
        errors.GroupingError,
        errors.AmbiguousColumn,
        errors.DivisionByZero,
    ) as e:
        detail = e.diag.message_primary or type(e).__name__
        raise QueryError(f"database rejected the query: {detail}") from e
    except psycopg.Error as e:
        raise QueryError(f"query failed ({type(e).__name__})") from e

    rows = [dict(zip(columns, map(_plain, r), strict=True)) for r in fetched[:row_limit]]
    return QueryResult(sql=query, columns=columns, rows=rows, truncated=len(fetched) > row_limit)
