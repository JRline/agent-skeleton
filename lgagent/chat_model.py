"""A LangChain chat model over a text-only `call(text, effort) -> str` API.

The one piece LangChain cannot provide: the production model has no native tool calling or structured output. This
model adds both through a JSON reply protocol, so everything above it (create_agent, ToolNode, middleware,
with_structured_output) is stock LangChain:

- bind_tools(): the tool list and reply format go into the prompt text; a reply {"tool": ..., "args": ...} becomes
  an AIMessage with tool_calls, a reply {"final": ...} an AIMessage with content.
- with_structured_output(Schema): the reply must be one JSON object that validates against Schema.
- An unusable reply gets a short correction (MAX_REPAIRS times, the last at effort "medium"), as the step machine
  did, because small models often get the format right on the second try.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_core.utils.json import parse_json_markdown
from pydantic import BaseModel, ConfigDict, ValidationError

from agent import config
from agent.protocol import extract_json

PROTOCOL = """## Reply format
Reply with exactly ONE JSON object and nothing else, in one of these shapes:
{"tool": "<tool name>", "args": {<arguments>}}
{"final": "<your answer to the user, markdown allowed>"}
Call one tool at a time and wait for its result. Never write a tool result yourself."""
FORCED = """## Reply format
Reply with exactly ONE JSON object and nothing else: {"tool": "<tool name>", "args": {<arguments>}}"""


class ReplyError(ValueError):
    """The reply cannot be used; the message is the correction sent back to the model."""


def render_messages(messages: list[BaseMessage]) -> str:
    """Role-tagged text for a single-text API. A tool call is shown as the JSON the model wrote."""
    parts = []
    for m in messages:
        if m.type == "ai" and getattr(m, "tool_calls", None):
            call = m.tool_calls[0]
            parts.append("ASSISTANT:\n" + json.dumps({"tool": call["name"], "args": call["args"]}, ensure_ascii=False))
        elif m.type == "tool":
            parts.append(f"TOOL RESULT ({m.name}):\n{m.content}")
        else:
            role = {"system": "SYSTEM", "human": "USER", "ai": "ASSISTANT"}.get(m.type, m.type.upper())
            parts.append(f"{role}:\n{m.text if hasattr(m, 'text') else m.content}")
    return "\n\n".join(parts)


def render_tools(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        fn = t["function"]
        props = fn.get("parameters", {}).get("properties", {})
        required = set(fn.get("parameters", {}).get("required", []))
        args = ", ".join(f'"{k}": {v.get("type", "any")}{"" if k in required else " (optional)"}'
                         for k, v in props.items())
        lines.append(f"- {fn['name']}: {fn.get('description', '')}\n  args: {{{args}}}")
    return "## Tools\n" + "\n".join(lines)


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


def to_message(obj: dict, tools: list[dict], forced: bool) -> AIMessage:
    names = {t["function"]["name"] for t in tools}
    if "tool" in obj:
        if obj["tool"] not in names:
            raise ReplyError(f"Unknown tool {obj['tool']!r}. Use one of: {', '.join(sorted(names))}.")
        args = obj.get("args", obj.get("input", {}))
        if not isinstance(args, dict):
            raise ReplyError('"args" must be a JSON object.')
        return AIMessage(content="", tool_calls=[{"name": obj["tool"], "args": args,
                                                   "id": "call_" + uuid.uuid4().hex[:12], "type": "tool_call"}])
    if "final" in obj and not forced:
        extra = {k: v for k, v in obj.items() if k != "final"}   # e.g. "tables": shown under the answer
        return AIMessage(content=str(obj["final"]), additional_kwargs=extra)
    raise ReplyError('Use exactly one of "tool" or "final".' if not forced else 'Reply with a "tool" object.')


class TextChatModel(BaseChatModel):
    """`call` is any backend module's call(text, effort) -> str (prod wrapper, Ollama, Claude CLI, a test fake)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    call: Callable[..., str]
    effort: str = "none"
    purpose: str = "executor"          # for the log; one model copy per role (see for_role)
    name_: str = "text-model"
    pass_messages: bool = False        # the backend accepts [{"role", "content"}] (keeps the system prompt separate)

    @property
    def _llm_type(self) -> str:
        return "text-protocol"

    @property
    def _identifying_params(self) -> dict:
        return {"model": self.name_, "effort": self.effort, "purpose": self.purpose}

    def for_role(self, purpose: str, effort: str) -> "TextChatModel":
        return self.model_copy(update={"purpose": purpose, "effort": effort})

    def bind_tools(self, tools, *, tool_choice: str | None = None, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], tool_choice=tool_choice, **kwargs)

    def with_structured_output(self, schema: type[BaseModel], *, include_raw: bool = False, **kwargs):
        return self.bind(schema=schema) | PydanticOutputParser(pydantic_object=schema)

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None, tools: list[dict] | None = None,
                  tool_choice: str | None = None, schema: type[BaseModel] | None = None, **kwargs: Any) -> ChatResult:
        system = "\n\n".join(m.text for m in messages if m.type == "system")
        body = render_messages([m for m in messages if m.type != "system"])
        if tools:
            forced = tool_choice not in (None, "auto", "none")
            system += "\n\n" + render_tools(tools) + "\n\n" + (FORCED if forced else PROTOCOL)
        turns = [{"role": "user", "content": body + "\n\nReply as ASSISTANT. Output only your reply."}]
        effort = self.effort
        for attempt in range(config.MAX_REPAIRS + 2):
            reply = self.call(self._payload(system.strip(), turns), effort=effort)
            try:
                message = self._read(reply, tools, tool_choice, schema)
                message.response_metadata.update(effort=effort, repairs=attempt,
                                                 prompt_chars=len(system) + sum(len(t["content"]) for t in turns))
                return ChatResult(generations=[ChatGeneration(message=message)])
            except (ReplyError, ValidationError) as e:
                if attempt > config.MAX_REPAIRS:
                    raise
                if attempt == config.MAX_REPAIRS:
                    effort = "medium"               # last attempt thinks harder
                turns += [{"role": "assistant", "content": reply[:2000]},
                          {"role": "user", "content": f"Invalid output: {str(e)[:300]} "
                                                      "Reply again with exactly one JSON object as specified."}]
        raise RuntimeError("unreachable")

    def _payload(self, system: str, turns: list[dict]):
        """Role-tagged messages for backends that take them (Claude CLI, Ollama), else one text."""
        messages = ([{"role": "system", "content": system}] if system else []) + turns
        if self.pass_messages:
            return messages
        return "\n\n".join(f"{m['role'].upper()}:\n{m['content']}" for m in messages)

    @staticmethod
    def _read(reply: str, tools, tool_choice, schema) -> AIMessage:
        if schema is not None:
            obj = parse_reply(reply)
            schema.model_validate(obj)
            return AIMessage(content=json.dumps(obj, ensure_ascii=False))
        if tools:
            return to_message(parse_reply(reply), tools, tool_choice not in (None, "auto", "none"))
        return AIMessage(content=reply)


def from_backend(backend: str) -> TextChatModel:
    """The existing backend modules, unchanged: claude (Haiku via the CLI), ollama, prod."""
    import importlib
    if backend == "claude":
        from agent.backends import claude_cli as module
    elif backend == "ollama":
        from agent.backends import ollama_api as module
    elif backend == "prod":
        module = importlib.import_module(config.PROD_API_MODULE)
    else:
        raise ValueError(f"Unknown backend {backend!r}: use claude, ollama or prod.")
    namer = getattr(module, "model_name", None)
    return TextChatModel(call=module.call, name_=namer("none") if callable(namer) else backend,
                         pass_messages=backend in ("claude", "ollama"))
