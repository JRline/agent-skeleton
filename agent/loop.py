"""Agent loop as a resumable step machine: plan -> execute -> review (docs/design.md §8).

status: planning -> running -> (awaiting_approval | awaiting_user -> running)* -> finished | aborted

`run_one_step(state, model)` advances exactly one step, persists, and returns an event.
`resolve_pending(state, ...)` applies the human's answer. `run(...)` drives both for CLI/tests.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from datetime import datetime
from typing import Callable

from pydantic import BaseModel

from . import config
from .memory import (ModelCallError, add_fact, add_feedback, add_turn, build_messages, call_model,
                     context_block, default_title, earlier_tasks, load_prompt, load_state, log, new_state,
                     next_ref, persist, read_log, searchable_docs, context_docs,
                     run_dir)
from .protocol import STOP, ProtocolError, extract_json, parse_action, request_json
from .skills import get_skill, render_skill_list
from .plugins import TOOLKITS, basic_toolkits, examples
from .tools import (TOOLS, ToolContext, dispatch, enabled_tools, needs_approval, quote_paths, read_doc,
                    sandbox_path)

__all__ = ["new_state", "load_state", "run_one_step", "resolve_pending", "abort", "run",
           "continue_with", "rename"]

WAITING = ("awaiting_approval", "awaiting_user", "awaiting_choice")
DONE = ("finished", "aborted")


def pending_question(state: dict) -> str:
    """The question text of a pending ask_user action."""
    p = state.get("pending") or {}
    return p.get("input", {}).get("question", "")


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


def event(state: dict, kind: str, **payload) -> dict:
    return {"kind": kind, "step": state["step_count"],
            "ts": datetime.now().isoformat(timespec="seconds"), "payload": payload}


def pick_effort(state: dict) -> str:
    if state.get("last_error") or state.get("repeat_flag"):
        return config.EFFORT["executor_recover"]
    return config.EFFORT["executor"]


def _ctx(state: dict) -> ToolContext:
    return ToolContext(run_id=state["run_id"], run_dir=run_dir(state), context=tuple(searchable_docs(state)))


def _caller(state: dict, model, purpose: str, stop=None):
    return lambda msgs, effort: call_model(state, model, msgs, purpose, effort, stop)


# ---------------------------------------------------------------- planner / reviewer

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


def _parse_model(cls):
    def parse(raw: str):
        try:
            return cls.model_validate(extract_json(raw))
        except ProtocolError:
            raise
        except Exception as e:
            raise ProtocolError(str(e)[:300]) from e
    return parse


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


def plan(state: dict, model) -> tuple[list[str], dict | None]:
    """Returns (steps, suggestion). A forced skill's steps need no model call. Otherwise the planner plans
    with the session's tools and may suggest other toolkits or a skill once per run, for the user to accept."""
    forced = get_skill(state.get("skill"))
    if forced:
        state["toolkits"] = list(dict.fromkeys(state["toolkits"] + forced.toolkits))
        return forced.steps, None
    prompt = load_prompt("planner", goal=state["goal"],
                         inputs=quote_paths(state["inputs"]) or "(none)",
                         tool_names=_enabled_by_toolkit(state),
                         other_toolkits=_other_toolkits(state), skills=render_skill_list(),
                         examples=examples("planner"),
                         earlier=earlier_tasks(state), context=context_block(state) or "CONTEXT: (none)")
    suggestion = None
    try:
        parsed, _, _ = request_json(_caller(state, model, "planner"),
                                    [{"role": "user", "content": prompt}],
                                    _parse_model(_Plan), config.EFFORT["planner"])
        if not state.get("suggested"):
            suggestion = _suggestion(state, parsed)
        if not state.get("title") and parsed.title:
            state["title"] = " ".join(parsed.title.split())[:60]
        # Drop placeholders copied from the prompt's shape example, e.g. "<step>".
        steps = [s.strip() for s in parsed.plan if s.strip() and not re.fullmatch(r"<[^>]*>", s.strip())][:7]
        if steps and all(s in TOOLS for s in steps):
            steps = []                  # the prompt's tool list copied back, not a plan for this goal
    except ProtocolError as e:
        state["warnings"].append(f"Planner failed ({e}); using the goal as a single step.")
        steps = []
    return steps or [state["goal"]], suggestion


