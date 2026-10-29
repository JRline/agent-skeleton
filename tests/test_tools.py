import json
import unittest

from agent import config
from agent.tools import ToolContext, dispatch

from tests.helpers import STD, TempDirs


class AnalysisTools(TempDirs):
    def ctx(self, context=()):
        d = config.RUNS_DIR / "r1"
        d.mkdir(parents=True, exist_ok=True)
        return ToolContext("r1", d, tuple(context))

    def run_tool(self, name, args, ctx=None):
        obs = dispatch(name, args, ctx or self.ctx(), f"{name}-1")
        self.assertTrue(obs["ok"], obs.get("error"))
        return obs["result"]

    RWA = f"scenario = 'USED' AND resource = 'RWA' AND {STD}"

    def test_list_and_describe_datasets(self):
        listing = self.run_tool("list_datasets", {})
        self.assertIn("usage_limits: Daily RWA and leverage exposure", listing)
        self.assertIn("partition_key", listing)
        self.assertIn("Required filter", listing)
        self.assertIn("Fiscal years run from 1 April to 31 March", listing)     # general notes are listed too
        self.assertLess(len(listing), 2500)                                      # fits the observation preview
        out = self.run_tool("describe_dataset", {"dataset": "usage_limits"})
        self.assertIn("Each resource and version has its own latest date", out)
        self.assertIn("partitions (partition_key): FY2025", out)
        self.assertIn("component", out)
        self.assertNotIn("2025-04-01,FY2025", out)                               # no data rows

    def test_sql_query_saves_a_work_table(self):
        out = self.run_tool("sql_query", {"sql": f"SELECT as_of_date, SUM(amount) AS rwa FROM usage_limits "
                                                 f"WHERE fiscal_quarter = 'FY2025Q4' AND {self.RWA} GROUP BY as_of_date",
                                          "save_as": "rwa_q4"})
        self.assertIn("rows=55", out)
        self.assertIn("work/r1/rwa_q4.csv", out)
        self.assertTrue((config.work_dir("r1") / "rwa_q4.csv").exists())

    def test_sql_query_refuses_writes(self):
        obs = dispatch("sql_query", {"sql": "DROP TABLE usage_limits", "save_as": "x"}, self.ctx(), "s")
        self.assertFalse(obs["ok"])
        self.assertIn("Only SELECT", obs["error"])

    def test_sql_extracts_then_python_analyses(self):
        self.run_tool("sql_query", {"sql": f"SELECT as_of_date, division AS division, SUM(amount) AS rwa "
                                           f"FROM usage_limits WHERE as_of_date IN ('2025-12-30', '2026-03-26') AND {self.RWA} "
                                           f"GROUP BY as_of_date, division", "save_as": "base"})
        code = ("p = tables['base'].pivot(index='division', columns='as_of_date', values='rwa').reset_index()\n"
                "p['change'] = p['2026-03-26'] - p['2025-12-30']\n"
                "result = p")
        out = json.loads(self.run_tool("run_python", {"code": code, "tables": {"base": "work/r1/base.csv"},
                                                      "save_as": "change"}))
        self.assertEqual(out["saved_as"], "work/r1/change.csv")
        import pandas as pd
        df = pd.read_csv(config.work_dir("r1") / "change.csv")
        row = df[df.division == "Global Markets"].iloc[0]
        self.assertEqual((row["2025-12-30"], row["2026-03-26"]), (69538, 69977))

    def test_run_python_rejects_unsafe_code(self):
        obs = dispatch("run_python", {"code": "import os\nresult = 1"}, self.ctx(), "p")
        self.assertFalse(obs["ok"])
        self.assertIn("import", obs["error"])

    def test_save_table_needs_approval_and_writes_outputs(self):
        from agent.tools import TOOLS
        self.assertTrue(TOOLS["save_table"].risky)
        self.run_tool("save_table", {"path": "inputs/sample_capital.csv", "name": "capital.csv"})
        self.assertTrue((config.outputs_dir("r1") / "capital.csv").exists())

    def test_search_docs_returns_matching_paragraphs(self):
        config.context_dir().mkdir(parents=True, exist_ok=True)
        (config.context_dir() / "calendar.md").write_text("# Calendar\n\nFiscal years run from 1 April to 31 March.\n")
        ctx = self.ctx(["context/calendar.md"])
        out = self.run_tool("search_docs", {"query": "fiscal years april"}, ctx)
        self.assertIn("calendar.md", out)
        self.assertIn("Fiscal years run from 1 April to 31 March", out)


