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