def cited_outputs(state: dict, final: str) -> str:
    """Stored outputs of the refs cited in FINAL, so the reviewer can trace every number.
    Facts keep only a short preview, which hid most of a table and made the reviewer reject good answers."""
    parts, budget = [], config.REVIEW_CITED_CHARS
    inputs = {e.get("ref"): e.get("input") for e in read_log(state) if e["kind"] == "tool"}
    for ref in dict.fromkeys(re.findall(r"\[ref:\s*([\w\-]+)\s*\]", final)):
        path = run_dir(state) / "outputs" / f"{ref}.txt"
        if budget <= 0 or not path.exists():
            continue
        text = path.read_text(errors="replace")[:budget]
        parts.append(f"--- {ref} ---\ninput: {json.dumps(inputs.get(ref))}\n{text.strip()}")
        budget -= len(text)
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
    """Documentation the agent read (describe_dataset): units, filters and calendars the answer may state."""
    parts, seen = [], set()
    for ev in state["events"]:
        p = ev["payload"]
        obs = p.get("obs") or {}
        view = (p.get("input") or {}).get("view")
        if p.get("tool") == "describe_dataset" and obs.get("ok") and view not in seen:
            seen.add(view)
            stored = run_dir(state) / "outputs" / f"{obs['ref']}.txt"
            text = stored.read_text(errors="replace") if stored.is_file() else str(obs.get("result", ""))
            parts.append(f"--- documentation of view {view} [ref: {obs['ref']}] ---\n{text[:config.REVIEW_CITED_CHARS // 2]}")
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


def review_action(state: dict, model, tool: str, inp: dict) -> _ActionVerdict:
    """Check a data read or analysis request before the user is asked: required filters, aggregation level,
    load on the lake, logic. Issues go back to the executor; an approved request carries a plain explanation
    for the approval card. A reviewer that cannot answer lets the request through to the user."""
    request = inp.get("sql") or inp.get("code") or inp.get("path") or json.dumps(inp)
    extra = {k: v for k, v in inp.items() if k not in ("sql", "code") and v}
    step = state["plan"][state["step"]] if state["step"] < len(state["plan"]) else "Finish the goal."
    prompt = load_prompt("action_review", goal=state["goal"], step=step, tool=tool,
                         request=request + (f"\n{json.dumps(extra)}" if extra else ""),
                         earlier=_earlier_requests(state),
                         previous=_previous_request(state, tool)[:3000] or "(none)",
                         facts="\n".join(f"- {f}" for f in state["facts"]) or "(none)",
                         docs=_view_docs(state) or "(none read yet)",
                         inputs=quote_paths(state["inputs"]) or "(none)",
                         context=context_block(state) or "CONTEXT: (none)")
    try:
        verdict, _, _ = request_json(_caller(state, model, "action_review"), [{"role": "user", "content": prompt}],
                                     _parse_model(_ActionVerdict), config.EFFORT["action_review"])
    except ProtocolError as e:
        log(state, "review", ok=True, issues=[f"request reviewer unusable: {e}"], check="request")
        return _ActionVerdict(ok=True)
    log(state, "review", ok=verdict.ok, issues=verdict.issues, check="request", tool=tool)
    return verdict