class Dispatch(TempDirs):
    def ctx(self):
        d = config.RUNS_DIR / "r1"
        d.mkdir(parents=True, exist_ok=True)
        return ToolContext("r1", d)

    def test_read_csv_preview_and_full_output_stored(self):
        ctx = self.ctx()
        obs = dispatch("read_file", {"path": "inputs/sample_capital.csv"}, ctx, "read_file-1")
        self.assertTrue(obs["ok"])
        self.assertIn("shape=(3, 4)", obs["result"])
        self.assertTrue((ctx.run_dir / "outputs" / "read_file-1.txt").exists())

    def test_sandbox_prefix_accepted(self):
        obs = dispatch("read_file", {"path": "sandbox/inputs/sample_capital.csv"}, self.ctx(), "r")
        self.assertTrue(obs["ok"])

    def test_path_escape_rejected(self):
        for path in ["../../etc/passwd", "/etc/passwd", "inputs/../../x"]:
            obs = dispatch("read_file", {"path": path}, self.ctx(), "r")
            self.assertFalse(obs["ok"], path)

    def test_missing_file_is_an_observation_not_a_crash(self):
        obs = dispatch("read_file", {"path": "inputs/nope.csv"}, self.ctx(), "r")
        self.assertFalse(obs["ok"])
        self.assertIn("not found", obs["error"])
        self.assertIn("inputs/sample_capital.csv", obs["error"])     # lists what does exist

    def test_write_file_goes_to_sandbox_outputs(self):
        obs = dispatch("write_file", {"name": "a.md", "content": "hi"}, self.ctx(), "w")
        self.assertTrue(obs["ok"])
        self.assertEqual((config.outputs_dir("r1") / "a.md").read_text(), "hi")

    def test_write_file_rejects_folders(self):
        for name in ["../a.md", "x/a.md", ".hidden"]:
            obs = dispatch("write_file", {"name": name, "content": "hi"}, self.ctx(), "w")
            self.assertFalse(obs["ok"], name)

    def test_recall_finds_keywords(self):
        obs = dispatch("recall", {"query": "2026Q2"}, self.ctx(), "r")
        self.assertIn("2026Q2", obs["result"])

    def test_long_output_truncated(self):
        (config.inputs_dir() / "big.txt").write_text("x" * 10000)
        obs = dispatch("read_file", {"path": "inputs/big.txt"}, self.ctx(), "r")
        self.assertLess(len(obs["result"]), config.OBS_PREVIEW_CHARS + 100)

    def test_ask_user_is_not_dispatchable(self):
        obs = dispatch("ask_user", {"question": "?"}, self.ctx(), "a")
        self.assertFalse(obs["ok"])


if __name__ == "__main__":
    unittest.main()


class StepSummary(TempDirs):
    def test_query_and_file_results_are_described_not_dumped(self):
        from agent.tools import step_summary
        obs = {"ok": True, "result": "rows=9; columns=['risk_type', 'amount_jpy_m']; saved as work/r1/x.csv\nrisk_type,amount\n"}
        self.assertEqual(step_summary(obs), "9 rows × 2 columns, saved as work/r1/x.csv")
        obs = {"ok": True, "result": "shape=(500, 4); columns=['a', 'b', 'c', 'd']\na,b,c,d\n"}
        self.assertEqual(step_summary(obs), "500 rows × 4 columns")
        obs = {"ok": True, "result": "rows=2; columns=['a']; saved as work/r1/t.csv (reused: the same query already ran)"}
        self.assertIn("(reused, not queried again)", step_summary(obs))


class WorkTables(TempDirs):
    def test_a_later_table_never_overwrites_an_earlier_one(self):
        import pandas as pd
        from agent.tools import _save_work
        ctx = ToolContext("r1", config.RUNS_DIR / "r1")
        first = _save_work(pd.DataFrame({"a": [1]}), ctx, "analysis")
        second = _save_work(pd.DataFrame({"a": [2]}), ctx, "analysis")
        self.assertEqual((first, second), ("work/r1/analysis.csv", "work/r1/analysis_2.csv"))
        self.assertEqual(pd.read_csv(config.SANDBOX_DIR / first).a[0], 1)
