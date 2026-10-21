-- Base tables. Owned by the ETL/admin role; the agent never touches these directly.

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE core.securities (
    symbol      text PRIMARY KEY,
    name        text NOT NULL,
    sector      text NOT NULL,
    industry    text NOT NULL,
    is_index    boolean NOT NULL DEFAULT false,
    first_date  date,
    last_date   date
);

CREATE TABLE core.prices_daily (
    symbol  text NOT NULL REFERENCES core.securities (symbol),
    date    date NOT NULL,
    open    numeric(14, 4) NOT NULL CHECK (open > 0),
    high    numeric(14, 4) NOT NULL,
    low     numeric(14, 4) NOT NULL CHECK (low > 0),
    close   numeric(14, 4) NOT NULL CHECK (close > 0),
    volume  bigint NOT NULL CHECK (volume >= 0),
    PRIMARY KEY (symbol, date),
    CHECK (low <= high)
);
CREATE INDEX prices_daily_date_idx ON core.prices_daily (date);

CREATE TABLE core.managers (
    manager_id  integer PRIMARY KEY,
    name        text NOT NULL
);

CREATE TABLE core.portfolios (
    portfolio_id      integer PRIMARY KEY,
    manager_id        integer NOT NULL REFERENCES core.managers (manager_id),
    name              text NOT NULL,
    mandate           text NOT NULL,
    benchmark_symbol  text NOT NULL REFERENCES core.securities (symbol),
    inception_date    date NOT NULL,
    initial_cash      numeric(18, 2) NOT NULL CHECK (initial_cash > 0)
);

CREATE TABLE core.transactions (
    txn_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portfolio_id  integer NOT NULL REFERENCES core.portfolios (portfolio_id),
    symbol        text NOT NULL REFERENCES core.securities (symbol),
    trade_date    date NOT NULL,
    side          text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity      numeric(18, 4) NOT NULL CHECK (quantity > 0),
    price         numeric(14, 4) NOT NULL CHECK (price > 0),
    fees          numeric(14, 2) NOT NULL DEFAULT 0 CHECK (fees >= 0)
);
CREATE INDEX transactions_portfolio_idx ON core.transactions (portfolio_id, trade_date);

CREATE TABLE core.cash_flows (
    cash_flow_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portfolio_id  integer NOT NULL REFERENCES core.portfolios (portfolio_id),
    date          date NOT NULL,
    amount        numeric(18, 2) NOT NULL,
    type          text NOT NULL CHECK (type IN ('DEPOSIT', 'WITHDRAWAL')),
    CHECK ((type = 'DEPOSIT' AND amount > 0) OR (type = 'WITHDRAWAL' AND amount < 0))
);
CREATE INDEX cash_flows_portfolio_idx ON core.cash_flows (portfolio_id, date);
