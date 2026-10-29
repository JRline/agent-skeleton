"""Checks and bookkeeping shared by the graph and the conversation driver (docs/design.md §7, §10).

They read the conversation's saved record (state.json: events, facts, inputs), so the same checks run whatever
executes the agent: numbers in an answer must trace to tool results, the reviewers see the outputs an answer cites
and the requests that already ran, and answers carry their timing and result tables.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from . import config
from .memory import context_docs, read_log, run_dir
from .extensions import TOOLKITS
from .skills import get_skill
from .tools import enabled_tools, read_doc, sandbox_path


def answer_timing(state: dict) -> dict[str, dict]:
    """Time to each final answer (keyed by the final event's ts): elapsed since its instruction began, of which the
    agent worked and the user was being waited on (approvals, questions, suggestions), plus steps taken."""
    out, start, waiting, steps, asked = {}, state.get("created"), 0.0, 0, None
    for ev in state["events"]:
        ts = datetime.fromisoformat(ev["ts"]) if ev.get("ts") else None
        if ev["kind"] == "task":
            start, waiting, steps, asked = ev["ts"], 0.0, 0, None
        elif ev["kind"] in ("approval", "ask_user", "suggest"):
            asked = ts
        elif ev["kind"] == "user" and asked and ts:
            waiting += (ts - asked).total_seconds()
            asked = None
        if ev["kind"] in ("tool", "user", "precheck", "review", "plan"):
            steps += 1
        if ev["kind"] == "final" and start and ts:
            elapsed = max(0.0, (ts - datetime.fromisoformat(start)).total_seconds())
            out[ev["ts"]] = {"elapsed": elapsed, "waiting": min(waiting, elapsed), "steps": steps}
    return out


def describe_timing(t: dict) -> str:
    def span(s):
        s = int(round(s))
        return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"
    text = f"Answered in {span(t['elapsed'])}"
    if t["waiting"] >= 1:
        text += f" (agent {span(t['elapsed'] - t['waiting'])}, waiting for you {span(t['waiting'])})"
    return text + f" · {t['steps']} steps"


class _Plan(BaseModel):
    plan: list[str] = []
    skill: str | None = None             # older prompt shape; treated as a suggestion
    suggest: dict | None = None          # {"toolkits": [...], "skill": "...", "why": "..."}
    title: str | None = None             # short name for the conversation (used once)


class _Verdict(BaseModel):
    ok: bool
    issues: list[str] = []


class _ActionVerdict(_Verdict):
    explain: str = ""


class _Final(BaseModel):
    final: str


def _suggestion(state: dict, parsed: _Plan) -> dict | None:
    """Keep only real, not-yet-enabled toolkits and a known skill. The user decides; never auto-enabled."""
    raw = parsed.suggest or {}
    toolkits = [t for t in raw.get("toolkits") or [] if t in TOOLKITS and t not in state["toolkits"]]
    skill = get_skill(raw.get("skill") or parsed.skill)
    if not (toolkits or skill):
        return None
    return {"toolkits": toolkits, "skill": skill.name if skill else None, "why": str(raw.get("why", ""))[:300]}


def _enabled_by_toolkit(state: dict) -> str:
    """Enabled tools grouped by toolkit, so the planner can see that a skill's toolkits are already on."""
    groups: dict[str, list[str]] = {}
    for tool in enabled_tools(state["toolkits"]).values():
        groups.setdefault(tool.toolkit, []).append(tool.name)
    return "\n".join(f"- {kit}: {', '.join(names)}" for kit, names in groups.items())


def _other_toolkits(state: dict) -> str:
    return "\n".join(f"- {n}: {k['description']}" for n, k in TOOLKITS.items()
                     if n not in state["toolkits"]) or "(none)"


def cited_outputs(state: dict, final: str) -> str:
    """Stored outputs of the refs cited in FINAL, so the reviewer can trace every number.
    Facts keep only a short preview, which hid most of a table and made the reviewer reject good answers."""
    inputs = {e.get("ref"): e.get("input") for e in read_log(state) if e["kind"] == "tool"}
    texts = {}
    for ref in dict.fromkeys(re.findall(r"\[ref:\s*([\w\-]+)\s*\]", final)):
        path = run_dir(state) / "outputs" / f"{ref}.txt"
        if path.exists():
            texts[ref] = path.read_text(errors="replace")
    # Shortest first, each with an even share of what is left: a large table cited first once cut a one-row total
    # off mid-number, and the reviewer then rejected a correct answer.
    shown, budget = {}, config.REVIEW_CITED_CHARS
    for i, ref in enumerate(sorted(texts, key=lambda r: len(texts[r]))):
        shown[ref] = texts[ref][:max(budget // (len(texts) - i), 0)]
        budget -= len(shown[ref])
    parts = [f"--- {ref} ---\ninput: {json.dumps(inputs.get(ref))}\n{shown[ref].strip()}"
             + (" …[cut]" if len(shown[ref]) < len(texts[ref]) else "") for ref in texts]
    return "\n".join(parts) or "(no cited outputs found)"


# Parts of an answer that are not data: citations, dates, quarters, numbered list markers.
_NOT_DATA = re.compile(r"\[(?:ref|doc):[^\]]*\]|\b\d{4}-\d{2}-\d{2}\b|\b\d{4}Q[1-4]\b|(?m:^\s*\d+[.)]\s)")


_NUMBER = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])-?\d+(?:\.\d+)?")


def _numbers(text: str) -> list[str]:
    return _NUMBER.findall(text)


_PLAIN_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _evidence_numbers(text: str) -> set[float]:
    """Numbers in tool outputs. Read both ways: "1,234" as a formatted number, and every value between commas,
    since stored results are CSV ("Credit risk,4,148898" is 4 and 148898, not 4,148 and 898)."""
    tokens = _NUMBER.findall(text) + _PLAIN_NUMBER.findall(text)
    return {abs(float(n.replace(",", ""))) for n in tokens}


_EVIDENCE: dict[tuple, frozenset] = {}      # (output file, mtime, size) -> its numbers, indexed once


def _file_numbers(path: Path) -> frozenset:
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _EVIDENCE:
        _EVIDENCE[key] = frozenset(_evidence_numbers(path.read_text(errors="replace")))
    return _EVIDENCE[key]


def uncited_numbers(state: dict, final: str) -> list[str]:
    """Numbers in the answer that appear in no tool result, reference document or user instruction.
    A deterministic check before the LLM reviewer: cheaper, and it can't be talked round by the model.
    Rounding is allowed (12.66 matches 12.660098); small counts (<= 12) and years are skipped."""
    evidence = [state["goal"]] + [t["goal"] for t in state.get("tasks", [])]
    out_dir = run_dir(state) / "outputs"
    stored = set().union(*(_file_numbers(p) for p in out_dir.glob("*.txt"))) if out_dir.exists() else set()
    # What the model was shown: previews carry lines the stored output lacks (e.g. read_file's shape=(48, 5)),
    # and answers to ask_user are the user's own numbers.
    for ev in state["events"]:
        payload = ev.get("payload") or {}
        evidence += [str((payload.get("obs") or {}).get("result", "")), str(payload.get("answer", ""))]
    from .memory import dataset_docs
    evidence.append(dataset_docs(state))                  # an accepted skill's dataset documentation
    for doc in context_docs(state):
        try:
            evidence.append(read_doc(sandbox_path(doc)))
        except Exception:
            pass
    known = stored.union(*(_evidence_numbers(text) for text in evidence))
    missing = []
    for token in _numbers(_NOT_DATA.sub(" ", final)):
        value = abs(float(token.replace(",", "")))
        decimals = len(token.split(".")[1]) if "." in token else 0
        if decimals == 0 and (value <= 12 or (1900 <= value <= 2100 and "," not in token)):   # counts, years
            continue
        if not any(abs(round(k, decimals) - value) < 1e-9 for k in known) and token not in missing:
            missing.append(token)
    return missing


def _shown_tables(tables) -> str:
    """Tables listed with the answer are shown under it as grids: the reviewer must know they are there."""
    parts = []
    for path in tables or []:
        full = config.SANDBOX_DIR / path
        if full.is_file():
            text = full.read_text(errors="replace")
            parts.append(f"--- table {path} (shown to the user under the answer as a grid) ---\n"
                         f"{text[:config.REVIEW_CITED_CHARS // 2]}")
    return "\n".join(parts)


def _view_docs(state: dict) -> str:
    """Documentation the agent read (describe_dataset): units, filters and calendars the answer may state. A dataset
    of the accepted skill is left out: its documentation is already in the CONTEXT, and the same text twice only
    made the checkers' prompts longer."""
    from .memory import skill_datasets
    covered = set(skill_datasets(state))
    parts, seen = [], set(covered)
    for ev in state["events"]:
        p = ev["payload"]
        obs = p.get("obs") or {}
        view = (p.get("input") or {}).get("dataset")
        if p.get("tool") == "describe_dataset" and obs.get("ok") and view not in seen:
            seen.add(view)
            stored = run_dir(state) / "outputs" / f"{obs['ref']}.txt"
            text = stored.read_text(errors="replace") if stored.is_file() else str(obs.get("result", ""))
            parts.append(f"--- documentation of dataset {view} [ref: {obs['ref']}] ---\n{text[:config.REVIEW_CITED_CHARS // 2]}")
    return "\n".join(parts)


def _previous_request(state: dict, tool: str) -> str:
    """The last earlier request of the same tool (approved, rejected or sent back): its SQL or code, so a revised
    request can be explained by what changed."""
    for ev in reversed(state["events"]):
        p = ev["payload"]
        if ev["kind"] in ("approval", "precheck") and p.get("tool") == tool:
            inp = p.get("input") or {}
            return str(inp.get("sql") or inp.get("code") or inp.get("path") or "")
    return ""


def _earlier_requests(state: dict, limit: int = 1500) -> str:
    """The SQL and code that already ran, in full, with what they saved: the request reviewer checks the new
    request against these instead of asking to re-verify what fact previews cut off."""
    parts, pending = [], {}
    for ev in state["events"]:
        p = ev["payload"]
        if ev["kind"] == "approval":
            pending = p.get("input") or {}
        elif ev["kind"] == "user" and p.get("approved") and (p.get("obs") or {}).get("ok"):
            obs, body = p["obs"], pending.get("sql") or pending.get("code") or pending.get("path") or ""
            head = str(obs.get("result", "")).split("\n", 1)[0][:300]
            parts.append(f"[{obs['ref']}] {p['tool']}:\n{str(body)[:limit]}\n-> {head}")
    return "\n\n".join(parts) or "(none)"


def _result_tables(state: dict, items) -> list[dict]:
    """The tables a final answer shows: [{path, title}]. An item is a work table name or sandbox path, or
    {"name", "title"}; a bare name means this run's work table, and a missing title is made from the name."""
    out = []
    for item in items or []:
        name, title = (item.get("name") or item.get("table") or item.get("path") or "", item.get("title") or "") \
            if isinstance(item, dict) else (str(item), "")
        if not name:
            continue
        path = name if "/" in name else f"work/{state['run_id']}/{name.removesuffix('.csv')}.csv"
        if "/" not in name and not (config.SANDBOX_DIR / path).is_file():      # a file delivered to the user
            path = f"outputs/{state['run_id']}/{name}"
        if (config.SANDBOX_DIR / path).is_file() and path not in [t["path"] for t in out]:
            out.append({"path": path, "title": title or Path(path).stem.replace("_", " ").capitalize()})
    return out


def _remember_results(state: dict, paths: list[str], titles: dict | None = None) -> None:
    """Result tables of the whole conversation, for the inline view and the Results page."""
    for path in paths:
        state.setdefault("results", []).append({"task": len(state.get("tasks", [])), "goal": state["goal"],
                                                "path": path,
                                                "title": (titles or {}).get(path) or Path(path).stem.replace("_", " ")})


def _retitle(state: dict, title: str) -> None:
    """At each final answer the agent names the conversation from what it has become (unless the user named it)."""
    title = " ".join(str(title or "").split())[:80]
    if title and not state.get("title_by_user"):
        state["title"] = title
