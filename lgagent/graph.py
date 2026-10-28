"""The agent as a LangGraph graph: plan -> agent (LangChain create_agent) -> answer check -> done or back.

Stock parts: create_agent (model/tool loop, ToolNode), HumanInTheLoopMiddleware (approve or reject with a reason),
ToolCallLimitMiddleware (step limit), ModelRetryMiddleware (transport errors), SummarizationMiddleware (long
conversations), interrupt() (questions to the user), a SQLite checkpointer (saving, resume), callbacks (log).

Custom parts, each because no stock part does it:
- SessionPrompt: the session's rules, toolkits, skill, plan, files and reference documents in the system prompt,
  and only the enabled toolkits' tools offered.
- RequestCheck: a data read or analysis request is checked (static check, then the request reviewer) before the
  user is asked to approve it; problems go back to the model instead.
- check_answer: every number in the answer must trace to a tool result (loop.uncited_numbers), then the reviewer.
Prompts for the planner, request check and reviewer are the existing ones (agent/prompts/).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, NotRequired

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import (AgentMiddleware, HumanInTheLoopMiddleware, ModelRetryMiddleware,
                                         SummarizationMiddleware, ToolCallLimitMiddleware, hook_config)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent import config, loop
from agent.memory import context_block, load_prompt, toolkit_guidance
from agent.plugins import TOOLKITS, basic_toolkits, examples
from agent.skills import get_skill, render_skill, render_skill_list
from agent.tools import TOOLS, enabled_tools, quote_paths

from .chat_model import TextChatModel
from .tools import approval_needed, lc_tools, log, run_id

AGENT_PROMPT = (Path(__file__).parent / "prompts" / "agent.md").read_text()


class RunState(AgentState):
    """Plain, JSON-like fields next to LangChain's `messages`; checkpointed after every node."""
    goal: NotRequired[str]
    inputs: NotRequired[list[str]]
    toolkits: NotRequired[list[str]]
    skill: NotRequired[str | None]
    plan: NotRequired[list[str]]
    title: NotRequired[str]
    explain: NotRequired[dict[str, str]]       # tool_call_id -> request reviewer's explanation (approval card)
    check_rejections: NotRequired[int]
    review_rounds: NotRequired[int]
    final: NotRequired[str]
    tables: NotRequired[list[str]]
    status: NotRequired[str]                   # finished | aborted
    warnings: NotRequired[list[str]]


def _shim(state: dict) -> dict:
    """The fields the step machine's helpers read (number check, cited outputs, context block)."""
    events = [{"kind": "tool", "payload": {"obs": {"result": m.content}}}
              for m in state.get("messages", []) if isinstance(m, ToolMessage)]
    return {"run_id": run_id(), "goal": state.get("goal", ""), "tasks": [], "events": events,
            "skill": state.get("skill"), "context": [], "facts": _facts(state)}


def _facts(state: dict) -> list[str]:
    out = []
    for m in state.get("messages", []):
        if isinstance(m, ToolMessage):
            try:
                obs = json.loads(m.content)
                out.append(f"[{obs.get('ref')}] {m.name}: " + str(obs.get("result") or obs.get("error"))[:300])
            except (ValueError, AttributeError):
                out.append(f"{m.name}: {str(m.content)[:300]}")
    return out


# ---------------------------------------------------------------- middleware (custom)

