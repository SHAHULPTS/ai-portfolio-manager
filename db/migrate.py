"""Apply db/migrations/*.sql in order, once each, then set the agent role's password.

Usage: python -m db.migrate
"""

from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from config import load_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def migrate(
    database_url: str, agent_database_url: str, memory_database_url: str | None = None
) -> list[str]:
    """Apply pending migrations, then set role passwords. Returns the files applied."""
    applied_now = []
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
            " filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT filename FROM public.schema_migrations")}
        conn.commit()

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            # One transaction per file: a failing migration leaves no half-applied state.
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute(
                    "INSERT INTO public.schema_migrations (filename) VALUES (%s)", (path.name,)
                )
            applied_now.append(path.name)

        for role, url in [("agent_ro", agent_database_url), ("agent_memory", memory_database_url)]:
            password = conninfo_to_dict(url).get("password") if url else None
            if password:
                conn.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
        conn.commit()
    return applied_now


if __name__ == "__main__":
    s = load_settings()
    applied = migrate(s.database_url, s.agent_database_url, s.memory_database_url)
    print(f"Applied {len(applied)} migration(s): {', '.join(applied) or 'none pending'}")
