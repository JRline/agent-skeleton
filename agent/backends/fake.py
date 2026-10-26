"""FakeModel backends: no LLM needed (docs/design.md §14).

FakeModel      - scripted replies for tests. Give a list (consumed in call order)
                 or a dict {purpose: [replies]} (consumed per purpose). A reply may
                 be a string or a function(messages) -> str. Every call is recorded
                 in `.calls` so tests can assert on efforts and prompts.
DemoFakeModel  - deterministic demo used by the CLI and Streamlit app with
                 LLM_BACKEND=fake. It reads sandbox/inputs/sample_capital.csv,
                 runs analysis code (needs approval) and finishes with a result table.
"""
from __future__ import annotations

import json
import re

from ..adapter import ChatModel, apply_stop


APPROVE_REQUEST = json.dumps({"ok": True, "issues": [], "explain": "Runs the requested step."})


class FakeModel(ChatModel):
    name = "fake"

    def __init__(self, replies):
        if isinstance(replies, dict):
            self.replies = {k: list(v) for k, v in replies.items()}
        else:
            self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, messages, effort="none", stop=None, purpose="executor"):
        self.calls.append({"purpose": purpose, "effort": effort, "messages": messages})
        if purpose == "action_review" and (not isinstance(self.replies, dict) or purpose not in self.replies):
            return apply_stop(APPROVE_REQUEST, stop)        # unless a test scripts the request review
        queue = self.replies.get(purpose, []) if isinstance(self.replies, dict) else self.replies
        if not queue:
            raise RuntimeError(f"FakeModel ran out of scripted replies for purpose={purpose!r}")
        reply = queue.pop(0)
        if callable(reply):
            reply = reply(messages)
        return apply_stop(reply, stop)

    def efforts(self, purpose: str | None = None) -> list[str]:
        return [c["effort"] for c in self.calls if purpose is None or c["purpose"] == purpose]


def _observations(messages) -> int:
    """Count tool observations already in the conversation."""
    text = "\n".join(m["content"] for m in messages if m["role"] != "system")
    return len(re.findall(r"^OBSERVATION:", text, flags=re.M))


DEMO_CODE = """df = tables["cap"]
df["cet1_ratio_pct"] = (df["cet1_capital"] / df["rwa"] * 100).round(2)
df["requirement_pct"] = 8.0
df["headroom_pp"] = (df["cet1_ratio_pct"] - df["requirement_pct"]).round(2)
result = df[["period", "cet1_ratio_pct", "requirement_pct", "headroom_pp"]]
notes = "Illustrative requirement: 4.5% minimum plus 3.5% buffers"
"""


class DemoFakeModel(ChatModel):
    """Stateless scripted demo: decides what to do from the prompt it receives."""

    name = "fake-demo"

    def __init__(self):
        self.calls: list[dict] = []

    def complete(self, messages, effort="none", stop=None, purpose="executor"):
        self.calls.append({"purpose": purpose, "effort": effort})
        if purpose == "planner":
            reply = {"plan": [
                "Compute the capital ratio and headroom for every period from the input file",
                "Return the result table",
            ]}
        elif purpose == "reviewer":
            reply = {"ok": True, "issues": []}
        elif purpose == "action_review":
            reply = {"ok": True, "issues": [], "explain": "Reads the capital file and computes each period's CET1 "
                                                          "ratio and its headroom over the 8% requirement."}
        elif purpose == "summariser":
            return "- Earlier steps read the input file and computed the ratio."
        else:
            reply = self._executor_step(_observations(messages))
        return apply_stop(json.dumps(reply), stop)

    @staticmethod
    def _executor_step(n_obs: int) -> dict:
        script = [   # one approved step reads the file and analyses it: no separate read to approve
            {"thought": "Compute every period's ratio and headroom in one run.", "tool": "run_python",
             "input": {"tables": {"cap": "inputs/sample_capital.csv"}, "save_as": "capital_ratios",
                       "code": DEMO_CODE},
             "step_done": True},
        ]
        if n_obs < len(script):
            return script[n_obs]
        return {"thought": "All steps are done.", "tables": ["capital_ratios"],
                "final": "Illustrative 2026Q3 CET1 ratio is 12.66% against a combined requirement of 8.0%, "
                         "leaving 4.66 pp of headroom [ref: run_python-1]."}