class SessionPrompt(AgentMiddleware):
    """System prompt and tool list per session, read from the graph state at every model call."""
    state_schema = RunState

    def wrap_model_call(self, request, handler):
        state = request.state
        toolkits = state.get("toolkits") or basic_toolkits()
        allowed = set(enabled_tools(toolkits))
        parts = [AGENT_PROMPT.replace("{{guidance}}", toolkit_guidance(toolkits))]
        skill = get_skill(state.get("skill"))
        if skill:
            parts.append(render_skill(skill))
        elif state.get("plan"):
            parts.append("## Plan\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(state["plan"], 1)))
        parts.append("INPUT FILES (inside the sandbox): " + (quote_paths(state.get("inputs") or []) or "(none)"))
        ctx = context_block({"skill": state.get("skill"), "context": []})
        if ctx:
            parts.append(ctx)
        return handler(request.override(system_message=SystemMessage("\n\n".join(parts)),
                                        tools=[t for t in request.tools if t.name in allowed]))


class RequestCheck(AgentMiddleware):
    """Before approval: the tool's static check, then the request reviewer (existing action_review prompt)."""
    state_schema = RunState

    def __init__(self, checker: TextChatModel):
        super().__init__()
        self.checker = checker.with_structured_output(loop._ActionVerdict).with_config(
            metadata={"purpose": "action_review"})

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime) -> dict[str, Any] | None:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return None
        call = last.tool_calls[0]
        tool = TOOLS.get(call["name"])
        if not tool or not approval_needed(call["name"], call["args"]):
            return None
        try:
            problem = tool.check(tool.args.model_validate(call["args"])) if tool.check else None
        except Exception as e:                            # bad arguments: ToolNode reports them as usual
            problem = None if "validation" in type(e).__name__.lower() else str(e)
        explain = ""
        if not problem and call["name"] in config.REVIEWED_TOOLS:
            verdict = self._review(state, call)
            rejections = state.get("check_rejections", 0)
            if not verdict.ok and rejections < config.MAX_ACTION_REJECTIONS:
                problem = "; ".join(verdict.issues)
            explain = verdict.explain
        if problem:
            log(run_id(), "review", check="request", tool=call["name"], ok=False, issues=[problem])
            return {"messages": [ToolMessage(f"The {call['name']} request was not sent; fix it first: {problem}",
                                             tool_call_id=call["id"], name=call["name"], status="error")],
                    "check_rejections": state.get("check_rejections", 0) + 1, "jump_to": "model"}
        return {"check_rejections": 0, "explain": {**state.get("explain", {}), call["id"]: explain}}

    def _review(self, state, call) -> loop._ActionVerdict:
        args = call["args"]
        request = args.get("sql") or args.get("code") or args.get("path") or json.dumps(args)
        earlier = [f"{c['name']}: {c['args'].get('sql') or c['args'].get('code') or c['args']}"[:1500]
                   for m in state["messages"][:-1] if isinstance(m, AIMessage) for c in m.tool_calls
                   if c["name"] in config.REVIEWED_TOOLS]
        docs = [m.content for m in state["messages"] if isinstance(m, ToolMessage) and m.name == "describe_dataset"]
        plan = state.get("plan") or []
        prompt = load_prompt("action_review", goal=state.get("goal", ""), tool=call["name"],
                             step="; ".join(plan) or "Finish the goal.", request=request,
                             earlier="\n\n".join(earlier) or "(none)",
                             previous=next((e for e in reversed(earlier) if e.startswith(call["name"])), "(none)"),
                             facts="\n".join(f"- {f}" for f in _facts(state)) or "(none)",
                             docs="\n".join(d[:4000] for d in docs) or "(none read yet)",
                             inputs=quote_paths(state.get("inputs") or []) or "(none)",
                             context=context_block({"skill": state.get("skill"), "context": []}) or "CONTEXT: (none)")
        try:
            return self.checker.invoke([HumanMessage(prompt)])
        except Exception as e:                            # an unusable reviewer lets the request through to the user
            return loop._ActionVerdict(ok=True, explain=f"(request check unavailable: {str(e)[:100]})")


def _approval_text(tool_call, state, runtime) -> str:
    """What the approval card says: the request reviewer's plain explanation."""
    return state.get("explain", {}).get(tool_call["id"]) or f"{tool_call['name']} needs your approval."


def approvals() -> HumanInTheLoopMiddleware:
    risky = [t.name for t in TOOLS.values() if t.risky and not t.special]
    return HumanInTheLoopMiddleware(
        interrupt_on={name: {"allowed_decisions": ["approve", "reject"], "description": _approval_text,
                             "when": lambda req: approval_needed(req.tool_call["name"], req.tool_call["args"])}
                      for name in risky},
        interrupt_mode="per_call")


# ---------------------------------------------------------------- graph nodes

def make_plan_node(planner: TextChatModel):
    structured = planner.with_structured_output(loop._Plan).with_config(metadata={"purpose": "planner"})

    def plan(state: RunState) -> dict:
        toolkits = list(state.get("toolkits") or basic_toolkits())
        skill = get_skill(state.get("skill"))
        steps: list[str] = []
        if not skill:
            groups: dict[str, list[str]] = {}
            for t in enabled_tools(toolkits).values():
                groups.setdefault(t.toolkit, []).append(t.name)
            prompt = load_prompt(
                "planner", goal=state["goal"], inputs=quote_paths(state.get("inputs") or []) or "(none)",
                tool_names="\n".join(f"- {k}: {', '.join(v)}" for k, v in groups.items()),
                other_toolkits="\n".join(f"- {n}: {k['description']}" for n, k in TOOLKITS.items()
                                         if n not in toolkits) or "(none)",
                skills=render_skill_list(), examples=examples("planner"), earlier="(none)",
                context="CONTEXT: (none)")
            try:
                parsed = structured.invoke([HumanMessage(prompt)])
                steps = [s for s in parsed.plan if s.strip() and not re.fullmatch(r"<[^>]*>", s.strip())][:7]
                raw = parsed.suggest or {}
                kits = [t for t in raw.get("toolkits") or [] if t in TOOLKITS and t not in toolkits]
                suggested = get_skill(raw.get("skill") or parsed.skill)
                if kits or suggested:                     # the user decides (LangGraph interrupt)
                    choice = interrupt({"type": "suggestion", "toolkits": kits,
                                        "skill": suggested.name if suggested else None,
                                        "why": str(raw.get("why", ""))[:300]})
                    toolkits += [t for t in choice.get("toolkits", []) if t in TOOLKITS]
                    skill = get_skill(choice.get("skill"))
            except Exception as e:
                log(run_id(), "warning", text=f"Planner failed ({e}); using the goal as the plan.")
        if skill:
            toolkits += skill.toolkits
            steps = skill.steps
        text = state["goal"] + (f"\n\nINPUT FILES: {quote_paths(state['inputs'])}" if state.get("inputs") else "")
        return {"plan": steps or [state["goal"]], "skill": skill.name if skill else None,
                "toolkits": list(dict.fromkeys(toolkits)), "messages": [HumanMessage(text)],
                "review_rounds": 0, "warnings": []}
    return plan


