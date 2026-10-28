"""Finding the one JSON object in a model reply, tolerant of fences, prose and reasoning around it (docs/design.md §5)."""
from __future__ import annotations

import json
import re
from typing import Any


class ProtocolError(Exception):
    """The model's reply could not be turned into a valid action."""


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
