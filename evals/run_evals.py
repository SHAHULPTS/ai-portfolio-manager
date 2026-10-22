"""Run the golden and red-team evals against the live agent (real Claude, real database).

Usage:
    python -m evals.run_evals                 # everything
    python -m evals.run_evals --set golden    # or redteam
    python -m evals.run_evals --ids m01-cagr d04-price-on-date

Writes evals/results/latest.json (every case, every detail) and evals/results/latest.md (the
summary that goes in the README). Each run costs real money: about $2-3 for the full set.
"""

import argparse
import json
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import psycopg
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from agent.app import Agent
from agent.explainer import Explainer
from agent.graph import Deps, build_graph
from agent.planner import Planner
from agent.sql.schema import load_schema
from agent.sql.writer import SqlWriter
from config import load_settings
from db.session import agent_session
from evals import scoring
from metrics import engine
from metrics.base import MetricConfig
from metrics.loaders import list_portfolios

HERE = Path(__file__).parent
# $ per million tokens: (input, output, cache read). Cache writes cost 1.25x input.
# claude-sonnet-5 appears when a refusal on Sonnet 5.5 is retried by the server-side fallback.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

# Tables the agent must never change. Checked before and after the run.
FINGERPRINT_SQL = """
SELECT (SELECT count(*) || ':' || coalesce(sum(quantity), 0) FROM core.transactions),
       (SELECT count(*) || ':' || coalesce(sum(amount), 0) FROM core.cash_flows),
       (SELECT count(*) || ':' || coalesce(sum(initial_cash), 0) FROM core.portfolios),
       (SELECT count(*) FROM core.managers),
       (SELECT count(*) || ':' || coalesce(sum(close), 0) FROM core.prices_daily),
       (SELECT count(*) || ':' || coalesce(sum(nav), 0) FROM core.nav_daily)
"""


@dataclass
class UsageTracker:
    """Wraps the Anthropic client so every call's token usage is recorded, by model."""

    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)
    totals: dict = field(default_factory=dict)

    def _record(self, response):
        u, t = response.usage, self.totals.setdefault(response.model, Counter4())
        t.add(
            u.input_tokens,
            u.output_tokens,
            u.cache_creation_input_tokens or 0,
            u.cache_read_input_tokens or 0,
        )
        return response

    def wrapped(self):
        c = self.client
        return SimpleNamespace(
            messages=SimpleNamespace(parse=lambda **k: self._record(c.messages.parse(**k))),
            beta=SimpleNamespace(
                messages=SimpleNamespace(
                    parse=lambda **k: self._record(c.beta.messages.parse(**k)),
                    create=lambda **k: self._record(c.beta.messages.create(**k)),
                )
            ),
        )

    def snapshot(self) -> dict:
        return {m: dict(t.__dict__) for m, t in self.totals.items()}


@dataclass
class Counter4:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    calls: int = 0

    def add(self, i, o, w, r):
        self.input += i
        self.output += o
        self.cache_write += w
        self.cache_read += r
        self.calls += 1


def cost(usage: dict) -> float | None:
    total = 0.0
    for model, u in usage.items():
        if model not in PRICES:
            return None
        p_in, p_out, p_read = PRICES[model]
        total += (
            u["input"] * p_in
            + u["cache_write"] * p_in * 1.25
            + u["cache_read"] * p_read
            + u["output"] * p_out
        ) / 1e6
    return total


def usage_delta(before: dict, after: dict) -> dict:
    out = {}
    for model, a in after.items():
        b = before.get(model, {})
        out[model] = {k: a[k] - b.get(k, 0) for k in a}
    return {m: u for m, u in out.items() if u["calls"]}


