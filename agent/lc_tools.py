"""The toolkits as LangChain tools, and the model-call log (docs/design.md §6, §12).

Toolkits and plugins are unchanged (agent/toolkits, plugins/tools, the @tool decorator): each registry tool becomes
a StructuredTool with the same Pydantic args model. Calls still go through `tools.dispatch`, which keeps the sandbox,
time limits, errors as observations and the full output stored under a ref, so answers cite [ref: ...] and the
number check can trace them. `ask_user` pauses the graph with LangGraph's interrupt().
"""
from __future__ import annotations

import json
import time

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import StructuredTool
from langgraph.config import get_config
from langgraph.types import interrupt

from .memory import load_state, log, run_dir, searchable_docs
from .tools import TOOLS, Tool, ToolContext, dispatch, needs_approval


def run_id() -> str:
    """The LangGraph thread id is the run id: one conversation, one thread, one runs/<id>/ folder."""
    return get_config()["configurable"]["thread_id"]


def _view(rid: str) -> dict:
    try:
        return load_state(rid)
    except FileNotFoundError:
        return {"run_id": rid}


def context(rid: str) -> ToolContext:
    """The run's documents for search_docs come from its saved state (state.json), written before every step."""
    return ToolContext(run_id=rid, run_dir=run_dir(rid), context=tuple(searchable_docs(_view(rid))))


def next_ref(rid: str, name: str) -> str:
    out = run_dir(rid) / "outputs"
    n = len(list(out.glob("*.txt"))) + 1 if out.exists() else 1
    return f"{name}-{n}"


def _wrap(t: Tool) -> StructuredTool:
    def run(**kwargs) -> str:
        rid = run_id()
        ref = next_ref(rid, t.name)
        obs = dispatch(t.name, kwargs, context(rid), ref)
        log({"run_id": rid}, "tool", tool=t.name, input=kwargs, ok=obs.get("ok"), ref=ref, error=obs.get("error"))
        return json.dumps(obs, ensure_ascii=False)
    return StructuredTool.from_function(func=run, name=t.name, description=t.desc, args_schema=t.args)


def _ask_user(question: str) -> str:
    """Pause for the user (LangGraph interrupt). The answer is stored under a ref like any tool result, so numbers
    the user gives count as sources in the answer check."""
    reply = interrupt({"type": "question", "question": question})
    rid = run_id()
    answer, ref = (str(reply.get("answer", "")).strip(), reply.get("ref")) if isinstance(reply, dict) \
        else (str(reply).strip(), None)
    ref = ref or next_ref(rid, "ask_user")
    out = run_dir(rid) / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{ref}.txt").write_text(answer)
    return json.dumps({"ok": True, "ref": ref, "result": answer}, ensure_ascii=False)


def lc_tools() -> list[StructuredTool]:
    """Every registered tool. Which ones the model is offered depends on the session (graph.SessionPrompt)."""
    out = []
    for t in TOOLS.values():
        if t.special:
            out.append(StructuredTool.from_function(func=_ask_user, name=t.name, description=t.desc,
                                                    args_schema=t.args))
        elif t.fn:
            out.append(_wrap(t))
    return out


def approval_needed(name: str, args: dict) -> bool:
    t = TOOLS.get(name)
    return bool(t) and not t.special and needs_approval(t, args, context(run_id()))


class ModelLog(BaseCallbackHandler):
    """Every model call in runs/<id>/log.jsonl with purpose, effort, latency and size (a LangChain callback)."""

    def __init__(self, rid: str):
        self.rid, self.started = rid, {}

    def on_chat_model_start(self, serialized, messages, *, run_id, invocation_params=None, **kw):
        self.started[run_id] = (time.perf_counter(), invocation_params or {})

    def on_llm_end(self, response, *, run_id, **kw):
        t0, params = self.started.pop(run_id, (time.perf_counter(), {}))
        msg = response.generations[0][0].message
        meta = msg.response_metadata
        log({"run_id": self.rid}, "model", purpose=params.get("purpose"), effort=meta.get("effort"),
            model=meta.get("model"), latency_ms=round((time.perf_counter() - t0) * 1000),
            prompt_tokens=meta.get("prompt_tokens", 0), repairs=meta.get("repairs", 0),
            **{k: meta[k] for k in ("channel", "send", "sent_tokens") if k in meta},
            output=meta.get("raw") or msg.text or json.dumps(msg.tool_calls))

    def on_llm_error(self, error, *, run_id, **kw):
        t0, params = self.started.pop(run_id, (time.perf_counter(), {}))
        log({"run_id": self.rid}, "model_error", purpose=params.get("purpose"), error=str(error)[:500],
            latency_ms=round((time.perf_counter() - t0) * 1000))
