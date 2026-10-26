"""Build a ready-to-send context pack for the GPT CLI when it is used to maintain this agent.

The GPT CLI cannot read the project, so each maintenance task starts by giving it the request, the rules and the
files it needs. This prints that bundle; the prompt for each task lives in docs/gpt_prompts/<task>.md.

  python scripts/gpt_pack.py skill "Add a skill that ..." [--like usage_comparison]
  python scripts/gpt_pack.py toolkit "Add a tool that ..."
  python scripts/gpt_pack.py golden "Add eval tasks for ..."
  python scripts/gpt_pack.py examples "Add an example where ..."
  python scripts/gpt_pack.py view "Add the view ... (columns and notes pasted here)"
  python scripts/gpt_pack.py diagnose "It answered for the whole year" --run 20261025-221016-fbe9
Add --out pack.md to write a file instead of printing. See docs/maintenance.md for the full procedures.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import config  # noqa: E402
from agent.memory import load_state, read_log  # noqa: E402
from agent.plugins import catalogue  # noqa: E402
from agent.skills import render_skill_list  # noqa: E402

TASKS = ("skill", "toolkit", "golden", "examples", "view", "diagnose")
FILE_LIMIT = 15000          # chars per file: the GPT CLI has a context limit
WARN_TOTAL = 60000


def block(title: str, text: str) -> str:
    if len(text) > FILE_LIMIT:
        text = text[:FILE_LIMIT] + f"\n…[cut at {FILE_LIMIT} of {len(text)} characters]"
    return f"\n===== {title} =====\n{text.rstrip()}\n"


def file_block(rel: str) -> str:
    path = ROOT / rel
    return block(rel, path.read_text()) if path.is_file() else block(rel, "(file not found)")


def dictionary_block() -> str:
    """The data dictionary lives in the sandbox, which tests and production may place elsewhere."""
    path = config.datalake_dictionary()
    title = "sandbox/references/" + path.name
    return block(title, path.read_text()) if path.is_file() else block(title, "(file not found)")


def tool_list() -> str:
    """Tools the agent can use, so a skill or example names only real ones."""
    items = catalogue()
    kits = [f"- toolkit {i['name']}: {i['description']} (tools: {', '.join(i['tools'])})"
            for i in items if i["kind"] == "toolkit"]
    tools = [f"- {i['name']} [{i['toolkit']}]: {i['description']}" for i in items if i["kind"] == "tool"]
    return "\n".join(kits + [""] + tools)


def run_record(run_id: str, limit: int = 600) -> str:
    """One line per logged event, outputs cut short: enough to see where a run went wrong."""
    state = load_state(run_id)
    lines = [f"goal: {state['goal']}", f"status: {state['status']}  skill: {state.get('skill')}",
             "plan:"] + [f"  {i}. {s}" for i, s in enumerate(state["plan"], 1)] + ["", "events:"]
    for e in read_log(state):
        kind = e["kind"]
        if kind == "model":
            text = e.get("error") or e.get("output") or ""
            lines.append(f"[{e.get('step')}] {e['purpose']} ({e['effort']}, {e['latency_ms']} ms): {text[:limit]}")
        elif kind == "tool":
            lines.append(f"[{e.get('step')}] TOOL {e.get('tool')} ok={e.get('ok')} "
                         f"input={json.dumps(e.get('input'))[:limit]}")
        elif kind in ("user", "review", "abort", "model_error"):
            rest = {k: v for k, v in e.items() if k not in ("ts", "kind", "step")}
            lines.append(f"[{e.get('step')}] {kind}: {json.dumps(rest)[:limit]}")
    return "\n".join(lines)


def build(task: str, request: str, like: str | None = None, run: str | None = None) -> str:
    prompt = (ROOT / "docs" / "gpt_prompts" / f"{task}.md").read_text().replace("{request}", request.strip())
    parts = [prompt]
    if task == "skill":
        parts += [file_block("docs/plugins.md"), block("TOOLS THE AGENT HAS", tool_list()),
                  block("EXISTING SKILLS", render_skill_list()),
                  file_block(f"plugins/skills/{like or 'usage_comparison'}.md"), dictionary_block()]
    elif task == "toolkit":
        parts += [file_block("docs/plugins.md"), file_block("plugins/tools/_example_toolkit.py"),
                  block("TOOLS THE AGENT ALREADY HAS", tool_list())]
    elif task == "golden":
        golden = sorted((ROOT / "tests" / "golden").glob("*.json"))
        parts += [block("EXISTING TASKS (do not copy)", "\n".join(p.read_text().strip() for p in golden)),
                  dictionary_block()]
    elif task == "examples":
        parts += [file_block("plugins/examples/planner.md"), block("SKILLS", render_skill_list()),
                  block("TOOLKITS AND TOOLS", tool_list())]
    elif task == "view":
        parts += [file_block("docs/datasource_entry.md"), dictionary_block()]
    elif task == "diagnose":
        if not run:
            raise SystemExit("diagnose needs --run RUN_ID (the folder name under runs/)")
        state = load_state(run)
        parts += [block(f"RUN {run}", run_record(run))]
        if state.get("skill"):
            parts.append(file_block(f"plugins/skills/{state['skill']}.md"))
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", choices=TASKS)
    ap.add_argument("request", help="what you want, in your own words")
    ap.add_argument("--like", help="skill: an existing skill to show as the example (default usage_comparison)")
    ap.add_argument("--run", help="diagnose: the run id (folder name under runs/)")
    ap.add_argument("--out", help="write the pack to this file instead of printing it")
    args = ap.parse_args()
    pack = build(args.task, args.request, args.like, args.run)
    if args.out:
        Path(args.out).write_text(pack)
        print(f"Wrote {args.out} ({len(pack):,} characters).")
    else:
        print(pack)
    if len(pack) > WARN_TOTAL:
        print(f"Note: {len(pack):,} characters; if the GPT CLI cuts it off, send the blocks in two parts.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
