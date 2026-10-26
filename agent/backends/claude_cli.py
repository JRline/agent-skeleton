"""Claude (Haiku by default) through the Claude Code CLI that ships with the Claude desktop app.

A real Claude model with the same `call(text, effort)` shape as production, but no API key,
package or server: it reuses the Claude Code login on this Mac. Development only.

Each call runs `claude -p` with all of Claude Code's own tools switched off and our system prompt,
in an empty temporary folder so no project CLAUDE.md is loaded. Prompts go to Anthropic, as they do
in any Claude Code session, so use synthetic data only.

Env:
  CLAUDE_MODEL   default claude-haiku-5-5 (e.g. claude-sonnet-5-5 for comparison)
  CLAUDE_CLI     path to the claude binary (default: PATH, then the desktop app's newest copy)
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading

from ..adapter import CallCancelled, render

EFFORT = {"none": "low", "medium": "medium"}
_active: set = set()          # running CLI processes, so cancel() can stop them
_lock = threading.Lock()


def model_name(effort: str) -> str:
    return os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")


def _version(path: str) -> tuple:
    m = re.search(r"/claude-code/([\d.]+)/", path)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def find_cli(path_lookup: bool = True) -> str:
    if os.getenv("CLAUDE_CLI"):
        return os.environ["CLAUDE_CLI"]
    if path_lookup and shutil.which("claude"):
        return shutil.which("claude")
    pattern = os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude")
    found = sorted(glob.glob(pattern), key=_version)
    if not found:
        raise RuntimeError("Claude Code CLI not found. Install it or set CLAUDE_CLI to its path.")
    return found[-1]


def call(text, effort: str = "none") -> str:
    messages = text if isinstance(text, list) else [{"role": "user", "content": text}]
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    conversation = render([m for m in messages if m["role"] != "system"])
    cmd = [find_cli(), "-p", "--model", model_name(effort), "--tools", "",
           "--effort", EFFORT.get(effort, "low"), "--output-format", "json",
           "--no-session-persistence", "--strict-mcp-config"]
    if system:
        cmd += ["--system-prompt", system]
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        proc.cancelled = False
        with _lock:
            _active.add(proc)
        try:
            out, err = proc.communicate(conversation, timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RuntimeError("claude CLI timed out after 600 s.")
        finally:
            with _lock:
                _active.discard(proc)
    if proc.cancelled:
        raise CallCancelled("Stopped by user.")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {(err or out).strip()[-300:]}")
    if data.get("is_error") or proc.returncode != 0:
        raise RuntimeError(f"claude CLI error: {str(data.get('result'))[:300]}")
    return str(data.get("result", "")).strip()


def cancel() -> None:
    """Stop running calls (called from another thread, e.g. the UI's Stop button)."""
    with _lock:
        procs = list(_active)
    for proc in procs:
        proc.cancelled = True
        proc.kill()
