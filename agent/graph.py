"""The agent as one LangChain `create_agent` graph (docs/design.md §8).

    Planner.before_agent -> Choice.before_agent -> model <-> tools -> AnswerCheck.after_agent -> end (or model)

Stock parts: create_agent (model/tool loop, ToolNode), HumanInTheLoopMiddleware (approve, or reject with a reason,
per call), SummarizationMiddleware (long conversations), interrupt() (questions, suggestions), the checkpointer
(save, resume) and static breakpoints after each tool call, plan and answer check, so the conversation driver
(loop.py) can advance one step at a time.

Custom middleware, each because no stock part does it:
- Planner / Choice: plan with the session's tools and skills; a suggested skill or toolkit waits for the user.
- SessionPrompt: rules, toolkit rules, skill or plan in the system prompt; only enabled toolkits' tools offered;
  a careful ("medium") retry after a failed tool call.
- RequestCheck: a data read or analysis request is checked (static check, then the request reviewer) before the user
  is asked to approve it; a call to a disabled toolkit asks the user to enable it.
- RepeatGuard: an identical repeated call is not run again; after MAX_REPEATS the answer is requested directly.
- AnswerCheck: every number must trace to a tool result (checks.uncited_numbers), then the reviewer.
Prompts are agent/prompts/*.md; the reply protocol is the chat model's (chat_model.py).
"""
from __future__ import annotations

import json
import re
from typing import Any, NotRequired

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import (AgentMiddleware, HumanInTheLoopMiddleware, ModelResponse,
                                         SummarizationMiddleware, hook_config)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.exceptions import OutputParserException
from langgraph.types import interrupt
from pydantic import ValidationError

from . import checks, config, credentials
from .chat_model import ReplyError, TextChatModel

UNUSABLE = (ReplyError, ValidationError, OutputParserException)   # a reply, not the backend, failed
from .lc_tools import approval_needed, lc_tools, run_id
from .memory import context_block, earlier_tasks, load_prompt, load_state, log, toolkit_guidance
from .plugins import TOOLKITS, basic_toolkits, examples
from .skills import get_skill, render_skill, render_skill_list
from .tools import TOOLS, enabled_tools, quote_paths

REPEAT_ERROR = "You already ran this exact action. Use the earlier result or change approach."
NOT_SENT = "request was not sent; fix it first:"


class RunState(AgentState):
    """LangChain's `messages` plus plain fields; checkpointed after every node."""
    goal: NotRequired[str]
    task: NotRequired[int]                   # instruction number in the conversation (follow-ups add one)
    planned: NotRequired[int]
    intro: NotRequired[str]                  # the instruction as the executor sees it (goal, files, context)
    inputs: NotRequired[list[str]]
    toolkits: NotRequired[list[str]]
    declined: NotRequired[list[str]]         # toolkits the user would not enable in this conversation
    skill: NotRequired[str | None]
    plan: NotRequired[list[str]]
    suggestion: NotRequired[dict | None]
    explain: NotRequired[dict[str, str]]     # tool_call_id -> request reviewer's explanation (approval card)
    reviewer_notes: NotRequired[dict[str, list[str]]]
    check_rejections: NotRequired[int]
    review_rounds: NotRequired[int]
    issues: NotRequired[list[str]]           # the answer reviewer's last issues
    status: NotRequired[str]                 # "" while working; finished | aborted
    final: NotRequired[str]
    title: NotRequired[str]


def view() -> dict:
    """The conversation record (state.json), saved by the driver before every step: events, facts, inputs."""
    return load_state(run_id())


def _toolkits(state) -> list[str]:
    return list(state.get("toolkits") or basic_toolkits())


# ---------------------------------------------------------------- plan

def make_plan(planner, goal: str, inputs: list[str], toolkits: list[str], v: dict, suggest: bool = True):
    """Plan with the session's tools; may suggest a skill or toolkits. Returns (steps, suggestion, title).
    A reply the planner cannot get right falls back to the goal as the only step."""
    prompt = load_prompt("planner", goal=goal, inputs=quote_paths(inputs) or "(none)",
                         tool_names=checks._enabled_by_toolkit({"toolkits": toolkits}),
                         other_toolkits=checks._other_toolkits({"toolkits": toolkits}),
                         skills=render_skill_list(), examples=examples("planner"), earlier=earlier_tasks(v),
                         context=context_block(v) or "CONTEXT: (none)")
    try:
        parsed = planner.invoke([HumanMessage(prompt)])
    except UNUSABLE as e:
        log(v, "warning", text=f"Planner failed ({e}); using the goal as a single step.")
        return [goal], None, ""
    suggestion = checks._suggestion({"toolkits": toolkits}, parsed) if suggest else None
    # Drop placeholders copied from the prompt's shape example ("<step>") and a copied tool list.
    steps = [s.strip() for s in parsed.plan if s.strip() and not re.fullmatch(r"<[^>]*>", s.strip())][:7]
    if steps and all(s in TOOLS for s in steps):
        steps = []
    return steps or [goal], suggestion, " ".join((parsed.title or "").split())[:60]


