"""Record a real conversation for the page's demo mode (?demo=1).

Usage: python -m app.record_demo   (needs ANTHROPIC_API_KEY; costs a few cents)

The replies are saved exactly as the agent returned them, so demo mode shows genuine output:
nothing is written by hand.
"""

import json
import uuid
from pathlib import Path

from agent.app import open_agent

MANAGER = 2
QUESTIONS = [
    "How has my tech fund done against its benchmark since inception?",
    "Which stocks made it the most money over the last year?",
]
OUT = Path(__file__).parent / "demo_conversation.json"


def main() -> None:
    messages = []
    with open_agent(memory="inmemory") as agent:
        thread = f"demo-{uuid.uuid4().hex[:8]}"
        for q in QUESTIONS:
            reply = agent.ask(thread, MANAGER, q)
            messages += [{"role": "user", "content": q}, {"role": "assistant", "reply": reply}]
    OUT.write_text(json.dumps({"manager": MANAGER, "messages": messages}, indent=1, default=str))
    print(f"Saved {len(messages)} messages to {OUT}")


if __name__ == "__main__":
    main()
