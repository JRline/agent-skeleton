"""Check a backend before running the agent on it.

  python smoke_model.py --backend ollama
  python smoke_model.py --backend prod

Sends a few protocol prompts at both efforts and reports latency and whether the
reply parsed as a valid action. Small models often fail here first; that is what
the repair logic in protocol.py is for.
"""
from __future__ import annotations

import argparse
import time

from agent import config
from agent.adapter import get_model
from agent.memory import load_prompt
from agent.protocol import STOP, ProtocolError, parse_action
from agent.tools import TOOLS, render_tools

CASES = [
    "GOAL: Read inputs/sample_capital.csv.\nReply with one JSON object.",
    "GOAL: Compute 12850 / 101500 * 100 using a tool.\nReply with one JSON object.",
    "GOAL: Say hello to the user as your final answer.\nReply with one JSON object.",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=config.BACKEND, choices=["fake", "ollama", "claude", "prod"])
    args = ap.parse_args()
    model = get_model(args.backend)
    system = {"role": "system", "content": load_prompt("system", tools=render_tools())}
    passed = total = 0
    for effort in ("none", "medium"):
        for case in CASES:
            total += 1
            t0 = time.perf_counter()
            raw = model.complete([system, {"role": "user", "content": case}], effort=effort, stop=STOP)
            ms = round((time.perf_counter() - t0) * 1000)
            try:
                action = parse_action(raw, TOOLS)
                passed += 1
                verdict = f"OK   {action.tool or 'final'}"
            except ProtocolError as e:
                verdict = f"FAIL {e}"
            print(f"[{effort:6}] {ms:6} ms  {verdict}\n         reply: {raw.strip()[:160]!r}")
    print(f"\n{passed}/{total} replies followed the protocol without repair.")


if __name__ == "__main__":
    main()