def plan(v: dict, chat) -> tuple[list[str], dict | None]:
    """The planner on its own (tests, maintenance): v is a conversation record, chat a backend model."""
    planner = TextChatModel(chat=chat).for_role("planner").with_structured_output(checks._Plan)
    steps, suggestion, _ = make_plan(planner, v["goal"], v.get("inputs") or [], _toolkits(v), v,
                                     not v.get("suggested"))
    return steps, suggestion


def _structured(model, schema, purpose: str):
    return model.with_structured_output(schema).with_config(metadata={"purpose": purpose})


class Planner(AgentMiddleware):
    state_schema = RunState

    def __init__(self, model: TextChatModel):
        super().__init__()
        self.planner = _structured(model, checks._Plan, "planner")

    def before_agent(self, state, runtime) -> dict[str, Any] | None:
        if state.get("planned") == state.get("task"):
            return None
        v = view()
        toolkits, skill = _toolkits(state), get_skill(state.get("skill"))
        if skill:                                         # a skill the user chose: its steps are the plan
            toolkits += skill.toolkits
            steps, suggestion, title = skill.steps, None, ""
        else:
            steps, suggestion, title = make_plan(self.planner, state["goal"], state.get("inputs") or [], toolkits, v,
                                                 not v.get("suggested"))
        return {"plan": steps, "skill": skill.name if skill else None, "suggestion": suggestion,
                "toolkits": list(dict.fromkeys(toolkits)), "planned": state.get("task"), "title": title,
                "status": "", "final": "", "issues": [], "review_rounds": 0, "check_rejections": 0,
                "messages": [HumanMessage(state.get("intro") or f"GOAL: {state['goal']}")]}


class Choice(AgentMiddleware):
    """A suggested skill or toolkit: the user decides (interrupt). Only offered items can be accepted.
    A skill brings its steps; toolkits alone mean planning again with them (no new suggestion)."""
    state_schema = RunState

    def __init__(self, model: TextChatModel):
        super().__init__()
        self.planner = _structured(model, checks._Plan, "planner")

    def before_agent(self, state, runtime) -> dict[str, Any] | None:
        offered = state.get("suggestion")
        if not offered:
            return None
        choice = interrupt({"type": "suggestion", **offered}) or {}
        kits = [t for t in choice.get("toolkits") or [] if t in offered["toolkits"]]
        skill = get_skill(choice.get("skill")) if choice.get("skill") == offered.get("skill") else None
        toolkits = list(dict.fromkeys(_toolkits(state) + kits + (skill.toolkits if skill else [])))
        update = {"suggestion": None, "toolkits": toolkits}
        if skill:
            update.update(skill=skill.name, plan=skill.steps)
        elif kits:
            update["plan"] = make_plan(self.planner, state["goal"], state.get("inputs") or [], toolkits, view(),
                                       suggest=False)[0]
        else:
            update["declined"] = list(state.get("declined") or []) + offered["toolkits"]
        return update


# ---------------------------------------------------------------- every model call

def _failed(message) -> bool:
    if not isinstance(message, ToolMessage):
        return False
    if message.status == "error":
        return True
    try:
        return json.loads(message.content).get("ok") is False
    except (ValueError, AttributeError):
        return False


