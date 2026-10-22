"""Runtime settings, read once from the environment (and .env if present).

Every tunable assumption lives here so that answers can state exactly which
risk-free rate, annualisation factor and SQL limits were used.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    database_url: str
    agent_database_url: str
    router_model: str
    reasoning_model: str
    risk_free_rate: float
    trading_days: int
    sql_row_limit: int
    sql_timeout_ms: int
    sql_max_cost: float
    sql_writer_effort: str


def load_settings() -> Settings:
    load_dotenv()
    env = os.environ.get
    return Settings(
        anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
        database_url=env(
            "DATABASE_URL", "postgresql://pm_admin:change-me@localhost:5433/portfolio"
        ),
        agent_database_url=env(
            "AGENT_DATABASE_URL", "postgresql://agent_ro:change-me-too@localhost:5433/portfolio"
        ),
        router_model=env("ROUTER_MODEL", "claude-haiku-5-5"),
        reasoning_model=env("REASONING_MODEL", "claude-sonnet-5-5"),
        risk_free_rate=float(env("RISK_FREE_RATE", "0.065")),
        trading_days=int(env("TRADING_DAYS", "252")),
        sql_row_limit=int(env("SQL_ROW_LIMIT", "500")),
        sql_timeout_ms=int(env("SQL_TIMEOUT_MS", "3000")),
        sql_max_cost=float(env("SQL_MAX_COST", "2000000")),
        sql_writer_effort=env("SQL_WRITER_EFFORT", "medium"),
    )
