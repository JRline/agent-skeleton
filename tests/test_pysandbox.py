"""Model-written analysis code: static check, sandboxed run, pre-agreed output format."""
import json
from unittest import mock

import pandas as pd

from agent import config
from agent.backends.fake import FakeModel
from agent.loop import resolve_pending, run_one_step
from agent.memory import new_state
from agent.pysandbox import check_code, run_code

from tests.helpers import TempDirs, j

GOOD = """
df = tables["cap"]
df["cet1_ratio_pct"] = (df["cet1_capital"] / df["rwa"] * 100).round(2)
result = df[["period", "cet1_ratio_pct"]]
notes = "CET1 ratio per period"
print("rows", len(df))
"""


class StaticCheck(TempDirs):
    def test_normal_pandas_code_passes(self):
        self.assertIsNone(check_code(GOOD))

    def test_forbidden_patterns_are_rejected(self):
        for code in ["import os", "from pathlib import Path", "import subprocess", "import pandas.io.sql",
                     "from numpy.lib import format", "import pickle", "open('x', 'w')", "exec('1')", "eval('1')",
                     "x = df.__class__", "__import__('os')", "getattr(pd, 'read_csv')", "pd.read_sql('x', c)",
                     "pd.read_pickle('p')", "tables['a'].to_csv('out.csv')", "tables['a'].to_excel('o.xlsx')",
                     "np.save('x', 1)", "tables['a'].to_pickle('p')", "compile('1', 'x', 'exec')"]:
            self.assertIsNotNone(check_code(code), code)

    def test_standard_analysis_packages_and_reads_are_allowed(self):
        for code in ["import pandas as pd", "import numpy as np", "from datetime import date",
                     "import math, statistics", "from collections import defaultdict", "import re",
                     "from decimal import Decimal", "df = pd.read_csv('inputs/a.csv')",
                     "df = pd.read_excel('inputs/a.xlsx')"]:
            self.assertIsNone(check_code(code), code)

    def test_harmless_to_methods_are_allowed(self):
        self.assertIsNone(check_code("result = tables['a'].to_dict('records')"))
        self.assertIsNone(check_code("result = pd.to_datetime(tables['a']['d']).dt.year.to_list()"))

    def test_pandas_rename_is_allowed_but_file_renames_are_not(self):
        self.assertIsNone(check_code("result = tables['a'].rename(columns={'x': 'y'})"))
        for code in ["import os\nos.rename('a', 'b')", "from pathlib import Path\nPath('a').rename('b')",
                     "open('a').rename('b')", "import shutil\nshutil.move('a', 'b')"]:
            self.assertIsNotNone(check_code(code), code)

    def test_guidance_says_never_type_data_into_code(self):
        from agent.toolkits.analysis import TOOLKIT
        self.assertIn("Never type data values into the code", TOOLKIT["guidance"])

    def test_size_limit(self):
        self.assertIsNotNone(check_code("x = 1\n" * 5000))


