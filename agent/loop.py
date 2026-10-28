"""Conversation driver over the LangGraph agent (docs/design.md §8).

status: planning -> running -> (awaiting_approval | awaiting_user | awaiting_choice -> running)* -> finished | aborted

The graph (graph.py) does the work and LangGraph saves it after every node (runs/checkpoints.sqlite, thread = run
id). This module advances it one step at a time and keeps the conversation record that the UI, export, replay and
eval read (runs/<id>/state.json: status, plan, facts, the pending approval or question, answers, events):

- `run_one_step(state, model)` runs the graph to its next breakpoint (plan, tool call, answer check) or pause
  (approval, question, suggestion), records what happened as events, persists, returns the last event.
- `resolve_pending(state, ...)` records the human's answer; an approved request runs at once, anything else resumes
  with the next step.
- `continue_with` (a follow-up instruction in the same thread), `rename`, `abort`, `load_state`, `new_state`, `run`.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Callable

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from . import config
from .checks import (_previous_request, _remember_results, _result_tables, _retitle, answer_timing,  # noqa: F401
                     cited_outputs, describe_timing, uncited_numbers)
from .graph import build, intro
from .lc_tools import ModelLog, next_ref
from .memory import add_fact, context_block, default_title, earlier_tasks, load_state, log, new_state, persist  # noqa: F401
from .skills import get_skill

__all__ = ["new_state", "load_state", "run_one_step", "resolve_pending", "abort", "run", "continue_with", "rename"]

WAITING = ("awaiting_approval", "awaiting_user", "awaiting_choice")
DONE = ("finished", "aborted")

_GRAPHS: dict[tuple, object] = {}       # (model, runs folder) -> compiled graph
_ACTIVE: dict[str, object] = {}         # run id -> the graph that last ran it (an approval runs on it at once)
_LOCK = threading.Lock()


def pending_question(state: dict) -> str:
    """The question text of a pending ask_user."""
    return ((state.get("pending") or {}).get("input") or {}).get("question", "")


def event(state: dict, kind: str, **payload) -> dict:
    return {"kind": kind, "step": state["step_count"],
            "ts": datetime.now().isoformat(timespec="seconds"), "payload": payload}


def checkpointer() -> SqliteSaver:
    path = config.RUNS_DIR / "checkpoints.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def graph_for(model):
    """One compiled graph per model and runs folder (tests point RUNS_DIR elsewhere)."""
    key = (id(model), str(config.RUNS_DIR))
    with _LOCK:
        if key not in _GRAPHS:
            _GRAPHS[key] = build(model, checkpointer())
        return _GRAPHS[key]


def _cfg(state: dict) -> dict:
    return {"configurable": {"thread_id": state["run_id"]}, "callbacks": [ModelLog(state["run_id"])],
            "recursion_limit": 1000}


def _start_payload(state: dict) -> dict:
    return {"goal": state["goal"], "task": len(state.get("tasks", [])), "intro": intro(state),
            "inputs": state["inputs"], "toolkits": state["toolkits"], "skill": state.get("skill")}


# ---------------------------------------------------------------- one step

def run_one_step(state: dict, model) -> dict:
    """A step either completes or, if the model can't be reached, leaves the conversation as it was plus a
    `model_error` event; retrying resumes the graph from its last checkpoint."""
    status = state["status"]
    if status in DONE:
        return event(state, status, final=state.get("final"))
    if status in WAITING:
        return event(state, status, **(state["pending"] or {}))   # wait for the human
    if state["step_count"] - state.get("task_start", 0) >= config.MAX_STEPS:
        return abort(state, f"Reached the step limit ({config.MAX_STEPS}).")
    graph = _ACTIVE[state["run_id"]] = graph_for(model)
    if state.get("start"):
        payload = _start_payload(state)
    elif state.get("resume"):
        payload = Command(resume=state["resume"])
    else:
        payload = None
    for _ in range(3):
        ev = _advance(state, graph, payload, model)
        if ev["kind"] != "noop" or not graph.get_state(_cfg(state)).next:
            return ev
        payload = None                               # nothing to show yet (e.g. a choice applied): go on
    return ev


def _advance(state: dict, graph, payload, model=None) -> dict:
    cfg = _cfg(state)
    persist(state)                                   # the graph's checks read this record
    try:
        graph.invoke(payload, cfg)
        error = None
    except Exception as e:
        error = e
    if _stopped(state):                              # stopped while the step ran: a late result changes nothing
        return state["events"][-1]
    snap = graph.get_state(cfg)
    # Whatever the graph saved before a failure is recorded too: it is not redone when the step is retried.
    seen = set(state.get("seen", []))
    new = [m for m in snap.values.get("messages", []) if m.id not in seen]
    state["seen"] = [m.id for m in snap.values.get("messages", [])]
    if error is None or snap.values.get("planned") == len(state.get("tasks", [])):
        state.pop("start", None)
    pending = {i.id for i in snap.interrupts}
    if error is None or not (set(state.get("resume") or {}) & pending):
        state.pop("resume", None)
    last = _record(state, snap, new) if (new or error is None) else None
    if error is not None:
        if model is not None:
            model.resync()                           # a backend with memory may hold part of the failed step
        log(state, "model_error", error=str(error))
        return persist(state, event(state, "model_error", error=f"{type(error).__name__}: {error}"[:500]))
    persist(state)
    return last


def _stopped(state: dict) -> bool:
    return state["status"] == "aborted"


def _obs(message: ToolMessage) -> dict:
    try:
        obs = json.loads(message.content)
        if isinstance(obs, dict):
            return obs
    except (ValueError, TypeError):
        pass
    return {"ok": message.status != "error", "result": str(message.content)}


def _record(state: dict, snap, new: list) -> dict:
    """Turn what the graph did in this step into the conversation's events."""
    values, last = snap.values, None
    calls = state.setdefault("calls", {})
    decided = state.setdefault("decided", {})
    state["toolkits"] = values.get("toolkits", state["toolkits"])
    if state["status"] == "planning" and values.get("planned") == len(state.get("tasks", [])) \
            and not values.get("suggestion"):
        state.update(plan=values.get("plan", []), skill=values.get("skill"), status="running")
        if not state.get("title"):
            first = state["tasks"][0]["goal"] if state.get("tasks") else state["goal"]
            state["title"] = values.get("title") or default_title(first)
        last = persist(state, event(state, "plan", plan=state["plan"], skill=state.get("skill")))
    for m in new:
        if isinstance(m, AIMessage) and m.tool_calls:
            c = m.tool_calls[0]
            calls[c["id"]] = {"tool": c["name"], "input": c["args"], "thought": m.additional_kwargs.get("thought", ""),
                              "attempts": m.response_metadata.get("repairs", 0) + 1,
                              "effort": m.response_metadata.get("effort")}
        elif isinstance(m, ToolMessage):
            last = _tool_event(state, m, calls.get(m.tool_call_id, {"tool": m.name, "input": {}}),
                               decided.pop(m.tool_call_id, None)) or last
        elif isinstance(m, HumanMessage) and m.additional_kwargs.get("issues"):
            state["review_rounds"] = values.get("review_rounds", 0)
            last = persist(state, event(state, "review", issues=m.additional_kwargs["issues"]))
    if values.get("status") == "finished" and state["status"] != "finished":
        last = _finish(state, values)
    elif values.get("status") == "aborted" and state["status"] != "aborted":
        last = abort(state, values.get("final") or "Stopped.")
    for intr in snap.interrupts:
        last = _pause(state, values, intr)
    return last or event(state, "noop")


