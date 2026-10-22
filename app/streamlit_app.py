"""Portfolio desk: ask about your portfolios in plain English.

Run:  streamlit run app/streamlit_app.py

The page is a thin shell around agent.app.Agent. The manager picker stands in for sign-in:
whoever is "signed in" is the manager_id every query runs under, so row-level security, not
this page, decides what data can be seen.
"""

import uuid
from contextlib import ExitStack

import streamlit as st

from agent.app import open_agent
from app.views import answer_body, chart_for, metrics_table, receipt
from config import load_settings
from db.session import agent_session
from metrics.loaders import list_portfolios
from portfolio.mandates import MANAGERS

EXAMPLES = [
    "How have my portfolios done against their benchmarks?",
    "Which of my portfolios had the best Sharpe ratio over the last 3 years?",
    "What's my sector exposure in each portfolio?",
    "Which stocks made the most money over the last year?",
    "Show my deposits and withdrawals since 2024.",
]

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap');
/* Answers read as prose: a text serif with figures that line up. */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"])
  [data-testid="stMarkdownContainer"] :is(p, li, td, th) {
  font-family: "Source Serif 4", Georgia, serif;
  font-size: 1.04rem;
  line-height: 1.62;
  font-variant-numeric: tabular-nums;
}
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] { max-width: 46rem; }
[data-testid="stDataFrame"], table { font-variant-numeric: tabular-nums; }
/* The serif is for the answer only: the receipt, disclaimer and evidence panel stay in the
   UI face. These selectors repeat the answer rule's prefix so they win on specificity. */
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] p:is(.receipt, .disclaimer),
[data-testid="stChatMessage"] [data-testid="stExpander"] [data-testid="stMarkdownContainer"]
  :is(p, li, td, th) {
  font-family: "Source Sans", sans-serif;  /* Streamlit's own UI face */
  font-size: 0.88rem;
  line-height: 1.45;
}
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] p.receipt {
  color: #0F5C63; margin: 0.15rem 0 0.4rem;
}
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] p.disclaimer {
  color: #5B6870; font-size: 0.8rem;
}
/* Example questions read as a list of prompts, not centred buttons. */
[data-testid="stSidebar"] [data-testid="stBaseButton-tertiary"] {
  justify-content: flex-start; text-align: left; padding-left: 0;
}
[data-testid="stSidebar"] [data-testid="stBaseButton-tertiary"] p { text-align: left; }
</style>
"""

st.set_page_config(page_title="Portfolio desk", page_icon=":material/query_stats:", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)


@st.cache_resource(show_spinner="Starting the agent…")
def get_agent():
    """One agent per server process; its Postgres memory connection stays open."""
    stack = ExitStack()
    agent = stack.enter_context(open_agent())
    agent._stack = stack  # keep the context (and the connection) alive with the agent
    return agent


@st.cache_data(ttl=300, show_spinner=False)
def portfolios_for(manager_id: int) -> list[dict]:
    with agent_session(load_settings().agent_database_url, manager_id) as conn:
        return list_portfolios(conn).to_dict("records")


def new_conversation(manager_id: int) -> None:
    st.session_state.conv = {
        "manager": manager_id,
        "thread": f"ui-{manager_id}-{uuid.uuid4().hex}",
        "messages": [],
        "awaiting_reply": False,
    }


def render_reply(reply: dict) -> None:
    st.markdown(answer_body(reply["answer"]))
    st.markdown(f'<p class="receipt">{receipt(reply)}</p>', unsafe_allow_html=True)

    chart = chart_for(reply)
    if chart is not None:
        st.altair_chart(chart, width="stretch")

    metrics, sql = reply.get("metrics"), reply.get("sql")
    if reply.get("status") != "answered":
        return
    with st.expander("How this was answered"):
        tabs = (["Numbers"] if metrics else []) + (["SQL"] if sql else []) + ["Plan", "Steps"]
        for tab, name in zip(st.tabs(tabs), tabs, strict=True):
            with tab:
                if name == "Numbers":
                    st.dataframe(metrics_table(metrics), hide_index=True, width="stretch")
                    for m in metrics:
                        if m.get("table") and m["name"] != "correlation_matrix":
                            st.caption(f"{m['portfolio_name']}: {m['label']}")
                            st.dataframe(m["table"], hide_index=True, width="stretch")
                elif name == "SQL":
                    st.caption(sql.get("explanation", ""))
                    if sql.get("sql"):
                        st.code(sql["sql"], language="sql")
                    if sql.get("rows"):
                        st.dataframe(sql["rows"], hide_index=True, width="stretch")
                        if sql.get("truncated"):
                            st.caption(
                                "Only the first rows are shown; the query hit the row limit."
                            )
                    rejected = [a for a in sql.get("attempts", []) if a.get("error")]
                    for i, a in enumerate(rejected, 1):
                        st.caption(f"Rejected draft {i}: {a['error']}")
                        st.code(a["sql"] or "(empty)", language="sql")
                elif name == "Plan":
                    st.json(reply.get("plan") or {}, expanded=True)
                else:
                    st.json(reply.get("trace") or [], expanded=False)


def ask(question: str) -> None:
    conv = st.session_state.conv
    conv["messages"].append({"role": "user", "content": question})
    agent = get_agent()
    try:
        if conv["awaiting_reply"]:
            reply = agent.resume(conv["thread"], conv["manager"], question)
        else:
            reply = agent.ask(conv["thread"], conv["manager"], question)
    except Exception as e:  # show the cause; the conversation itself is kept
        conv["messages"].append({"role": "error", "content": f"{type(e).__name__}: {e}"})
        conv["awaiting_reply"] = False
        return
    conv["awaiting_reply"] = reply["type"] == "clarify"
    conv["messages"].append({"role": "assistant", "reply": reply})


def render_message(msg: dict) -> None:
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.markdown(msg["content"])
        return
    with st.chat_message("assistant"):
        if msg["role"] == "error":
            st.error(f"The agent stopped with an error ({msg['content']}). Try again or rephrase.")
            return
        reply = msg["reply"]
        if reply["type"] == "clarify":
            st.markdown(reply["question"])
            st.caption("Reply below and I'll carry on.")
            return
        render_reply(reply)
        st.markdown(
            '<p class="disclaimer">Synthetic portfolios on real NSE prices. '
            "Price returns, dividends not included. Not investment advice.</p>",
            unsafe_allow_html=True,
        )


# --- page --------------------------------------------------------------------------------

settings = load_settings()
if not settings.anthropic_api_key:
    st.error("ANTHROPIC_API_KEY isn't set. Add it to .env and restart the app.")
    st.stop()

with st.sidebar:
    manager = st.selectbox("Signed in as", list(MANAGERS), format_func=MANAGERS.get, index=1)
    if "conv" not in st.session_state or st.session_state.conv["manager"] != manager:
        new_conversation(manager)

    st.subheader("Your portfolios")
    for p in portfolios_for(manager):
        st.markdown(f"**{p['name']}**")
        st.caption(
            f"{p['mandate']}. Benchmark {p['benchmark']}. "
            f"Since {p['inception']:%b %Y}. Portfolio {p['portfolio_id']}."
        )

    st.subheader("Try asking")
    for q in EXAMPLES:
        if st.button(q, key=f"ex-{q}", width="stretch", type="tertiary"):
            st.session_state.queued = q

    st.divider()
    if st.button("Start a new conversation", width="stretch"):
        new_conversation(manager)
        st.rerun()
    st.caption(
        "Synthetic portfolios on real NSE prices, 2015 to April 2026. Price returns "
        "only. Not investment advice."
    )

conv = st.session_state.conv
question = st.chat_input(
    "Reply to the question above" if conv["awaiting_reply"] else "Ask about your portfolios"
)
question = question or st.session_state.pop("queued", None)

if not conv["messages"] and not question:
    st.title("Ask about your portfolios")
    st.markdown(
        "Returns, risk, holdings, trades and cash flows for "
        f"{MANAGERS[manager].split()[0]}'s portfolios. Figures come from tested calculations "
        "and checked SQL, and every number in an answer is verified before you see it."
    )

for msg in conv["messages"]:
    render_message(msg)

if question:
    render_message({"role": "user", "content": question})
    with st.chat_message("assistant"), st.spinner("Working it out…"):
        ask(question)
    st.rerun()  # redraw from history, so the new answer renders like every other one
