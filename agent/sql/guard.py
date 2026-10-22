"""Guardrail layer 2: check LLM-written SQL before it gets near the database.

The checks work on sqlglot's parse tree, not on the text, so casing, comments, whitespace or
quoting tricks change nothing. A query passes only if:

1. it parses as PostgreSQL and is exactly one statement, at most MAX_SQL_CHARS long;
2. it is a read: a SELECT or a set operation of SELECTs (UNION etc.), optionally with WITH,
   and nothing anywhere in the tree writes, locks, creates or runs a command;
3. every table is an analytics view the agent can see (or a CTE defined in the query);
4. every column exists on the table it's read from (checked by sqlglot's qualifier
   against the live schema), so the LLM gets a precise error to fix instead of a vague one;
5. no function on the deny-list is called (sleeping, file access, config changes, ...);
6. it has a LIMIT no larger than the row limit (one is added if missing).

This layer catches mistakes early and explains them; it is not the last line of defence. The
database (layer 1) refuses writes and other managers' rows even if a query got past this.
"""

import logging
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify

from agent.sql.schema import SchemaInfo

MAX_SQL_CHARS = 5000

# sqlglot logs a warning when it falls back to parsing something as a raw Command. We reject
# those anyway, and the guard's own reason is what callers should see.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# Node types that must not appear anywhere in the tree. getattr() keeps this working across
# sqlglot versions that add or rename a class.
FORBIDDEN_NODES = tuple(
    cls
    for name in [
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "Drop",
        "Create",
        "Alter",
        "TruncateTable",
        "Command",
        "Into",
        "Lock",
        "Set",
        "SetItem",
        "Transaction",
        "Commit",
        "Rollback",
        "Copy",
        "Grant",
        "Revoke",
        "Use",
        "Pragma",
        "LoadData",
        "Analyze",
        "Describe",
    ]
    if (cls := getattr(exp, name, None)) is not None
)

DENIED_FUNCTIONS = {
    "set_config",
    "current_setting",
    "nextval",
    "setval",
    "currval",
    "lastval",
    "lo_import",
    "lo_export",
    "lo_get",
    "lo_put",
    "lo_unlink",
    "dblink",
    "dblink_exec",
    "dblink_connect",
    "query_to_xml",
    "query_to_xml_and_xmlschema",
    "table_to_xml",
    "cursor_to_xml",
    "schema_to_xml",
    "database_to_xml",
    "txid_current",
    "version",
    "inet_server_addr",
}
DENIED_PREFIXES = ("pg_", "lo_", "dblink", "has_", "txid_")


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    sql: str  # the query to run (LIMIT added or capped); empty when rejected
    reason: str = ""  # why it was rejected, phrased so the SQL writer can fix it


def reject(reason: str) -> GuardResult:
    return GuardResult(ok=False, sql="", reason=reason)


def check(sql: str, schema: SchemaInfo, row_limit: int = 500) -> GuardResult:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        return reject("empty query")
    if len(sql) > MAX_SQL_CHARS:
        return reject(f"query is longer than {MAX_SQL_CHARS} characters")
    if "\x00" in sql:
        return reject("query contains a NUL byte")

    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except (ParseError, TokenError) as e:
        return reject(f"not valid PostgreSQL: {str(e).splitlines()[0]}")
    if len(statements) != 1:
        return reject(f"expected exactly one statement, found {len(statements)}")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        return reject(f"only SELECT queries are allowed, not {tree.key.upper()}")
    forbidden = next(tree.find_all(*FORBIDDEN_NODES), None)
    if forbidden is not None:
        return reject(f"{forbidden.key.upper()} is not allowed in a read-only query")
    with_ = tree.args.get("with") or tree.find(exp.With)
    if with_ is not None and with_.args.get("recursive"):
        return reject("recursive CTEs are not allowed")

    problem = _check_tables(tree, schema) or _check_functions(tree)
    if problem:
        return reject(problem)

    try:
        qualify(
            tree.copy(),
            schema=schema.for_sqlglot(),
            dialect="postgres",
            validate_qualify_columns=True,
            identify=False,
        )
    except OptimizeError as e:
        return reject(f"column check failed: {e}")

    limited, problem = _enforce_limit(tree, row_limit)
    if problem:
        return reject(problem)
    return GuardResult(ok=True, sql=limited.sql(dialect="postgres"))


def _check_tables(tree: exp.Expression, schema: SchemaInfo) -> str:
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            continue  # a table function such as generate_series(); checked as a function
        name, db = table.name, table.db
        if table.catalog:
            return f"cross-database reference {table.sql()} is not allowed"
        if not db and name in cte_names:
            continue
        if db not in ("", "analytics") or name not in schema.view_names:
            allowed = ", ".join(f"analytics.{v}" for v in sorted(schema.view_names))
            return f"table {table.sql()} is not allowed; use only: {allowed}"
        if not db:
            table.set("db", exp.to_identifier("analytics"))
    return ""


def _function_name(fn: exp.Func) -> str:
    if isinstance(fn, exp.Anonymous):
        return str(fn.this).lower()
    return fn.sql_name().lower()


def _check_functions(tree: exp.Expression) -> str:
    for fn in tree.find_all(exp.Func):
        name = _function_name(fn)
        if name in DENIED_FUNCTIONS or name.startswith(DENIED_PREFIXES):
            return f"function {name}() is not allowed"
    return ""


def _enforce_limit(tree: exp.Query, row_limit: int) -> tuple[exp.Query, str]:
    limit = tree.args.get("limit")
    if limit is None:
        return tree.limit(row_limit), ""
    value = limit.expression
    if not (isinstance(value, exp.Literal) and value.is_int):
        return tree, "LIMIT must be a plain number"
    if int(value.this) > row_limit:
        return tree.limit(row_limit), ""
    return tree, ""
