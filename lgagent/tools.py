"""The existing toolkits as LangChain tools, plus the run log.

Toolkits and plugins stay exactly as they are (agent/toolkits, plugins/tools, the @tool decorator): each registry
tool becomes a StructuredTool with the same Pydantic args model. Calls still go through `agent.tools.dispatch`,
which keeps the sandbox, time limits, error-to-observation and the full output stored under a ref, so answers
cite [ref: ...] and the number check can trace them, exactly as before.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import StructuredTool
from langgraph.config import get_config
from langgraph.types import interrupt

from agent import config
from agent.tools import TOOLS, Tool, ToolContext, dispatch, needs_approval


def run_id() -> str:
    """The LangGraph thread id is the run id: one conversation, one checkpoint thread, one runs/<id>/ folder."""
    return get_config()["configurable"]["thread_id"]


def run_dir(rid: str) -> Path:
    return config.RUNS_DIR / rid


def log(rid: str, kind: str, **fields) -> None:
    """Same log.jsonl as the step machine, so replay tools and loop.cited_outputs can read it."""
    path = run_dir(rid) / "log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "kind": kind, **fields}, default=str) + "\n")


def _next_ref(rid: str, name: str) -> str:
    out = run_dir(rid) / "outputs"
    n = len(list(out.glob("*.txt"))) + 1 if out.exists() else 1
    return f"{name}-{n}"


def context(rid: str) -> ToolContext:
    return ToolContext(run_id=rid, run_dir=run_dir(rid))


def _lc_tool(t: Tool) -> StructuredTool:
    def run(**kwargs) -> str:
        rid = run_id()
        ref = _next_ref(rid, t.name)
        obs = dispatch(t.name, kwargs, context(rid), ref)
        log(rid, "tool", tool=t.name, ref=ref, input=kwargs, ok=obs.get("ok"))
        return json.dumps(obs, ensure_ascii=False)
    return StructuredTool.from_function(func=run, name=t.name, description=t.desc, args_schema=t.args)


def _ask_user(question: str) -> str:
    """LangGraph's own human-in-the-loop: the run pauses here and resumes with the user's answer."""
    answer = interrupt({"type": "question", "question": question})
    log(run_id(), "user", question=question, answer=answer)
    return f"USER ANSWER: {answer}"


def lc_tools() -> list[StructuredTool]:
    """Every registered tool; the session's enabled toolkits are filtered per model call (graph.SessionPrompt)."""
    out = []
    for t in TOOLS.values():
        if t.special:
            out.append(StructuredTool.from_function(func=_ask_user, name=t.name, description=t.desc,
                                                    args_schema=t.args))
        elif t.fn:
            out.append(_lc_tool(t))
    return out


def approval_needed(name: str, args: dict) -> bool:
    t = TOOLS.get(name)
    return bool(t) and not t.special and needs_approval(t, args, context(run_id()))


class JsonlLog(BaseCallbackHandler):
    """Every model call in runs/<id>/log.jsonl with purpose, effort and latency (a LangChain callback)."""

    def __init__(self, rid: str):
        self.rid, self.started = rid, {}

    def on_chat_model_start(self, serialized, messages, *, run_id, invocation_params=None, **kw):
        self.started[run_id] = (time.perf_counter(), invocation_params or {})

    def on_llm_end(self, response, *, run_id, **kw):
        t0, params = self.started.pop(run_id, (time.perf_counter(), {}))
        msg = response.generations[0][0].message
        meta = msg.response_metadata
        log(self.rid, "model", purpose=params.get("purpose"), effort=meta.get("effort", params.get("effort")),
            model=params.get("model"), latency_ms=round((time.perf_counter() - t0) * 1000),
            prompt_tokens=meta.get("prompt_chars", 0) // 4, repairs=meta.get("repairs", 0),
            output=(msg.content or json.dumps(msg.tool_calls))[:2000])

    def on_llm_error(self, error, *, run_id, **kw):
        t0, params = self.started.pop(run_id, (time.perf_counter(), {}))
        log(self.rid, "model_error", purpose=params.get("purpose"), error=str(error)[:500],
            latency_ms=round((time.perf_counter() - t0) * 1000))