def _tool_event(state: dict, m: ToolMessage, call: dict, decision: str | None) -> dict | None:
    obs = _obs(m)
    tool, inp = call["tool"], call["input"]
    state["step_count"] += 1
    if m.artifact and m.artifact.get("check"):      # sent back by the request check before the user was asked
        return persist(state, event(state, "precheck", tool=tool, input=inp, thought=call.get("thought", ""),
                                    issues=m.artifact["issues"], check=m.artifact["check"]))
    if tool == "ask_user":
        return None                                  # recorded when the user answered
    if obs.get("ok"):
        add_fact(state, f"{tool}({json.dumps(inp)[:120]}) -> {str(obs.get('result', ''))[:200]} [ref: {obs.get('ref')}]")
    if decision == "rejected":
        return None                                  # recorded when the user rejected it
    if decision == "approved":
        return persist(state, event(state, "user", approved=True, tool=tool, obs=obs))
    return persist(state, event(state, "tool", tool=tool, input=inp, thought=call.get("thought", ""), obs=obs,
                                attempts=call.get("attempts", 1), effort=call.get("effort")))


def _finish(state: dict, values: dict) -> dict:
    last_ai = next((m for m in reversed(values["messages"]) if isinstance(m, AIMessage) and not m.tool_calls), None)
    extra = last_ai.additional_kwargs if last_ai else {}
    tables = _result_tables(state, extra.get("tables"))
    text = values.get("final") or ""
    payload = {"text": text, "thought": extra.get("thought", ""), "tables": [t["path"] for t in tables],
               "table_titles": {t["path"]: t["title"] for t in tables}}
    if extra.get("via"):
        payload["via"] = extra["via"]
    if values.get("issues"):
        payload["warnings"] = values["issues"]
        state["warnings"].append("Finished with unresolved reviewer issues: " + "; ".join(values["issues"]))
    state["status"], state["final"], state["pending"] = "finished", text, None
    _retitle(state, extra.get("title", ""))
    _remember_results(state, payload["tables"], payload["table_titles"])
    return persist(state, event(state, "final", **payload))


