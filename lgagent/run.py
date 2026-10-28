"""Run the LangGraph agent from the command line, or from code (eval) with answer callbacks.

  python -m lgagent.run --backend claude "What is DemoBank's latest total RWA usage?"
  python -m lgagent.run --backend claude --yes "..."             # approve every request
  python -m lgagent.run --backend claude --resume RUN_ID         # continue a paused run (another process is fine)

A pause (approval, question, suggestion) is a LangGraph interrupt; the run is saved in runs/langgraph.sqlite and
continues with Command(resume=...). Callbacks get the interrupt value and return the answer:
  approve(value) -> True | "reason for rejecting";  ask(value) -> str;  choose(value) -> {"toolkits", "skill"}
"""
from __future__ import annotations

import argparse
import sys
from typing import Callable

from langgraph.types import Command

from agent import config
from agent.memory import new_run_id

from .chat_model import from_backend
from .graph import build, sqlite_checkpointer
from .tools import JsonlLog


def _answer(value: dict, approve: Callable, ask: Callable, choose: Callable):
    kind = value.get("type")
    if kind == "question":
        return ask(value)
    if kind == "suggestion":
        return choose(value)
    verdict = approve(value)                          # tool_approval (HumanInTheLoopMiddleware, per call)
    return {"type": "approve"} if verdict is True else {"type": "reject", "message": f"USER REJECTED: {verdict}"}


def run(goal: str | None, model, inputs=None, toolkits=None, skill=None, approve=None, ask=None, choose=None,
        thread_id: str | None = None, graph=None, max_steps: int | None = None) -> dict:
    """Run until finished, answering every pause through the callbacks. Returns the final graph state."""
    rid = thread_id or new_run_id()
    graph = graph or build(model, sqlite_checkpointer(), max_steps=max_steps)
    cfg = {"configurable": {"thread_id": rid}, "callbacks": [JsonlLog(rid)], "recursion_limit": 300}
    payload = None if goal is None else {"goal": goal, "inputs": inputs or [], "toolkits": toolkits or [],
                                         "skill": skill}
    approve = approve or (lambda v: True)
    ask = ask or (lambda v: "Yes, confirmed: go ahead with your proposal.")
    choose = choose or (lambda v: {"toolkits": v.get("toolkits", []), "skill": v.get("skill")})
    if payload is not None:
        graph.invoke(payload, cfg)
    while (snap := graph.get_state(cfg)).interrupts:
        answers = {i.id: _answer(i.value, approve, ask, choose) for i in snap.interrupts}
        graph.invoke(Command(resume=answers), cfg)
    return {**snap.values, "run_id": rid}


def _ask_cli(value):
    return input(f"\nAgent asks: {value['question']}\n> ")


def _approve_cli(value):
    print(f"\n{value.get('name')} wants to run:\n{value.get('args')}\n{value.get('description', '')}")
    if input("Approve? [y/N] ").strip().lower() == "y":
        return True
    return input("Reason for rejecting: ").strip() or "rejected without a reason"


def _choose_cli(value):
    print(f"\nSuggestion: toolkits {value.get('toolkits')}, skill {value.get('skill')}: {value.get('why')}")
    return value if input("Accept? [y/N] ").strip().lower() == "y" else {"toolkits": [], "skill": None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("goal", nargs="?")
    ap.add_argument("--backend", default=config.BACKEND if config.BACKEND != "fake" else "claude")
    ap.add_argument("--yes", action="store_true", help="approve every request and accept suggestions")
    ap.add_argument("--resume", help="run id of a paused run")
    ap.add_argument("--skill")
    args = ap.parse_args()
    if not (args.goal or args.resume):
        ap.error("give a goal or --resume RUN_ID")
    cb = {} if args.yes else {"approve": _approve_cli, "ask": _ask_cli, "choose": _choose_cli}
    state = run(None if args.resume else args.goal, from_backend(args.backend), skill=args.skill,
                thread_id=args.resume, **cb)
    print(f"\n[{state.get('status')}] run {state['run_id']}\n\n{state.get('final', '')}")
    for w in state.get("warnings", []):
        print(f"warning: {w}")
    return 0 if state.get("status") == "finished" else 1


if __name__ == "__main__":
    sys.exit(main())
