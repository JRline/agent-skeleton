"""Production stand-in: a backend that behaves like the production GPT CLI stack, answered by Claude Haiku.

Use it to try session memory mode before production:

  LLM_BACKEND=prod PROD_API_MODULE=agent.backends.session_sim MODEL_MEMORY=session python eval.py --backend prod

What it mimics (docs/design.md §4a):
- a fixed set of instances, one per channel (executor, check, review), each with a fixed effort
  (config.CHANNEL_EFFORT) and its own memory that persists between calls;
- fresh instances only when a new agent session starts (new_session());
- memory compressed automatically once it passes SIM_MEMORY_TOKENS: the oldest half becomes short notes.
Every call sends the channel's whole remembered conversation to the inner backend, so it costs the same IO as
before; what it tests is the agent's side: what it sends, and whether answers survive memory and compression.

Env:
  SIM_INNER          inner backend module (default agent.backends.claude_cli; tests use a fake)
  SIM_MEMORY_TOKENS  memory size per instance before compression (default 24000)
"""
from __future__ import annotations

import importlib
import os

from .. import config
from ..adapter import render

# The inner CLI would otherwise use its own default system prompt; production has none of ours either.
SYSTEM = {"role": "system", "content": "You are the language model behind an analysis agent. Follow the "
          "instructions and reply formats given in the conversation exactly."}
_memory: dict[str, list[dict]] = {}       # channel -> remembered messages (user/assistant)
compressions: dict[str, int] = {}          # channel -> times compressed, for tests and logs


def _inner():
    return importlib.import_module(os.getenv("SIM_INNER", "agent.backends.claude_cli"))


def _limit() -> int:
    return int(os.getenv("SIM_MEMORY_TOKENS", "24000"))


def model_name(effort: str) -> str:
    inner = _inner()
    name = inner.model_name(effort) if hasattr(inner, "model_name") else inner.__name__.rsplit(".", 1)[-1]
    return f"sim:{name}"


def new_session() -> None:
    """A new agent session: every instance starts with an empty memory."""
    _memory.clear()
    compressions.clear()


def _tokens(messages: list[dict]) -> int:
    return sum(len(m["content"]) for m in messages) // 4


def _compress(channel: str, history: list[dict], effort: str) -> None:
    """Fold the oldest half of the memory into notes, as the production stack does on its own."""
    if _tokens(history) <= _limit() or len(history) < 4:
        return
    half = len(history) // 2
    old, keep = history[:half], history[half:]
    notes = _inner().call([SYSTEM, {"role": "user", "content":
                            "Compress this earlier part of a conversation into short notes for yourself. Keep "
                            "exact names, numbers, file names, rules and the reply format.\n\n" + render(old)}],
                          effort=effort)
    history[:] = [{"role": "user", "content": "[Earlier conversation, compressed]\n" + notes},
                  {"role": "assistant", "content": "Noted."}] + keep
    compressions[channel] = compressions.get(channel, 0) + 1


def call(text: str, effort: str = "none", channel: str = "review") -> str:
    """One message to one instance. The instance's effort is fixed, whatever effort is asked for."""
    fixed = config.CHANNEL_EFFORT.get(channel, effort)
    history = _memory.setdefault(channel, [])
    history.append({"role": "user", "content": text})
    _compress(channel, history, fixed)
    try:
        reply = _inner().call([SYSTEM] + history, effort=fixed)
    except Exception:
        history.pop()                     # the instance never answered: it keeps no half exchange
        raise
    history.append({"role": "assistant", "content": reply})
    return reply


def cancel() -> None:
    inner = _inner()
    if hasattr(inner, "cancel"):
        inner.cancel()
