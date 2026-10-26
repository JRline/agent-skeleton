"""JSON action protocol: parsing, validation and repair (docs/design.md §5)."""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from pydantic import BaseModel, ValidationError, model_validator

from . import config

STOP = ["OBSERVATION:"]


class ProtocolError(Exception):
    """The model's reply could not be turned into a valid action."""


class Action(BaseModel):
    thought: str = ""
    tool: str | None = None
    input: dict[str, Any] = {}
    final: str | None = None
    # with "final": result tables to show: work table names or sandbox paths, or {"name": ..., "title": ...}
    tables: list[str | dict[str, str]] = []
    title: str = ""              # with "final": a name for the whole conversation so far
    step_done: bool = False

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.tool is None) == (self.final is None):
            raise ValueError("Use exactly one of 'tool' or 'final'.")
        return self

    def signature(self) -> str:
        """Identity of a tool call, used for repeat detection."""
        return f"{self.tool}:{json.dumps(self.input, sort_keys=True)}"


def _scan_object(text: str, start: int) -> int:
    """Return the index just past the JSON object starting at `start` (string-aware)."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ProtocolError("Unterminated JSON object.")


def _objects(text: str) -> list[tuple[int, int, Any]]:
    """Every top-level {...} block as (start, end, parsed or the parse error)."""
    found, i = [], text.find("{")
    while i != -1:
        try:
            end = _scan_object(text, i)
        except ProtocolError as e:
            found.append((i, len(text), e))
            break
        try:
            found.append((i, end, json.loads(text[i:end])))
        except json.JSONDecodeError as e:
            found.append((i, end, e))
        i = text.find("{", end)
    return found


def extract_json(text: str) -> dict:
    """Take the answer: the last valid JSON object, tolerating code fences.

    Reasoning models often think out loud and quote JSON before answering, so earlier
    objects separated from the answer by prose are ignored. Rejected: a broken block after
    the answer (cut off or invalid), and two objects back to back (two actions at once).
    """
    try:                                    # fences inside the answer text (e.g. ```sql) must survive
        return _extract(text)
    except ProtocolError:
        return _extract(re.sub(r"```(?:json)?", "", text))   # fences that break the JSON itself


def _extract(cleaned: str) -> dict:
    found = _objects(cleaned)
    if not found:
        raise ProtocolError("No JSON object found.")
    valid = [k for k, (_, _, data) in enumerate(found) if isinstance(data, dict)]
    if not valid:
        err = found[-1][2]
        if isinstance(err, json.JSONDecodeError):
            raise ProtocolError(f"Invalid JSON: {err.msg}") from err
        if isinstance(err, ProtocolError):
            raise err
        raise ProtocolError("The JSON must be an object.")
    last = valid[-1]
    if last < len(found) - 1:
        raise ProtocolError("Reply with exactly one complete JSON object, and nothing after it.")
    if last > 0 and not cleaned[found[last - 1][1]:found[last][0]].strip(" \n\t,"):
        raise ProtocolError("Reply with exactly one JSON object.")
    return found[last][2]


def _short(err: Exception, limit: int = 300) -> str:
    if isinstance(err, ValidationError):
        msg = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'reply'}: {e['msg']}" for e in err.errors())
    else:
        msg = str(err)
    return msg[:limit]


def parse_action(text: str, tools: dict) -> Action:
    """Parse and validate one action against the tool registry."""
    try:
        action = Action.model_validate(extract_json(text))
    except ValidationError as e:
        raise ProtocolError(_short(e)) from e
    if action.tool is not None:
        tool = tools.get(action.tool)
        if tool is None:
            raise ProtocolError(f"Unknown tool {action.tool!r}. Use one of: {', '.join(tools)}.")
        try:
            action.input = tool.args.model_validate(action.input).model_dump()
        except ValidationError as e:
            raise ProtocolError(f"Bad input for {action.tool}: {_short(e)}") from e
    return action


def request_json(call: Callable[[list, str], str], messages: list, parse: Callable[[str], Any],
                 base_effort: str) -> tuple[Any, str, int]:
    """Call the model and parse; repair invalid replies.

    Schedule: first call at base_effort, then MAX_REPAIRS repairs at effort "none",
    then one escalated repair at "medium". Returns (parsed, raw_reply, attempts).
    """
    efforts = [base_effort] + [config.EFFORT["repair"]] * config.MAX_REPAIRS \
        + [config.EFFORT["repair_escalate"]]
    msgs = list(messages)
    last_error = ""
    for attempt, effort in enumerate(efforts, start=1):
        raw = call(msgs, effort)
        try:
            return parse(raw), raw, attempt
        except ProtocolError as e:
            last_error = str(e)
            # Only the latest bad reply, shortened: long reasoning replies piled up in the
            # prompt make small models slower and more confused with every repair.
            msgs = list(messages) + [
                {"role": "assistant", "content": raw if len(raw) <= 400 else "…" + raw[-400:]},
                {"role": "user", "content": f"Invalid output: {last_error} "
                                            "Reply with one JSON object per the protocol, and no other text."},
            ]
    raise ProtocolError(f"No valid reply after {len(efforts)} attempts: {last_error}")