def system_prompt(toolkits: list[str], skill: str | None = None, steps: list[str] | None = None,
                  step: int | None = None, declined=()) -> str:
    """What the executor knows in full, and what only by name (docs/design.md §7, "summaries until enabled"):
    rules and the enabled toolkits' rules; the accepted skill with its reference documents, or the plan (the
    current step marked); and the other toolkits as one line each, so the agent can ask for one. The full tool list
    and the reply protocol are added by the chat model, for enabled toolkits only. Each part is its own `## ` block,
    so a backend with memory receives it once and again only when it changes."""
    parts = [load_prompt("system", guidance=toolkit_guidance(toolkits))]
    active = get_skill(skill)
    lines = [f"{i + 1}. {s}" for i, s in enumerate((active.steps if active else steps) or [])]
    if active:
        parts.append(render_skill(active))
        refs = context_block({"skill": skill, "context": []})          # only once the skill is accepted
        if refs:
            parts.append("## Skill references\n" + refs)
    elif lines:
        parts.append("## Plan\n" + "\n".join(lines))
    if step is not None and lines:                 # its own block: the plan block stays the same between steps
        parts.append("## Current step\n" + (lines[step] if step < len(lines) else
                                             "All plan steps are done: give the final answer."))
    others = [f"- {n}: {k['description']} Tools: {', '.join(t.name for t in TOOLS.values() if t.toolkit == n)}."
              for n, k in TOOLKITS.items() if n not in toolkits and n not in declined]
    if others:
        parts.append("## Other toolkits (not enabled: call one of their tools and the user is asked to enable it)\n"
                     + "\n".join(others))
    return "\n\n".join(parts)


def current_step(messages) -> int:
    """Index of the plan step being worked on: tool calls marked step_done since this instruction began. Repeating
    a call that worked also counts: small models often never mark a step done and repeat it instead."""
    repeated = {m.tool_call_id for m in messages if isinstance(m, ToolMessage) and REPEAT_ERROR in str(m.content)}
    done = 0
    for m in messages:
        if isinstance(m, HumanMessage) and str(m.content).startswith(("GOAL:", "NEW INSTRUCTION:")):
            done = 0
        elif isinstance(m, AIMessage) and m.tool_calls and (m.additional_kwargs.get("step_done")
                                                            or m.tool_calls[0]["id"] in repeated):
            done += 1
    return done


def intro(v: dict) -> str:
    """The instruction as the executor sees it: goal (or follow-up), input files, the user's own context documents.
    A skill's reference documents are not here: they belong to the skill (system_prompt) and arrive once it is
    accepted."""
    text = f"GOAL: {v['goal']}" if not v.get("tasks") else (
        f"NEW INSTRUCTION: {v['goal']}\nEARLIER IN THIS CONVERSATION (their tool results are above and still "
        f"valid):\n{earlier_tasks(v)}")
    if v.get("inputs"):
        text += "\nINPUT FILES: " + quote_paths(v["inputs"])
    block = context_block({"context": v.get("context") or [], "skill": None, "inputs": v.get("inputs") or []})
    return text + ("\n\n" + block if block else "")


def first_prompt(v: dict) -> list[dict]:
    """What the executor is sent first for a conversation record: system prompt with tools and protocol, then the
    instruction (tests, maintenance)."""
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from .chat_model import PROTOCOL, render_tools
    toolkits = _toolkits(v)
    tools = [convert_to_openai_tool(t) for t in lc_tools() if t.name in enabled_tools(toolkits)]
    system = (system_prompt(toolkits, v.get("skill"), v.get("plan"), 0, v.get("declined") or ())
              + "\n\n" + render_tools(tools) + "\n\n" + PROTOCOL)
    return [{"role": "system", "content": system}, {"role": "user", "content": intro(v)}]


def compact(messages: list, keep: int | None = None) -> list:
    """The model's view of the history: the last `keep` tool results in full, older ones as one line with their ref
    (like the step machine's facts), so a long run does not resend every table and document. The graph keeps them
    whole: the reviewers, the number check and the Sources list read the stored outputs."""
    keep = config.KEEP_FULL_RESULTS if keep is None else keep
    results = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    old = set(results[:-keep]) if keep else set(results)
    calls = [i for i, m in enumerate(messages) if isinstance(m, AIMessage) and m.tool_calls]
    old_calls = set(calls[:-keep]) if keep else set(calls)
    out = []
    for i, m in enumerate(messages):
        if i in old_calls:                        # its own earlier SQL / code: the start is enough to recall it
            call = m.tool_calls[0]
            args = {k: (v[:160] + " …" if isinstance(v, str) and len(v) > 200 else v) for k, v in call["args"].items()}
            if args != call["args"]:
                m = m.model_copy(update={"tool_calls": [{**call, "args": args}]})
        if i in old and len(str(m.content)) > config.SHORT_RESULT_CHARS:
            try:
                obs = json.loads(m.content)
                line = str(obs.get("result") or obs.get("error") or "").split("\n", 1)[0][:200]
                text = json.dumps({"ok": obs.get("ok"), "ref": obs.get("ref"),
                                   "result": f"{line} …[earlier result, shortened; cite its ref]"}, ensure_ascii=False)
            except (ValueError, AttributeError):
                text = str(m.content)[:200] + " …[earlier result, shortened]"
            m = m.model_copy(update={"content": text})
        out.append(m)
    return out


