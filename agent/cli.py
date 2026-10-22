"""Chat with the agent in a terminal.  python -m agent.cli --manager 2"""

import argparse
import json
import uuid

from agent.app import open_agent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager", type=int, default=1)
    parser.add_argument("--trace", action="store_true", help="print each step's details")
    args = parser.parse_args()
    thread = f"cli-{args.manager}-{uuid.uuid4().hex[:8]}"
    with open_agent() as agent:
        print(f"Manager {args.manager}, conversation {thread}. Empty line to quit.")
        pending_clarification = False
        while question := input("\nyou> ").strip():
            if pending_clarification:
                reply = agent.resume(thread, args.manager, question)
            else:
                reply = agent.ask(thread, args.manager, question)
            pending_clarification = reply["type"] == "clarify"
            print(f"\nagent> {reply.get('question') or reply['answer']}")
            if args.trace and reply["type"] == "answer":
                print(json.dumps(reply["trace"], indent=1, default=str))


if __name__ == "__main__":
    main()
