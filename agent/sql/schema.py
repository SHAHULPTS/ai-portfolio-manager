"""What the SQL writer is told about the database, and what the guard checks against.

Both come from one source: the live catalog of the `analytics` schema, read as agent_ro, so
it lists only what the agent can actually query. The column and view COMMENTs written in the
migrations become the descriptions the LLM reads, so the docs and the prompt can't drift.
"""

from dataclasses import dataclass

import psycopg

CATALOG_SQL = """
SELECT c.relname, obj_description(c.oid, 'pg_class'),
       a.attname, format_type(a.atttypid, a.atttypmod), col_description(c.oid, a.attnum)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE n.nspname = 'analytics' AND c.relkind = 'v' AND has_table_privilege(c.oid, 'SELECT')
ORDER BY c.relname, a.attnum
"""


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    comment: str


@dataclass(frozen=True)
class View:
    name: str
    comment: str
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class SchemaInfo:
    views: tuple[View, ...]

    @property
    def view_names(self) -> frozenset[str]:
        return frozenset(v.name for v in self.views)

    def for_sqlglot(self) -> dict:
        """{"analytics": {view: {column: type}}}, the shape sqlglot's qualifier expects."""
        return {
            "analytics": {
                v.name: {c.name: c.type.split("(")[0] for c in v.columns} for v in self.views
            }
        }

    def describe(self) -> str:
        """Prompt text. Deterministic (catalog order), so the prompt cache keeps hitting."""
        blocks = []
        for v in self.views:
            lines = [f"analytics.{v.name} -- {v.comment}"]
            for c in v.columns:
                note = f"  -- {c.comment}" if c.comment else ""
                lines.append(f"  {c.name} {c.type}{note}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


def load_schema(conn: psycopg.Connection) -> SchemaInfo:
    views: dict[str, tuple[str, list[Column]]] = {}
    for view, view_comment, col, col_type, col_comment in conn.execute(CATALOG_SQL):
        _, cols = views.setdefault(view, (view_comment or "", []))
        cols.append(Column(col, col_type, col_comment or ""))
    return SchemaInfo(
        tuple(View(name, comment, tuple(cols)) for name, (comment, cols) in views.items())
    )
