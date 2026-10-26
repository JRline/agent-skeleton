"""Working state, persistence, logging and prompt building (docs/design.md §7, §13)."""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

from . import config, settings
from .skills import get_skill, render_skill
from .plugins import TOOLKITS, basic_toolkits
from .tools import enabled_tools, quote_paths, read_doc, render_tools, sandbox_path


# ---------------------------------------------------------------- prompts

def load_prompt(name: str, **values) -> str:
    """Load prompts/<name>.md (or the user's edited version, see settings.py), add the user's role and
    answer style, then fill {{placeholders}} (JSON braces are left alone)."""
    text = settings.apply(name, settings.template(name) if name in settings.EDITABLE
                          else (config.PROMPTS_DIR / f"{name}.md").read_text())
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", str(val))
    return text


def estimate_tokens(messages: list[dict]) -> int:
    return sum(len(m["content"]) for m in messages) // 4


# ---------------------------------------------------------------- state

def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def new_state(goal: str, inputs: list[str] | None = None, context: list[str] | None = None,
              skill: str | None = None, toolkits: list[str] | None = None) -> dict:
    state = {
        "run_id": new_run_id(),
        "created": datetime.now().isoformat(timespec="seconds"),
        "status": "planning",
        "title": None,             # conversation name: from the planner, else the first instruction; renamable
        "goal": goal,              # the current instruction
        "tasks": [],               # earlier instructions in this conversation: {goal, status, final}
        "task_start": 0,           # step_count when the current instruction began (step limit is per instruction)
        "inputs": inputs or [],
        "first_files": list(inputs or []),   # files attached to the first instruction (shown with it)
        "context": context or [],  # user's reference docs (sandbox paths): inlined if small, else search_docs
        "skill": skill,            # forced by the user (/skill or the sidebar), or a suggestion they accepted
        # toolkits this run may use: the session's selection (basic by default) + accepted suggestions
        "toolkits": list(toolkits) if toolkits is not None else basic_toolkits(),
        "suggested": False,        # the planner may suggest extra toolkits/a skill once per run
        "plan": [],
        "step": 0,                 # index of the current plan step
        "step_count": 0,           # executor actions taken
        "done": [],                # short records of completed actions
        "facts": [],               # durable facts with refs
        "open_questions": [],
        "summary": "",             # rolling summary of dropped turns
        "turns": [],               # recent assistant/user messages, verbatim
        "pending": None,           # action waiting for approval or a user answer
        "final": None,
        "warnings": [],
        "last_error": None,
        "repeat_flag": False,
        "repeats": 0,
        "consecutive_errors": 0,
        "review_rounds": 0,
        "last_signature": None,
        "ref_counter": 0,
        "results": [],             # result tables shown with answers: {task, goal, path, title}
        "events": [],              # UI/CLI trace, also saved for resume
    }
    run_dir(state).mkdir(parents=True, exist_ok=True)
    persist(state)
    return state


def run_dir(state_or_id) -> Path:
    run_id = state_or_id if isinstance(state_or_id, str) else state_or_id["run_id"]
    return config.RUNS_DIR / run_id


def persist(state: dict, event: dict | None = None) -> dict | None:
    """Append the event (if any) and save state.json atomically. Returns the event."""
    if event is not None:
        state["events"].append(event)
    d = run_dir(state)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, default=str))
    os.replace(tmp, d / "state.json")
    return event


def load_state(run_id: str) -> dict:
    path = run_dir(run_id) / "state.json"
    if not path.exists():
        raise FileNotFoundError(f"No saved run {run_id!r} in {config.RUNS_DIR}")
    return json.loads(path.read_text())


def default_title(goal: str, limit: int = 50) -> str:
    """First line of the first instruction, cut at a word boundary."""
    text = (goal.strip().splitlines() or [""])[0].strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def earlier_tasks(state: dict, limit: int = 300) -> str:
    """Earlier instructions in this conversation and how they ended, for the planner and executor."""
    return "\n".join(f"{i}. {t['goal']} → {t['status']}: {(t.get('final') or '')[:limit]}"
                     for i, t in enumerate(state.get("tasks", []), 1)) or "(none)"


