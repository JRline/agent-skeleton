"""Prompt settings: role, answer style and template overrides, with placeholder validation."""
from agent import settings
from agent.memory import build_messages, load_prompt, new_state

from tests.helpers import TempDirs


class PromptSettings(TempDirs):
    def test_defaults_when_nothing_is_saved(self):
        self.assertEqual(settings.load(), {"role": "", "style": "", "templates": {}})
        self.assertIn("Reply protocol", load_prompt("system", tools="T", guidance="G"))

    def test_role_and_style_shape_the_system_prompt(self):
        settings.save_role_style("You are a regulatory capital analyst.",
                                 "Answer in English, concise, amounts in JPY bn.")
        system = build_messages(new_state("hi"))[0]["content"]
        self.assertTrue(system.startswith("ROLE: You are a regulatory capital analyst"))
        self.assertIn("ANSWER STYLE: Answer in English, concise, amounts in JPY bn.", system)
        self.assertIn("ANSWER STYLE", load_prompt("finisher", goal="g", facts="f"))
        self.assertNotIn("ROLE:", load_prompt("planner", goal="g", inputs="", tool_names="", other_toolkits="",
                                              skills="", earlier="", context=""))

    def test_template_override_is_used_and_can_be_reset(self):
        default = settings.default_template("reviewer")
        edited = default.replace("1. It answers the whole goal.", "1. It answers the whole goal (be strict).")
        self.assertIsNone(settings.save_template("reviewer", edited))
        self.assertIn("be strict", load_prompt("reviewer", goal="g", final="f", facts="x", cited="c",
                                               checks="", context=""))
        settings.reset_template("reviewer")
        self.assertEqual(settings.template("reviewer"), default)

    def test_reviewer_sees_where_the_final_answer_ends(self):
        """Without markers Haiku read the checklist after FINAL as part of the answer and rejected it."""
        prompt = load_prompt("reviewer", goal="g", final="The answer.", facts="x", cited="c", checks="", context="")
        self.assertIn("<<<FINAL\nThe answer.\nFINAL>>>", prompt)
        self.assertLess(prompt.index("FINAL>>>"), prompt.index("REVIEW CHECKLIST"))

    def test_override_missing_a_placeholder_is_refused(self):
        problem = settings.save_template("system", "Be nice. No tools here.")
        self.assertIn("{{tools}}", problem)
        self.assertEqual(settings.load()["templates"], {})

    def test_unknown_template_is_refused(self):
        self.assertIsNotNone(settings.save_template("secret", "x"))


class ReviewerScope(TempDirs):
    def test_reviewer_reports_only_what_makes_the_answer_wrong(self):
        prompt = load_prompt("reviewer", goal="g", final="f", facts="x", cited="c", checks="", context="")
        self.assertIn("Report a problem ONLY when it makes the answer wrong", prompt)
        self.assertIn("Do NOT report: missing caveats, method notes or scope statements", prompt)
        self.assertIn("the documentation is a\nvalid source", prompt)
