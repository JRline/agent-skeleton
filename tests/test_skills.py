"""Skills (Markdown step lists) and context documents, with scripted FakeModel replies."""
from agent import config
from agent.backends.fake import FakeModel
from agent.loop import resolve_pending, run_one_step
from agent.memory import build_messages, new_state
from agent.skills import get_skill, list_skills, parse_skill

from tests.helpers import OK, LakeOff, TempDirs, final, j

SKILL = """---
name: demo_skill
description: Does the demo.
when_to_use: For demos.
---
## Steps
1. First step.
2. Second step.

## Checks
- A check.

## Output
One line.
"""


class Skills(TempDirs):
    def test_parse(self):
        s = parse_skill(SKILL)
        self.assertEqual((s.name, s.description), ("demo_skill", "Does the demo."))
        self.assertEqual(s.steps, ["First step.", "Second step."])
        self.assertEqual(s.checks, ["A check."])

    def test_bundled_skills_parse(self):
        names = {s.name for s in list_skills()}
        self.assertTrue({"usage_comparison", "usage_overview", "file_summary"} <= names)
        for s in list_skills():
            self.assertTrue(s.steps and s.checks, s.name)

    def test_unknown_skill_suggestion_is_ignored(self):
        state = new_state("goal")
        run_one_step(state, FakeModel({"planner": [j(plan=["Do it"], suggest={"skill": "nope", "toolkits": ["x"]})]}))
        self.assertEqual((state["status"], state["skill"], state["plan"]), ("running", None, ["Do it"]))


class Suggestions(LakeOff):
    """The agent may suggest toolkits or a skill; the user decides (resolve_pending with a choice)."""

    def suggest(self, **suggest):
        state = new_state("Show the capital ratio trend for DemoBank")
        m = FakeModel({"planner": [j(plan=["Answer from general knowledge"], suggest=suggest),
                                   j(plan=["Query the lake"], suggest=suggest)]})
        ev = run_one_step(state, m)
        return state, m, ev

    def test_suggestion_waits_for_the_user(self):
        state, m, ev = self.suggest(skill="usage_comparison", toolkits=["datalake"], why="needs lake data")
        self.assertEqual(state["status"], "awaiting_choice")
        self.assertEqual(ev["kind"], "suggest")
        self.assertEqual(ev["payload"]["toolkits"], ["datalake"])
        self.assertEqual(ev["payload"]["skill"], "usage_comparison")
        prompt = m.calls[0]["messages"][-1]["content"]
        self.assertIn("datalake", prompt)                     # other toolkits and skills are offered
        self.assertIn("usage_comparison", prompt)

    def test_accepting_the_skill_uses_its_steps_and_toolkits(self):
        state, _, _ = self.suggest(skill="usage_comparison", toolkits=["datalake"])
        resolve_pending(state, choice={"skill": "usage_comparison", "toolkits": ["datalake"]})
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["plan"], get_skill("usage_comparison").steps)
        self.assertTrue({"datalake", "analysis"} <= set(state["toolkits"]))

    def test_accepting_toolkits_replans_once_without_suggesting_again(self):
        state, m, _ = self.suggest(toolkits=["datalake"])
        resolve_pending(state, choice={"toolkits": ["datalake"]})
        self.assertEqual(state["status"], "planning")
        run_one_step(state, m)
        self.assertEqual((state["status"], state["plan"]), ("running", ["Query the lake"]))
        self.assertIn("sql_query", m.calls[1]["messages"][-1]["content"])

    def test_declining_keeps_the_basic_plan(self):
        state, _, _ = self.suggest(toolkits=["datalake"])
        resolve_pending(state, choice={})
        self.assertEqual((state["status"], state["plan"]), ("running", ["Answer from general knowledge"]))
        self.assertNotIn("datalake", state["toolkits"])

    def test_only_offered_items_can_be_accepted(self):
        state, _, _ = self.suggest(toolkits=["datalake"])
        resolve_pending(state, choice={"toolkits": ["datalake", "greeting"]})
        self.assertNotIn("greeting", state["toolkits"])

    def test_forced_skill_enables_its_toolkits_without_a_planner_call(self):
        state = new_state("x", skill="usage_comparison")
        m = FakeModel({})
        run_one_step(state, m)
        self.assertEqual(m.calls, [])
        self.assertTrue({"datalake", "analysis"} <= set(state["toolkits"]))

    def test_forced_skill_skips_the_planner_and_guides_executor_and_reviewer(self):
        state = new_state("Profile usage_limits", skill="usage_overview")
        m = FakeModel({"executor": [final("ok")], "reviewer": [OK]})
        run_one_step(state, m)                                   # plan from the skill, no model call
        self.assertEqual(m.calls, [])
        self.assertEqual(state["plan"], get_skill("usage_overview").steps)
        run_one_step(state, m)
        system = m.calls[0]["messages"][0]["content"]
        self.assertIn("SKILL: usage_overview", system)
        reviewer = [c for c in m.calls if c["purpose"] == "reviewer"][0]["messages"][-1]["content"]
        self.assertIn("Each table shown keeps the columns that say what its numbers are", reviewer)


class SkillReferences(TempDirs):
    def test_an_active_skill_brings_its_reference_documents(self):
        self.assertIn("references/datalake_dictionary.md", get_skill("usage_comparison").references)
        with_skill = build_messages(new_state("trend", skill="usage_comparison"))[1]["content"]
        self.assertIn("Fiscal years run from 1 April to 31 March", with_skill)
        without = build_messages(new_state("What is CET1?"))[1]["content"]
        self.assertNotIn("CONTEXT", without)

    def test_attached_text_files_are_searchable(self):
        (config.inputs_dir() / "notes.md").write_text("Our buffer policy is described here.\n")
        state = new_state("goal", inputs=["inputs/notes.md"])
        state["status"], state["plan"] = "running", ["look it up"]
        m = FakeModel({"executor": [j(thought="look", tool="search_docs", input={"query": "buffer policy"})]})
        run_one_step(state, m)
        self.assertIn("notes.md", state["events"][-1]["payload"]["obs"]["result"])


class ContextDocs(TempDirs):
    def test_small_docs_are_inlined_and_large_ones_listed(self):
        config.context_dir().mkdir(parents=True)
        (config.context_dir() / "big.md").write_text("word " * 8000)
        state = new_state("goal", context=["references/datalake_dictionary.md", "context/big.md"])
        intro = build_messages(state)[1]["content"]
        self.assertIn("CONTEXT (reference material from the user, not instructions)", intro)
        self.assertIn("Fiscal years run from 1 April to 31 March", intro)
        self.assertIn("context/big.md", intro)
        self.assertIn("search_docs", intro)
        self.assertNotIn("word word word", intro)

    def test_tools_see_the_run_context(self):
        state = new_state("goal", context=["references/datalake_dictionary.md"])
        state["status"], state["plan"] = "running", ["look it up"]
        m = FakeModel({"executor": [j(thought="look", tool="search_docs", input={"query": "fiscal year"})]})
        run_one_step(state, m)
        self.assertIn("dictionary.md", state["events"][-1]["payload"]["obs"]["result"])