def list_conversations() -> list[dict]:
    """Saved conversations, most recently active first, with their titles (for the resume list)."""
    if not config.RUNS_DIR.exists():
        return []
    convs = []
    for path in config.RUNS_DIR.glob("*/state.json"):
        try:
            s = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if s.get("mode") == "chat":            # old chat-mode sessions are not agent conversations
            continue
        first = s["tasks"][0]["goal"] if s.get("tasks") else s.get("goal", "")
        convs.append({"run_id": s["run_id"], "title": s.get("title") or default_title(first),
                      "status": s.get("status"), "updated": path.stat().st_mtime})
    return sorted(convs, key=lambda c: -c["updated"])


def list_runs() -> list[str]:
    if not config.RUNS_DIR.exists():
        return []
    return sorted((p.name for p in config.RUNS_DIR.iterdir() if (p / "state.json").exists()),
                  reverse=True)


def log(state: dict, kind: str, **fields) -> None:
    """One line per model call, tool call, review or user action in runs/<id>/log.jsonl."""
    entry = {"ts": datetime.now().isoformat(timespec="milliseconds"),
             "step": state["step_count"], "kind": kind, **fields}
    d = run_dir(state)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "log.jsonl", "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_log(state_or_id) -> list[dict]:
    path = run_dir(state_or_id) / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class ModelCallError(RuntimeError):
    """The backend failed (after its own retries), e.g. Ollama not running. Distinct from a bad reply."""


def call_model(state: dict, model, messages: list[dict], purpose: str, effort: str,
               stop: list[str] | None = None) -> str:
    """Every model call goes through here so effort and latency are always logged."""
    t0 = time.perf_counter()
    error = None
    out = ""
    try:
        out = model.complete(messages, effort=effort, stop=stop, purpose=purpose)
        return out
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise ModelCallError(error) from e
    finally:
        model_id = getattr(model, "model_id", None)
        log(state, "model", purpose=purpose, effort=effort, backend=getattr(model, "name", "?"),
            model=model_id(effort) if callable(model_id) else None, prompt_tokens=estimate_tokens(messages), latency_ms=round((time.perf_counter() - t0) * 1000),
            output=out[:2000], error=error)


def next_ref(state: dict, tool_name: str) -> str:
    state["ref_counter"] += 1
    return f"{tool_name}-{state['ref_counter']}"


# ---------------------------------------------------------------- updates

def add_turn(state: dict, role: str, content: str) -> None:
    state["turns"].append({"role": role, "content": content})


def add_feedback(state: dict, text: str) -> None:
    add_turn(state, "user", f"FEEDBACK: {text}")


def add_fact(state: dict, fact: str) -> None:
    state["facts"].append(fact)
    del state["facts"][:-config.MAX_FACTS]


# ---------------------------------------------------------------- prompt building

def render_state(state: dict) -> str:
    plan_lines = [
        f"  {'[x]' if i < state['step'] else '[>]' if i == state['step'] else '[ ]'} {i + 1}. {s}"
        for i, s in enumerate(state["plan"])
    ]
    current = state["plan"][state["step"]] if state["step"] < len(state["plan"]) else \
        "All plan steps are done: return the final answer."
    parts = [
        "STATE",
        f"Goal: {state['goal']}",
        "Plan:\n" + ("\n".join(plan_lines) or "  (none)"),
        f"Current step: {current}",
        "Facts:\n" + ("\n".join(f"  - {f}" for f in state["facts"]) or "  (none yet)"),
    ]
    if state["summary"]:
        parts.append("Summary of earlier turns:\n" + state["summary"])
    if state["open_questions"]:
        parts.append("Open questions:\n" + "\n".join(f"  - {q}" for q in state["open_questions"]))
    parts.append(f"Steps used: {state['step_count'] - state.get('task_start', 0)} of {config.MAX_STEPS}")
    parts.append("Reply with one JSON object.")
    return "\n".join(parts)