class SessionPrompt(AgentMiddleware):
    state_schema = RunState

    def wrap_model_call(self, request, handler):
        state = request.state
        toolkits = _toolkits(state)
        enabled = set(enabled_tools(toolkits))
        parts = [system_prompt(toolkits, state.get("skill"), state.get("plan"), current_step(request.messages),
                               state.get("declined") or ())]
        settings = {}
        if isinstance(request.model, TextChatModel):     # settings of the JSON reply protocol (text-only models)
            declined = set(state.get("declined") or [])
            settings["other_tools"] = {n: t.toolkit for n, t in TOOLS.items()
                                       if n not in enabled and t.toolkit not in declined}
            if request.messages and _failed(request.messages[-1]):
                settings["effort"] = config.EFFORT["executor_recover"]    # think harder after an error
        return handler(request.override(system_message=SystemMessage(parts[0]), messages=compact(request.messages),
                                        tools=[t for t in request.tools if t.name in enabled],
                                        model_settings={**request.model_settings, **settings}))


class RepeatGuard(AgentMiddleware):
    """Small models sometimes repeat a call that already worked instead of moving on."""
    state_schema = RunState

    def __init__(self, finisher: TextChatModel):
        super().__init__()
        self.finisher = _structured(finisher, checks._Final, "finisher")

    def wrap_tool_call(self, request, handler):
        call, messages = request.tool_call, request.state["messages"]
        results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
        for m in reversed([m for m in messages if isinstance(m, AIMessage) and m.tool_calls][:-1]):
            prev, result = m.tool_calls[0], results.get(m.tool_calls[0]["id"])
            if result is not None and REPEAT_ERROR in str(result.content):
                continue                                  # an earlier repeat: compare with the call it repeated
            if prev["name"] == call["name"] and prev["args"] == call["args"] and result is not None \
                    and not _failed(result):
                return ToolMessage(json.dumps({"ok": False, "error": REPEAT_ERROR}), tool_call_id=call["id"],
                                   name=call["name"], status="error")
            break
        return handler(request)

    def wrap_model_call(self, request, handler):
        repeats = 0                                       # repeated calls in a row, at the end of the history
        for m in reversed(request.messages):
            if isinstance(m, AIMessage) and m.tool_calls:
                continue
            if not (isinstance(m, ToolMessage) and REPEAT_ERROR in str(m.content)):
                break
            repeats += 1
        if repeats < config.MAX_REPEATS:
            return handler(request)
        v = view()
        try:
            text = self.finisher.invoke([HumanMessage(load_prompt(
                "finisher", goal=request.state.get("goal", ""),
                facts="\n".join(f"- {f}" for f in v.get("facts", [])) or "(none)"))]).final
            msg = AIMessage(content=text, additional_kwargs={
                "via": "finisher", "thought": "The model kept repeating; answer requested directly."})
        except UNUSABLE:
            last = next((m for m in reversed(request.messages) if isinstance(m, AIMessage) and m.tool_calls), None)
            msg = AIMessage(content="", additional_kwargs={
                "abort": f"Repeated the same action: {last.tool_calls[0]['name'] if last else 'unknown'}."})
        msg.response_metadata["effort"] = config.EFFORT["finisher"]
        return ModelResponse(result=[msg])


