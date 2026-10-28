"""Score the LangGraph prototype on the same golden tasks and checks as eval.py.

  python -m lgagent.eval --backend claude
  python -m lgagent.eval --backend claude --only rwa_latest_total,file_average

Results go to runs/evals/<time>-lg-<backend>.json and .md, next to the step machine's results.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

import eval as base
from agent import config
from agent.plugins import basic_toolkits

from .chat_model import from_backend
from .graph import build, sqlite_checkpointer


def shown(state: dict) -> str:
    parts = [state.get("final") or ""]
    for path in state.get("tables") or []:
        full = config.SANDBOX_DIR / path
        if full.is_file():
            parts.append(full.read_text(errors="replace"))
    return "\n".join(parts)


def run_task(task: dict, model) -> dict:
    from .run import run
    graph = build(model, sqlite_checkpointer(), max_steps=task.get("max_steps"))
    asks = []
    t0 = time.perf_counter()
    try:
        state = run(task["instruction"], model, inputs=task.get("inputs", []),
                    toolkits=list(dict.fromkeys(basic_toolkits() + task.get("toolkits", []))),
                    skill=task.get("skill"), graph=graph,
                    ask=lambda v: asks.append(v) or "Yes, confirmed: go ahead with your proposal.")
    except Exception as e:                       # a crash scores 0 but does not stop the suite
        state = {"status": "error", "final": f"{type(e).__name__}: {e}", "run_id": "?"}
    seconds = time.perf_counter() - t0
    log_path = config.RUNS_DIR / state["run_id"] / "log.jsonl"
    log = [json.loads(line) for line in log_path.read_text().splitlines()] if log_path.exists() else []
    tool_calls = sum(1 for e in log if e["kind"] == "tool")
    answer = shown(state)
    checks = {"completed": state["status"] == "finished"}
    for n in task.get("expect_numbers", []):
        checks[f"number {n['value']:g}"] = base.found_number(answer, n["value"], n.get("tol", 0))
    checks.update(base.keyword_checks(task, answer))
    if task.get("expect_ask"):
        checks["asked the user to confirm"] = bool(asks)
    if "max_asks" in task:
        checks[f"questions <= {task['max_asks']}"] = len(asks) <= task["max_asks"]
    if "max_tool_calls" in task:
        checks[f"tool calls <= {task['max_tool_calls']}"] = tool_calls <= task["max_tool_calls"]
    models = [e for e in log if e["kind"] == "model"]
    return {"id": task["id"], "run_id": state["run_id"], "status": state["status"],
            "score": round(sum(checks.values()) / len(checks), 3), "checks": checks,
            "steps": tool_calls, "tool_calls": tool_calls, "model_calls": len(models),
            "model_seconds": round(sum(e.get("latency_ms", 0) for e in models) / 1000, 1),
            "prompt_tokens": sum(e.get("prompt_tokens", 0) for e in models),
            "repairs": sum(e.get("repairs", 0) for e in models),
            "review_rounds": state.get("review_rounds", 0), "seconds": round(seconds, 1),
            "answer": (state.get("final") or "")[:500]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="claude")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    tasks = base.load_tasks(base.GOLDEN_DIR, [t for t in args.only.split(",") if t])
    model = from_backend(args.backend)
    out_dir = config.RUNS_DIR / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"lg-{args.backend}"
    previous = sorted(out_dir.glob(f"*-{name}.json"))
    results = []
    for task in tasks:
        print(f"· {task['id']} …", flush=True)
        results.append(run_task(task, model))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    result = {"backend": name, "time": stamp, "tasks": results,
              "score": round(sum(r["score"] for r in results) / max(len(results), 1), 3)}
    (out_dir / f"{stamp}-{name}.json").write_text(json.dumps(result, indent=2))
    report = base.report(result, json.loads(previous[-1].read_text()) if previous else None)
    (out_dir / f"{stamp}-{name}.md").write_text(report)
    print("\n" + report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
