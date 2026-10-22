# AI Portfolio Manager

Ask questions about a stock portfolio in plain English, for example "What's my Sharpe ratio since inception vs NIFTY 50?" or "Which sector am I most exposed to?", and get an answer you can check.

A LangGraph agent splits each question into three jobs:

1. **Understand it.** A planner LLM turns the question into a structured plan: the intent, which stocks, which period and which metrics.
2. **Get the numbers.**
   - Lookups ("show my TCS trades") go through LLM-written SQL behind layered guardrails: a read-only role, row-level security, AST validation, timeouts and row limits.
   - Metrics (Sharpe, drawdown, beta, VaR …) come from unit-tested Python formulas. They never come from LLM-written SQL.
3. **Explain them.** An explainer LLM writes the answer in plain English. An output check rejects any number that isn't in the computed results.

The portfolios are synthetic, built on real NSE prices for roughly the NIFTY 100 from 2015 to 2026.

> **Status: in progress.** Each build phase lives on its own branch and is merged by its own PR.

| Phase | Branch | What it adds |
|---|---|---|
| 0 | `main` | Project skeleton, config, CI |
| 1 | `phase-1-data` | Postgres schema, daily price load, roles and RLS |
| 2 | `phase-2-portfolios` | Synthetic portfolio generator and NAV |
| 3 | `phase-3-metrics` | Metric formulas and tests |
| 4 | `phase-4-sql-guard` | Text-to-SQL with guardrails |
| 5 | `phase-5-agent` | LangGraph orchestration |
| 6 | `phase-6-ui` | Streamlit chat |
| 7 | `phase-7-evals` | Golden and red-team evals |

## Stack
Python 3.12, LangGraph, Claude (Haiku for routing and guards, Sonnet for SQL and explanations), PostgreSQL 16, Streamlit.

## Quickstart (so far)

```bash
cp .env.example .env        # then fill in values
make install                # uv venv + editable install with dev tools
make db-up                  # Postgres on localhost:5433
make data                   # archive/ -> daily prices -> synthetic portfolios, with checks
make test                   # includes database guardrail tests (needs Postgres up)
```

## Data
`make data` turns about 110M minute bars into about 288k daily bars for 114 stocks and 2 indices (2015–2026). It drops zero-price bars and pre-listing junk, and adjusts three demergers the source missed; see [DESIGN_NOTES.md](DESIGN_NOTES.md#phase-1-data-foundation). Sectors come from a hand-built map in `etl/securities.csv`.

The raw data is 1-minute OHLCV bars for each stock plus the NIFTY 50 and NIFTY BANK indices. It is not committed because of its size (5.3 GB). Download it into `archive/` (one `<SYMBOL>_minute.csv` per stock). Source: _TODO: add dataset link_.

## The synthetic book
There are 3 fictional managers and 7 portfolios (Banking, Balanced, Tech, Defensive, Consumption, PSU & Energy, Infrastructure). Each one trades real NSE closes from a random start in 2016–2021 until April 2026. The simulation covers quarterly rebalancing within a drift band, discretionary swaps, deposits and withdrawals, forced exits when a stock's data ends, and 0.03% fees. The generator is seeded, so every rebuild gives the same book, and it refuses to write anything that breaks an invariant (negative cash, shorts, trades off the closing price …).

## Metrics
There are 27 named metrics in `metrics/catalog.py`: returns (total, CAGR, monthly, vs benchmark), risk (volatility, max drawdown with dates, downside deviation, VaR, CVaR), risk-adjusted (Sharpe, Sortino, Calmar), relative (beta, alpha, tracking error, information ratio, correlation) and holdings (positions, sector allocation, concentration, realised and unrealised P&L, P&L by stock, correlation matrix, risk contribution).

They are time-weighted, so deposits and withdrawals aren't counted as returns, and they work for any period ("YTD", "3y", explicit dates). They agree with the `empyrical` library to 1e-9. Each result carries its formula and assumptions, so the explanation can state them.

## Limitations, stated up front
- **Survivorship bias.** The universe is today's index members, so every portfolio beats its benchmark. Equal-weighting those stocks from 2016 returns 20.5% a year against the NIFTY's 11.6%. That is the bias, not skill.
- **Price returns only.** Dividends aren't in the data.
- **The portfolios are synthetic.** They trade like a disciplined manager would, but they are not real funds.

## Design notes
[DESIGN_NOTES.md](DESIGN_NOTES.md) explains why each decision was made, phase by phase.

## Licence
MIT
