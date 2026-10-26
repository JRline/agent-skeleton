"""Tool registry, shared helpers and dispatch (agents.md §6).

Tools live in toolkits: agent/toolkits/*.py (built in) and plugins/tools/*.py (added later), loaded by
agent/plugins.py. Each toolkit module defines TOOLKIT = {...} and decorates functions
`fn(args, ctx) -> str | ToolOutput` with @tool(...); see plugins/AGENTS.md. The system prompt is rendered
from the session's enabled tools, so the model only sees what it may use.
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field, ValidationError

from . import config


# ---------------------------------------------------------------- registry

@dataclass
class Tool:
    name: str
    desc: str
    args: type[BaseModel]
    fn: Callable | None
    risky: bool | Callable = False   # needs human approval: True, or risky(args: dict, ctx) -> bool per call
    special: bool = False    # handled by the loop, never dispatched (ask_user)
    toolkit: str = "core"    # from the defining module's TOOLKIT["name"]; "core" tools are always enabled
    check: Callable | None = None  # check(args) -> problem or None; runs before approval is asked
    preview_chars: int | None = None  # observation size for this tool (default OBS_PREVIEW_CHARS)


@dataclass
class ToolOutput:
    preview: str             # what the model sees (truncated)
    full: str                # what is stored under the ref


@dataclass
class ToolContext:
    run_id: str
    run_dir: Path
    context: tuple = ()      # the run's context documents (sandbox paths), for search_docs
    ref: str = ""            # this call's ref (set by dispatch), e.g. for a tool that remembers its results


TOOLS: dict[str, Tool] = {}


def tool(name: str, desc: str, args: type[BaseModel], risky: bool = False, special: bool = False,
         check: Callable | None = None, preview_chars: int | None = None):
    def wrap(fn):
        module = sys.modules.get(getattr(fn, "__module__", ""), None)
        toolkit = getattr(module, "TOOLKIT", {}).get("name", "core")
        old = TOOLS.get(name)
        if old and old.fn and fn and old.fn.__module__ != fn.__module__:
            raise ValueError(f"tool name {name!r} is already defined in {old.fn.__module__}")
        TOOLS[name] = Tool(name, desc, args, fn, risky, special, toolkit, check, preview_chars)
        return fn
    return wrap


def needs_approval(tool: Tool, args: dict, ctx: ToolContext) -> bool:
    """Whether this call waits for the user. A callable `risky` decides per call, e.g. a lake query asks,
    reading back this conversation's own saved result does not."""
    if callable(tool.risky):
        try:
            return bool(tool.risky(args, ctx))
        except Exception:                    # undecidable (e.g. bad arguments): ask rather than skip
            return True
    return bool(tool.risky)


def enabled_tools(toolkits) -> dict[str, Tool]:
    """Core tools (ask_user) plus the tools of the given toolkits, in registration order."""
    allowed = set(toolkits or ()) | {"core"}
    return {n: t for n, t in TOOLS.items() if t.toolkit in allowed}


def render_tools(tools: dict[str, Tool] | None = None) -> str:
    """Tool list for the system prompt: name, description, argument schema."""
    lines = []
    for t in (tools or TOOLS).values():
        props = t.args.model_json_schema().get("properties", {})
        required = set(t.args.model_json_schema().get("required", []))
        arg_desc = ", ".join(
            f'"{k}": {v.get("type", "any")}{"" if k in required else " (optional)"}'
            for k, v in props.items()
        )
        flag = " [needs user approval]" if t.risky is True else " [user approval for data reads]" if t.risky else ""
        lines.append(f"- {t.name}{flag}: {t.desc}\n  input: {{{arg_desc}}}")
    return "\n".join(lines)


# ---------------------------------------------------------------- helpers

class ToolError(Exception):
    pass


def sandbox_path(path: str) -> Path:
    """Resolve a user/model path inside the sandbox; refuse anything outside it."""
    p = path.strip().lstrip("/")
    if p.startswith("sandbox/"):
        p = p[len("sandbox/"):]
    root = config.SANDBOX_DIR.resolve()
    full = (root / p).resolve()
    if root != full and root not in full.parents:
        raise ToolError(f"Path {path!r} is outside the sandbox.")
    return full


def quote_paths(paths) -> str:
    """File names as the model sees them: each in double quotes, comma-separated. Unquoted, a name with a
    space ("daily captial.xlsx") reads like a typo or two names, and small models rewrite it."""
    return ", ".join(json.dumps(str(p), ensure_ascii=False) for p in paths)


def _truncate(text: str, limit: int | None = None) -> str:
    limit = limit or config.OBS_PREVIEW_CHARS
    return text if len(text) <= limit else text[:limit] + f"\n…[truncated, {len(text)} chars total]"


def _load_table(path: Path):
    """Parsed once per file version (tablecache): the same file read again in a session costs nothing."""
    from . import tablecache
    if path.suffix.lower() not in (".csv", ".xlsx", ".xlsm", ".xls"):
        raise ToolError(f"Not a table file: {path.name}")
    return tablecache.load(path)


