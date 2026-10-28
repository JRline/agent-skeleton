"""Model adapter (docs/design.md §4).

The rest of the code calls only `ChatModel.complete(messages, effort, stop, purpose)`.
Any backend module that exposes `call(text: str, effort: str) -> str` can be wrapped
with `ModuleChatModel`, so production, Ollama and test stubs are interchangeable.
"""
from __future__ import annotations

import importlib
import inspect
import time
from typing import Literal

from . import config

Effort = Literal["none", "medium"]
Message = dict  # {"role": "system" | "user" | "assistant", "content": str}


class CallCancelled(Exception):
    """A backend call was cancelled on purpose (Stop button). Never retried."""


class ChatModel:
    """The only interface the agent depends on. Always pass the full message list; a model whose backend keeps
    memory (session mode) decides itself what part of it to send."""

    name = "base"
    remembers = False        # True: the backend keeps the conversation, so the agent need not summarise it

    def complete(self, messages: list[Message], effort: Effort = "none",
                 stop: list[str] | None = None, purpose: str = "executor", conversation: str | None = None) -> str:
        raise NotImplementedError

    def new_session(self) -> None:
        """A new agent session: backends with memory start fresh. Default: no-op."""

    def resync(self, purpose: str | None = None) -> None:
        """The backend's memory may not match the saved state: send everything next time. Default: no-op."""

    def model_id(self, effort: Effort = "none") -> str:
        """Which model answers at this effort, for the log, so runs on different models can be compared."""
        return self.name

    def cancel(self) -> None:
        """Abandon in-flight calls if the backend can; called from another thread. Default: no-op."""


def render(messages: list[Message]) -> str:
    """Flatten role-tagged messages into one string for single-text APIs."""
    parts = [f"{m['role'].upper()}:\n{m['content']}" for m in messages]
    parts.append("Reply as ASSISTANT. Output only your reply.")
    return "\n\n".join(parts)


def apply_stop(text: str, stop: list[str] | None) -> str:
    """Emulate stop sequences by truncating at the first one found."""
    if not stop:
        return text
    cuts = [text.find(s) for s in stop if s in text]
    return text[: min(cuts)] if cuts else text


class ModuleChatModel(ChatModel):
    """Wraps a module exposing `call(text, effort) -> str` with retries and stop emulation.

    Set pass_messages=True if the module accepts the role-tagged message list directly.
    """

    def __init__(self, module, name: str, retries: int | None = None,
                 backoff: float = 2.0, pass_messages: bool = False):
        self.module = module
        self.name = name
        self.retries = retries or config.MODEL_RETRIES
        self.backoff = backoff
        self.pass_messages = pass_messages
        # Session mode needs a backend whose call() takes a channel (one remembering instance per channel).
        accepts_channel = "channel" in inspect.signature(module.call).parameters
        self.session = None
        if config.MODEL_MEMORY == "session" and accepts_channel:
            from .session import Session
            self.session = Session()
        self.remembers = self.session is not None
        self.last_send: dict | None = None

    def complete(self, messages, effort="none", stop=None, purpose="executor", conversation=None):
        kwargs = {"effort": effort}
        if self.session:
            payload, self.last_send = self.session.prepare(messages, purpose, conversation)
            kwargs = {"effort": config.CHANNEL_EFFORT[self.last_send["channel"]], "channel": self.last_send["channel"]}
            self.last_send["effort"] = kwargs["effort"]
        else:
            payload = messages if self.pass_messages else render(messages)
        for attempt in range(self.retries):
            try:
                out = self.module.call(payload, **kwargs)
                return apply_stop(out, stop)
            except CallCancelled:
                raise
            except Exception:
                if attempt == self.retries - 1:
                    raise
                time.sleep(self.backoff ** attempt)
        raise RuntimeError("unreachable")

    def new_session(self):
        """Forget what each channel holds, and let a backend with memory start fresh instances."""
        if self.session:
            self.session.new_session()
            if hasattr(self.module, "new_session"):
                self.module.new_session()

    def resync(self, purpose=None):
        if self.session:
            from .session import channel_of
            self.session.resync(channel_of(purpose) if purpose else None)

    def model_id(self, effort="none"):
        """Uses the module's optional `model_name(effort)`; modules without it log the backend name."""
        fn = getattr(self.module, "model_name", None)
        return fn(effort) if callable(fn) else self.name

    def cancel(self):
        """Uses the module's optional `cancel()`; production modules without it simply finish the call."""
        fn = getattr(self.module, "cancel", None)
        if callable(fn):
            fn()


def get_model(backend: str | None = None) -> ChatModel:
    """Build the model for a backend name: fake | ollama | claude | prod."""
    backend = backend or config.BACKEND
    if backend == "fake":
        from .backends.fake import DemoFakeModel
        return DemoFakeModel()
    if backend == "ollama":
        from .backends import ollama_api
        return ModuleChatModel(ollama_api, "ollama", pass_messages=True)
    if backend == "claude":
        from .backends import claude_cli
        return ModuleChatModel(claude_cli, "claude", pass_messages=True)
    if backend == "prod":
        module = importlib.import_module(config.PROD_API_MODULE)
        return ModuleChatModel(module, "prod")
    raise ValueError(f"Unknown backend {backend!r}. Use fake, ollama, claude or prod.")
