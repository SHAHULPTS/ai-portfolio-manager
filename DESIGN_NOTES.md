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

---

## Phase 3: Metric engine

### Why metrics are code, not LLM output
A Sharpe ratio from LLM-written SQL could be off by a missed cash flow or a wrong join and still look perfectly plausible. So every number the agent reports comes from a plain, unit-tested Python function. The LLM's only job here is to pick metric **names** from a fixed catalogue. Phase 5 gives the planner `CATALOG`'s keys as an enum, so it can't ask for something that doesn't exist.

### How it's laid out
| File | What it holds |
|---|---|
| `metrics/formulas.py` | The maths, as pure functions of daily-return series. No database. |
| `metrics/holdings.py` | Sector allocation, concentration (HHI), P&L by stock. |
| `metrics/catalog.py` | 27 named metrics. Each declares the inputs it needs and returns a `MetricResult`. |
| `metrics/loaders.py` | Fixed, parameterised SQL. No LLM writes any of it. |
| `metrics/engine.py` | `run(...)`, the single entry point the agent calls. |
| `metrics/periods.py` | Turns `ytd`, `3y`, `since_inception` or explicit dates into one `(start, end)`. |

### A result explains itself
A `MetricResult` carries:
- the value and its unit
- the **formula in words**
- the exact period and the number of trading days in it
- the **assumptions** (risk-free 6.5%, 252-day annualisation, "price returns, dividends not included")
- any extra numbers, such as drawdown peak, trough and recovery dates, or VaR in rupees

The explainer in phase 5 only *restates* these. It never computes anything, and the output guard checks every number it writes against the result.

### Conventions, and why
- **Time-weighted returns:** `r_t = (NAV_t − flow_t) / NAV_{t−1} − 1`. A ₹50 lakh deposit is not a gain, and a withdrawal is not a loss. Drawdown runs on the time-weighted wealth index for the same reason; on raw NAV, a deposit could hide a crash.
- **Period boundaries:** "2025" means returns for trading days in 2025, the first measured from the 31 December 2024 close. Every metric in one answer uses the same resolved window.
- **CAGR uses calendar time.** At first I used empyrical's trading-day version, `(1 + R)^(252/n) − 1`. Then I counted: **NSE averages 247.8 trading days a year (2016–2025), not 252**. That overstated a 21% CAGR by about 0.4 points, and the Banking portfolio showed 11.6% instead of 11.4%. Reported CAGR is now `(1 + R)^(365.25/days) − 1`, which doesn't depend on how many days the market was open. Volatility and Sharpe keep the standard √252 scaling: the √248 difference is under 1% relative, and 252 is the convention every reference uses.
- **VaR and CVaR** are 1-day, at 95%, reported as positive losses and in rupees on the current NAV. The parametric version notes that a normal distribution understates fat tails.
- **Jensen's alpha** is `(1 + mean((r_p − r_f) − β(r_b − r_f)))^252 − 1`. A test with a portfolio that is exactly 2× the benchmark comes out to alpha = r_f. That's correct: leverage with no borrowing cost earns the risk-free rate as "alpha". It's a good reminder that alpha measures performance *relative to the CAPM line*, not skill.
- **Holdings are rebuilt as of the period end** from transactions, using the same accounting module that filled the database. So "what did I hold at the end of 2023?" works. A test confirms that at the latest date this matches `v_positions` exactly.
- **P&L by stock** is `end value − start value − net purchases`. A test proves the per-stock amounts sum to the portfolio's gain (NAV change minus flows) over any window. Nothing is lost or double-counted.

### Testing the maths three ways
1. **By hand:** each formula has a case worked out on paper. Examples: a ₹50 deposit inside a ₹110 → ₹160 NAV move is a 0% return; a 1.10 → 0.88 wealth path is a −20% drawdown; 21% over exactly two years is a 10% CAGR.
2. **Against empyrical:** on four years of fat-tailed, correlated random returns, our numbers agree with the independent `empyrical-reloaded` library to **1e-9** for all 12 overlapping metrics. It's a dev-only dependency, and the app never imports it.
3. **End to end:** a second test database holds a simulated book built by the real simulator and refresh code. All 27 metrics compute through the RLS-protected agent role. Asking for another manager's portfolio raises `PortfolioNotFound`, the same error as for a portfolio that doesn't exist, so the error itself reveals nothing.

### Security: the metric path obeys RLS too
The loaders connect as `agent_ro` with `app.manager_id` set, exactly like the LLM's SQL will in phase 4. There's no admin-connection shortcut that a bug in phase 5 could misuse to read another manager's data.

### Sanity check on the real book
All 27 metrics for all 7 portfolios compute in 0.5 s with no warnings, and the numbers match real market history:
- The five portfolios started before 2020 hit their worst trough on 2020-03-23/24, the actual NIFTY Covid low.
- Betas follow the mandates: Defensive 0.58, Tech 0.75, Infrastructure 1.09.
- Sharpe ratios are 0.34–0.80, and 1-day 95% VaR is 1.4–2.2%.

---

## Phase 4: Text-to-SQL with guardrails

### The path
`question → SQL writer (Claude) → guard (sqlglot) → executor (agent_ro + RLS) → rows`

If the guard or the database rejects a draft, the writer gets the exact reason and tries again, up to 3 attempts. That loop lives in `agent/sql/pipeline.py` as plain code. Phase 5 wraps the same steps in LangGraph nodes, but having the loop here first means its security can be tested without a graph or an LLM.