class Run(TempDirs):
    def run_ok(self, code, tables=None):
        out = run_code(code, tables or {"cap": "inputs/sample_capital.csv"}, run_id="r1", save_as="out")
        self.assertTrue(out["ok"], out)
        return out

    def test_table_result_in_agreed_format_and_saved_by_the_harness(self):
        out = self.run_ok(GOOD)
        self.assertEqual(out["result"]["type"], "table")
        self.assertEqual(out["result"]["columns"], ["period", "cet1_ratio_pct"])
        self.assertEqual(out["result"]["rows"][-1], ["2026Q3", 12.66])
        self.assertEqual(out["notes"], "CET1 ratio per period")
        self.assertIn("rows 3", out["stdout"])
        self.assertEqual(out["saved_as"], "work/r1/out.csv")
        self.assertTrue((config.work_dir("r1") / "out.csv").exists())

    def test_rename_runs(self):
        out = self.run_ok("result = tables['cap'].rename(columns={'period': 'quarter'})")
        self.assertIn("quarter", out["result"]["columns"])

    def test_value_result(self):
        out = self.run_ok("result = float(tables['cap']['rwa'].sum())")
        self.assertEqual(out["result"], {"type": "value", "value": 296000.0})
        self.assertNotIn("saved_as", out)

    def test_reads_work_inside_the_sandbox_only(self):
        self.run_ok("result = read_table('inputs/sample_capital.csv').shape[0]", tables={})
        out = self.run_ok("import pandas as pd\nresult = pd.read_csv('inputs/sample_capital.csv')", tables={})
        self.assertEqual(len(out["result"]["rows"]), 3)
        for reader in ("read_table", "pd.read_csv"):
            for path in [str(config.datalake_dir() / "usage_limits.csv"), "../mock_lake/data/usage_limits.csv",
                         "../outside.csv", "/etc/passwd",
                         "https://example.com/x.csv"]:
                out = run_code(f"result = {reader}('{path}')", {}, run_id="r1", save_as="x")
                self.assertFalse(out["ok"], (reader, path))

    def test_excel_workbooks_open_by_sandbox_path_with_or_without_prefix(self):
        """A real run bounced between 'inputs/x.xlsx' and 'sandbox/inputs/x.xlsx': pd.ExcelFile was unguarded,
        so it looked in the empty temp folder and both failed."""
        pd.DataFrame({"a": [1, 2]}).to_excel(config.inputs_dir() / "daily capital.xlsx", index=False)
        for path in ("inputs/daily capital.xlsx", "sandbox/inputs/daily capital.xlsx"):
            out = self.run_ok(f"import pandas as pd\nxl = pd.ExcelFile('{path}')\nresult = xl.sheet_names", tables={})
            self.assertEqual(out["result"]["value"], ["Sheet1"])
        for path in (str(config.datalake_dir() / "usage_limits.csv"), "/etc/passwd", "../x.xlsx"):
            self.assertFalse(run_code(f"result = pd.ExcelFile('{path}')", {}, run_id="r1", save_as="x")["ok"], path)

    def test_a_missing_file_says_how_paths_work(self):
        out = run_code("result = pd.read_csv('nope.csv')", {}, run_id="r1", save_as="x")
        self.assertFalse(out["ok"])
        self.assertIn("inputs/<file name>", out["error"])

    def test_code_with_escaped_newlines_is_repaired(self):
        out = self.run_ok("df = tables['cap']\\nresult = len(df)")       # literal backslash-n from a small model
        self.assertEqual(out["result"]["value"], 3)

    def test_allowed_imports_work_at_runtime_and_others_do_not(self):
        out = self.run_ok("import numpy as np\nfrom collections import Counter\n"
                          "result = int(np.sum(list(Counter('aab').values())))", tables={})
        self.assertEqual(out["result"]["value"], 3)
        with mock.patch("agent.pysandbox.check_code", return_value=None):
            out = run_code("import os\nresult = 1", {}, run_id="r1", save_as="x")
        self.assertFalse(out["ok"])
        self.assertIn("ImportError", out["error"])

    def test_runtime_errors_and_timeouts_are_reported(self):
        out = run_code("result = 1 / 0", {}, run_id="r1", save_as="x")
        self.assertFalse(out["ok"])
        self.assertIn("ZeroDivisionError", out["error"])
        saved, config.PY_TIMEOUT_S = config.PY_TIMEOUT_S, 2
        try:
            out = run_code("while True:\n    pass", {}, run_id="r1", save_as="x")
        finally:
            config.PY_TIMEOUT_S = saved
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["error"])

    def test_no_file_access_even_if_the_check_were_bypassed(self):
        with mock.patch("agent.pysandbox.check_code", return_value=None):      # second layer on its own
            for code in ["open('evil.txt', 'w').write('x')", "__import__('os').remove('x')",
                         "result = eval('1+1')"]:
                out = run_code(code, {}, run_id="r1", save_as="x")
                self.assertFalse(out["ok"], code)
                self.assertRegex(out["error"], "NameError|ImportError", code)

    def test_missing_result(self):
        out = run_code("x = 1", {}, run_id="r1", save_as="x")
        self.assertFalse(out["ok"])
        self.assertIn("result", out["error"])

    def test_printed_output_is_used_when_no_result_is_set(self):
        out = self.run_ok("df = tables['cap']\nprint('mean rwa', df['rwa'].mean())")
        self.assertEqual(out["result"], {"type": "value", "value": "mean rwa 98666.66666666667"})
        self.assertIn("printed output", out["notes"])


class InTheLoop(TempDirs):
    def running(self, code):
        state = new_state("analyse")
        state["status"], state["plan"] = "running", ["analyse"]
        action = j(thought="analyse", tool="run_python",
                   input={"code": code, "tables": {"cap": "inputs/sample_capital.csv"}, "save_as": "ratios"})
        return state, FakeModel({"executor": [action]})

    def test_rejected_code_never_reaches_approval(self):
        state, m = self.running("import os\nresult = 1")
        run_one_step(state, m)
        self.assertEqual(state["status"], "running")
        ev = state["events"][-1]
        self.assertEqual(ev["kind"], "precheck")                    # sent back before the user is asked
        self.assertIn("import", ev["payload"]["issues"][0])
        self.assertIn("not sent; fix it first", state["turns"][-1]["content"])

    def test_good_code_waits_for_approval_then_runs(self):
        state, m = self.running(GOOD)
        run_one_step(state, m)
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertIn("cet1_ratio_pct", state["pending"]["input"]["code"])
        resolve_pending(state, approved=True)
        result = json.loads(state["turns"][-1]["content"].split("OBSERVATION: ", 1)[1])["result"]
        self.assertIn('"type": "table"', result)
        self.assertIn("work/", result)