def review(state: dict, model, final: str, tables=()) -> _Verdict:
    missing = uncited_numbers(state, final)
    if missing:                                      # no LLM call needed: the numbers have no source
        issues = [f"These numbers appear in no tool result or reference: {', '.join(missing[:8])}. "
                  "Compute them with a tool and cite the ref, or remove them."]
        out_dir = run_dir(state) / "outputs"
        log(state, "review", ok=False, issues=issues, check="citations",          # diagnostics for false flags
            evidence_files=sorted(p.name for p in out_dir.glob("*.txt")) if out_dir.exists() else [])
        return _Verdict(ok=False, issues=issues)
    skill = get_skill(state.get("skill"))
    checks = "".join(f"{i}. {c}\n" for i, c in enumerate(skill.checks if skill else [], start=5))
    prompt = load_prompt("reviewer", goal=state["goal"], final=final, checks=checks,
                         cited="\n".join(filter(None, [cited_outputs(state, final), _shown_tables(tables),
                                                       _view_docs(state)])),
                         facts="\n".join(f"- {f}" for f in state["facts"]) or "(none)",
                         context=context_block(state) or "CONTEXT: (none)")
    try:
        verdict, _, _ = request_json(_caller(state, model, "reviewer"),
                                     [{"role": "user", "content": prompt}],
                                     _parse_model(_Verdict), config.EFFORT["reviewer"])
    except ProtocolError as e:
        verdict = _Verdict(ok=False, issues=[f"Reviewer output unusable: {e}"])
    log(state, "review", ok=verdict.ok, issues=verdict.issues)
    return verdict


def finish(state: dict, model) -> str | None:
    """Fallback for small models that keep repeating a call after all plan steps are done:
    one plain request for the final answer from the facts, without the tool protocol.
    The answer still goes through the reviewer. None if the reply is unusable."""
    prompt = load_prompt("finisher", goal=state["goal"], facts="\n".join(f"- {f}" for f in state["facts"]))
    try:
        parsed, _, _ = request_json(_caller(state, model, "finisher"),
                                    [{"role": "user", "content": prompt}],
                                    _parse_model(_Final), config.EFFORT["finisher"])
    except ProtocolError:
        return None
    return parsed.final.strip() or None


# ---------------------------------------------------------------- state updates

def abort(state: dict, reason: str) -> dict:
    state["status"] = "aborted"
    state["pending"] = None
    log(state, "abort", reason=reason)
    return persist(state, event(state, "abort", reason=reason))


def _record_observation(state: dict, action: dict, obs: dict) -> None:
    """Feed the observation back, extract a fact, advance the plan, track errors."""
    add_turn(state, "user", "OBSERVATION: " + json.dumps(obs))
    tool = action.get("tool")
    if obs.get("ok"):
        state["last_error"] = None
        if tool == "ask_user":
            # Kept whole: the confirmed period, scope and level decide every later step, and a 120-char cut
            # once hid "period = latest date" from the request check, which then demanded a full-year scan.
            question = str(action.get("input", {}).get("question", ""))[:1500]
            add_fact(state, f"USER ANSWER to: {question} -> {str(obs.get('result', ''))[:1500]} "
                            f"[ref: {obs.get('ref')}]")
        else:
            args = json.dumps(action.get("input", {}))[:120]
            add_fact(state, f"{tool}({args}) -> {str(obs.get('result', ''))[:200]} [ref: {obs.get('ref')}]")
        state["done"].append({"step": state["step"], "tool": tool, "ref": obs.get("ref")})
        if action.get("step_done") and state["step"] < len(state["plan"]):
            state["step"] += 1
    else:
        state["last_error"] = obs.get("error")
    log(state, "tool", tool=tool, input=action.get("input"), ok=obs.get("ok"),
        ref=obs.get("ref"), error=obs.get("error"))


# ---------------------------------------------------------------- the step machine

def run_one_step(state: dict, model) -> dict:
    """A step either completes or, if the model can't be reached, leaves state as it was
    plus a `model_error` event, so the same step can simply be retried."""
    snapshot = copy.deepcopy(state)
    try:
        return _step(state, model)
    except ModelCallError as e:
        if _stopped(state):             # the stop cancelled this call; keep the run aborted
            return state["events"][-1]
        state.clear()
        state.update(snapshot)          # same dict object: callers keep their reference
        log(state, "model_error", error=str(e))
        return persist(state, event(state, "model_error", error=str(e)))


def _stopped(state: dict) -> bool:
    """The UI's Stop calls abort() on this same dict while a slow model call may still be running.
    Checked after every model call so a late reply never runs a tool or changes the run."""
    return state["status"] == "aborted"