### Calling Claude
- **The official `anthropic` SDK, not a LangChain wrapper.** LangGraph orchestrates plain Python functions, so a node can call the SDK directly. That gives one less abstraction to debug, and the newest API features arrive with no waiting.
- **Structured output.** `client.beta.messages.parse(..., output_format=SqlDraft)` returns a validated Pydantic object (`answerable`, `sql`, `explanation`). On current models, forcing a tool call returns a 400, so this is the supported way to get JSON. `SqlDraft` forbids extra fields, so the schema is strict.
- **Prompt caching.** The system prompt holds the rules, the live schema and the 12 worked examples, and it is byte-identical on every call, marked `cache_control`. Everything that varies goes in the user message: the manager's portfolios, the data's last date, the question, any previous error. A test asserts that two different questions produce identical system prompts, because one stray timestamp would silently break caching. At about 2,000 tokens it is above Sonnet 5.5's 512-token minimum.
- **Refusals.** `fallbacks="default"` lets the API retry certain safety-classifier declines on another model. A decline that survives raises `WriterRefused` instead of passing on an empty answer.
- **Model and effort.** The model is `claude-sonnet-5-5`, as in the plan (configurable via `REASONING_MODEL`). Effort is `medium`: SQL drafting is short and well specified, and the retry loop catches what lower effort misses. Configurable via `SQL_WRITER_EFFORT`.
- **The writer may say no.** Questions about returns, Sharpe, drawdown and so on get `answerable=false`, because those belong to the metric engine. Writing them in SQL is exactly the risk phase 3 removed.
- **Dates come from the data, not the clock.** The data ends on 2026-04-08 but today is later, so "this year" must be measured from the data's last date, which goes in every request. One of my own worked examples used `current_date`. Examples teach more strongly than rules, so I fixed it, and a test now fails if any example uses `current_date` or `now()`.

### The schema the LLM sees is the schema the guard enforces
`agent/sql/schema.py` reads the `analytics` views and their `COMMENT`s from the live catalog, as `agent_ro`. That one object produces both the prompt text and the column map the guard checks against, so the prompt can't describe a column the guard rejects, or the other way round.

### Layer 2: the guard (`agent/sql/guard.py`)
It checks sqlglot's **parse tree**, not the text, so casing, comments and whitespace tricks don't help an attacker. A query passes only if:

1. It parses as **exactly one** PostgreSQL statement, under 5,000 characters.
2. It is a **query**: a SELECT, or a UNION of SELECTs. There must be no INSERT, UPDATE, DELETE, DROP, `SELECT INTO`, `FOR UPDATE`, COPY, SET or GRANT anywhere in the tree, and no recursive CTEs.
3. Every table is an `analytics` view (unqualified names get `analytics.` added) or a CTE defined in the query. No `core.*`, `pg_catalog` or `information_schema`.
4. **Every column exists**, checked by sqlglot's qualifier against the live schema. A typo then comes back as "Column 'weightt' could not be resolved" rather than a vague database error, and the writer fixes it on the next attempt.
5. No **denied functions**: `set_config`, `current_setting`, anything `pg_*`, `lo_*`, `dblink*`, sequence functions, and `query_to_xml` and friends.
6. A **LIMIT** of at most 500. It's added if missing, capped if too high, and rejected if it isn't a plain number.

Its rejection messages are written for the LLM to act on. For example, "table core.positions is not allowed; use only: analytics.v_cash_flows, …".

### Layer 1 again, plus the executor's own checks
The guard is the helpful layer, not the final one. The executor (`agent/sql/executor.py`) adds:
- **Prepared statements.** Postgres refuses more than one command in a prepared statement, so even a guard bug can't smuggle in `; DELETE …`.
- **EXPLAIN first.** A plan whose estimated cost is over 2,000,000 is refused before it runs. A cartesian join of the price history estimates at 1.1 *billion*; the 12 worked examples all estimate under 900.
- **A per-transaction `statement_timeout`** of 3 s.
- **At most 500 rows,** with a `truncated` flag.
- **Clean errors.** Database errors are translated into short, safe messages for the retry loop.

Both the executor and the metric loaders open connections through `db/session.py`. That makes it the single place where `app.manager_id` is set, and nothing in the agent holds an admin connection.

### What the tests show (`tests/test_sql_*.py`)
- **34 attacks are blocked by the guard**, each with a reason. They include stacked statements, comment-hidden DROPs, `UNION` into `pg_shadow`, `set_config` inside a CTE, `pg_sleep` inside a WHERE clause, and `SELECT INTO`.
- **Every worked example passes the guard and runs.**
- **With the guard deliberately bypassed,** the database layer still refuses stacked statements, writes, base-table reads, slow queries and expensive plans, and a test confirms nothing was written.
- **A scripted "compromised model"** that only writes attacks fails after 3 attempts and changes nothing.
- **Prompt injection that fully fools the model:** the model "obeys" an injected instruction and drops the portfolio filter, and RLS still returns only the signed-in manager's portfolios. This is the defence-in-depth argument in one test.
- **The writer's request** is checked against a fake client: the model, the structured output, caching, fallbacks, retry context, and refusal handling.

### Not yet verified
No Anthropic credentials were available while building this, so **the live model call hasn't run yet**. Everything around it is tested: request shape, retries and security. To try it, set `ANTHROPIC_API_KEY` in `.env`, then run:
```bash
python -m agent.sql.cli "Which of my holdings are showing a loss?" --manager 2
```
Phase 7's evals measure how often the real model gets questions right.
