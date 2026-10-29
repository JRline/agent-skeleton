"""A real model behind any OpenAI-compatible chat API (`POST <API_URL>/chat/completions`), standard library only.

Swap it in with LLM_BACKEND=chatgpt. It has the same `call(text, effort)` shape as production, so the agent drives it
with its JSON reply protocol and needs nothing else from the API (no native tool calling).

Env:
  API_URL           base URL, e.g. https://llm.example.internal/v1
  API_MODEL         model for effort "none"
  API_MODEL_MEDIUM  optional model for effort "medium" (default API_MODEL)
  API_KEY_FILE      file holding the API key (preferred: keep it outside the repository), or API_KEY
  API_EFFORT_PARAM  optional request field the effort is sent as, e.g. reasoning_effort ("none" -> "low")
  API_TIMEOUT_S     seconds per call (default 600)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def model_name(effort: str) -> str:
    base = os.getenv("API_MODEL", "")
    return os.getenv("API_MODEL_MEDIUM", base) if effort == "medium" else base


def _key() -> str:
    path = os.getenv("API_KEY_FILE")
    return Path(path).expanduser().read_text().strip() if path else os.getenv("API_KEY", "")


def call(text, effort: str = "none") -> str:
    url = os.getenv("API_URL", "").rstrip("/")
    if not url or not model_name(effort):
        raise RuntimeError("Set API_URL and API_MODEL for LLM_BACKEND=chatgpt.")
    messages = text if isinstance(text, list) else [{"role": "user", "content": text}]
    body = {"model": model_name(effort), "messages": messages, "temperature": 0}
    if os.getenv("API_EFFORT_PARAM"):
        body[os.environ["API_EFFORT_PARAM"]] = {"none": "low"}.get(effort, effort)
    req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    key = _key()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=int(os.getenv("API_TIMEOUT_S", "600"))) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        e.close()
        raise RuntimeError(f"Model API refused the request ({e.code}): {detail}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ConnectionError(f"Model API not reachable at {url} ({getattr(e, 'reason', e)})") from None
    return str(data["choices"][0]["message"].get("content") or "").strip()