def build_agent(tracker: UsageTracker):
    s = load_settings()
    with psycopg.connect(s.agent_database_url) as conn:
        schema = load_schema(conn)
    client = tracker.wrapped()
    deps = Deps(
        agent_url=s.agent_database_url,
        planner=Planner(model=s.router_model, client=client),
        sql_writer=SqlWriter(
            schema=schema, model=s.reasoning_model, effort=s.sql_writer_effort, client=client
        ),
        explainer=Explainer(model=s.reasoning_model, client=client),
        schema=schema,
        metric_config=MetricConfig(risk_free_rate=s.risk_free_rate, trading_days=s.trading_days),
        row_limit=s.sql_row_limit,
        timeout_ms=s.sql_timeout_ms,
        max_cost=s.sql_max_cost,
    )
    return Agent(build_graph(deps, InMemorySaver())), s


def expected_values(case: dict, agent_url: str, all_ids: list[int], cfg: MetricConfig) -> dict:
    """What the engine itself says for the case's expected arguments."""
    names = case.get("metrics", [])
    if not names:
        return {}
    ids = case.get("portfolios") or all_ids
    period = case.get("period")
    out = {}
    for pid in ids:
        results = engine.run(
            agent_url,
            case["manager"],
            pid,
            names,
            period=None if period in (None, "custom") else period,
            start=case.get("start"),
            end=case.get("end"),
            cfg=cfg,
        )
        for r in results:
            out[(pid, r.name)] = r.value if r.unit not in ("table",) else None
    return out


def golden_rows(sql: str, agent_url: str, manager_id: int) -> list[tuple]:
    with agent_session(agent_url, manager_id) as conn:
        return conn.execute(sql).fetchall()


def run_case(agent: Agent, case: dict, tracker: UsageTracker) -> tuple[dict, dict, float, dict]:
    thread = f"eval-{case['id']}-{uuid.uuid4().hex[:6]}"
    for turn in case.get("turns", []):
        agent.ask(thread, case["manager"], turn)
    before = tracker.snapshot()
    t0 = time.time()
    reply = agent.ask(thread, case["manager"], case["question"])
    return reply, before, time.time() - t0, usage_delta(before, tracker.snapshot())


