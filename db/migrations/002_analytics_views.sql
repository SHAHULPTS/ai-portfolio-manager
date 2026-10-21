-- The semantic layer: the only objects the agent can query.
-- Comments here are read into the SQL-writer prompt in phase 4, so keep them precise.
-- Portfolio views (positions, NAV) are added in phase 2.

CREATE VIEW analytics.v_securities AS
SELECT symbol, name, sector, industry, is_index, first_date, last_date
FROM core.securities;

COMMENT ON VIEW analytics.v_securities IS
    'One row per stock or index. Indices (is_index = true) are NIFTY50 and NIFTYBANK.';
COMMENT ON COLUMN analytics.v_securities.symbol IS 'NSE ticker, e.g. TCS, HDFCBANK, NIFTY50';
COMMENT ON COLUMN analytics.v_securities.sector IS 'NSE macro sector, e.g. Financial Services, Information Technology';
COMMENT ON COLUMN analytics.v_securities.first_date IS 'First trading date with price data';
COMMENT ON COLUMN analytics.v_securities.last_date IS 'Last trading date with price data';

CREATE VIEW analytics.v_prices AS
SELECT symbol, date, open, high, low, close, volume
FROM core.prices_daily;

COMMENT ON VIEW analytics.v_prices IS
    'Daily OHLCV per symbol, adjusted for splits and bonuses but not dividends (price return only). '
    'Only trading days appear. Index volume is 0.';
COMMENT ON COLUMN analytics.v_prices.close IS 'Closing price in INR (index points for indices)';

CREATE VIEW analytics.v_portfolios AS
SELECT portfolio_id, manager_id, name, mandate, benchmark_symbol, inception_date, initial_cash
FROM core.portfolios;

COMMENT ON VIEW analytics.v_portfolios IS
    'Portfolios belonging to the signed-in manager only (enforced by row-level security).';
COMMENT ON COLUMN analytics.v_portfolios.mandate IS 'Investment theme, e.g. Banking-heavy large cap';
COMMENT ON COLUMN analytics.v_portfolios.benchmark_symbol IS 'Index the portfolio is measured against';
COMMENT ON COLUMN analytics.v_portfolios.initial_cash IS 'Starting cash in INR';
