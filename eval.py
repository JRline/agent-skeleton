"""Score a backend on the golden tasks (docs/design.md §14). Works unchanged against `prod`.

  python eval.py --backend claude                 # every task in tests/golden/
  python eval.py --backend ollama --only cet1_latest,total_rwa
  python eval.py --backend fake

Numbers and keywords are checked in what the user sees: the answer and the tables shown under it.
Each task runs as a fresh conversation with risky tools auto-approved and suggestions accepted (synthetic
data only). Scored: completed, expected numbers in the answer, expected keywords, tool-call budget and step
budget. Also recorded: steps, tool calls, model calls, model seconds, repairs, reviewer rounds, wall time.
Results go to runs/evals/<time>-<backend>.json and .md, compared with the previous run of the same backend.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from agent import config
from agent.adapter import get_model
from agent.loop import run
from agent.memory import read_log
from agent.plugins import basic_toolkits

GOLDEN_DIR = Path(__file__).resolve().parent / "tests" / "golden"


def load_tasks(folder: Path, only: list[str] | None = None) -> list[dict]:
    tasks = [json.loads(p.read_text()) for p in sorted(Path(folder).glob("*.json"))]
    return [t for t in tasks if not only or t["id"] in only]


def found_number(text: str, value: float, tol: float) -> bool:
    for token in re.findall(r"-?\d[\d,]*\.?\d*", text):
        try:
            if abs(float(token.replace(",", "").rstrip(".")) - value) <= tol:
                return True
        except ValueError:
            continue
    return False


def keyword_checks(task: dict, answer: str) -> dict[str, bool]:
    """Each expected keyword must appear; a list means any one of them (e.g. a product name or its code)."""
    checks = {}
    for word in task.get("expect_keywords", []):
        options = word if isinstance(word, list) else [word]
        checks[f"mentions '{' or '.join(options)}'"] = any(w.lower() in answer.lower() for w in options)
    return checks


def shown_answer(state: dict) -> str:
    """What the user sees for the last instruction: the answer text plus the tables shown under it (answers are
    told not to repeat a listed table, so its numbers count as part of the answer)."""
    final = next((e for e in reversed(state["events"]) if e["kind"] == "final"), None)
    parts = [state.get("final") or ""]
    for path in (final["payload"].get("tables", []) if final else []):
        full = config.SANDBOX_DIR / path
        if full.is_file():
            parts.append(full.read_text(errors="replace"))
    return "\n".join(parts)


def run_task(task: dict, model) -> dict:
    """One golden task as a fresh conversation; returns checks, score and metrics."""
    saved = config.MAX_STEPS
    config.MAX_STEPS = task.get("max_steps", saved)
    model.new_session()                  # session memory mode: each task starts with fresh instances
    t0 = time.perf_counter()
    try:
        state = run(task["instruction"], model, inputs=task.get("inputs", []),
                    toolkits=list(dict.fromkeys(basic_toolkits() + task.get("toolkits", []))),
                    skill=task.get("skill"), approve=lambda e: True,
                    ask=lambda e: "Yes, confirmed: go ahead with your proposal.",
                    choose=lambda e: {"toolkits": e["payload"]["toolkits"], "skill": e["payload"].get("skill")})
    finally:
        config.MAX_STEPS = saved
    seconds = time.perf_counter() - t0
    answer = state.get("final") or ""
    shown = shown_answer(state)
    log = read_log(state)
    tool_calls = sum(1 for e in log if e["kind"] == "tool")
    checks = {"completed": state["status"] == "finished"}
    for n in task.get("expect_numbers", []):
        label = f"number {n['value']:g}"
        checks[label] = found_number(shown, n["value"], n.get("tol", 0))
    checks.update(keyword_checks(task, shown))
    asks = sum(1 for e in state["events"] if e["kind"] == "ask_user")
    if task.get("expect_ask"):
        checks["asked the user to confirm"] = asks > 0
    if "max_asks" in task:
        checks[f"questions <= {task['max_asks']}"] = asks <= task["max_asks"]
    if "max_tool_calls" in task:
        checks[f"tool calls <= {task['max_tool_calls']}"] = tool_calls <= task["max_tool_calls"]
    models = [e for e in log if e["kind"] == "model"]
    return {
        "id": task["id"], "run_id": state["run_id"], "status": state["status"],
        "score": round(sum(checks.values()) / len(checks), 3), "checks": checks,
        "steps": state["step_count"], "tool_calls": tool_calls, "model_calls": len(models),
        "model_seconds": round(sum(e.get("latency_ms", 0) for e in models) / 1000, 1),
        "prompt_tokens": sum(e.get("prompt_tokens", 0) for e in models),
        "sent_tokens": sum(e.get("sent_tokens", e.get("prompt_tokens", 0)) for e in models),
        "repairs": sum(1 for e in state["events"] if e["kind"] == "error")
        + sum(1 for e in state["events"] if e["kind"] == "tool" and e["payload"].get("attempts", 1) > 1),
        "review_rounds": state.get("review_rounds", 0), "seconds": round(seconds, 1),
        "answer": answer[:500],
    }


def report(result: dict, previous: dict | None) -> str:
    prev = {r["id"]: r for r in (previous or {}).get("tasks", [])}
    lines = [f"# Eval {result['backend']} · {result['time']} · score {result['score']:.2f}"
             + (f" (previous {previous['score']:.2f})" if previous else ""), "",
             "| task | score | prev | steps | tools | model calls | model s | repairs | failed checks |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in result["tasks"]:
        failed = ", ".join(k for k, ok in r["checks"].items() if not ok) or "-"
        before = f"{prev[r['id']]['score']:.2f}" if r["id"] in prev else "-"
        lines.append(f"| {r['id']} | {r['score']:.2f} | {before} | {r['steps']} | {r['tool_calls']} | "
                     f"{r['model_calls']} | {r['model_seconds']} | {r['repairs']} | {failed} |")
    return "\n".join(lines) + "\n"


def run_suite(tasks: list[dict], make_model, backend: str, out_dir: Path | None = None) -> dict:
    out_dir = Path(out_dir or config.RUNS_DIR / "evals")
    out_dir.mkdir(parents=True, exist_ok=True)
    previous_files = sorted(out_dir.glob(f"*-{backend}.json"))
    previous = json.loads(previous_files[-1].read_text()) if previous_files else None
    results = []
    for task in tasks:
        print(f"· {task['id']} …", flush=True)
        results.append(run_task(task, make_model()))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    result = {"backend": backend, "time": stamp, "tasks": results,
              "score": round(sum(r["score"] for r in results) / max(len(results), 1), 3)}
    (out_dir / f"{stamp}-{backend}.json").write_text(json.dumps(result, indent=2))
    (out_dir / f"{stamp}-{backend}.md").write_text(report(result, previous))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=config.BACKENDS, default=config.BACKEND)
    ap.add_argument("--only", default="", help="comma-separated task ids")
    ap.add_argument("--tasks", default=str(GOLDEN_DIR), help="folder of task files")
    args = ap.parse_args()
    tasks = load_tasks(Path(args.tasks), [t for t in args.only.split(",") if t])
    if not tasks:
        ap.error("no tasks found")
    result = run_suite(tasks, lambda: get_model(args.backend), args.backend)
    out = sorted((config.RUNS_DIR / "evals").glob(f"*-{args.backend}.md"))[-1]
    print("\n" + out.read_text() + f"\nSaved {out} (and .json)")
    return 0 if result["score"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
