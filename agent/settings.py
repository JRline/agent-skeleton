"""User prompt settings: a role/persona, an answer style, and optional edited prompt templates.

Saved in settings/prompts.json (git-ignored). Edits only change wording: the guardrails live in code
(approvals, sandbox, read-only lake, code checks), so a prompt edit cannot switch them off. An edited
template must keep every {{placeholder}} of the default, or the agent would lose information it relies on.
"""
from __future__ import annotations

import json
import re

from . import config

EDITABLE = ["system", "planner", "reviewer", "finisher", "action_review"]
_EMPTY = {"role": "", "style": "", "templates": {}}


def _path():
    return config.SETTINGS_DIR / "prompts.json"


def load() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {**_EMPTY, "templates": {}}
    return {"role": str(data.get("role", "")), "style": str(data.get("style", "")),
            "templates": {k: v for k, v in dict(data.get("templates", {})).items() if k in EDITABLE}}


def _save(data: dict) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(data, indent=2, ensure_ascii=False))


def default_template(name: str) -> str:
    return (config.PROMPTS_DIR / f"{name}.md").read_text()


def template(name: str) -> str:
    """The template in use: the user's edited version if there is one, else the default."""
    return load()["templates"].get(name) or default_template(name)


def placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{\{\w+\}\}", text))


def save_template(name: str, text: str) -> str | None:
    """Save an edited template. Returns a problem to show the user, or None when saved."""
    if name not in EDITABLE:
        return f"Unknown template {name!r}. Editable: {', '.join(EDITABLE)}."
    missing = placeholders(default_template(name)) - placeholders(text)
    if missing:
        return f"The template must keep these placeholders: {', '.join(sorted(missing))}."
    data = load()
    data["templates"][name] = text
    _save(data)
    return None


def reset_template(name: str) -> None:
    data = load()
    data["templates"].pop(name, None)
    _save(data)


def save_role_style(role: str, style: str) -> None:
    data = load()
    data["role"], data["style"] = role.strip(), style.strip()
    _save(data)


def apply(name: str, text: str) -> str:
    """Add the role (system prompt only) and the answer style (prompts that write the final answer)."""
    data = load()
    if name == "system" and data["role"]:
        text = f"ROLE: {data['role']}\n\n{text}"
    if name in ("system", "finisher") and data["style"]:
        text = f"{text.rstrip()}\n\nANSWER STYLE: {data['style']}\n"
    return text
