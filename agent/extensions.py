"""Discovers toolkits (Python) and skills (Markdown) so the UI, CLI and /commands share one catalogue.

Built in:     agent/toolkits/*.py
Added later:  plugins/tools/*.py   (same contract as the built-in toolkits)
              plugins/skills/*.md  (see agent/skills.py)
The contract for both is in docs/plugins.md. A plugin that fails to load is reported in ERRORS
instead of breaking the app, so a half-written plugin never takes the agent down.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path

from . import config

TOOLKITS: dict[str, dict] = {}     # name -> TOOLKIT dict (+ "source": module file)
ERRORS: list[str] = []
_loaded = False


def load(force: bool = False) -> None:
    global _loaded
    if _loaded and not force:
        return
    _loaded = True
    ERRORS.clear()
    for path in sorted((Path(__file__).parent / "toolkits").glob("*.py")):
        if not path.stem.startswith("_"):
            _register(lambda: importlib.import_module(f"agent.toolkits.{path.stem}"), path)
    for path in sorted(config.TOOLKITS_DIR.glob("*.py")):
        if not path.stem.startswith("_"):
            _register(lambda: _import_file(path), path)


def _import_file(path: Path):
    name = f"plugins_tools_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module          # so @tool can read this module's TOOLKIT while it loads
    spec.loader.exec_module(module)
    return module


def _register(importer, path: Path) -> None:
    try:
        module = importer()
        kit = getattr(module, "TOOLKIT", None)
        if not (isinstance(kit, dict) and kit.get("name") and kit.get("description")):
            raise ValueError("TOOLKIT = {'name': ..., 'description': ...} is missing")
        if kit["name"] in TOOLKITS and TOOLKITS[kit["name"]]["source"] != str(path):
            raise ValueError(f"toolkit name {kit['name']!r} is already used by {TOOLKITS[kit['name']]['source']}")
        TOOLKITS[kit["name"]] = {"basic": False, "guidance": "", **kit, "source": str(path)}
    except Exception as e:
        ERRORS.append(f"{path.name}: {type(e).__name__}: {e}")


def basic_toolkits() -> list[str]:
    """The default toolkits of a new session: config.BASIC_TOOLKITS if set, else those marked basic."""
    load()
    names = config.BASIC_TOOLKITS or [n for n, k in TOOLKITS.items() if k.get("basic")]
    return [n for n in names if n in TOOLKITS]


def examples(prompt: str) -> str:
    """Worked examples for a prompt, from examples/<prompt>.md. Kept out of the prompt itself so each environment
    (local, production) can swap in examples that name its own skills and views. HTML comments are notes for
    authors and are removed; a missing or empty file means no examples."""
    try:
        text = (config.EXAMPLES_DIR / f"{prompt}.md").read_text()
    except OSError:
        return "(none)"
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip() or "(none)"


def toolkit_tools(name: str) -> list[str]:
    from .tools import TOOLS
    return [t.name for t in TOOLS.values() if t.toolkit == name]


def catalogue() -> list[dict]:
    """Everything a user can enable or invoke with /name: toolkits, their tools, and skills."""
    from .skills import list_skills
    load()
    items = [{"name": n, "kind": "toolkit", "description": k["description"], "tools": toolkit_tools(n)}
             for n, k in TOOLKITS.items()]
    from .tools import TOOLS
    items += [{"name": t.name, "kind": "tool", "description": t.desc, "toolkit": t.toolkit}
              for t in TOOLS.values() if t.toolkit != "core"]
    items += [{"name": s.name, "kind": "skill", "description": s.description, "toolkits": s.toolkits}
              for s in list_skills()]
    return items


_COMMAND = re.compile(r"(?<![\w/:.])/([A-Za-z_][\w\-]*)")


def parse_commands(goal: str) -> dict:
    """Find /name commands in a task. Returns the goal without them, the toolkits to enable,
    a skill to force (if any) and unknown names (left in the text: they may be paths)."""
    by_name = {}
    for item in catalogue():
        by_name.setdefault(item["name"], item)
    toolkits, skill, unknown = [], None, []

    def handle(m):
        nonlocal skill
        item = by_name.get(m.group(1))
        if item is None:
            unknown.append(m.group(1))
            return m.group(0)
        if item["kind"] == "toolkit":
            toolkits.append(item["name"])
        elif item["kind"] == "tool":
            toolkits.append(item["toolkit"])
        else:
            skill = item["name"]
            toolkits.extend(item["toolkits"])
        return ""

    clean = re.sub(r"\s{2,}", " ", _COMMAND.sub(handle, goal)).strip()
    return {"goal": clean, "toolkits": list(dict.fromkeys(toolkits)), "skill": skill, "unknown": unknown}
