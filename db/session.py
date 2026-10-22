"""The one way the agent talks to the database: as agent_ro, scoped to one manager.

Both the metric loaders and the SQL executor open their connections here, so there is a
single place where app.manager_id gets set, and nothing else in the agent holds an admin
connection that a bug could misuse.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import sql


@contextmanager
def agent_session(agent_url: str, manager_id: int) -> Iterator[psycopg.Connection]:
    """A transaction as agent_ro with app.manager_id set for row-level security.

    SET LOCAL lasts only until the transaction ends, so a pooled connection can never carry
    one manager's identity into another manager's request.
    """
    with psycopg.connect(agent_url) as conn, conn.transaction():
        conn.execute(sql.SQL("SET LOCAL app.manager_id = {}").format(sql.Literal(str(manager_id))))
        yield conn
