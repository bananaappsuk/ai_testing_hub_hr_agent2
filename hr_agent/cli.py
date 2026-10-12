"""Chat loop for the HR policy agent.

Equivalent of the n8n `When chat message received` trigger.

    python -m hr_agent.cli                               # interactive
    python -m hr_agent.cli "how much leave do I get?"    # one-shot
"""

from __future__ import annotations

import sys

from hr_agent.agent import HRPolicyAgent


def _run_one(agent: HRPolicyAgent, question: str) -> int:
    try:
        print(agent.ask(question))
    except Exception as exc:  # surfaced to the operator, not the model
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    try:
        agent = HRPolicyAgent()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if argv:
        return _run_one(agent, " ".join(argv))

    print(
        f"HR policy agent ({agent.model_name}). "
        "Ctrl-C or an empty line to quit; /reset starts a new conversation."
    )
    while True:
        try:
            question = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not question:
            return 0
        if question == "/reset":
            agent.reset()
            print("New conversation started.")
            continue

        print()
        if _run_one(agent, question) != 0:
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