# ---------------------------------------------------------------- shared table helpers (used by toolkits)

_NAME = re.compile(r"[A-Za-z0-9_\-]{1,60}")
_FLOAT = "%.10g"     # 12.47 - 7.0 is stored as 5.47, not 5.470000000000001


def _save_work(df, ctx: ToolContext, save_as: str) -> str:
    """Store a table in the run's scratch folder and return its sandbox path for later tools."""
    if not _NAME.fullmatch(save_as):
        raise ToolError("save_as must be a short name: letters, digits, _ or -.")
    out_dir = config.work_dir(ctx.run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    name, n = save_as, 2
    while (out_dir / f"{name}.csv").exists():      # never overwrite an earlier step's table: answers point at it
        name, n = f"{save_as}_{n}", n + 1
    df.to_csv(out_dir / f"{name}.csv", index=False, float_format=_FLOAT)
    from . import tablecache
    tablecache.store(out_dir / f"{name}.csv", df)              # the next step loads it without parsing
    return f"work/{ctx.run_id}/{name}.csv"


def _table_preview(df, path: str, note: str = "") -> ToolOutput:
    head = f"rows={len(df)}; columns={list(df.columns)}; saved as {path}{note}\n"
    return ToolOutput(head + df.head(20).to_csv(index=False, float_format=_FLOAT),
                      head + df.to_csv(index=False, float_format=_FLOAT))



def step_summary(obs: dict) -> str:
    """One line about a tool result that never shows the data itself (UI and CLI, verbose off)."""
    if not obs.get("ok"):
        return "error: " + (str(obs.get("error", "")).splitlines() or [""])[0][:160]
    result = str(obs.get("result", ""))
    try:                                        # run_python's pre-agreed JSON format
        out = json.loads(result)
        res = out["result"]
        if res["type"] == "table":
            return (f"table {res.get('row_count', len(res['rows']))} × {len(res['columns'])}"
                    + (f", saved as {out['saved_as']}" if out.get("saved_as") else ""))
        return "value computed"
    except (ValueError, KeyError, TypeError):
        pass
    first = result.splitlines()[0] if result else ""
    m = re.match(r"(?:rows=(\d+)|shape=\((\d+), (\d+)\)); columns=\[(.*?)\](?:; saved as (\S+))?(.*)", first)
    if m:                                       # a query or file result: say its size, not its raw header
        rows = int(m.group(1) or m.group(2))
        cols = int(m.group(3)) if m.group(3) else len([c for c in m.group(4).split(",") if c.strip()])
        text = f"{rows:,} rows × {cols} columns"
        text += f", saved as {m.group(5)}" if m.group(5) else ""
        return text + (" (reused, not queried again)" if "reused" in m.group(6) else "")
    if re.match(r"(Saved |columns:)", first):
        return first[:200]
    return f"done ({len(result)} characters of output)"


def read_doc(path: Path) -> str:
    """Text of a context document: TXT/MD/JSON as is, CSV/XLSX as CSV text."""
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return _load_table(path).to_csv(index=False)
    return path.read_text(errors="replace")



class AskUserArgs(BaseModel):
    question: str = Field(description="One clear question for the user")


tool("ask_user", "Ask the user a question when information is missing. Do not guess instead. To settle an open "
     "choice, propose the values you plan to use and ask the user to confirm or correct them.",
     AskUserArgs, special=True)(None)



# ---------------------------------------------------------------- dispatch

def dispatch(name: str, args: dict, ctx: ToolContext, ref: str) -> dict:
    """Validate, run with a timeout, store the full output under `ref`, return an observation.

    Errors never raise: they come back as {"ok": False, "error": ...} for the model to read.
    """
    t = TOOLS.get(name)
    if t is None or t.fn is None:
        return {"ok": False, "ref": ref, "error": f"Tool {name!r} cannot be dispatched."}
    try:
        parsed = t.args.model_validate(args)
    except ValidationError as e:
        return {"ok": False, "ref": ref, "error": f"Bad input: {e.errors()[0]['msg']}"}
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        out = pool.submit(t.fn, parsed, replace(ctx, ref=ref)).result(timeout=config.TOOL_TIMEOUT_S)
    except FutureTimeout:
        return {"ok": False, "ref": ref, "error": f"Timed out after {config.TOOL_TIMEOUT_S}s."}
    except Exception as e:
        return {"ok": False, "ref": ref, "error": f"{type(e).__name__}: {e}"[:500]}
    finally:
        pool.shutdown(wait=False)
    if not isinstance(out, ToolOutput):
        out = ToolOutput(str(out), str(out))
    out_dir = ctx.run_dir / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{ref}.txt").write_text(out.full)
    return {"ok": True, "ref": ref, "result": _truncate(out.preview, t.preview_chars)}


# Toolkits are loaded last: they import the registry and helpers defined above.
from .plugins import load as _load_plugins  # noqa: E402

_load_plugins()
