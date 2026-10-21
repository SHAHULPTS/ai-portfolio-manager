"""Apply db/migrations/*.sql in order, once each, then set the agent role's password.

Usage: python -m db.migrate
"""

from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from config import load_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def migrate(database_url: str, agent_database_url: str) -> list[str]:
    """Apply pending migrations. Returns the filenames applied this run."""
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

        password = conninfo_to_dict(agent_database_url).get("password")
        if password:
            conn.execute(sql.SQL("ALTER ROLE agent_ro PASSWORD {}").format(sql.Literal(password)))
            conn.commit()
    return applied_now


if __name__ == "__main__":
    s = load_settings()
    applied = migrate(s.database_url, s.agent_database_url)
    print(f"Applied {len(applied)} migration(s): {', '.join(applied) or 'none pending'}")
