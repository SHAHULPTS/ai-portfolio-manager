# Design Notes

This is a running log of the decisions behind the project and why each was made. Each phase adds a section.

---

## Phase 0: Project skeleton

### Why a separate git repo
`asset_manager/` began as a folder inside a larger personal workspace repo. A showcase needs its own clean history, so the folder became a repo of its own, and the parent repo ignores it.

### Why one branch per phase, stacked
Each phase depends on the one before it: portfolios need prices, metrics need portfolio values, and the agent needs metrics. So each branch starts from the previous phase's branch, not from `main`. At the end, each phase becomes its own PR into `main`, merged in order with a merge commit rather than a squash. A reviewer then reads seven small, focused PRs instead of one giant diff, and `main`'s history shows how the project was built.

### Why `archive/` is never committed
The raw data is 5.3 GB, and GitHub rejects any file over 100 MB. The data also has its own licence. The README says where to download it, and the ETL in phase 1 rebuilds everything from it.

### Why a single `config.py`
Every number that changes an answer lives in one place:
- the risk-free rate (6.5%)
- the annualisation factor (252 trading days)
- the SQL row limit and timeout
- the model names

The explainer can then state the assumptions it used ("Sharpe at a 6.5% risk-free rate"), and a tester can change one value without hunting through the code. The settings object is frozen, so nothing can change them during a request.

### Why dependencies are added phase by phase
`pyproject.toml` starts with only `python-dotenv`, `pytest` and `ruff`. Each phase adds the libraries it needs (psycopg in phase 1, sqlglot in phase 4, langgraph in phase 5 …). Each PR then shows exactly what it pulled in and why.

### Why Postgres runs on port 5433
Port 5432 is often already taken by another local Postgres; the meals-now project uses one. 5433 avoids the clash, and the port is configurable in `.env`.

### CI from day one
GitHub Actions runs `ruff check`, `ruff format --check` and `pytest` on every push and PR. The live LLM evals stay out of CI: they need an API key and cost money per run, so they will run on demand instead.

### Secret hygiene
Only `.env.example` is committed. A `gitleaks` pre-commit hook blocks commits that contain API keys. To enable it, run `pre-commit install`.

---

## Phase 1: Data foundation

### From 110M minute bars to 288k daily bars
Every metric in this project is daily: returns, volatility, drawdown, beta. Loading 110 million minute rows into Postgres would make every query slower and would add nothing. `etl/resample_to_daily.py` uses **DuckDB**, which reads all 116 CSVs in parallel and aggregates them in about 5 seconds:
- open = the first bar's open
- close = the last bar's close
- high = the max of the bars' highs
- low = the min of the bars' lows
- volume = the sum of the bars' volumes

The result is a single Parquet file. `etl/load_db.py` streams it into Postgres with `COPY`, the fastest bulk-load path.

The pipeline is two steps (CSV → Parquet → Postgres), not one, so the slow part (the 5.3 GB scan) and the database part can be re-run separately.

### What the data check found, and how each problem was fixed
`etl/verify_load.py` has two parts:
- **Hard checks** that must pass, such as the NIFTY 50 close on 2026-04-08 being exactly 24012.35.
- **Reports** of things a human should look at: stocks with missing trading days, and one-day moves over 25%.

The reports turned up these problems:

| Problem | Evidence | Fix |
|---|---|---|
| Duplicate files | `MM_minute_new.csv` holds all of `MM_minute.csv` plus later bars (1 close differs by 0.30 in 1M overlapping bars). `LTM` has the same relationship to `LTIM`. | Skip `MM` and `LTIM` |
| Zero-price bars | 34,649 bars with every price 0, e.g. BAJAJHLDNG on 2015-05-15 | Drop any bar with a price ≤ 0 |
| After-close filler | HEROMOTOCO, ICICIPRULI and IRCTC have flat prices from 15:31 to 16:51 on 2026-01-22 | Drop bars between 15:31 and 16:59 |
| Diwali Muhurat sessions | Evening bars (17:30–19:29) on one day each year | **Keep**: these were real trades |
| Junk before listing | SBILIFE has prices from 2015 at ₹13 and jumps 5,259% on 2017-10-03, its listing day. HDFCLIFE, IRCTC and IRFC look the same. | Drop bars before each stock's real listing date |
| One-day unadjusted spikes | e.g. BEL ₹34 → ₹101 → ₹33 over 2015-09-10 to 09-14. INFY, DIVISLAB, MOTHERSON and ZYDUSLIFE have the same pattern. | Drop those days |
| Unadjusted demergers | CGPOWER −72% (2016-03-15), ADANIENT −42% (2015-06-03), SIEMENS −24% (2025-04-07, ENRIN split), GRASIM −20% (2017-07-19, Aditya Birla Capital split), TMPV −14% (2025-10-14, Tata Motors CV split) | Scale earlier prices so the ex-date move matches the NIFTY's |

The listing-date, bad-day and demerger fixes are in `etl/corrections.py` as plain lists, each with a comment saying why. They are not patched into the database by hand, so `make data` always rebuilds the same clean result.

