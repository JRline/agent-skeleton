"""Worked examples for prompts (plugins/examples/<prompt>.md): valid, swappable per environment, and sent."""
import json
import re
import tempfile
from pathlib import Path

from agent import config
from agent.backends.fake import FakeModel
from agent.graph import plan
from agent.checks import _Plan
from agent.memory import new_state
from agent.extensions import TOOLKITS, examples, load
from agent.skills import get_skill

from tests.helpers import PLAN, TempDirs

GOLDEN = Path(__file__).resolve().parent / "golden"


def blocks(text: str) -> list[str]:
    return re.findall(r"<example>(.*?)</example>", text, flags=re.S)


class PlannerExamples(TempDirs):
    def test_bundled_examples_are_valid_planner_replies(self):
        load()
        found = blocks(examples("planner"))
        self.assertTrue(3 <= len(found) <= 5, len(found))
        for block in found:
            for label in ("GOAL:", "INPUT FILES:", "Reasoning:", "REPLY:"):
                self.assertIn(label, block)
            reply = _Plan.model_validate(json.loads(block.split("REPLY:", 1)[1].strip()))
            self.assertTrue(1 <= len(reply.plan) <= 7)
            skill = reply.suggest["skill"]
            self.assertTrue(skill == "none" or get_skill(skill), f"unknown skill {skill!r}")
            self.assertTrue(set(reply.suggest["toolkits"]) <= set(TOOLKITS))
            self.assertTrue(reply.title)

    def test_examples_never_copy_a_golden_task(self):
        text = examples("planner").lower()
        for path in GOLDEN.glob("*.json"):
            self.assertNotIn(json.loads(path.read_text())["instruction"].lower(), text, path.name)

    def test_author_notes_are_removed_and_a_missing_file_means_none(self):
        self.assertNotIn("<!--", examples("planner"))
        saved = config.EXAMPLES_DIR
        self.addCleanup(setattr, config, "EXAMPLES_DIR", saved)
        config.EXAMPLES_DIR = Path(tempfile.mkdtemp())
        self.assertEqual(examples("planner"), "(none)")
        (config.EXAMPLES_DIR / "planner.md").write_text("<!-- note -->\n<example>GOAL: swapped</example>\n")
        self.assertEqual(examples("planner"), "<example>GOAL: swapped</example>")

    def test_planner_prompt_carries_the_examples(self):
        model = FakeModel({"planner": [PLAN]})
        plan(new_state("What is in the file?"), model)
        prompt = model.calls[0]["messages"][0]["content"]
        self.assertIn("<example>", prompt)
        self.assertNotIn("{{examples}}", prompt)
