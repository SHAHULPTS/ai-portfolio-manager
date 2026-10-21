"""Turn the raw 1-minute CSVs in archive/ into one daily OHLCV Parquet file.

Usage: python -m etl.resample_to_daily [archive_dir] [out_path]

Cleaning rules (see DESIGN_NOTES.md, phase 1, for how each was found):
- MM_minute.csv and LTIM_minute.csv are skipped: MM_minute_new.csv and LTM_minute.csv
  contain the same bars plus later ones.
- Bars with any price <= 0 are dropped (zero-filled gaps in the source).
- Bars between 15:31 and 16:59 are dropped (stale after-close filler). Bars from 17:00 on
  are kept: they are the Diwali Muhurat trading sessions.
- high/low are widened to cover open and close, for the few bars where they don't.
- Hand-checked fixes in etl/corrections.py are applied to the daily bars: pre-listing
  junk, single bad days, and unadjusted demergers.
"""

import sys
from pathlib import Path

import duckdb

from etl.corrections import BAD_DAYS, DEMERGERS, LISTING_DATES

SKIP_FILES = {"MM_minute.csv", "LTIM_minute.csv"}
SYMBOL_OVERRIDES = {"MM_minute_new.csv": "MM", "NIFTY 50": "NIFTY50", "NIFTY BANK": "NIFTYBANK"}


def symbol_for(path: Path) -> str:
    if path.name in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[path.name]
    stem = path.name.removesuffix("_minute.csv")
    return SYMBOL_OVERRIDES.get(stem, stem)


def source_files(archive_dir: Path) -> dict[Path, str]:
    return {
        p: symbol_for(p)
        for p in sorted(archive_dir.glob("*_minute*.csv"))
        if p.name not in SKIP_FILES
    }


DAILY_SQL = """
WITH bars AS (
    SELECT f.symbol, b.date AS ts, b.open, b.high, b.low, b.close, b.volume
    FROM read_csv($paths, filename = true, header = true, columns = {
            'date': 'TIMESTAMP', 'open': 'DOUBLE', 'high': 'DOUBLE',
            'low': 'DOUBLE', 'close': 'DOUBLE', 'volume': 'BIGINT'}) b
    JOIN files f ON f.path = b.filename
    WHERE b.open > 0 AND b.high > 0 AND b.low > 0 AND b.close > 0
      AND (CAST(b.date AS TIME) <= TIME '15:30' OR CAST(b.date AS TIME) >= TIME '17:00')
)
SELECT symbol,
       CAST(ts AS DATE) AS date,
       arg_min(open, ts) AS open,
       max(greatest(high, open, close)) AS high,
       min(least(low, open, close)) AS low,
       arg_max(close, ts) AS close,
       CAST(sum(volume) AS BIGINT) AS volume
FROM bars
GROUP BY symbol, CAST(ts AS DATE)
"""

CORRECTED_SQL = """
WITH kept AS (
    SELECT d.*
    FROM raw_daily d
    LEFT JOIN listing l USING (symbol)
    WHERE (l.listed_on IS NULL OR d.date >= l.listed_on)
      AND NOT EXISTS (SELECT 1 FROM bad_days b WHERE b.symbol = d.symbol AND b.date = d.date)
),
factors AS (
    SELECT m.symbol, m.ex_date,
           ex.close / (SELECT p.close FROM kept p
                       WHERE p.symbol = m.symbol AND p.date < m.ex_date
                       ORDER BY p.date DESC LIMIT 1) AS f
    FROM demergers m
    JOIN kept ex ON ex.symbol = m.symbol AND ex.date = m.ex_date
),
scaled AS (
    SELECT k.*,
           coalesce((SELECT product(f) FROM factors x
                     WHERE x.symbol = k.symbol AND x.ex_date > k.date), 1) AS adj
    FROM kept k
)
SELECT symbol, date,
       round(open * adj, 4) AS open, round(high * adj, 4) AS high,
       round(low * adj, 4) AS low, round(close * adj, 4) AS close,
       volume
FROM scaled
ORDER BY symbol, date
"""


def daily_bars(
    con: duckdb.DuckDBPyConnection,
    files: dict[Path, str],
    listing_dates: dict[str, str] = LISTING_DATES,
    bad_days: list[tuple[str, str]] = BAD_DAYS,
    demergers: list[tuple[str, str]] = DEMERGERS,
) -> duckdb.DuckDBPyRelation:
    con.execute("CREATE OR REPLACE TEMP TABLE files (path TEXT, symbol TEXT)")
    con.executemany("INSERT INTO files VALUES (?, ?)", [(str(p), s) for p, s in files.items()])
    con.execute("CREATE OR REPLACE TEMP TABLE listing (symbol TEXT, listed_on DATE)")
    con.execute("CREATE OR REPLACE TEMP TABLE bad_days (symbol TEXT, date DATE)")
    con.execute("CREATE OR REPLACE TEMP TABLE demergers (symbol TEXT, ex_date DATE)")
    if listing_dates:
        con.executemany("INSERT INTO listing VALUES (?, ?)", list(listing_dates.items()))
    if bad_days:
        con.executemany("INSERT INTO bad_days VALUES (?, ?)", bad_days)
    if demergers:
        con.executemany("INSERT INTO demergers VALUES (?, ?)", demergers)

    con.execute(
        f"CREATE OR REPLACE TEMP TABLE raw_daily AS {DAILY_SQL}",
        {"paths": [str(p) for p in files]},
    )
    # A demerger whose ex-date has no bar would silently go unadjusted; fail loudly instead.
    missing = con.execute(
        "SELECT m.symbol, m.ex_date FROM demergers m "
        "WHERE NOT EXISTS (SELECT 1 FROM raw_daily d "
        "WHERE d.symbol = m.symbol AND d.date = m.ex_date)"
    ).fetchall()
    if missing:
        raise ValueError(f"Demerger ex-dates with no price bar: {missing}")
    return con.sql(CORRECTED_SQL)


def main(archive_dir: Path, out_path: Path) -> None:
    files = source_files(archive_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    daily_bars(con, files).write_parquet(str(out_path))
    n, n_sym = con.execute(f"SELECT count(*), count(DISTINCT symbol) FROM '{out_path}'").fetchone()
    print(f"Wrote {n:,} daily bars for {n_sym} symbols to {out_path}")


if __name__ == "__main__":
    args = sys.argv[1:]
    main(
        Path(args[0] if args else "archive"),
        Path(args[1] if len(args) > 1 else "data/prices_daily.parquet"),
    )
