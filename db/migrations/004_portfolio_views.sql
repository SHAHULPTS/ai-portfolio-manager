-- Derived portfolio data and the views the agent uses to ask about it.
--
-- nav_daily and positions are computed in Python (portfolio/accounting.py) by
-- etl/refresh_derived.py and stored as plain tables. Not materialized views: Postgres can't
-- put row-level security on a materialized view, and refreshing one would run with no
-- app.manager_id set, which under our fail-closed policy would store zero rows.

CREATE TABLE core.nav_daily (
    portfolio_id    integer NOT NULL REFERENCES core.portfolios (portfolio_id),
    date            date NOT NULL,
    holdings_value  numeric(18, 2) NOT NULL,
    cash            numeric(18, 2) NOT NULL,
    nav             numeric(18, 2) NOT NULL,
    net_flow        numeric(18, 2) NOT NULL,
    PRIMARY KEY (portfolio_id, date)
);

CREATE TABLE core.positions (
    portfolio_id  integer NOT NULL REFERENCES core.portfolios (portfolio_id),
    symbol        text NOT NULL REFERENCES core.securities (symbol),
    quantity      numeric(18, 4) NOT NULL CHECK (quantity >= 0),
    avg_cost      numeric(14, 4) NOT NULL,
    realized_pnl  numeric(18, 2) NOT NULL,
    PRIMARY KEY (portfolio_id, symbol)
);

ALTER TABLE core.nav_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.positions ENABLE ROW LEVEL SECURITY;
CREATE POLICY manager_isolation ON core.nav_daily
    USING (portfolio_id IN (SELECT portfolio_id FROM core.portfolios));
CREATE POLICY manager_isolation ON core.positions
    USING (portfolio_id IN (SELECT portfolio_id FROM core.portfolios));

GRANT SELECT ON core.nav_daily, core.positions TO analytics_owner;

-- Create the views AS analytics_owner, so it owns them and RLS applies when they run.
-- (Its default privileges, set in 003, grant SELECT on them to agent_ro automatically.)
SET ROLE analytics_owner;

CREATE VIEW analytics.v_portfolio_nav AS
SELECT portfolio_id, date, holdings_value, cash, nav, net_flow
FROM core.nav_daily;

COMMENT ON VIEW analytics.v_portfolio_nav IS
    'One row per portfolio per trading day from inception: market value of holdings, cash, '
    'and NAV = holdings_value + cash, all in INR at that day''s close.';
COMMENT ON COLUMN analytics.v_portfolio_nav.net_flow IS
    'Deposits (+) and withdrawals (-) that day. NAV already includes them.';

CREATE VIEW analytics.v_transactions AS
SELECT t.txn_id, t.portfolio_id, t.trade_date, t.symbol, s.name, s.sector, t.side,
       t.quantity, t.price, t.quantity * t.price AS gross_value, t.fees
FROM core.transactions t
JOIN core.securities s USING (symbol);

COMMENT ON VIEW analytics.v_transactions IS
    'Every buy and sell, executed at that day''s closing price. gross_value = quantity * price, '
    'before fees.';
COMMENT ON COLUMN analytics.v_transactions.side IS 'BUY or SELL';

CREATE VIEW analytics.v_cash_flows AS
SELECT portfolio_id, date, type, amount
FROM core.cash_flows;

COMMENT ON VIEW analytics.v_cash_flows IS
    'Money added to or taken out of a portfolio. The first DEPOSIT is the initial investment.';
COMMENT ON COLUMN analytics.v_cash_flows.amount IS 'INR; positive for DEPOSIT, negative for WITHDRAWAL';

CREATE VIEW analytics.v_positions AS
WITH latest_nav AS (
    SELECT DISTINCT ON (portfolio_id) portfolio_id, date, nav
    FROM core.nav_daily
    ORDER BY portfolio_id, date DESC
)
SELECT pos.portfolio_id, n.date AS as_of_date, pos.symbol, s.name, s.sector, s.industry,
       pos.quantity, pos.avg_cost, pos.quantity * pos.avg_cost AS cost_basis,
       px.close AS last_price, pos.quantity * px.close AS market_value,
       pos.quantity * (px.close - pos.avg_cost) AS unrealized_pnl,
       pos.quantity * px.close / n.nav AS weight
FROM core.positions pos
JOIN core.securities s USING (symbol)
JOIN latest_nav n USING (portfolio_id)
JOIN LATERAL (
    SELECT p.close FROM core.prices_daily p
    WHERE p.symbol = pos.symbol AND p.date <= n.date
    ORDER BY p.date DESC LIMIT 1
) px ON true
WHERE pos.quantity > 0;

COMMENT ON VIEW analytics.v_positions IS
    'Current open holdings at the latest NAV date (as_of_date). weight = market_value / '
    'portfolio NAV including cash, so weights of a portfolio sum to slightly under 1.';
COMMENT ON COLUMN analytics.v_positions.avg_cost IS 'Average cost per share including buy fees';
COMMENT ON COLUMN analytics.v_positions.unrealized_pnl IS 'market_value - cost_basis, INR';

CREATE VIEW analytics.v_realized_pnl AS
SELECT pos.portfolio_id, pos.symbol, s.name, s.sector, pos.realized_pnl
FROM core.positions pos
JOIN core.securities s USING (symbol)
WHERE pos.realized_pnl <> 0;

COMMENT ON VIEW analytics.v_realized_pnl IS
    'Profit or loss locked in by sales since inception, per stock (average-cost method, '
    'net of fees). Includes stocks no longer held.';

RESET ROLE;
