"""LLM 3, the explainer: turns computed results into a plain-English answer.

It receives the question and a JSON payload of results (metric values with display
strings, formulas, periods, assumptions; SQL rows), and is told to quote those numbers,
never calculate new ones. The output guard then checks that it did.

Model: Claude Sonnet 5.5 (REASONING_MODEL), with server-side refusal fallback.
"""

import json
from dataclasses import dataclass

import anthropic

FALLBACK_BETA = "server-side-fallback-2026-07-01"

RULES = """You explain portfolio analytics to the portfolio manager who owns the portfolios.
You receive their question and a JSON payload of results that were computed by tested
code (metrics) or read from the database (rows). Write the answer.

Rules:
- Answer the question first, in plain English, then add only the context that helps.
  Short: a few sentences, or a compact markdown table when comparing several portfolios
  or listing rows.
- Use ONLY numbers that appear in the payload. Prefer each metric's "display" string.
  Do not calculate anything new: no differences, sums, averages or conversions that
  aren't in the payload. If a comparison number isn't there, describe it in words.
- Say which period a metric covers (period_start to period_end).
- Mention the assumption that matters: the risk-free rate for Sharpe/Sortino, "price
  returns (dividends not included)" for returns. If the payload has a "caveat" (e.g.
  survivorship bias) and you report beating a benchmark, include it in one sentence.
- If a metric has an "error", say it couldn't be computed and why.
- If SQL rows are "truncated", say the list is cut off.
- If something is "unanswerable", explain why in one sentence and suggest a question
  that can be answered.
- Never recommend buying, selling or holding anything, and don't predict the future.
- Money is INR. Use the display strings (lakh / Cr) where given.
- Write for the manager: refer to "the results" or just state the facts. Never mention
  "payload", "JSON", field names or how the system works.
- Talk only about the portfolios in the results. Don't point out that other portfolios
  are missing unless the question asked about them.
"""


@dataclass
class Explainer:
    model: str
    effort: str = "medium"
    client: anthropic.Anthropic | None = None

    def __post_init__(self):
        self.client = self.client or anthropic.Anthropic()
        self._system = [{"type": "text", "text": RULES, "cache_control": {"type": "ephemeral"}}]

    def explain(self, question: str, payload: dict, feedback: str = "") -> str:
        content = (
            f"<question>\n{question}\n</question>\n\n<results>\n"
            f"{json.dumps(payload, default=str, indent=1)}\n</results>"
        )
        if feedback:
            content += f"\n\n{feedback}"
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=self._system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": self.effort},
        )
        if response.stop_reason == "refusal":
            raise ExplainerError("the explainer declined to answer")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise ExplainerError(f"empty explanation (stop_reason={response.stop_reason})")
        return text


class ExplainerError(Exception):
    pass
