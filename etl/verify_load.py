"""Sanity-check the loaded price data. Exits non-zero if a hard check fails.

Usage: python -m etl.verify_load

Hard checks must hold for the data to be usable. The report sections list things that
are real but worth knowing (missing days, huge one-day moves) so they can be documented.
"""

import sys

import psycopg

from config import load_settings

HARD_CHECKS = [
    (
        "daily bar count is plausible (250k-350k)",
        "SELECT count(*) BETWEEN 250000 AND 350000 FROM core.prices_daily",
    ),
    (
        "NIFTY50 close on 2026-04-08 is 24012.35 (last minute bar of the raw file)",
        "SELECT close = 24012.35 FROM core.prices_daily "
        "WHERE symbol = 'NIFTY50' AND date = '2026-04-08'",
    ),
    (
        "every security has prices and a date range",
        "SELECT bool_and(first_date IS NOT NULL AND last_date IS NOT NULL) FROM core.securities",
    ),
    (
        "no OHLC inconsistencies",
        "SELECT NOT EXISTS (SELECT 1 FROM core.prices_daily "
        " WHERE high < greatest(open, close) OR low > least(open, close))",
    ),
    (
        "no stock trades on a day the NIFTY50 did not",
        "SELECT NOT EXISTS (SELECT 1 FROM core.prices_daily p WHERE NOT EXISTS ("
        " SELECT 1 FROM core.prices_daily n WHERE n.symbol = 'NIFTY50' AND n.date = p.date))",
    ),
    (
        "no one-day move beyond +/-60% (the largest real one is the 2017 PSU-bank rally, +50%)",
        "SELECT NOT EXISTS (SELECT 1 FROM (SELECT close / lag(close) OVER ("
        " PARTITION BY symbol ORDER BY date) - 1 AS r FROM core.prices_daily) t"
        " WHERE abs(r) > 0.6)",
    ),
]

# Trading days (by the NIFTY50 calendar) inside each stock's own date range with no bar.
MISSING_DAYS_SQL = """
SELECT s.symbol, count(*) AS missing_days
FROM core.securities s
JOIN core.prices_daily n ON n.symbol = 'NIFTY50' AND n.date BETWEEN s.first_date AND s.last_date
LEFT JOIN core.prices_daily p ON p.symbol = s.symbol AND p.date = n.date
WHERE p.symbol IS NULL AND NOT s.is_index
GROUP BY s.symbol HAVING count(*) > 5
ORDER BY missing_days DESC
"""

# One-day close-to-close moves over 25%: likely unadjusted corporate actions.
BIG_MOVES_SQL = """
SELECT symbol, date, prev_close, close, round((close / prev_close - 1) * 100, 1) AS pct
FROM (SELECT symbol, date, close, lag(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close
      FROM core.prices_daily) t
WHERE prev_close IS NOT NULL AND abs(close / prev_close - 1) > 0.25
ORDER BY abs(close / prev_close - 1) DESC
"""


def main() -> int:
    failed = 0
    with psycopg.connect(load_settings().database_url) as conn:
        for label, query in HARD_CHECKS:
            ok = conn.execute(query).fetchone()[0]
            print(f"[{'PASS' if ok else 'FAIL'}] {label}")
            failed += not ok

        print("\nStocks with more than 5 missing trading days inside their own range:")
        for symbol, n in conn.execute(MISSING_DAYS_SQL).fetchall():
            print(f"  {symbol:<12} {n}")

        print("\nOne-day moves over 25% (check for unadjusted splits or demergers):")
        for symbol, date, prev, close, pct in conn.execute(BIG_MOVES_SQL).fetchall():
            print(f"  {symbol:<12} {date}  {prev} -> {close}  ({pct}%)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