def _pause(state: dict, values: dict, intr) -> dict:
    v = intr.value or {}
    kind = v.get("type")
    last_call = next((m for m in reversed(values.get("messages", [])) if isinstance(m, AIMessage) and m.tool_calls),
                     None)
    thought = last_call.additional_kwargs.get("thought", "") if last_call else ""
    if kind == "question":
        state["status"] = "awaiting_user"
        state["pending"] = {"tool": "ask_user", "input": {"question": v["question"]}, "thought": thought,
                            "interrupt": intr.id}
        return persist(state, event(state, "ask_user", question=v["question"], thought=thought))
    if kind == "suggestion":
        state["status"], state["suggested"] = "awaiting_choice", True
        if v.get("replan") is not False:
            state["plan"] = values.get("plan") or state["plan"]      # the plan without the suggestion
        state["pending"] = {"toolkits": v.get("toolkits", []), "skill": v.get("skill"), "why": v.get("why", ""),
                            "interrupt": intr.id, **({"replan": False} if v.get("replan") is False else {})}
        return persist(state, event(state, "suggest", toolkits=v.get("toolkits", []), skill=v.get("skill"),
                                    why=v.get("why", "")))
    tool, inp, call_id = v.get("name"), v.get("args", {}), v.get("tool_call_id")   # HumanInTheLoopMiddleware
    state["status"] = "awaiting_approval"
    state["pending"] = {"tool": tool, "input": inp, "thought": thought, "interrupt": intr.id, "tool_call_id": call_id}
    return persist(state, event(state, "approval", tool=tool, input=inp, thought=thought,
                                explain=(values.get("explain") or {}).get(call_id, ""),
                                reviewer_notes=(values.get("reviewer_notes") or {}).get(call_id, []),
                                previous=_previous_request(state, tool)))


# ---------------------------------------------------------------- the human's answer

def resolve_pending(state: dict, approved: bool | None = None, answer: str | None = None,
                    choice: dict | None = None, reason: str | None = None) -> dict:
    """Record the human's response to a pending approval, question or suggestion.
    A rejection's reason goes back to the model and into the facts: without it the agent can only guess what was
    wrong, and it once resent the same whole-year query in a "lighter" form."""
    if state["status"] not in WAITING or not state["pending"]:
        raise ValueError(f"Nothing pending (status={state['status']}).")
    pending, status = state["pending"], state["status"]
    if status == "awaiting_choice":
        choice = choice or {}
        kits = [t for t in choice.get("toolkits") or [] if t in pending["toolkits"]]
        skill = choice.get("skill") if choice.get("skill") and choice.get("skill") == pending.get("skill") else None
        resume, payload = {"toolkits": kits, "skill": skill}, {"choice": {"toolkits": kits, "skill": skill}}
        log(state, "user", kind_detail="choice", toolkits=kits, skill=skill)
        state["toolkits"] = list(dict.fromkeys(state["toolkits"] + kits))
        if pending.get("replan") is False or not kits:
            next_status = "running"                  # mid-run, or a skill's steps / the current plan stand
        else:
            next_status = "planning"                 # toolkits only: plan again with them (next step)
        if skill and pending.get("replan") is not False:
            sk = get_skill(skill)
            state["toolkits"] = list(dict.fromkeys(state["toolkits"] + sk.toolkits))
            state["skill"], state["plan"], next_status = skill, sk.steps, "running"
    elif status == "awaiting_user":
        if not (answer or "").strip():
            raise ValueError("An answer is required.")
        ref = next_ref(state["run_id"], "ask_user")
        resume = {"answer": answer.strip(), "ref": ref}
        log(state, "user", kind_detail="answer", question=pending["input"].get("question"), answer=answer)
        # Kept whole: the confirmed period, scope and level decide every later step, and a 120-char cut once hid
        # "period = latest date" from the request check, which then demanded a full-year scan.
        add_fact(state, f"USER ANSWER to: {str(pending['input'].get('question', ''))[:1500]} -> "
                        f"{answer.strip()[:1500]} [ref: {ref}]")
        payload = {"answer": answer.strip(), "question": pending["input"].get("question", ""),
                   "obs": {"ok": True, "ref": ref, "result": answer.strip()}}
        next_status = "running"
    elif approved:
        log(state, "user", kind_detail="approve", tool=pending["tool"])
        state.setdefault("decided", {})[pending["tool_call_id"]] = "approved"
        resume, payload, next_status = {"type": "approve"}, None, "running"
    else:
        reason = (reason or "").strip()[:1500]
        log(state, "user", kind_detail="reject", tool=pending["tool"], reason=reason or None)
        if reason:
            message = (f"User rejected {pending['tool']} because: {reason}\n"
                       "Follow this reason; do not resend the same request in another form.")
            add_fact(state, f"USER REJECTED {pending['tool']} because: {reason}")
        else:
            message = f"User rejected {pending['tool']}. Choose another approach."
        state.setdefault("decided", {})[pending["tool_call_id"]] = "rejected"
        resume, next_status = {"type": "reject", "message": message}, "running"
        payload = {"approved": False, "tool": pending["tool"], **({"reason": reason} if reason else {})}
    state["resume"] = {pending["interrupt"]: resume}
    state["pending"], state["status"] = None, next_status
    graph = _ACTIVE.get(state["run_id"])
    if payload is None and graph is not None:        # an approved request runs now, as part of the approval
        return _advance(state, graph, Command(resume=state["resume"]))
    ev = persist(state, event(state, "user", **(payload or {"approved": True, "tool": pending["tool"]})))
    if status == "awaiting_choice" and next_status == "running" and pending.get("replan") is not False:
        ev = persist(state, event(state, "plan", plan=state["plan"], skill=state.get("skill")))
    return ev