class RequestCheck(AgentMiddleware):
    """Before the user is asked: toolkit enabled? static check passes? request reviewer content?"""
    state_schema = RunState

    def __init__(self, checker: TextChatModel):
        super().__init__()
        self.checker = _structured(checker, checks._ActionVerdict, "action_review")

    @staticmethod
    def _back(call: dict, text: str, check: str = "", issues=(), **update) -> dict:
        """The request goes back to the model as a failed call that never ran (artifact: why, for the record)."""
        return {"messages": [ToolMessage(json.dumps({"ok": False, "error": text}), tool_call_id=call["id"],
                                         name=call["name"], status="error",
                                         artifact={"check": check, "issues": list(issues)} if check else None)],
                "jump_to": "model", **update}

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime) -> dict[str, Any] | None:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return None
        call, update = last.tool_calls[0], {}
        kit = last.additional_kwargs.get("needs_toolkit")
        if kit:                                           # the model reached for a disabled toolkit
            choice = interrupt({"type": "suggestion", "toolkits": [kit], "skill": None, "replan": False,
                                "why": f"The agent wants to use {call['name']}, which is in the {kit} toolkit."})
            if kit not in ((choice or {}).get("toolkits") or []):
                return self._back(call, f"The user did not enable the {kit} toolkit. Continue with the enabled "
                                        "tools, or explain what is missing in your final answer.",
                                  declined=list(state.get("declined") or []) + [kit])
            update["toolkits"] = _toolkits(state) + [kit]
        tool = TOOLS.get(call["name"])
        if tool and tool.fn:
            try:
                args = tool.args.model_validate(call["args"])
            except ValidationError as e:                  # the model sees the real error and fixes the call
                return self._back(call, f"Bad input: {e.errors()[0]['msg']}", check="input",
                                  issues=[e.errors()[0]["msg"]], **update)
        if not tool or not approval_needed(call["name"], call["args"]):
            return update or None
        token = credentials.CURRENT.set(run_id())         # the check may look up the user's lake catalogue
        try:
            problem = tool.check(args) if tool.check else None
        finally:
            credentials.CURRENT.reset(token)
        explain, notes, rejections, check = "", [], state.get("check_rejections", 0), "static"
        if not problem and call["name"] in config.REVIEWED_TOOLS:
            check = "request"
            verdict = self._review(state, call)
            if not verdict.ok and rejections < config.MAX_ACTION_REJECTIONS:
                problem = "; ".join(verdict.issues)
            explain, notes = verdict.explain, ([] if verdict.ok else verdict.issues)
        if problem:
            log(view(), "review", check="request", tool=call["name"], ok=False, issues=[problem])
            return self._back(call, f"The {call['name']} {NOT_SENT} {problem}", check=check,
                              issues=[problem] if check == "static" else notes, check_rejections=rejections + 1,
                              **update)
        return {**update, "check_rejections": 0, "explain": {**(state.get("explain") or {}), call["id"]: explain},
                "reviewer_notes": {**(state.get("reviewer_notes") or {}), call["id"]: notes}}

    def _review(self, state, call) -> checks._ActionVerdict:
        # The same tool's last request, from this graph's own history: it may have been sent back moments ago.
        earlier = [c for m in state["messages"][:-1] if isinstance(m, AIMessage) for c in m.tool_calls
                   if c["name"] == call["name"]]
        previous = str(earlier[-1]["args"].get("sql") or earlier[-1]["args"].get("code") or
                       earlier[-1]["args"].get("path") or "") if earlier else None
        plan = state.get("plan") or []
        step = current_step(state["messages"])
        current = [plan[step]] if step < len(plan) else []         # the step the request serves, as the old engine did
        return review_request(self.checker, view(), call["name"], call["args"], current, previous)


def review_request(checker, v: dict, tool: str, args: dict, steps: list[str] | None = None,
                   previous: str | None = None) -> checks._ActionVerdict:
    """Required filters, aggregation level, load on the lake, logic (agent/prompts/action_review.md). An unusable
    reviewer lets the request through to the user. `checker` is a structured-output runnable or a backend model."""
    if not hasattr(checker, "invoke"):
        checker = TextChatModel(chat=checker).for_role("action_review").with_structured_output(checks._ActionVerdict)
    request = args.get("sql") or args.get("code") or args.get("path") or json.dumps(args)
    extra = {k: val for k, val in args.items() if k not in ("sql", "code") and val}
    if previous is None:
        previous = checks._previous_request(v, tool)
    prompt = load_prompt("action_review", goal=v.get("goal", ""), tool=tool,
                         step="; ".join(steps if steps is not None else (v.get("plan") or [])[:1]) or "Finish the goal.",
                         request=request + (f"\n{json.dumps(extra)}" if extra else ""),
                         earlier=checks._earlier_requests(v), previous=previous[:3000] or "(none)",
                         facts="\n".join(f"- {f}" for f in v.get("facts", [])) or "(none)",
                         docs=checks._view_docs(v) or "(none read yet)",
                         inputs=quote_paths(v.get("inputs") or []) or "(none)",
                         context=context_block(v) or "CONTEXT: (none)")
    try:
        verdict = checker.invoke([HumanMessage(prompt)])
    except UNUSABLE as e:
        log(v, "review", ok=True, issues=[f"request reviewer unusable: {e}"], check="request")
        return checks._ActionVerdict(ok=True)
    log(v, "review", ok=verdict.ok, issues=verdict.issues, check="request", tool=tool)
    return verdict


def _approval_text(tool_call, state, runtime) -> str:
    return (state.get("explain") or {}).get(tool_call["id"]) or f"{tool_call['name']} needs your approval."


