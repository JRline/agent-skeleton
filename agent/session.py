"""Session memory mode (docs/design.md §4a): send fixed prompt parts once, then only what is new.

For a backend whose model instances keep their own memory: one instance per channel (executor, check, review),
each with a fixed effort, fresh only when a new agent session starts. The agent still builds the full prompt
exactly as in stateless mode; this module works out what the channel's memory already holds and returns only
the rest, so prompt building, logging and tests stay the same.

Executor channel: the fixed blocks (reply protocol, tool list, toolkit rules, active skill, the conversation's
goal and files) are sent once and again only when they change; after that each call sends the new turns, a
one-line reminder and the STATE block. A full send happens on a new conversation, after a retry or resume
(the memory may not match the saved state), after a protocol error, and periodically (old memory is
compressed). Check and review channels get their whole, self-contained prompt each time, under a header.
"""
from __future__ import annotations

import hashlib
import re

from . import config

STATE_MARK = "\n\nSTATE\n"
REPAIR_MARK = "Invalid output:"


def channel_of(purpose: str) -> str:
    return config.CHANNEL_OF.get(purpose, "review")


def _blocks(system: str) -> list[tuple[str, str]]:
    """The system prompt as named blocks: the text before the first '## ' heading, then each '## ' section."""
    parts = re.split(r"(?m)^(?=## )", system)
    out = [("rules", parts[0])] if parts[0].strip() else []
    out += [(p.split("\n", 1)[0].strip(), p) for p in parts[1:]]
    return out


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def _reminder(system: str) -> str:
    """One line each delta call: exact reply format and tool names survive even if memory is compressed."""
    tools = system[system.find("## Tools"):]
    names = re.findall(r"(?m)^- ([A-Za-z_]\w*)", tools.split("\n## ", 1)[0])
    return (f"REMINDER: reply with exactly one JSON object (a tool call or a final answer). "
            f"Tools: {', '.join(names)}.")


def _render(messages: list[dict]) -> str:
    return "\n\n".join(f"{m['role'].upper()}:\n{m['content']}" for m in messages)


class Session:
    """What each channel's memory holds. One per agent session; new_session() forgets everything."""

    def __init__(self):
        self.channels: dict[str, dict] = {}

    def new_session(self) -> None:
        self.channels.clear()

    def resync(self, channel: str | None = None) -> None:
        """The memory may not match the saved state: the next call on the channel (or all) sends everything."""
        for name, ch in self.channels.items():
            if channel is None or name == channel:
                ch["full"] = True

    def prepare(self, messages: list[dict], purpose: str, conversation: str | None) -> tuple[str, dict]:
        """The text to send for this call, and a record of what was sent (for the log)."""
        channel = channel_of(purpose)
        ch = self.channels.setdefault(channel, {"blocks": {}, "conversation": None, "turns": 0,
                                                "since_full": 0, "full": True})
        if channel != "executor":
            text = (f"NEW REQUEST ({purpose}). Judge only what follows; earlier requests are finished.\n\n"
                    + _render(messages))
            return text, self._sent(ch, channel, "full", text)
        if messages[-1]["role"] == "user" and messages[-1]["content"].startswith(REPAIR_MARK):
            # A repair: the memory holds the bad reply; only the correction is new.
            return messages[-1]["content"], self._sent(ch, channel, "repair", messages[-1]["content"])

        system = messages[0]["content"] if messages[0]["role"] == "system" else ""
        rest = messages[1:] if system else messages
        last = rest[-1]["content"]
        state = ""
        if last.startswith("STATE\n"):                       # STATE sent as its own message
            state, rest = last, rest[:-1]
        elif (cut := last.rfind(STATE_MARK)) >= 0:          # STATE appended to the last user message
            state, rest = last[cut + 2:], rest[:-1] + [{**rest[-1], "content": last[:cut]}]
        header, turns = rest[0]["content"], rest[1:]
        blocks = _blocks(system) + [("conversation", header)]

        full = (ch["full"] or ch["conversation"] != conversation or len(turns) < ch["turns"]
                or ch["since_full"] >= config.MEMORY_REFRESH_TOKENS)
        if full:
            recent = turns[-2 * config.RECENT_TURNS:]
            parts = [f"NEW CONVERSATION {conversation}: anything earlier is finished; use only what follows."]
            parts += [text for _, text in blocks]
            if recent:
                parts.append("RECENT TURNS:\n" + _render(recent))
            parts.append(state)
            ch.update(blocks={n: _digest(t) for n, t in blocks}, conversation=conversation, turns=len(turns),
                      since_full=0, full=False)
            text = "\n\n".join(p for p in parts if p.strip())
            return text, self._sent(ch, channel, "full", text, count=False)

        parts = [f"UPDATE ({name}):\n{text}" for name, text in blocks if ch["blocks"].get(name) != _digest(text)]
        new = [t for t in turns[ch["turns"]:] if t["role"] != "assistant"]   # its own replies are in memory
        if new:
            parts.append(_render(new))
        parts += [_reminder(system), state]
        ch["blocks"].update({n: _digest(t) for n, t in blocks})
        ch["turns"] = len(turns)
        text = "\n\n".join(p for p in parts if p.strip())
        return text, self._sent(ch, channel, "delta", text)

    @staticmethod
    def _sent(ch: dict, channel: str, mode: str, text: str, count: bool = True) -> dict:
        tokens = len(text) // 4
        if count:
            ch["since_full"] += tokens
        return {"channel": channel, "mode": mode, "sent_tokens": tokens}
