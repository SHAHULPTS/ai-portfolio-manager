"""Worked question -> SQL examples for the SQL writer's prompt.

Every example is also a test: it must pass the guard and run on the test database
(tests/test_sql_guard.py). They cover the shapes managers ask about most: lookups, filters by
date and stock, aggregates by sector, and "as of" questions on the NAV history.
"""

FEWSHOTS: list[tuple[str, str]] = [
    (
        "What are my top 5 holdings in portfolio 201?",
        """SELECT symbol, name, sector, quantity, market_value, weight
FROM analytics.v_positions
WHERE portfolio_id = 201
ORDER BY weight DESC
LIMIT 5""",
    ),
    (
        "Show my trades in TCS this year across all portfolios. (data as of 2026-04-08)",
        """SELECT portfolio_id, trade_date, side, quantity, price, gross_value, fees
FROM analytics.v_transactions
WHERE symbol = 'TCS' AND trade_date >= DATE '2026-01-01'
ORDER BY trade_date
LIMIT 200""",
    ),
    (
        "How much do I have in banking stocks, per portfolio?",
        """SELECT portfolio_id, sum(market_value) AS financials_value,
       sum(weight) AS financials_weight
FROM analytics.v_positions
WHERE sector = 'Financial Services'
GROUP BY portfolio_id
ORDER BY portfolio_id
LIMIT 50""",
    ),
    (
        "Sector breakdown of portfolio 302.",
        """SELECT sector, sum(market_value) AS market_value, sum(weight) AS weight
FROM analytics.v_positions
WHERE portfolio_id = 302
GROUP BY sector
ORDER BY weight DESC
LIMIT 50""",
    ),
    (
        "What was portfolio 102 worth at the end of 2023?",
        """SELECT date, nav, holdings_value, cash
FROM analytics.v_portfolio_nav
WHERE portfolio_id = 102 AND date <= DATE '2023-12-31'
ORDER BY date DESC
LIMIT 1""",
    ),
    (
        "List all deposits and withdrawals in my Banking fund.",
        """SELECT f.date, f.type, f.amount
FROM analytics.v_cash_flows AS f
JOIN analytics.v_portfolios AS p ON p.portfolio_id = f.portfolio_id
WHERE p.mandate ILIKE '%bank%'
ORDER BY f.date
LIMIT 200""",
    ),
    (
        "Which of my holdings are showing a loss right now?",
        """SELECT portfolio_id, symbol, quantity, avg_cost, last_price, unrealized_pnl
FROM analytics.v_positions
WHERE unrealized_pnl < 0
ORDER BY unrealized_pnl
LIMIT 100""",
    ),
    (
        "How many trades did each portfolio make per year?",
        """SELECT portfolio_id, extract(year FROM trade_date)::int AS year, count(*) AS trades,
       sum(fees) AS total_fees
FROM analytics.v_transactions
GROUP BY portfolio_id, year
ORDER BY portfolio_id, year
LIMIT 200""",
    ),
    (
        "Which stocks have I sold at a profit, and how much did each make?",
        """SELECT portfolio_id, symbol, name, realized_pnl
FROM analytics.v_realized_pnl
WHERE realized_pnl > 0
ORDER BY realized_pnl DESC
LIMIT 100""",
    ),
    (
        "What did HDFCBANK close at on the last trading day of March 2024?",
        """SELECT date, close
FROM analytics.v_prices
WHERE symbol = 'HDFCBANK' AND date <= DATE '2024-03-31'
ORDER BY date DESC
LIMIT 1""",
    ),
    (
        "Do I own any stock in more than one portfolio?",
        """SELECT symbol, count(DISTINCT portfolio_id) AS portfolios,
       sum(market_value) AS total_value
FROM analytics.v_positions
GROUP BY symbol
HAVING count(DISTINCT portfolio_id) > 1
ORDER BY total_value DESC
LIMIT 100""",
    ),
    (
        "Month-end NAV of portfolio 301 during 2025.",
        """WITH month_end AS (
    SELECT portfolio_id, max(date) AS date
    FROM analytics.v_portfolio_nav
    WHERE portfolio_id = 301 AND date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
    GROUP BY portfolio_id, date_trunc('month', date)
)
SELECT n.date, n.nav, n.cash
FROM analytics.v_portfolio_nav AS n
JOIN month_end AS m ON m.portfolio_id = n.portfolio_id AND m.date = n.date
ORDER BY n.date
LIMIT 12""",
    ),
]