**Why the demergers matter:** a portfolio holding TMPV in October 2025 would have shown a 14% loss on paper that no real investor suffered, because they received TMCV shares worth the difference.

**How the adjustment works:** every price before the ex-date is multiplied by `(stock_ex / stock_prev) / (nifty_ex / nifty_prev)`. The adjusted stock then moves exactly with the market on the ex-date, and the rest of the drop is put down to the demerger. My first version used the plain price ratio, which assumes the market was flat. SIEMENS's ex-date was the 2025-04-07 tariff crash (NIFTY −3.2%), and that version would have erased a real fall.

**How the smaller ones were found:** the 25% report missed GRASIM and SIEMENS. A second scan for drops of 10–25% on days the NIFTY moved less than 1.5% found them. The scan's other hits were real news, mostly the Adani group events. ITC Hotels and Jio Financial don't need fixing: NSE ran special price-discovery sessions for those demergers, so the source already reflects them.

After the fixes, every remaining move over 25% is a real event: the October 2017 PSU-bank recapitalisation rally, the March 2020 Covid crash, the 2023 Hindenburg report on Adani, the CG Power fraud (2019) and IndusInd Bank's accounting issue (2025). A new hard check fails if any one-day move exceeds ±60%, so these errors can't come back unnoticed.

**Still known:** prices are adjusted for splits and bonuses but not dividends, so all returns are *price returns*. INFY, ITC and ICICIBANK have about 13 missing days in early 2015 (zero-filled in the source). Phase 2 carries the last price forward over gaps like these.

### The database guardrail design (layer 1)

**Two schemas.** `core` holds the base tables. `analytics` holds views, the only objects the agent can query. Each view and column has a SQL `COMMENT`. Phase 4 builds the SQL-writer prompt from these comments, so the documentation and the prompt can't drift apart.

**Three roles:**
- `pm_admin` owns the tables and runs the ETL and migrations.
- `analytics_owner` (cannot log in) owns the views.
- `agent_ro` (can log in) reads the views and nothing else.

**Why the views need their own owner.** I nearly missed this one. A Postgres view runs with its *owner's* rights. Row-level security never applies to superusers or to a table's owner. If `pm_admin` owned the views, every manager would see every portfolio, and nothing would warn you. Because `analytics_owner` is neither a superuser nor a table owner, RLS applies whenever its views read the tables. The tests check this.

**Row-level security.** Each policy compares `manager_id` with the session setting `app.manager_id`. The app sets it for each transaction (`SET LOCAL`). If it isn't set, `NULLIF(..., '')` turns the comparison into `NULL` and no rows come back: the policy *fails closed*. `transactions` and `cash_flows` inherit the check through a subquery on `portfolios`, which is itself filtered by RLS.

**A hole found while writing the tests.** `set_config()` is an ordinary function, so a single `SELECT` could change the setting partway through a query:
```sql
WITH x AS (SELECT set_config('app.manager_id','2',false)) SELECT * FROM x, analytics.v_portfolios
```
That would read another manager's portfolios. Migration 003 revokes `set_config` from everyone except superusers. The app sets the manager with the `SET` command instead, which is a separate statement. Phase 4's executor only ever sends one `SELECT`, so the LLM can't issue one. `test_query_cannot_switch_manager_with_set_config` keeps this hole closed.

**Grants are the wall; read-only mode is a backstop.** `agent_ro` also has the role defaults `default_transaction_read_only = on` and `statement_timeout = 3s`. Any session can override those with `SET`, so they are not the security boundary: the missing write privileges are. `test_grants_hold_even_if_read_only_mode_is_switched_off` proves it.

**Secrets.** The `agent_ro` password is never written in SQL. `db/migrate.py` reads it from `AGENT_DATABASE_URL` and sets it with a safely quoted literal.

### Testing against a real Postgres
The guardrail tests run against a real Postgres, not a mock, because what's being tested is the database's own behaviour. `tests/conftest.py` creates a throwaway `portfolio_test` database, runs the migrations, adds two managers with one portfolio each, and drops it all at the end.

Without Postgres running, the tests skip locally. CI sets `REQUIRE_DB=1` and runs a Postgres service container, so a skip there is a failure.

### Small things that bit
- **DuckDB 2.0** removed `fetchone()` and `fetchmany()` on relations, because a relation re-runs its query on every call. The code uses `con.execute(...)` instead, which works on 1.x and 2.x.
- **DuckDB's `sum(BIGINT)` returns a 128-bit integer.** It reached Postgres as `75469.0` and was rejected. The fix is a cast to `BIGINT`. The failed load left nothing half-done, because the whole load runs in one transaction.

### For phase 2
New views must be owned by `analytics_owner` (`ALTER VIEW ... OWNER TO analytics_owner`), or RLS silently stops applying. The NAV calculation must carry the last price forward over missing days.

---

## Phase 2: Synthetic portfolios

### The book
There are 3 fictional managers and 7 portfolios, each with its own **mandate** (`portfolio/mandates.py`): Banking & Financials, Balanced Nifty Core, IT & Tech, Defensive FMCG & Pharma, Consumption & Auto, PSU & Energy, and Infrastructure. A mandate is a set of target weights plus a few *alternates* the manager may rotate into. Each portfolio:
- starts on a random trading day between 2016 and 2021, with ₹1–5 Cr
- trades on the real daily closes until 2026-04-08

