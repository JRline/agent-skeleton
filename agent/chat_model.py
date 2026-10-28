"""A LangChain chat model over the text-only production API (docs/design.md §4-5).

The production model has no native tool calling or structured output, so this model adds both through a JSON reply
protocol; everything above it (create_agent, ToolNode, middleware, with_structured_output) is stock LangChain.
It wraps an `agent.adapter.ChatModel`, which keeps the transport: retries, stop, cancel, the session memory mode
and the backends (prod wrapper, Ollama, Claude CLI, FakeModel in tests).

- bind_tools(): the tool list and the reply format go into the system prompt; a reply {"tool": ..., "input": ...}
  becomes an AIMessage with a tool call, a reply {"final": ...} an AIMessage with the answer as content.
- with_structured_output(Schema): the reply must be one JSON object that validates against Schema.
- An unusable reply gets a short correction (MAX_REPAIRS times at the role's effort, then once at "medium"):
  small models often get the format right on the second try.
- A call to a tool of a toolkit that is not enabled comes back as a tool call marked `needs_toolkit`, so the agent
  can ask the user to enable it (graph.RequestCheck).
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_core.utils.json import parse_json_markdown
from pydantic import BaseModel, ConfigDict, ValidationError

from . import config
from .protocol import extract_json

PROTOCOL = """## Reply protocol
Reply with exactly ONE JSON object and nothing else, in one of these shapes:
{"thought": "<one short sentence>", "tool": "<tool name>", "input": {<arguments>}, "step_done": false}
{"thought": "<one short sentence>", "final": "<your answer to the user, markdown allowed>"}
Use exactly one of "tool" or "final", and only the tools listed above, with inputs that match their arguments.
Set "step_done": true when this tool call completes the current step.
Call one tool at a time. Never write OBSERVATION yourself: after a tool call, stop and wait for it."""
FORCED = """## Reply protocol
Reply with exactly ONE JSON object and nothing else: {"tool": "<tool name>", "input": {<arguments>}}"""
OBSERVATION = "OBSERVATION: "


class ReplyError(ValueError):
    """The reply cannot be used; the message is the correction sent back to the model."""


def render_tools(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        fn = t["function"]
        params = fn.get("parameters", {})
        required = set(params.get("required", []))
        args = ", ".join(f'"{k}": {v.get("type", "any")}{"" if k in required else " (optional)"}'
                         for k, v in params.get("properties", {}).items())
        lines.append(f"- {fn['name']}: {fn.get('description', '')}\n  input: {{{args}}}")
    return "## Tools\n" + "\n".join(lines)


def to_dicts(messages: list[BaseMessage]) -> list[dict]:
    """Role-tagged messages as the backends take them. Tool calls appear as the JSON the model wrote and tool
    results as OBSERVATION lines, so a backend that keeps its own memory sees the same shapes every time."""
    out = []
    for m in messages:
        if m.type == "system":
            out.append({"role": "system", "content": m.text})
        elif m.type == "ai" and m.tool_calls:
            call = m.tool_calls[0]
            out.append({"role": "assistant", "content": json.dumps(
                {"thought": m.additional_kwargs.get("thought", ""), "tool": call["name"], "input": call["args"]},
                ensure_ascii=False)})
        elif m.type == "ai":
            out.append({"role": "assistant", "content": json.dumps({"final": m.text}, ensure_ascii=False)})
        elif m.type == "tool":
            out.append({"role": "user", "content": OBSERVATION + str(m.content)})
        else:
            out.append({"role": "user", "content": m.text})
    system = "\n\n".join(m["content"] for m in out if m["role"] == "system")
    rest = [m for m in out if m["role"] != "system"]
    return ([{"role": "system", "content": system}] if system else []) + rest


def parse_reply(text: str) -> dict:
    try:
        obj = parse_json_markdown(text)
    except Exception:
        obj = None
    if not isinstance(obj, dict):
        try:
            obj = extract_json(text)               # tolerant of text around the object (small models)
        except Exception as e:
            raise ReplyError(f"No JSON object found ({e}).") from None
    return obj


def to_message(obj: dict, tools: list[dict], forced: bool, other_tools: dict | None = None) -> AIMessage:
    names = {t["function"]["name"] for t in tools}
    thought = str(obj.get("thought", ""))
    if obj.get("tool") is not None:
        name, args = obj["tool"], obj.get("input", obj.get("args", {}))
        if not isinstance(args, dict):
            raise ReplyError('"input" must be a JSON object.')
        extra = {"thought": thought, "step_done": bool(obj.get("step_done"))}
        if name not in names:
            if name in (other_tools or {}):
                extra["needs_toolkit"] = other_tools[name]        # the user decides (graph.RequestCheck)
            else:
                raise ReplyError(f"Unknown tool {name!r}. Use one of: {', '.join(sorted(names))}.")
        return AIMessage(content="", additional_kwargs=extra,
                         tool_calls=[{"name": name, "args": args, "id": "call_" + uuid.uuid4().hex[:12],
                                      "type": "tool_call"}])
    if obj.get("final") is not None and not forced:
        extra = {k: v for k, v in obj.items() if k != "final"}   # thought, tables, title
        return AIMessage(content=str(obj["final"]), additional_kwargs=extra)
    raise ReplyError('Reply with a "tool" object.' if forced else 'Use exactly one of "tool" or "final".')


def _thread() -> str | None:
    """The conversation (LangGraph thread) this call belongs to, for the session memory mode."""
    try:
        from langgraph.config import get_config
        return get_config()["configurable"].get("thread_id")
    except Exception:
        return None


class TextChatModel(BaseChatModel):
    """One copy per role (`for_role`), so each role's purpose and effort are fixed and logged."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    chat: Any                          # agent.adapter.ChatModel
    effort: str = "none"
    purpose: str = "executor"

    @property
    def _llm_type(self) -> str:
        return "text-protocol"

    @property
    def _identifying_params(self) -> dict:
        return {"model": self.chat.model_id(self.effort), "effort": self.effort, "purpose": self.purpose}

    def for_role(self, purpose: str, effort: str | None = None) -> "TextChatModel":
        return self.model_copy(update={"purpose": purpose, "effort": effort or config.EFFORT.get(purpose, "none")})

    def bind_tools(self, tools, *, tool_choice: str | None = None, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], tool_choice=tool_choice, **kwargs)

    def with_structured_output(self, schema: type[BaseModel], *, include_raw: bool = False, **kwargs):
        return self.bind(schema=schema) | PydanticOutputParser(pydantic_object=schema)

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None,
                  tools: list[dict] | None = None, tool_choice: str | None = None,
                  schema: type[BaseModel] | None = None, other_tools: dict | None = None,
                  effort: str | None = None, **kwargs: Any) -> ChatResult:
        msgs = to_dicts(messages)
        forced = tool_choice not in (None, "auto", "none")
        if tools:
            block = render_tools(tools) + "\n\n" + (FORCED if forced else PROTOCOL)
            if msgs and msgs[0]["role"] == "system":
                msgs[0] = {"role": "system", "content": msgs[0]["content"] + "\n\n" + block}
            else:
                msgs.insert(0, {"role": "system", "content": block})
        effort = effort or self.effort
        stop = [OBSERVATION.strip()] if tools else None
        base = msgs
        for attempt in range(config.MAX_REPAIRS + 2):
            if hasattr(self.chat, "last_send"):
                self.chat.last_send = None
            reply = self.chat.complete(msgs, effort=effort, stop=stop, purpose=self.purpose,
                                       conversation=_thread())
            try:
                message = self._read(reply, tools, forced, schema, other_tools)
            except (ReplyError, ValidationError) as e:
                if attempt > config.MAX_REPAIRS:
                    raise ReplyError(f"Model kept replying outside the protocol: {str(e)[:300]}") from None
                if attempt == config.MAX_REPAIRS:
                    effort = config.EFFORT["repair_escalate"]      # last attempt thinks harder
                # Only the latest bad reply, shortened: long reasoning replies piled up in the prompt make small
                # models slower and more confused with every repair.
                msgs = base + [{"role": "assistant", "content": reply if len(reply) <= 400 else "…" + reply[-400:]},
                               {"role": "user", "content": f"Invalid output: {str(e)[:300]} "
                                                           "Reply again with exactly one JSON object as specified."}]
                continue
            sent = getattr(self.chat, "last_send", None) or {}
            message.response_metadata.update(
                effort=effort, repairs=attempt, model=self.chat.model_id(effort),
                prompt_tokens=sum(len(m["content"]) for m in msgs) // 4, raw=reply[:4000],
                **({"channel": sent["channel"], "send": sent["mode"], "sent_tokens": sent["sent_tokens"]}
                   if sent else {}))
            return ChatResult(generations=[ChatGeneration(message=message)])
        raise RuntimeError("unreachable")

    @staticmethod
    def _read(reply: str, tools, forced: bool, schema, other_tools) -> AIMessage:
        if schema is not None:
            obj = parse_reply(reply)
            schema.model_validate(obj)
            return AIMessage(content=json.dumps(obj, ensure_ascii=False))
        if tools:
            return to_message(parse_reply(reply), tools, forced, other_tools)
        return AIMessage(content=reply.strip())