def make_check_node(reviewer: TextChatModel):
    structured = reviewer.with_structured_output(loop._Verdict).with_config(metadata={"purpose": "reviewer"})

    def check_answer(state: RunState) -> dict:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or "effort" not in last.response_metadata:
            return {"status": "aborted", "final": str(getattr(last, "content", ""))}   # e.g. step limit reached
        final, rounds = last.text, state.get("review_rounds", 0)
        tables = [_table_path(t) for t in last.additional_kwargs.get("tables", [])]
        shim = _shim(state)
        missing = loop.uncited_numbers(shim, final)
        if missing:
            issues = [f"These numbers appear in no tool result or reference: {', '.join(missing[:8])}. "
                      "Compute them with a tool and cite the ref, or remove them."]
        else:
            skill = get_skill(state.get("skill"))
            prompt = load_prompt(
                "reviewer", goal=state["goal"], final=final,
                checks="".join(f"{i}. {c}\n" for i, c in enumerate(skill.checks if skill else [], start=5)),
                cited="\n".join(filter(None, [loop.cited_outputs(shim, final), loop._shown_tables(tables)])),
                facts="\n".join(f"- {f}" for f in shim["facts"]) or "(none)",
                context=context_block(shim) or "CONTEXT: (none)")
            try:
                verdict = structured.invoke([HumanMessage(prompt)])
            except Exception as e:
                verdict = loop._Verdict(ok=False, issues=[f"Reviewer output unusable: {e}"])
            issues = [] if verdict.ok else verdict.issues
        log(run_id(), "review", check="answer", ok=not issues, issues=issues)
        if issues and rounds < config.MAX_REVIEW_ROUNDS:
            return {"review_rounds": rounds + 1,
                    "messages": [HumanMessage("REVIEW FEEDBACK on your answer (fix and answer again):\n- "
                                              + "\n- ".join(issues))]}
        return {"status": "finished", "final": final, "tables": [t for t in tables if t],
                "warnings": state.get("warnings", []) + [f"Reviewer: {i}" for i in issues]}
    return check_answer


def _table_path(item) -> str:
    name = item.get("name") if isinstance(item, dict) else str(item)
    if not name:
        return ""
    if "/" in name:
        return name
    return f"work/{run_id()}/{Path(name).stem}.csv"


def after_check(state: RunState) -> str:
    return END if state.get("status") else "agent"


# ---------------------------------------------------------------- build

def build(model: TextChatModel, checkpointer=None, max_steps: int | None = None):
    agent = create_agent(
        model.for_role("executor", config.EFFORT["executor"]),
        tools=lc_tools(),
        state_schema=RunState,
        middleware=[
            SessionPrompt(),
            ToolCallLimitMiddleware(run_limit=max_steps or config.MAX_STEPS, exit_behavior="end"),
            ModelRetryMiddleware(max_retries=config.MODEL_RETRIES - 1),
            SummarizationMiddleware(model=model.for_role("summariser", config.EFFORT["summariser"]),
                                    trigger=("tokens", config.PROMPT_BUDGET_TOKENS * 2),
                                    keep=("messages", config.RECENT_TURNS * 2)),
            RequestCheck(model.for_role("action_review", config.EFFORT["action_review"])),
            approvals(),
        ],
    )
    g = StateGraph(RunState)
    g.add_node("plan", make_plan_node(model.for_role("planner", config.EFFORT["planner"])))
    g.add_node("agent", agent)
    g.add_node("check_answer", make_check_node(model.for_role("reviewer", config.EFFORT["reviewer"])))
    g.add_edge(START, "plan")
    g.add_edge("plan", "agent")
    g.add_edge("agent", "check_answer")
    g.add_conditional_edges("check_answer", after_check, ["agent", END])
    return g.compile(checkpointer=checkpointer)


def sqlite_checkpointer(path: Path | None = None) -> SqliteSaver:
    path = path or config.RUNS_DIR / "langgraph.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))
