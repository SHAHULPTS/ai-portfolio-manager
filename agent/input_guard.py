"""Guardrail layer 3a: cheap, deterministic checks before any LLM sees the question.

Deliberately small. The real protection is downstream (the planner classifies intent, the
SQL guard and the database enforce access), so this layer only handles cases that are
certain without understanding language:
- empty or oversized input, and control characters (stripped);
- messages that are themselves SQL write/DDL statements, which get a fixed reply with no
  LLM call at all;
- phrases typical of prompt injection, which are flagged (not blocked: "ignore the cash,
  what's my equity value?" is a fine question) so the trace shows them.

There is no separate LLM classifier. The planner already labels advice requests, other
managers and write requests as out of scope, and a second model call would add cost and
latency without adding a guarantee that the database layer doesn't already give.
"""

import re
from dataclasses import dataclass, field

MAX_QUESTION_CHARS = 2000

SQL_WRITE = re.compile(
    r"^\s*(drop|delete|insert|update|alter|truncate|grant|revoke|create|copy|set)\b", re.I
)
INJECTION_HINTS = re.compile(
    r"ignore (all |any |the )?(previous|prior|above) (instructions|rules)|system prompt"
    r"|you are now|admin mode|developer mode|jailbreak|act as (an? )?(admin|dba|root)",
    re.I,
)
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class InputCheck:
    question: str
    blocked: bool = False
    reason: str = ""  # out-of-scope reason when blocked
    flags: list[str] = field(default_factory=list)


def check_input(raw: str) -> InputCheck:
    question = CONTROL_CHARS.sub("", raw or "").strip()
    if not question:
        return InputCheck(question, blocked=True, reason="off_topic", flags=["empty"])
    if len(question) > MAX_QUESTION_CHARS:
        return InputCheck(
            question[:MAX_QUESTION_CHARS], blocked=True, reason="off_topic", flags=["too_long"]
        )
    if SQL_WRITE.match(question):
        return InputCheck(question, blocked=True, reason="write_request", flags=["sql_statement"])
    flags = ["possible_injection"] if INJECTION_HINTS.search(question) else []
    return InputCheck(question, flags=flags)
