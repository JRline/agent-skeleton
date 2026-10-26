"""Local model on your Mac via Ollama, with the same `call(text, effort)` shape as production.

Setup:  brew install ollama  (or the Mac app), then  ollama pull <model>
Env:
  OLLAMA_URL           default http://localhost:11434
  OLLAMA_MODEL         model used for effort "none"            (default qwen3:4b)
  OLLAMA_MODEL_MEDIUM  optional larger model for effort "medium"
  OLLAMA_THINK         default 1: thinking off for effort "none", on for "medium". Models without
                       thinking support are retried without it. Set 0 to leave the model's default,
                       or never to turn thinking off for every effort (for models such as qwen3.5
                       whose thinking runs on without reaching an answer).
  OLLAMA_NUM_CTX       context window to request (default 8192)

Uses only the standard library, so nothing extra to install.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
import urllib.request
from urllib.parse import urlsplit

from ..adapter import CallCancelled

_active: set = set()          # in-flight connections, so cancel() can close them
_lock = threading.Lock()
_no_think: set = set()        # models that rejected the think option


def _url(path: str) -> str:
    return os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/") + path


def list_models() -> list[str]:
    """Names of installed models (like `ollama list`). Reads the catalogue only; loads no model."""
    with urllib.request.urlopen(_url("/api/tags"), timeout=3) as resp:
        data = json.loads(resp.read())
    return sorted(m["name"] for m in data.get("models", []))


def model_name(effort: str) -> str:
    """Also read by the adapter so the log records which model answered."""
    base = os.getenv("OLLAMA_MODEL", "qwen3:4b")
    if effort == "medium":
        return os.getenv("OLLAMA_MODEL_MEDIUM", base)
    return base


def call(text, effort: str = "none") -> str:
    messages = text if isinstance(text, list) else [{"role": "user", "content": text}]
    body = {
        "model": model_name(effort),
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192"))},
    }
    think = os.getenv("OLLAMA_THINK", "1")
    if think in ("1", "never") and body["model"] not in _no_think:
        body["think"] = think == "1" and effort == "medium"
    try:
        data = _post("/api/chat", body, timeout=600)
    except RuntimeError as e:
        if "think" not in body or "does not support thinking" not in str(e):
            raise
        _no_think.add(body["model"])    # remember, so later calls skip the failed attempt
        del body["think"]
        data = _post("/api/chat", body, timeout=600)
    return answer_only(data.get("message", {}).get("content", ""))


def answer_only(content: str) -> str:
    """Drop thinking text: <think> blocks, and anything before a lone </think> (seen with qwen3)."""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    return content.split("</think>")[-1].strip()


def cancel() -> None:
    """Close in-flight requests (called from another thread, e.g. the UI's Stop button).
    Ollama stops generating when the client disconnects, which frees the GPU at once."""
    with _lock:
        conns = list(_active)
    for conn in conns:
        conn.cancelled = True
        try:
            if conn.sock:
                conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _post(path: str, body: dict, timeout: float) -> dict:
    url = urlsplit(_url(path))
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=timeout)
    conn.cancelled = False
    with _lock:
        _active.add(conn)
    try:
        conn.request("POST", url.path, body=json.dumps(body), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if conn.cancelled:
            raise CallCancelled("Stopped by user.")
        if resp.status != 200:
            raise RuntimeError(f"Ollama HTTP {resp.status}: {raw[:200]!r}")
        return json.loads(raw)
    except CallCancelled:
        raise
    except Exception as e:
        if conn.cancelled:
            raise CallCancelled("Stopped by user.") from e
        raise
    finally:
        with _lock:
            _active.discard(conn)
        conn.close()
