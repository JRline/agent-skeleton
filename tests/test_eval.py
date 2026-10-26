"""The golden-task runner, offline: a scripted FakeModel answers the tasks."""
import json

import eval as evaluation
from agent.backends.fake import FakeModel

from tests.helpers import OK, PLAN, TempDirs, final, tool_call

READ = tool_call("read_file", step_done=True, path="inputs/sample_capital.csv")
TASK = {"id": "cet1_capital", "instruction": "What was CET1 capital in 2026Q3 in inputs/sample_capital.csv?",
        "inputs": ["inputs/sample_capital.csv"], "expect_numbers": [{"value": 12850, "tol": 0.5}],
        "expect_keywords": ["CET1"], "max_steps": 5}


def model(answer):
    return FakeModel({"planner": [PLAN], "executor": [READ, final(answer)], "reviewer": [OK]})


class Runner(TempDirs):
    def test_a_correct_answer_scores_full_marks_with_metrics(self):
        result = evaluation.run_task(TASK, model("CET1 capital was 12,850 in 2026Q3 [ref: read_file-1]"))
        self.assertEqual(result["score"], 1.0)
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(result["model_calls"], 5)     # planner, 2 executor, request review (file read), reviewer
        self.assertIn("seconds", result)

    def test_a_wrong_answer_loses_the_number_check(self):
        result = evaluation.run_task(TASK, model("CET1 capital is in the file [ref: read_file-1]"))
        self.assertLess(result["score"], 1.0)
        self.assertEqual(result["checks"]["number 12850"], False)

    def test_tool_budget_check(self):
        task = {**TASK, "max_tool_calls": 0}
        result = evaluation.run_task(task, model("CET1 capital was 12850 [ref: read_file-1]"))
        self.assertFalse(result["checks"]["tool calls <= 0"])

    def test_suite_writes_results_and_compares_with_the_previous_run(self):
        out = evaluation.config.RUNS_DIR / "evals"
        first = evaluation.run_suite([TASK], lambda: model("CET1 capital was 12850 [ref: read_file-1]"),
                                     backend="fake", out_dir=out)
        second = evaluation.run_suite([TASK], lambda: model("No figure here."), backend="fake", out_dir=out)
        self.assertEqual(first["score"], 1.0)
        files = sorted(out.glob("*.json"))
        self.assertEqual(len(files), 2)
        self.assertEqual(json.loads(files[-1].read_text())["score"], second["score"])
        report = (out / files[-1].name.replace(".json", ".md")).read_text()
        self.assertIn("cet1_capital", report)
        self.assertIn("previous", report.lower())

    def test_golden_tasks_are_valid(self):
        tasks = evaluation.load_tasks(evaluation.GOLDEN_DIR)
        self.assertGreaterEqual(len(tasks), 8)
        for t in tasks:
            self.assertTrue(t["instruction"] and (t.get("expect_numbers") or t.get("expect_keywords")
                                                  or t.get("expect_ask")), t["id"])


class KeywordAlternatives(TempDirs):
    def test_a_keyword_list_accepts_any_of_its_words(self):
        from eval import keyword_checks
        task = {"expect_keywords": [["Equity Derivatives", "GM-EQ-DER"], "Hong Kong"]}
        self.assertEqual(keyword_checks(task, "GM-EQ-DER in Hong Kong"),
                         {"mentions 'Equity Derivatives or GM-EQ-DER'": True, "mentions 'Hong Kong'": True})
        self.assertFalse(all(keyword_checks(task, "FX Options in Hong Kong").values()))


class ShownAnswer(TempDirs):
    def test_tables_shown_under_the_answer_count(self):
        from agent.memory import new_state
        state = new_state("g")
        cfg = evaluation.config
        cfg.work_dir(state["run_id"]).mkdir(parents=True, exist_ok=True)
        (cfg.work_dir(state["run_id"]) / "avg.csv").write_text("division,avg\nTreasury & ALM,6894.83\n")
        state["final"] = "CIB has the highest average, 120,362.59."
        state["events"].append({"kind": "final", "payload": {"text": state["final"],
                                                              "tables": [f"work/{state['run_id']}/avg.csv"]}})
        shown = evaluation.shown_answer(state)
        self.assertTrue(evaluation.found_number(shown, 6894.83, 1))
        self.assertTrue(evaluation.found_number(shown, 120362.59, 1))


class AskChecks(TempDirs):
    def test_expect_ask_and_max_asks(self):
        from agent.backends.fake import FakeModel
        from tests.helpers import OK, PLAN, final, tool_call
        ask = tool_call("ask_user", question="I plan to use RWA, USED, CLOSED, FY2025 by month. Confirm or correct.")
        m = FakeModel({"planner": [PLAN], "executor": [ask, final("Confirmed scope.")], "reviewer": [OK],
                       "summariser": ["- s"] * 3})
        result = evaluation.run_task({"id": "v", "instruction": "How has RWA moved?", "expect_ask": True,
                                      "max_asks": 1}, m)
        self.assertEqual(result["checks"]["asked the user to confirm"], True)
        self.assertEqual(result["checks"]["questions <= 1"], True)
