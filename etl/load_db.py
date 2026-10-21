"""Load etl/securities.csv and the daily Parquet into Postgres.

Usage: python -m etl.load_db [parquet_path]

Safe to re-run: securities are upserted (portfolios reference them, so they are never
deleted) and prices are replaced wholesale inside one transaction.
"""

import csv
import sys
from pathlib import Path

import duckdb
import psycopg

from config import load_settings

SECURITIES_CSV = Path(__file__).parent / "securities.csv"
BATCH = 50_000


def read_securities(path: Path = SECURITIES_CSV) -> list[dict]:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["is_index"] = r["is_index"].strip().lower() == "true"
    return rows


def check_symbols_match(securities: list[dict], price_symbols: set[str]) -> None:
    listed = {r["symbol"] for r in securities}
    no_metadata = price_symbols - listed
    no_prices = listed - price_symbols
    if no_metadata or no_prices:
        raise ValueError(
            f"securities.csv and price data disagree. "
            f"Missing from securities.csv: {sorted(no_metadata)}. "
            f"Missing prices: {sorted(no_prices)}."
        )


def load(database_url: str, parquet_path: Path) -> int:
    securities = read_securities()
    duck = duckdb.connect()
    price_symbols = {
        r[0] for r in duck.execute(f"SELECT DISTINCT symbol FROM '{parquet_path}'").fetchall()
    }
    check_symbols_match(securities, price_symbols)
    prices = duck.execute(
        f"SELECT symbol, date, open, high, low, close, volume FROM '{parquet_path}' "
        "ORDER BY symbol, date"
    )

    with psycopg.connect(database_url) as conn, conn.transaction():
        conn.cursor().executemany(
            """
            INSERT INTO core.securities (symbol, name, sector, industry, is_index)
            VALUES (%(symbol)s, %(name)s, %(sector)s, %(industry)s, %(is_index)s)
            ON CONFLICT (symbol) DO UPDATE SET
                name = EXCLUDED.name, sector = EXCLUDED.sector,
                industry = EXCLUDED.industry, is_index = EXCLUDED.is_index
            """,
            securities,
        )
        conn.execute("TRUNCATE core.prices_daily")
        n = 0
        with conn.cursor().copy(
            "COPY core.prices_daily (symbol, date, open, high, low, close, volume) FROM STDIN"
        ) as copy:
            while batch := prices.fetchmany(BATCH):
                for row in batch:
                    copy.write_row(row)
                n += len(batch)
        conn.execute(
            """
            UPDATE core.securities s
            SET first_date = p.first_date, last_date = p.last_date
            FROM (SELECT symbol, min(date) first_date, max(date) last_date
                  FROM core.prices_daily GROUP BY symbol) p
            WHERE s.symbol = p.symbol
            """
        )
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("ANALYZE core.prices_daily")
    return n


if __name__ == "__main__":
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/prices_daily.parquet")
    n = load(load_settings().database_url, path)
    print(f"Loaded {n:,} daily bars")
