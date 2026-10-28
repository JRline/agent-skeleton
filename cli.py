"""Run the agent from the terminal. One invocation is one session; a conversation continues with --resume.

  python cli.py "What is a CET1 ratio?"                         # basic tools; plain questions need none
  python cli.py "Compute the latest capital ratio from inputs/sample_capital.csv"
  python cli.py --backend claude "/usage_comparison RWA by division"   # /skill, /toolkit or /tool in the task
  python cli.py --tools datalake,tables "RWA by risk type for SampleTrust"
  python cli.py --verbose ...                                   # also print thoughts, inputs and results
  python cli.py --yes ...                                       # auto-approve risky tools and suggestions
  python cli.py --resume 20261021-213000-ab12                   # continue a saved run
  python cli.py --resume 20261021-213000-ab12 "and for SampleTrust?"   # follow-up in that conversation
  python cli.py --input ~/Desktop/data.csv "..."             # attach a file (becomes an input)
  python cli.py --export-script 20261021-213000-ab12            # one .py that replays the whole analysis
  python cli.py --export-html 20261021-213000-ab12              # the whole conversation as one HTML page
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from agent import config, extensions
from agent.adapter import get_model
from agent.loop import DONE, continue_with, load_state, pending_question, run
from agent.skills import get_skill, list_skills
from agent.tools import step_summary


def show(ev: dict, verbose: bool) -> None:
    kind, p = ev["kind"], ev["payload"]
    if kind == "plan":
        print("\nPLAN" + (f" (skill {p['skill']})" if p.get("skill") else ""))
        for i, s in enumerate(p["plan"], 1):
            print(f"  {i}. {s}")
    elif kind == "suggest":
        offered = [f"toolkit {t}" for t in p["toolkits"]] + ([f"skill {p['skill']}"] if p.get("skill") else [])
        print(f"\nSUGGESTED: {', '.join(offered)}" + (f"  ({p['why']})" if p.get("why") else ""))
    elif kind == "tool":
        print(f"\n[{ev['step']}] {p['tool']}: {step_summary(p['obs'])}")
        if verbose:
            if p.get("thought"):
                print(f"    thought: {p['thought']}")
            print(f"    input: {json.dumps(p['input'])[:300]}  (effort={p['effort']})")
            print("    " + str(p["obs"].get("result") or p["obs"].get("error"))[:400].replace("\n", "\n    "))
    elif kind == "user" and "approved" in p:
        line = step_summary(p["obs"]) if p.get("obs") else ""
        print(f"\n[{ev['step']}] {p['tool']}: {'approved' if p['approved'] else 'rejected'}" + (f" · {line}" if line else ""))
    elif kind == "final":
        print("\nFINAL\n" + p["text"])
        for path in p.get("tables", []):
            print(f"  table: {path}")
        for w in p.get("warnings", []):
            print(f"  warning: {w}")
    elif kind == "model_error":
        print(f"\nMODEL CALL FAILED (step not taken): {p['error']}")
    elif kind == "review":
        print("\nReviewer asked for changes" + (": " + "; ".join(p.get("issues", [])) if verbose else ""))
    elif verbose:
        print(f"\n[{ev['step']}] {kind}: {json.dumps(p)[:300]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("goal", nargs="?", help="what the agent should do; may contain /commands")
    ap.add_argument("--backend", choices=config.BACKENDS, default=config.BACKEND)
    ap.add_argument("--tools", default="", help="extra toolkits for this session, e.g. datalake,tables")
    ap.add_argument("--verbose", action="store_true", help="print thoughts, tool inputs and results")
    ap.add_argument("--yes", action="store_true", help="auto-approve risky tools and accept suggestions")
    ap.add_argument("--resume", metavar="RUN_ID")
    ap.add_argument("--input", action="append", default=[], help="file to copy into sandbox/inputs")
    ap.add_argument("--skill", help="force a skill (same as /skill_name in the task)")
    ap.add_argument("--sign-in", action="append", default=[], metavar="CONNECTOR=USER_ID",
                    help="sign in to a connector that needs your own login (repeatable); the password is asked "
                         "without echo")
    ap.add_argument("--export-script", metavar="RUN_ID",
                    help="write a Python script that replays the conversation's analysis, then exit")
    ap.add_argument("--export-html", metavar="RUN_ID",
                    help="write the whole conversation as one HTML page, then exit")
    args = ap.parse_args()

    if args.export_html:
        from agent.export import export_html
        print(export_html(load_state(args.export_html)))
        return 0
    if args.export_script:
        from agent.replay import export
        print(export(load_state(args.export_script)))
        return 0

    if not args.goal and not args.resume:
        ap.error("give a goal or --resume RUN_ID")
    for item in args.sign_in:                # kept in this process's memory only (agent/credentials.py)
        from getpass import getpass
        from agent import credentials
        name, _, user = item.partition("=")
        if not user:
            ap.error("--sign-in takes CONNECTOR=USER_ID")
        credentials.use(credentials.DEFAULT, name, credentials.Login(user, getpass(f"Password for {name}: ")))

    inputs = []
    for f in args.input:
        src = Path(f).expanduser()
        dest = config.inputs_dir() / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
        inputs.append(f"inputs/{src.name}")

    cmd = extensions.parse_commands(args.goal or "")
    skill = args.skill or cmd["skill"]
    if skill and not get_skill(skill):
        ap.error(f"unknown skill {skill!r}; available: {', '.join(s.name for s in list_skills())}")
    extra = [t for t in args.tools.split(",") if t]
    unknown = [t for t in extra if t not in extensions.TOOLKITS]
    if unknown:
        ap.error(f"unknown toolkit(s) {unknown}; available: {', '.join(extensions.TOOLKITS)}")
    toolkits = list(dict.fromkeys(extensions.basic_toolkits() + extra + cmd["toolkits"]
                                  + (get_skill(skill).toolkits if skill else [])))
    for err in extensions.ERRORS:
        print(f"Plugin not loaded: {err}")

    def approve(ev):
        if args.yes:
            return True
        p = ev["payload"]
        print(f"\nAPPROVAL NEEDED: {p.get('tool')} {json.dumps(p.get('input'))[:300]}")
        if input("Approve? [y/N] ").strip().lower() == "y":
            return True
        return input("Why not? (the agent follows your reason) ").strip() or False

    def ask(ev):
        q = ev["payload"].get("question") or ev["payload"].get("input", {}).get("question", "")
        return input(f"\nAGENT ASKS: {q}\n> ").strip() or "No answer."

    def choose(ev):
        p = ev["payload"]
        accepted = {"toolkits": p["toolkits"], "skill": p.get("skill")}
        if args.yes:
            return accepted
        return accepted if input("Use the suggestion? [y/N] ").strip().lower() == "y" else {}

    model = get_model(args.backend)
    state = load_state(args.resume) if args.resume else None
    goal = cmd["goal"] or args.goal or ""
    if state and args.goal and state["status"] in DONE:       # a follow-up in an earlier conversation
        continue_with(state, goal, inputs=inputs, skill=skill, toolkits=toolkits)
        print(f"Continuing \"{state.get('title') or state['run_id']}\" · tools: {', '.join(toolkits)}")
    elif state:
        print(f"Resuming {state['run_id']} (status={state['status']}) {pending_question(state)}")
    else:
        print(f"Tools: {', '.join(toolkits)}" + (f" · skill {skill}" if skill else ""))
    state = run(goal, model, inputs=inputs, on_event=lambda e: show(e, args.verbose),
                approve=approve, ask=ask, choose=choose, state=state, skill=skill, toolkits=toolkits)
    if state["events"] and state["events"][-1]["kind"] == "model_error":
        print(f"\nFix the backend (for Ollama: `ollama serve`), then continue with:\n"
              f"  python cli.py --backend {args.backend} --resume {state['run_id']}")
    print(f"\nRun {state['run_id']} {state['status']}. Files in runs/{state['run_id']}/")
    return 0 if state["status"] == "finished" else 1


if __name__ == "__main__":
    sys.exit(main())
