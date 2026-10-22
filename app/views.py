"""Pure helpers for the Streamlit page: tables, charts and the answer receipt.

Kept apart from streamlit_app.py so they can be unit-tested without a browser or an LLM.
"""

import altair as alt
import pandas as pd

from agent.output_guard import DISCLAIMER, numbers_in_text

GAIN, LOSS, INK, TEAL = "#1E7A4F", "#A8323E", "#16232B", "#0F5C63"


def answer_body(answer: str) -> str:
    return answer.removesuffix(DISCLAIMER)


def receipt(reply: dict) -> str:
    """One plain sentence on how the answer was produced, shown under every answer."""
    plan = reply.get("plan") or {}
    intent = plan.get("intent")
    if reply.get("status") == "out_of_scope":
        return "Answered with a fixed reply: no data was read."
    if reply.get("status") == "error":
        return "Couldn't be answered."

    parts = []
    metrics = reply.get("metrics") or []
    if metrics:
        names = sorted({m["name"] for m in metrics})
        portfolios = sorted({m["portfolio_id"] for m in metrics})
        parts.append(
            f"{len(names)} metric{'s' * (len(names) != 1)} computed for "
            f"portfolio{'s' * (len(portfolios) != 1)} {', '.join(map(str, portfolios))}"
        )
    sql = reply.get("sql")
    if sql and sql.get("status") == "ok":
        attempts = len(sql.get("attempts", []))
        parts.append(
            f"{sql['row_count']} rows read with a checked SQL query"
            + (f" (after {attempts} attempts)" if attempts > 1 else "")
        )
    elif sql:
        parts.append("the data question couldn't be answered from the database")

    fell_back = any(
        "plain summary" in n for s in reply.get("trace", []) for n in s.get("notes") or []
    )
    figures = len(numbers_in_text(answer_body(reply.get("answer", ""))))
    if fell_back:
        check = "the written explanation couldn't be verified, so this is a plain summary"
    elif figures:
        check = f"all {figures} figures checked against the computed results"
    else:
        check = "no figures to check"
    lead = "; ".join(parts) if parts else f"routed as {intent}"
    return f"{lead[0].upper()}{lead[1:]}. {check[0].upper()}{check[1:]}."


def metrics_table(metrics: list[dict]) -> pd.DataFrame:
    rows = []
    for m in metrics:
        if m.get("unit") == "table" and not m.get("error"):
            continue
        rows.append(
            {
                "Portfolio": m.get("portfolio_name", m.get("portfolio_id")),
                "Metric": m.get("label", m.get("name")),
                "Value": m.get("display")
                or (f"not computed: {m['error']}" if m.get("error") else ""),
                "Period": (
                    f"{m['period_start']} to {m['period_end']}"
                    if m.get("period_start")
                    else f"as of {m.get('period_end', '')}"
                ),
                "Formula": m.get("formula", ""),
            }
        )
    return pd.DataFrame(rows)


def _bars(df: pd.DataFrame, label: str, value: str, title: str, fmt: str, signed=False):
    color = (
        alt.condition(alt.datum[value] >= 0, alt.value(GAIN), alt.value(LOSS))
        if signed
        else alt.value(TEAL)
    )
    return (
        alt.Chart(df, title=title)
        .mark_bar()
        .encode(
            x=alt.X(f"{value}:Q", title=None, axis=alt.Axis(format=fmt, tickCount=5)),
            y=alt.Y(f"{label}:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
            color=color,
            tooltip=[alt.Tooltip(f"{label}:N"), alt.Tooltip(f"{value}:Q", format=fmt)],
        )
        .properties(height=alt.Step(22))
    )


def chart_for(reply: dict):
    """The one chart that best fits this answer, or None. Most specific result first."""
    by_name = {m["name"]: m for m in reply.get("metrics") or [] if m.get("table")}
    if len({m["portfolio_id"] for m in reply.get("metrics") or []}) > 1:
        return _comparison_chart(reply["metrics"])
    if "pnl_contribution" in by_name:
        df = pd.DataFrame(by_name["pnl_contribution"]["table"])
        return _bars(df, "symbol", "pnl", "P&L by stock (INR)", ",.0f", signed=True)
    if "sector_allocation" in by_name:
        df = pd.DataFrame(by_name["sector_allocation"]["table"])
        return _bars(df, "sector", "weight", "Weight by sector", ".0%")
    if "holdings" in by_name:
        df = pd.DataFrame(by_name["holdings"]["table"])
        return _bars(df, "symbol", "weight", "Holdings by weight", ".1%")
    if "monthly_returns" in by_name:
        df = pd.DataFrame(by_name["monthly_returns"]["table"]).tail(36)
        return (
            alt.Chart(df, title="Monthly returns (last 36 months)")
            .mark_bar()
            .encode(
                x=alt.X("month:O", title=None, axis=alt.Axis(labelAngle=-60)),
                y=alt.Y("return:Q", title=None, axis=alt.Axis(format=".0%")),
                color=alt.condition(alt.datum["return"] >= 0, alt.value(GAIN), alt.value(LOSS)),
                tooltip=["month", alt.Tooltip("return:Q", format=".2%")],
            )
        )
    return _sql_chart(reply.get("sql"))


def _comparison_chart(metrics: list[dict]):
    scalar = [
        m for m in metrics if m.get("value") is not None and m.get("unit") in ("%", "ratio", "INR")
    ]
    if not scalar:
        return None
    first = scalar[0]["name"]  # chart the first metric across portfolios
    df = pd.DataFrame(
        [
            {"portfolio": m["portfolio_name"], "value": m["value"]}
            for m in scalar
            if m["name"] == first
        ]
    )
    unit = scalar[0]["unit"]
    fmt = {"%": ".1%", "ratio": ".2f", "INR": ",.0f"}[unit]
    if first == "pnl_contribution":  # its label says "by stock"; across portfolios it's totals
        title = "Total P&L by portfolio (INR)"
    else:
        title = scalar[0]["label"] + (" (INR)" if unit == "INR" else "")
    return _bars(df, "portfolio", "value", title, fmt, signed=True)


def _sql_chart(sql: dict | None):
    """A line over time when the rows have one date column and a numeric column."""
    if not sql or sql.get("status") != "ok" or len(sql.get("rows") or []) < 3:
        return None
    df = pd.DataFrame(sql["rows"])
    dates = [c for c in df.columns if c in ("date", "trade_date")]
    numeric = [
        c
        for c in df.columns
        if pd.api.types.is_numeric_dtype(df[c]) and c not in ("portfolio_id", "txn_id", "quantity")
    ]
    if not dates or not numeric or df[dates[0]].nunique() < 3:
        return None
    df[dates[0]] = pd.to_datetime(df[dates[0]])
    color = (
        alt.Color("portfolio_id:N", title="Portfolio") if "portfolio_id" in df else alt.value(TEAL)
    )
    return (
        alt.Chart(df, title=numeric[0].replace("_", " ").capitalize())
        .mark_line(point=True)
        .encode(
            x=alt.X(f"{dates[0]}:T", title=None),
            y=alt.Y(f"{numeric[0]}:Q", title=None),
            color=color,
            tooltip=list(df.columns),
        )
    )
