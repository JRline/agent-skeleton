"""File names with spaces or typos reach every model quoted and are kept exactly as written.
(A small model once rewrote "daily captial.xlsx" as "daily_captial.xlsx", and the request check then
rejected the correct name as invalid: three reads and a needless question to the user.)"""
from agent.graph import first_prompt
from agent import config
from agent.backends.fake import FakeModel
from agent.graph import plan, review_request
from agent.memory import new_state
from agent.tools import dispatch, quote_paths, ToolContext

from tests.helpers import PLAN, TempDirs, j

NAME = "inputs/daily captial.xlsx"
QUOTED = '"inputs/daily captial.xlsx"'


class FileNames(TempDirs):
    def test_quote_paths(self):
        self.assertEqual(quote_paths([NAME, "inputs/a.csv"]), f'{QUOTED}, "inputs/a.csv"')
        self.assertEqual(quote_paths([]), "")

    def test_executor_sees_quoted_names_and_the_copy_exactly_rule(self):
        system, intro = first_prompt(new_state("what is in this file", inputs=[NAME]))[:2]
        self.assertIn(f"INPUT FILES: {QUOTED}", intro["content"])
        self.assertIn("character for character", system["content"])

    def test_planner_sees_quoted_names(self):
        model = FakeModel({"planner": [PLAN]})
        plan(new_state("what is in this file", inputs=[NAME]), model)
        self.assertIn(QUOTED, model.calls[0]["messages"][0]["content"])

    def test_request_check_sees_the_input_files_and_accepts_spaces(self):
        model = FakeModel({"action_review": [j(ok=True, issues=[], explain="Reads the file.")]})
        review_request(model, new_state("what is in this file", inputs=[NAME]), "read_file", {"path": NAME})
        prompt = model.calls[0]["messages"][0]["content"]
        self.assertIn(f"INPUT FILES (the user's files, exact names):\n{QUOTED}", prompt)
        self.assertIn("valid exactly as written", prompt)

    def test_not_found_lists_quoted_names_without_hidden_files(self):
        (config.inputs_dir() / "daily captial.xlsx").write_bytes(b"x")
        (config.inputs_dir() / ".DS_Store").write_bytes(b"x")
        d = config.RUNS_DIR / "r1"
        d.mkdir(parents=True, exist_ok=True)
        obs = dispatch("read_file", {"path": "inputs/daily_captial.xlsx"}, ToolContext("r1", d, ()), "r")
        self.assertFalse(obs["ok"])
        self.assertIn(QUOTED, obs["error"])
        self.assertNotIn(".DS_Store", obs["error"])