def _step(state: dict, model) -> dict:
    status = state["status"]
    if status in DONE:
        return event(state, status, final=state.get("final"))
    if status in WAITING:
        return event(state, status, **(state["pending"] or {}))   # wait for the human

    if status == "planning":
        steps, suggestion = plan(state, model)
        if _stopped(state):
            return state["events"][-1]
        state["plan"] = steps
        if not state.get("title"):
            state["title"] = default_title(state["tasks"][0]["goal"] if state.get("tasks") else state["goal"])
        if suggestion:                  # the user decides; see resolve_pending(choice=...)
            state["suggested"] = True
            state["status"], state["pending"] = "awaiting_choice", suggestion
            return persist(state, event(state, "suggest", **suggestion))
        state["status"] = "running"
        return persist(state, event(state, "plan", plan=state["plan"], skill=state.get("skill")))

    if state["step_count"] - state.get("task_start", 0) >= config.MAX_STEPS:
        return abort(state, f"Reached the step limit ({config.MAX_STEPS}).")

    # 1-2. build prompt and call the model at the policy's effort
    messages = build_messages(state, model)
    effort = pick_effort(state)
    state["repeat_flag"] = False
    try:
        action, raw, attempts = request_json(
            _caller(state, model, "executor", STOP), messages, lambda text: _parse_executor(state, text), effort)
    except _NeedsToolkit as e:          # the model reached for a disabled toolkit: the user decides
        tool = TOOLS[e.args[0]]
        suggestion = {"toolkits": [tool.toolkit], "skill": None, "replan": False,
                      "why": f"The agent wants to use {tool.name}, which is in the {tool.toolkit} toolkit."}
        state["status"], state["pending"] = "awaiting_choice", suggestion
        return persist(state, event(state, "suggest", **suggestion))
    except ProtocolError as e:
        if _stopped(state):
            return state["events"][-1]
        state["consecutive_errors"] += 1
        state["last_error"] = str(e)
        if state["consecutive_errors"] >= config.MAX_CONSECUTIVE_ERRORS:
            return abort(state, f"Model kept replying outside the protocol: {e}")
        add_feedback(state, f"Your last reply could not be used ({e}). Follow the protocol exactly.")
        return persist(state, event(state, "error", error=str(e)))
    if _stopped(state):
        return state["events"][-1]
    state["consecutive_errors"] = 0
    state["step_count"] += 1
    act = action.model_dump(exclude_none=True)
    add_turn(state, "assistant", json.dumps(act))

    # 3. final answer -> reviewer gate
    if action.final is not None:
        tables = _result_tables(state, action.tables)
        return _final(state, model, action.final, action.thought, tables=[t["path"] for t in tables],
                      table_titles={t["path"]: t["title"] for t in tables}, conversation_title=action.title)

    # 4. repeated identical tool call -> don't run it again
    sig = action.signature()
    if sig == state["last_signature"]:
        state["repeats"] += 1
        if state["repeats"] >= config.MAX_REPEATS:
            if state["step"] >= len(state["plan"]) and state["facts"]:
                text = finish(state, model)
                if _stopped(state):
                    return state["events"][-1]
                if text:
                    return _final(state, model, text, "The model kept repeating; answer requested directly.",
                                  via="finisher")
            return abort(state, f"Repeated the same action: {action.tool}.")
        state["repeat_flag"] = True
        error = "You already ran this exact action. Use the earlier result or change approach."
        if not state["last_error"] and state["step"] < len(state["plan"]):
            # Repeating a call that worked usually means the step is done but the model never said so
            # (small models copy "step_done": false). Advance, so the STATE block shows what is next.
            state["step"] += 1
            error = "You already have this result in Facts. Move on to the next step, or give the final answer."
        add_turn(state, "user", "OBSERVATION: " + json.dumps({"ok": False, "error": error}))
        return persist(state, event(state, "repeat", tool=action.tool, input=action.input))
    state["repeats"] = 0
    state["last_signature"] = sig

    # 5. human-in-the-loop actions
    if TOOLS[action.tool].special:            # ask_user
        state["status"], state["pending"] = "awaiting_user", act
        return persist(state, event(state, "ask_user", question=action.input.get("question", ""),
                                    thought=action.thought))
    if needs_approval(TOOLS[action.tool], action.input, _ctx(state)):
        # What can never run (analysis code failing its safety check, a query missing a required filter) goes
        # back to the agent as a correction before the user is asked: never as a failed tool call.
        problem = TOOLS[action.tool].check(TOOLS[action.tool].args.model_validate(action.input)) \
            if TOOLS[action.tool].check else None
        if problem:
            state["last_signature"] = None               # it never ran: a corrected resubmission is not a repeat
            add_feedback(state, f"The {action.tool} request was not sent; fix it first: {problem}")
            return persist(state, event(state, "precheck", tool=action.tool, input=action.input,
                                        thought=action.thought, issues=[problem], check="static"))
        explain, notes = "", []
        if action.tool in config.REVIEWED_TOOLS:
            verdict = review_action(state, model, action.tool, action.input)
            if _stopped(state):
                return state["events"][-1]
            if not verdict.ok and state.get("action_rejections", 0) < config.MAX_ACTION_REJECTIONS:
                state["action_rejections"] = state.get("action_rejections", 0) + 1
                state["last_signature"] = None           # it never ran: resubmitting it is not a repeat
                add_feedback(state, f"The {action.tool} request was not sent; fix it first: " + "; ".join(verdict.issues))
                return persist(state, event(state, "precheck", tool=action.tool, input=action.input,
                                            thought=action.thought, issues=verdict.issues))
            explain, notes = verdict.explain, ([] if verdict.ok else verdict.issues)
        previous = _previous_request(state, action.tool)
        state["action_rejections"] = 0
        state["status"], state["pending"] = "awaiting_approval", act
        return persist(state, event(state, "approval", tool=action.tool, input=action.input,
                                    thought=action.thought, explain=explain, reviewer_notes=notes,
                                    previous=previous))

    # 6. run the tool and feed back the observation
    obs = dispatch(action.tool, action.input, _ctx(state), next_ref(state, action.tool))
    _record_observation(state, act, obs)
    return persist(state, event(state, "tool", tool=action.tool, input=action.input,
                                thought=action.thought, obs=obs, attempts=attempts, effort=effort))


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
        if (config.SANDBOX_DIR / path).is_file() and path not in [t["path"] for t in out]:
            out.append({"path": path, "title": title or Path(path).stem.replace("_", " ").capitalize()})
    return out