`SEED = 2026` makes the whole book reproducible: the same seed always gives the same trades.

### What the simulator does each day (`portfolio/simulator.py`)
1. **Forced exit:** if a stock's data ends before the dataset does (DABUR, HAVELLS, HEROMOTOCO, LICI, NHPC and BHEL end on 2026-01-22/23), sell it in full on its last day. After that we'd have no price to value it.
2. **Cash flows:** on random days (about 1.5 a year), add a deposit of 2–10% of NAV, invested straight away, or take a withdrawal, raised by selling first.
3. **Discretionary swap:** on the first trading day of each month, with a 15% chance, replace one holding with an alternate, which takes over its target weight.
4. **Quarterly rebalance:** on the first trading day of each quarter, trade back to target. Only positions more than 1% of NAV off target are traded. A real manager doesn't pay fees to fix a 0.2% drift, and this keeps turnover believable: about 200–350 trades per portfolio over 5–10 years.

Target weights are renormalised over whatever is listed and trading that day. So SBILIFE, a target in the banking mandate, joins at the first rebalance after it lists, and a stock missing a price on a given day simply isn't traded that day.

### The rules, checked every time (`portfolio/invariants.py`)
- cash never below zero
- no short positions
- whole shares only
- every trade at that day's **actual close**, within the stock's listed dates
- only stocks from the mandate
- nothing held after a stock's data ends
- one NAV row per trading day from inception
- day-one NAV = initial cash − day-one fees

The generator **refuses to write anything** if a single check fails, and the tests run the same checker. One test tampers with a trade price on purpose to prove the checker would catch it.

### One accounting module for everything (`portfolio/accounting.py`)
Positions use the **average-cost** method: buy fees go into the cost basis, and sell fees reduce realised P&L. Daily NAV = shares held × last known close (carried forward over data gaps) + cash.

The generator uses this module to check its own output, and `etl/refresh_derived.py` uses the same module to fill the database. That way, what the simulator "believed" it held can't drift from what gets reported.

**Timing convention:** trades happen at the close, and flows land at the end of the day, so NAV on day *t* already includes that day's flows. Phase 3's time-weighted return removes them: `r_t = (NAV_t − flow_t) / NAV_{t−1} − 1`.

### Why NAV is a table, not a materialized view
A materialized view looked like the obvious choice for "pre-computed NAV". It doesn't work here, for two reasons:
- Postgres **can't put row-level security on a materialized view**, so every manager would see every NAV.
- A refresh would run with no `app.manager_id` set, so under our fail-closed policy it would store **zero rows**.

Instead, `core.nav_daily` and `core.positions` are plain tables with their own RLS policies. The refresh step recomputes them in Python as the admin role.

Migration 004 also creates its views **as** `analytics_owner` (`SET ROLE analytics_owner`) rather than changing the owner afterwards. A new test checks that every view in `analytics` belongs to `analytics_owner`, so a future migration can't quietly break RLS.

### Bugs caught by sanity-checking the output
**Cash ran down to zero.** The first run passed every invariant, but the cash share sat at 0.00% for long stretches, though I'd meant to keep a 1% buffer. The cause was the drift band: names slightly *overweight* kept their extra value (inside the band, so not sold), while new names were bought with whatever cash was left. The fix is that buys never go below the buffer. A test now checks that cash stays between 0.5% and about 3%.

**Two more unadjusted demergers** turned up when I scanned for big drops on quiet market days: GRASIM (2017) and SIEMENS (2025). They were fixed on the phase-1 branch, where they belong, and this branch was rebuilt on top. See the phase 1 notes.

**Missing day in the source:** 2016-01-01 has index bars but no stock bars, although NSE was open. The NAV carries prices forward over it.

### The returns look too good, and why that's expected
| Portfolio | CAGR | Benchmark CAGR |
|---|---|---|
| 101 Banking (from 2021) | 11.4% | 8.9% (NIFTY BANK) |
| 102 Balanced (from 2017) | 17.2% | 11.8% |
| 201 Tech (from 2016) | 16.7% | 12.9% |
| 202 Defensive (from 2019) | 19.4% | 11.6% |
| 203 Consumption (from 2018) | 21.3% | 10.9% |
| 301 PSU & Energy (from 2018) | 23.3% | 12.2% |
| 302 Infrastructure (from 2016) | 19.4% | 11.8% |

CAGR here is time-weighted (deposits and withdrawals removed), computed ad hoc for this check. Phase 3 adds the tested version.

Every portfolio beats its benchmark, which no real fund book does. This isn't a bug. It's **survivorship bias**: the dataset contains only stocks that are in the index *today*, which are by definition the winners of the last decade. To measure it: equal-weighting the 90 current constituents that were already listed in January 2016 returns **20.5% a year against the NIFTY 50's 11.6%**. The synthetic portfolios sit inside that range.

Any answer about "alpha" or "beating the benchmark" must be read in that light. The explainer (phase 5) should say so when it reports outperformance.