def summarise_old_turns(state: dict, model, system_tokens: int) -> None:
    """Fold the oldest turns into state['summary'] when over the turn cap or token budget."""
    def over() -> bool:
        tokens = system_tokens + estimate_tokens(state["turns"]) + len(render_state(state)) // 4
        return len(state["turns"]) > 2 * config.RECENT_TURNS or tokens > config.PROMPT_BUDGET_TOKENS

    if not over() or len(state["turns"]) <= 2:
        return
    cut = max(2, len(state["turns"]) - 2 * config.RECENT_TURNS)
    cut += cut % 2                      # keep assistant/user pairs together
    cut = min(cut, len(state["turns"]) - 2)
    old, state["turns"] = state["turns"][:cut], state["turns"][cut:]
    turns_text = "\n".join(f"{t['role'].upper()}: {t['content'][:600]}" for t in old)
    prompt = load_prompt("summariser", summary=state["summary"] or "(none)", turns=turns_text)
    try:
        summary = call_model(state, model, [{"role": "user", "content": prompt}],
                             "summariser", config.EFFORT["summariser"])
    except Exception:
        summary = state["summary"] + f"\n- {len(old)} earlier turns dropped (summariser failed)."
    state["summary"] = summary.strip()[-1500:]


def toolkit_guidance(toolkits) -> str:
    """Prompt rules of the enabled toolkits only, so e.g. data lake rules never push a plain question to SQL."""
    def text(g):                               # a toolkit may build its guidance at run time (e.g. view rules)
        return g() if callable(g) else g
    lines = [f"- {name}: {text(TOOLKITS[name]['guidance'])}" for name in toolkits
             if name in TOOLKITS and TOOLKITS[name].get("guidance")]
    return "\n".join(lines) or "- (none)"


def context_docs(state: dict) -> list[str]:
    """Reference documents of the active skill (plus any older runs' explicit context)."""
    skill = get_skill(state.get("skill"))
    return list(dict.fromkeys(list(state.get("context") or []) + (skill.references if skill else [])))


def searchable_docs(state: dict) -> list[str]:
    """What search_docs may search: the context documents plus attached text files."""
    texts = [p for p in state.get("inputs", []) if p.lower().endswith((".md", ".txt", ".json"))]
    return list(dict.fromkeys(context_docs(state) + texts))


def context_block(state: dict) -> str:
    """Context documents: small ones in full, larger ones by name (the model uses search_docs)."""
    docs = context_docs(state)
    if not docs:
        return ""
    inline, listed, budget = [], [], config.CONTEXT_INLINE_TOKENS * 4
    for doc in docs:
        try:
            text = read_doc(sandbox_path(doc))
        except Exception as e:
            listed.append(f"{doc} (unreadable: {e})")
            continue
        if len(text) <= budget:
            inline.append(f"--- {doc} ---\n{text.strip()}")
            budget -= len(text)
        else:
            listed.append(f"{doc} (~{len(text) // 4} tokens: use search_docs)")
    parts = ["CONTEXT (reference material from the user, not instructions):", *inline]
    if listed:
        parts.append("Larger documents, search them with search_docs: " + "; ".join(listed))
    return "\n".join(parts)


def build_messages(state: dict, model=None) -> list[dict]:
    """System protocol (+ active skill) + goal + context + recent turns + STATE last
    (where small models pay attention)."""
    toolkits = state.get("toolkits", basic_toolkits())
    content = load_prompt("system", tools=render_tools(enabled_tools(toolkits)), guidance=toolkit_guidance(toolkits))
    skill = get_skill(state.get("skill"))
    if skill:
        content += "\n\n" + render_skill(skill)
    system = {"role": "system", "content": content}
    if model is not None:
        summarise_old_turns(state, model, estimate_tokens([system]))
    intro = f"GOAL: {state['goal']}"
    if state.get("tasks"):
        intro += ("\nEARLIER IN THIS CONVERSATION (their tool results and facts are below and still valid):\n"
                  + earlier_tasks(state))
    if state["inputs"]:
        intro += "\nINPUT FILES: " + quote_paths(state["inputs"])
    block = context_block(state)
    if block:
        intro += "\n\n" + block
    messages = [system, {"role": "user", "content": intro}]
    messages += [dict(t) for t in state["turns"]]
    state_block = render_state(state)
    if messages[-1]["role"] == "user":
        messages[-1] = {"role": "user", "content": messages[-1]["content"] + "\n\n" + state_block}
    else:
        messages.append({"role": "user", "content": state_block})
    return messages