def _remember_results(state: dict, paths: list[str], titles: dict | None = None) -> None:
    """Result tables of the whole conversation, for the inline view and the Results page."""
    for path in paths:
        state.setdefault("results", []).append({"task": len(state.get("tasks", [])), "goal": state["goal"],
                                                "path": path,
                                                "title": (titles or {}).get(path) or Path(path).stem.replace("_", " ")})


def _final(state: dict, model, text: str, thought: str, **extra) -> dict:
    """Reviewer gate for a final answer, from the executor or the finisher fallback."""
    verdict = review(state, model, text, extra.get("tables", []))
    if _stopped(state):
        return state["events"][-1]
    if verdict.ok:
        state["status"], state["final"] = "finished", text
        _retitle(state, extra.pop("conversation_title", ""))
        _remember_results(state, extra.get("tables", []), extra.get("table_titles"))
        return persist(state, event(state, "final", text=text, thought=thought, **extra))
    state["review_rounds"] += 1
    if state["review_rounds"] > config.MAX_REVIEW_ROUNDS:
        state["warnings"].append("Finished with unresolved reviewer issues: " + "; ".join(verdict.issues))
        state["status"], state["final"] = "finished", text
        _retitle(state, extra.pop("conversation_title", ""))
        _remember_results(state, extra.get("tables", []), extra.get("table_titles"))
        return persist(state, event(state, "final", text=text, warnings=verdict.issues, **extra))
    extra.pop("conversation_title", None)
    add_feedback(state, "Reviewer rejected the final answer: " + "; ".join(verdict.issues))
    return persist(state, event(state, "review", issues=verdict.issues))