# ---------------------------------------------------------------- conversations

def abort(state: dict, reason: str) -> dict:
    state["status"] = "aborted"
    state["pending"] = None
    log(state, "abort", reason=reason)
    return persist(state, event(state, "abort", reason=reason))


def continue_with(state: dict, goal: str, inputs: list[str] | None = None, skill: str | None = None,
                  toolkits: list[str] | None = None) -> dict:
    """A new instruction in the same conversation (the same LangGraph thread): earlier messages, facts, refs and
    work tables stay available; the plan and per-instruction counters restart."""
    if state["status"] not in DONE:
        raise ValueError(f"Finish or stop the current instruction first (status={state['status']}).")
    state.setdefault("tasks", []).append({"goal": state["goal"], "status": state["status"],
                                          "final": state.get("final")})
    state["goal"], state["skill"] = goal, skill
    state["inputs"] = list(dict.fromkeys(state["inputs"] + list(inputs or [])))
    if toolkits is not None:
        state["toolkits"] = list(toolkits)
    state.update(status="planning", plan=[], final=None, pending=None, warnings=[], review_rounds=0,
                 suggested=False, task_start=state["step_count"], start=True)
    log(state, "user", kind_detail="instruction", goal=goal)
    return persist(state, event(state, "task", goal=goal, files=list(inputs or [])))


def rename(state: dict, title: str) -> None:
    """Name the conversation (shown on top and in the resume list). A name the user gives is kept."""
    title = " ".join(title.split())[:80]
    if title:
        state["title"], state["title_by_user"] = title, True
        persist(state)


# ---------------------------------------------------------------- driver

def run(goal: str, model, inputs: list[str] | None = None,
        on_event: Callable[[dict], None] = lambda e: None,
        approve: Callable[[dict], bool | str] = lambda e: False,
        ask: Callable[[dict], str] = lambda e: "No answer available.",
        state: dict | None = None, context: list[str] | None = None, skill: str | None = None,
        toolkits: list[str] | None = None, choose: Callable[[dict], dict] = lambda e: {}) -> dict:
    """Run to completion, or stop at a model_error so it can be resumed later. Pass `state` to resume.
    `approve` returns True, or False / a reason to reject. `choose` answers a suggestion, e.g.
    {"toolkits": [...], "skill": ...}; {} declines."""
    state = state or new_state(goal, inputs, context=context, skill=skill, toolkits=toolkits)
    idle = 0
    while state["status"] not in DONE:
        was_waiting = state["status"] in WAITING
        ev = run_one_step(state, model)
        if not was_waiting:
            on_event(ev)
        if ev["kind"] == "model_error":
            break
        idle = idle + 1 if ev["kind"] == "noop" else 0
        if idle >= 3:                                # a graph that stops without an event would loop forever
            abort(state, "The agent made no progress.")
            break
        if state["status"] == "awaiting_approval":
            verdict = approve(ev)
            on_event(resolve_pending(state, approved=verdict is True,
                                     reason=verdict if isinstance(verdict, str) else None))
        elif state["status"] == "awaiting_user":
            on_event(resolve_pending(state, answer=ask(ev)))
        elif state["status"] == "awaiting_choice":
            on_event(resolve_pending(state, choice=choose(ev)))
    return state
