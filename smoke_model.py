"""Check a backend before running the agent on it.

  python smoke_model.py --backend ollama
  python smoke_model.py --backend prod

Sends a few prompts with the agent's tools at both efforts, through the same chat model the agent uses
(agent/chat_model.py), and reports latency, what came back and how many repairs it took. Small models often fail
here first; that is what the repairs are for.
"""
from __future__ import annotations

import argparse
import time

from agent import config
from agent.adapter import get_model
from agent.chat_model import TextChatModel
from agent.graph import system_prompt
from agent.lc_tools import lc_tools
from agent.plugins import basic_toolkits
from agent.tools import enabled_tools
from langchain_core.messages import HumanMessage, SystemMessage

CASES = [
    "GOAL: Read inputs/sample_capital.csv.",
    "GOAL: Compute 12850 / 101500 * 100 using a tool.",
    "GOAL: Say hello to the user as your final answer.",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=config.BACKEND, choices=config.BACKENDS)
    args = ap.parse_args()
    toolkits = basic_toolkits()
    tools = [t for t in lc_tools() if t.name in enabled_tools(toolkits)]
    system = SystemMessage(system_prompt(toolkits))
    passed = clean = total = 0
    for effort in ("none", "medium"):
        model = TextChatModel(chat=get_model(args.backend)).for_role("executor", effort).bind_tools(tools)
        for case in CASES:
            total += 1
            t0 = time.perf_counter()
            try:
                msg = model.invoke([system, HumanMessage(case)])
                repairs = msg.response_metadata.get("repairs", 0)
                passed += 1
                clean += repairs == 0
                verdict = f"OK   {msg.tool_calls[0]['name'] if msg.tool_calls else 'final'} (repairs: {repairs})"
                reply = msg.response_metadata.get("raw", "")
            except Exception as e:
                verdict, reply = f"FAIL {e}", ""
            ms = round((time.perf_counter() - t0) * 1000)
            print(f"[{effort:6}] {ms:6} ms  {verdict}\n         reply: {reply.strip()[:160]!r}")
    print(f"\n{passed}/{total} replies usable; {clean}/{total} followed the protocol without repair.")


if __name__ == "__main__":
    main()