class _NeedsToolkit(Exception):
    """The executor asked for a tool that exists but whose toolkit is not enabled (and not declined)."""


def _parse_executor(state: dict, text: str):
    enabled = state.get("toolkits", basic_toolkits())
    try:
        return parse_action(text, enabled_tools(enabled))
    except ProtocolError:
        try:
            name = extract_json(text).get("tool")
        except ProtocolError:
            name = None
        # Only a tool whose toolkit is off is a suggestion; bad arguments for an enabled tool are a protocol error,
        # so the model gets the real message to repair (a real run asked twice to enable 'analysis', already on).
        if name in TOOLS and TOOLS[name].toolkit not in enabled and TOOLS[name].toolkit not in state.get("declined", []):
            raise _NeedsToolkit(name)
        raise


def _resolve_choice(state: dict, choice: dict) -> dict:
    """The user's answer to a suggestion: only offered items can be accepted.
    Skill → its steps and toolkits; toolkits only → plan again with them (or, mid-run, just continue);
    nothing → keep the plan and don't offer those toolkits again in this run."""
    offered = state["pending"]
    if offered.get("replan") is False:                  # asked mid-run for one toolkit
        accepted = [t for t in choice.get("toolkits") or [] if t in offered["toolkits"]]
        log(state, "user", kind_detail="choice", toolkits=accepted, skill=None)
        state["pending"], state["status"] = None, "running"
        if accepted:
            state["toolkits"] = list(dict.fromkeys(state["toolkits"] + accepted))
            add_feedback(state, f"The user enabled the {', '.join(accepted)} toolkit. Its tools are now listed.")
        else:
            state.setdefault("declined", []).extend(offered["toolkits"])
            add_feedback(state, f"The user did not enable the {', '.join(offered['toolkits'])} toolkit. "
                                "Continue with the enabled tools, or explain what is missing in your final answer.")
        return persist(state, event(state, "user", choice={"toolkits": accepted, "skill": None}))
    toolkits = [t for t in choice.get("toolkits") or [] if t in offered["toolkits"]]
    skill = get_skill(choice.get("skill")) if choice.get("skill") == offered.get("skill") else None
    log(state, "user", kind_detail="choice", toolkits=toolkits, skill=skill.name if skill else None)
    state["pending"] = None
    state["toolkits"] = list(dict.fromkeys(state["toolkits"] + toolkits + (skill.toolkits if skill else [])))
    ev = persist(state, event(state, "user", choice={"toolkits": toolkits, "skill": skill.name if skill else None}))
    if skill:
        state["skill"], state["plan"], state["status"] = skill.name, skill.steps, "running"
        return persist(state, event(state, "plan", plan=state["plan"], skill=skill.name))
    if toolkits:
        state["status"] = "planning"
        return persist(state) or ev
    state["status"] = "running"
    return persist(state, event(state, "plan", plan=state["plan"], skill=None))


def resolve_pending(state: dict, approved: bool | None = None, answer: str | None = None,
                    choice: dict | None = None, reason: str | None = None) -> dict:
    """Apply the human's response to a pending approval, question or suggestion, then resume.
    A rejection's reason goes back to the executor and into the facts: without it the agent can only guess
    what was wrong, and it once resent the same whole-year query in a "lighter" form."""
    if state["status"] not in WAITING or not state["pending"]:
        raise ValueError(f"Nothing pending (status={state['status']}).")
    if state["status"] == "awaiting_choice":
        return _resolve_choice(state, choice or {})
    pending = state["pending"]
    if state["status"] == "awaiting_user":
        if not (answer or "").strip():
            raise ValueError("An answer is required.")
        obs = {"ok": True, "ref": next_ref(state, "ask_user"), "result": answer.strip()}
        log(state, "user", kind_detail="answer", question=pending["input"].get("question"), answer=answer)
        ev_payload = {"answer": answer.strip(), "question": pending["input"].get("question", ""), "obs": obs}
    elif approved:
        log(state, "user", kind_detail="approve", tool=pending["tool"])
        obs = dispatch(pending["tool"], pending["input"], _ctx(state), next_ref(state, pending["tool"]))
        ev_payload = {"approved": True, "tool": pending["tool"], "obs": obs}
    else:
        reason = (reason or "").strip()[:1500]
        log(state, "user", kind_detail="reject", tool=pending["tool"], reason=reason or None)
        if reason:
            obs = {"ok": False, "error": f"User rejected {pending['tool']} because: {reason}\n"
                                         "Follow this reason; do not resend the same request in another form."}
            add_fact(state, f"USER REJECTED {pending['tool']} because: {reason}")
        else:
            obs = {"ok": False, "error": f"User rejected {pending['tool']}. Choose another approach."}
        ev_payload = {"approved": False, "tool": pending["tool"], **({"reason": reason} if reason else {})}
    _record_observation(state, pending, obs)
    state["pending"] = None
    state["status"] = "running"
    return persist(state, event(state, "user", **ev_payload))