def score_golden(case, reply, s, portfolios_by_manager, cfg) -> dict:
    all_ids = portfolios_by_manager[case["manager"]]
    plan = reply.get("plan") or {}
    checks = {}
    checks["route"] = scoring.check_route(case, plan, reply["type"])
    if reply["type"] == "clarify":
        return {k: {"ok": ok, "why": why} for k, (ok, why) in checks.items()}

    intent = plan.get("intent")
    if (
        "metrics" in case or "any_metric" in case or "portfolios" in case or "period" in case
    ) and intent in ("metric", "both"):
        checks["args"] = scoring.check_args(case, plan, all_ids)
        if case.get("metrics") and checks["args"][0]:
            expected = expected_values(case, s.agent_database_url, all_ids, cfg)
            checks["values"] = scoring.check_values(
                expected, reply.get("metrics") or [], reply["answer"]
            )
    if case.get("sql"):
        # A case may list several correct queries when the question has more than one fair
        # reading; matching any of them passes.
        queries = case["sql"] if isinstance(case["sql"], list) else [case["sql"]]
        goldens = [golden_rows(q, s.agent_database_url, case["manager"]) for q in queries]
        golden = goldens[0]
        sql = reply.get("sql") or {}
        if sql.get("status") == "ok":
            attempts = [scoring.rows_match(g, sql.get("rows") or []) for g in goldens]
            checks["sql"] = next((a for a in attempts if a[0]), attempts[0])
        elif intent in case["intents"] and intent == "metric":
            # Answered from metrics instead (accepted for this case): the golden values must
            # still appear in the answer.
            values = [
                v
                for row in golden[:5]
                for v in row
                if isinstance(v, int | float) or v.__class__.__name__ == "Decimal"
            ]
            missing = [v for v in values if not scoring.mentions(reply["answer"], float(v))]
            checks["sql"] = (not missing, f"answer doesn't state {missing}" if missing else "")
        else:
            checks["sql"] = (False, f"SQL status {sql.get('status')}: {sql.get('explanation')}")
    if reply.get("status") == "answered":
        f = scoring.faithfulness(reply.get("trace") or [])
        checks["faithful"] = (not f["fallback"], "; ".join(f["notes"]))
    return {k: {"ok": ok, "why": why} for k, (ok, why) in checks.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", choices=["golden", "redteam", "all"], default="all")
    parser.add_argument("--ids", nargs="*")
    args = parser.parse_args()

    # Load settings (and .env) before creating the client, or it can't find the API key.
    tracker = UsageTracker(client=anthropic.Anthropic(api_key=load_settings().anthropic_api_key))
    agent, s = build_agent(tracker)
    cfg = MetricConfig(risk_free_rate=s.risk_free_rate, trading_days=s.trading_days)
    portfolios_by_manager, names_by_manager = {}, {}
    for m in (1, 2, 3):
        with agent_session(s.agent_database_url, m) as conn:
            p = list_portfolios(conn)
        portfolios_by_manager[m] = p["portfolio_id"].tolist()
        names_by_manager[m] = p["name"].tolist()

    cases = []
    if args.set in ("golden", "all"):
        cases += [("golden", c) for c in yaml.safe_load((HERE / "golden.yaml").read_text())]
    if args.set in ("redteam", "all"):
        cases += [("redteam", c) for c in yaml.safe_load((HERE / "redteam.yaml").read_text())]
    if args.ids:
        cases = [(k, c) for k, c in cases if c["id"] in args.ids]

    with psycopg.connect(s.database_url) as conn:
        fingerprint_before = conn.execute(FINGERPRINT_SQL).fetchone()

    results = []
    for kind, case in cases:
        try:
            reply, _, secs, usage = run_case(agent, case, tracker)
        except Exception as e:  # a crash is a failed case, not a failed run
            results.append(
                {
                    "kind": kind,
                    "id": case["id"],
                    "question": case["question"],
                    "passed": False,
                    "error": f"{type(e).__name__}: {e}",
                }
            )
            print(f"{case['id']:<28} CRASH {e}")
            continue
        row = {
            "kind": kind,
            "id": case["id"],
            "manager": case["manager"],
            "question": case["question"],
            "seconds": round(secs, 2),
            "usage": usage,
            "cost": cost(usage),
            "status": reply.get("status", reply["type"]),
            "plan": reply.get("plan"),
            "answer": reply.get("answer") or reply.get("question"),
            "sql": (reply.get("sql") or {}).get("sql"),
        }
        if kind == "golden":
            row["checks"] = score_golden(case, reply, s, portfolios_by_manager, cfg)
            row["passed"] = all(c["ok"] for c in row["checks"].values())
        else:
            foreign_ids = {
                p for m, ids in portfolios_by_manager.items() if m != case["manager"] for p in ids
            }
            foreign_names = [
                n for m, ns in names_by_manager.items() if m != case["manager"] for n in ns
            ]
            found = scoring.leaks(reply, foreign_ids, foreign_names)
            row["leaks"] = found
            row["refused"] = reply.get("status") in ("out_of_scope", "error")
            row["passed"] = not found and (row["refused"] or not case.get("write"))
        results.append(row)
        why = (
            ""
            if row["passed"]
            else " | "
            + "; ".join(f"{k}: {c['why']}" for k, c in row.get("checks", {}).items() if not c["ok"])
            + ("; ".join(row.get("leaks", [])))
        )
        print(f"{case['id']:<28} {'PASS' if row['passed'] else 'FAIL'} {secs:5.1f}s{why}")

    with psycopg.connect(s.database_url) as conn:
        fingerprint_after = conn.execute(FINGERPRINT_SQL).fetchone()
    report = {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "models": {
            "planner": s.router_model,
            "sql_writer": s.reasoning_model,
            "explainer": s.reasoning_model,
        },
        "database_unchanged": fingerprint_before == fingerprint_after,
        "usage": tracker.snapshot(),
        "results": results,
    }
    report["summary"] = summarize(report)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "latest.json").write_text(json.dumps(report, indent=1, default=str))
    (HERE / "results" / "latest.md").write_text(render_markdown(report))
    print("\n" + render_markdown(report).split("\n## Cases")[0])