def approvals() -> HumanInTheLoopMiddleware:
    """Approve, or reject with a reason, each call that writes, sends or reads user data (tool.risky)."""
    risky = [t.name for t in TOOLS.values() if t.risky and not t.special]
    return HumanInTheLoopMiddleware(
        interrupt_on={name: {"allowed_decisions": ["approve", "reject"], "description": _approval_text,
                             "when": lambda req: approval_needed(req.tool_call["name"], req.tool_call["args"])}
                      for name in risky},
        interrupt_mode="per_call")


# ---------------------------------------------------------------- answer

class AnswerCheck(AgentMiddleware):
    """Numbers first (deterministic, cannot be talked round), then the reviewer. Issues go back to the model up to
    MAX_REVIEW_ROUNDS times; after that the answer is returned with the issues as warnings."""
    state_schema = RunState

    def __init__(self, reviewer: TextChatModel):
        super().__init__()
        self.reviewer = _structured(reviewer, checks._Verdict, "reviewer")

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state, runtime) -> dict[str, Any] | None:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        if last.additional_kwargs.get("abort"):
            return {"status": "aborted", "final": last.additional_kwargs["abort"]}
        v, final = view(), last.text
        tables = [t["path"] for t in checks._result_tables(v, last.additional_kwargs.get("tables"))]
        missing = checks.uncited_numbers(v, final)
        if missing:
            issues = [f"These numbers appear in no tool result or reference: {', '.join(missing[:8])}. "
                      "Compute them with a tool and cite the ref, or remove them."]
            log(v, "review", ok=False, issues=issues, check="citations")
        else:
            issues = self._review(v, state, final, tables)
        rounds = state.get("review_rounds", 0) + (1 if issues else 0)
        if issues and rounds <= config.MAX_REVIEW_ROUNDS:
            return {"review_rounds": rounds, "issues": issues, "jump_to": "model",
                    "messages": [HumanMessage("Reviewer rejected the final answer: " + "; ".join(issues),
                                              additional_kwargs={"issues": issues})]}
        return {"status": "finished", "final": final, "issues": issues}

    def _review(self, v, state, final, tables) -> list[str]:
        skill = get_skill(state.get("skill"))
        prompt = load_prompt(
            "reviewer", goal=state.get("goal", ""), final=final,
            checks="".join(f"{i}. {c}\n" for i, c in enumerate(skill.checks if skill else [], start=5)),
            cited="\n".join(filter(None, [checks.cited_outputs(v, final), checks._shown_tables(tables),
                                          checks._view_docs(v)])),
            facts="\n".join(f"- {f}" for f in v.get("facts", [])) or "(none)",
            context=context_block(v) or "CONTEXT: (none)")
        try:
            verdict = self.reviewer.invoke([HumanMessage(prompt)])
        except UNUSABLE as e:
            verdict = checks._Verdict(ok=False, issues=[f"Reviewer output unusable: {e}"])
        log(v, "review", ok=verdict.ok, issues=verdict.issues)
        return [] if verdict.ok else verdict.issues


# ---------------------------------------------------------------- build

STEP_ENDS = ["Choice.before_agent", "tools", "AnswerCheck.after_agent"]


def build(chat, checkpointer):
    """`chat` is an agent.adapter.ChatModel (backend). Breakpoints after STEP_ENDS make each invoke one step.
    A text-only backend is wrapped in TextChatModel (JSON reply protocol); a LangChain chat model (adapter
    LangChainModel, `.lc`) is used as it is, with its native tool calling and structured output."""
    native = getattr(chat, "lc", None)
    base = native if native is not None else TextChatModel(chat=chat)

    def role(purpose: str):
        return base if native is not None else base.for_role(purpose)
    middleware = [Planner(role("planner")), Choice(role("planner")), SessionPrompt()]
    if not getattr(chat, "remembers", False):           # a backend with memory keeps the history itself
        middleware.append(SummarizationMiddleware(model=role("summariser"),
                                                  trigger=("tokens", config.PROMPT_BUDGET_TOKENS),
                                                  keep=("messages", config.RECENT_TURNS * 2)))
    middleware += [RequestCheck(role("action_review")), RepeatGuard(role("finisher")),
                   approvals(), AnswerCheck(role("reviewer"))]
    return create_agent(role("executor"), tools=lc_tools(), state_schema=RunState, middleware=middleware,
                        checkpointer=checkpointer, interrupt_after=STEP_ENDS)