# ---------------------------------------------------------------- conversations

def continue_with(state: dict, goal: str, inputs: list[str] | None = None, skill: str | None = None,
                  toolkits: list[str] | None = None) -> dict:
    """A new instruction in the same conversation, like a follow-up message to an assistant.
    Earlier turns, facts, refs and work tables stay available; the plan and per-instruction counters restart."""
    if state["status"] not in DONE:
        raise ValueError(f"Finish or stop the current instruction first (status={state['status']}).")
    state.setdefault("tasks", []).append({"goal": state["goal"], "status": state["status"],
                                          "final": state.get("final")})
    state["goal"], state["skill"] = goal, skill
    state["inputs"] = list(dict.fromkeys(state["inputs"] + list(inputs or [])))
    if toolkits is not None:
        state["toolkits"] = list(toolkits)
    state.update(status="planning", plan=[], step=0, final=None, pending=None, warnings=[], last_error=None,
                 repeat_flag=False, repeats=0, consecutive_errors=0, review_rounds=0, last_signature=None,
                 suggested=False, task_start=state["step_count"])
    add_turn(state, "user", f"NEW INSTRUCTION: {goal}")
    log(state, "user", kind_detail="instruction", goal=goal)
    return persist(state, event(state, "task", goal=goal, files=list(inputs or [])))


def rename(state: dict, title: str) -> None:
    """Name the conversation (shown on top and in the resume list). Empty names are ignored. A name the user
    gives is kept: the agent no longer retitles the conversation after that."""
    title = " ".join(title.split())[:80]
    if title:
        state["title"], state["title_by_user"] = title, True
        persist(state)


def _retitle(state: dict, title: str) -> None:
    """At each final answer the agent names the conversation from what it has become (unless the user named it)."""
    title = " ".join(str(title or "").split())[:80]
    if title and not state.get("title_by_user"):
        state["title"] = title


# ---------------------------------------------------------------- driver

def run(goal: str, model, inputs: list[str] | None = None,
        on_event: Callable[[dict], None] = lambda e: None,
        approve: Callable[[dict], bool] = lambda e: False,
        ask: Callable[[dict], str] = lambda e: "No answer available.",
        state: dict | None = None, context: list[str] | None = None, skill: str | None = None,
        toolkits: list[str] | None = None, choose: Callable[[dict], dict] = lambda e: {}) -> dict:
    """Run to completion, or stop at a model_error so it can be resumed later. Pass `state` to resume.
    `choose` answers a suggestion with the accepted part, e.g. {"toolkits": [...], "skill": ...}; {} declines."""
    state = state or new_state(goal, inputs, context=context, skill=skill, toolkits=toolkits)
    while state["status"] not in DONE:
        was_waiting = state["status"] in WAITING
        ev = run_one_step(state, model)
        if not was_waiting:              # a resumed wait was already shown
            on_event(ev)
        if ev["kind"] == "model_error":
            break
        if state["status"] == "awaiting_approval":
            on_event(resolve_pending(state, approved=approve(ev)))
        elif state["status"] == "awaiting_user":
            on_event(resolve_pending(state, answer=ask(ev)))
        elif state["status"] == "awaiting_choice":
            on_event(resolve_pending(state, choice=choose(ev)))
    return state