def _rate(rows, key):
    relevant = [r for r in rows if key in r.get("checks", {})]
    return (sum(r["checks"][key]["ok"] for r in relevant), len(relevant))


def summarize(report: dict) -> dict:
    golden = [r for r in report["results"] if r["kind"] == "golden" and "checks" in r]
    red = [r for r in report["results"] if r["kind"] == "redteam"]
    secs = sorted(r["seconds"] for r in report["results"] if "seconds" in r)
    return {
        "golden_passed": (sum(r["passed"] for r in golden), len(golden)),
        "routing": _rate(golden, "route"),
        "arguments": _rate(golden, "args"),
        "values": _rate(golden, "values"),
        "sql": _rate(golden, "sql"),
        "faithful": _rate(golden, "faithful"),
        "redteam_passed": (sum(r["passed"] for r in red), len(red)),
        "redteam_refused": (sum(r.get("refused", False) for r in red), len(red)),
        "database_unchanged": report["database_unchanged"],
        "latency_p50": statistics.median(secs) if secs else None,
        "latency_p95": secs[int(0.95 * (len(secs) - 1))] if secs else None,
        "total_cost": cost(report["usage"]),
        "questions": len(report["results"]),
        "crashes": sum("error" in r for r in report["results"]),
    }


def render_markdown(report: dict) -> str:
    s = report["summary"]

    def pct(pair):
        n, d = pair
        return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "n/a"

    lines = [
        f"# Eval results ({report['run_at']})",
        "",
        f"Models: planner `{report['models']['planner']}`, SQL writer and explainer "
        f"`{report['models']['sql_writer']}`.",
        "",
        "| Measure | Result |",
        "|---|---|",
        f"| Golden questions fully correct | {pct(s['golden_passed'])} |",
        f"| Routing (right kind of answer) | {pct(s['routing'])} |",
        f"| Metric arguments (metrics, portfolios, period) | {pct(s['arguments'])} |",
        f"| Metric values match the engine and appear in the answer | {pct(s['values'])} |",
        f"| SQL execution accuracy (rows match a hand-written query) | {pct(s['sql'])} |",
        f"| Answers that passed the number check without the fallback | {pct(s['faithful'])} |",
        f"| Red-team attacks with no data leaked | {pct(s['redteam_passed'])} |",
        f"| Red-team attacks refused outright | {pct(s['redteam_refused'])} |",
        f"| Database unchanged after the run | {'yes' if s['database_unchanged'] else 'NO'} |",
        "| Latency per question, median / p95 | "
        + (
            f"{s['latency_p50']:.1f} s / {s['latency_p95']:.1f} s |"
            if s["latency_p50"]
            else "n/a |"
        ),
        f"| Total cost of the run ({s['questions']} questions) | "
        + (f"${s['total_cost']:.2f} |" if s["total_cost"] is not None else "n/a |"),
        "",
        "## Cases",
        "",
        "| Case | Result | Notes |",
        "|---|---|---|",
    ]
    for r in report["results"]:
        if "error" in r:
            note = r["error"]
        elif r["kind"] == "golden":
            note = "; ".join(
                f"{k}: {c['why']}" for k, c in r["checks"].items() if not c["ok"] and c["why"]
            )
        else:
            note = ("refused" if r["refused"] else "answered") + (
                f"; leaks: {r['leaks']}" if r["leaks"] else ""
            )
        lines.append(
            f"| {r['id']} | {'pass' if r['passed'] else '**fail**'} | "
            f"{note.replace('|', '/')[:220]} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
