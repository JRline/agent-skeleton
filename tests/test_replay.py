"""The exported analysis script: built from what ran, replays against the live lake, flags changed data."""
import os
import re
import subprocess
import sys
import threading

import pandas as pd

from agent import config, replay
from agent.backends.fake import FakeModel
from agent.loop import run
from scripts import mock_lake_server as server

from tests.helpers import STD, OK, PLAN, TempDirs, j

SQL = ("SELECT division AS book, as_of_date AS as_of_date, SUM(amount) AS rwa FROM usage_limits "
       f"WHERE as_of_date IN ('2025-12-30', '2026-03-26') AND scenario = 'USED' AND resource = 'RWA' AND {STD} "
       "GROUP BY division, as_of_date")
CODE = ("df = tables['two']\n"
        "p = df.pivot_table(index='book', columns='as_of_date', values='rwa', aggfunc='sum').reset_index()\n"
        "p.columns = ['book', 'before', 'after']\n"
        "p['change'] = p['after'] - p['before']\n"
        "result = p\nnotes = 'change by book'")


def _last(messages, pattern):
    return re.findall(pattern, "\n".join(m["content"] for m in messages))[-1]


def analysis_conversation():
    """sql_query → a failing run_python → the approved one → an answer citing it."""
    python = lambda msgs: j(thought="t", tool="run_python", input={       # noqa: E731
        "code": CODE, "tables": {"two": _last(msgs, r"saved as (work/\S+?\.csv)")}, "save_as": "by_book"})
    answer = lambda msgs: j(thought="t", final=(                         # noqa: E731
        f"On 2026-03-26, Global Markets RWA rose by 439 to 69,977 [ref: {_last(msgs, r'run_python-[0-9]+')}] "
        "in FY2025Q4, one of the 4 divisions."))
    return FakeModel({
        "planner": [PLAN],
        "executor": [j(thought="t", tool="sql_query", input={"sql": SQL, "save_as": "two"}),
                     j(thought="t", tool="run_python", input={"code": "result = missing_name"}),
                     python, answer],
        "reviewer": [OK], "summariser": ["- s"] * 5})


class Replay(TempDirs):
    def setUp(self):
        super().setUp()
        self.state = run("RWA change by book", analysis_conversation(), approve=lambda e: True,
                         toolkits=["files", "analysis", "datalake"])
        self.assertEqual(self.state["status"], "finished")

    def serve_lake(self):
        from agent import connectors
        srv = server.serve(0, connectors.enabled()["sample_lake"].path("data"), quiet=True)   # follows copy_lake
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def run_script(self, path, url):
        env = {**os.environ, "ANALYSIS_LAKE": "mock", "MOCK_LAKE_URL": url,
               "ANALYSIS_INPUTS": str(config.inputs_dir())}
        return subprocess.run([sys.executable, str(path)], capture_output=True, text=True, env=env, timeout=60)

    def test_script_has_the_exact_steps_in_cells(self):
        text = replay.build_script(self.state)
        compile(text, "analysis.py", "exec")
        from agent.sqlfmt import format_sql
        self.assertIn(format_sql(SQL), text)                                       # the query, one clause per line
        self.assertIn("\n    p['change'] = p['after'] - p['before']\n", text)    # approved code, in its step guard
        self.assertIn("two = run_sql(SQL, 'sample_lake')", text)                    # named after its saved table
        self.assertLess(text.index("two = run_sql(SQL"), text.index("# %% Support code"))    # analysis first
        self.assertIn("# %% Not replayed: run_python attempt", text)               # the failed attempt
        self.assertIn("#     result = missing_name", text.replace("# result", "#     result"))
        self.assertLess(text.index("# %% Step 1 · data extraction"), text.index("# %% Step 2 · analysis"))
        self.assertIn("# %% Checks", text)
        self.assertEqual(text.count("compare("), 2)                    # defined in support, used only in Checks
        self.assertGreater(text.index("compare(exp, R[ref])"), text.index("# %% Checks"))

    def test_replay_matches_and_rebuilds_the_answer(self):
        out = self.run_script(replay.export(self.state), self.serve_lake())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertRegex(out.stdout, r"sql_query-\d+\s+MATCH")
        self.assertRegex(out.stdout, r"run_python-\d+\s+MATCH")
        self.assertIn("rose by 439 to 69,977", out.stdout)
        self.assertIn("On 2026-03-26", out.stdout)                                 # dates are kept as written

    def test_changed_data_is_flagged_step_by_step_and_in_the_answer(self):
        path = replay.export(self.state)
        lake = self.copy_lake() / "usage_limits.csv"                                   # a private copy to change
        df = pd.read_csv(lake)
        df.loc[(df.as_of_date == "2026-03-26") & (df.scenario == "USED") & (df.version == "CLOSED")
               & (df.method == "BOOKED") & (df.component_code == "MKT_INT")
               & (df.product_id == "GM-RT-IRS") & (df.location == "Tokyo"), "amount"] += 100   # restated
        df.to_csv(lake, index=False)
        out = self.run_script(path, self.serve_lake())
        self.assertEqual(out.returncode, 1)
        self.assertRegex(out.stdout, r"sql_query-\d+\s+DIFF: sum\(rwa\)")
        self.assertIn("2 step(s) differ", out.stdout)
        self.assertIn("rose by 539 [was 439] to 70,077 [was 69,977]", out.stdout)

    def test_a_step_that_fails_on_changed_data_is_reported_and_the_script_goes_on(self):
        """E.g. a new limit breach the approved code was not written for: the steps run on, the Checks cell says
        which step failed, and the answer marks the numbers that came from it."""
        path = replay.export(self.state)
        lake = self.copy_lake() / "usage_limits.csv"
        df = pd.read_csv(lake)
        df[df.division != "Global Markets"].to_csv(lake, index=False)          # the row the code expects is gone
        text = path.read_text()
        path.write_text(text.replace("p.columns = ['book', 'before', 'after']",
                                     "p.columns = ['book', 'before', 'after']\n    p.loc[p.book == 'Global Markets', 'before'].iloc[0]"))
        out = self.run_script(path, self.serve_lake())
        self.assertEqual(out.returncode, 1)
        self.assertIn("IndexError", out.stderr)                                    # the traceback, where it ran
        self.assertRegex(out.stdout, r"sql_query-\d+\s+DIFF")                       # earlier steps still checked
        self.assertRegex(out.stdout, r"run_python-\d+\s+FAILED: IndexError")
        self.assertIn("[step run_python-", out.stdout)                             # the answer is still rebuilt


class BindNumbers(TempDirs):
    def test_numbers_bind_to_named_rows_and_the_rest_stay_literal(self):
        table = pd.DataFrame({"book": ["SME", "Total"], "change": [-20.0, 1300.0], "pct": [7.3612, 9.99]})
        text = "Total rose by +1,300 (7.36%), SME fell by 20 [ref: run_python-1] in 2026Q3; 5 books; user said 250."
        template, bindings, literal = replay.bind_numbers(text, {"run_python-1": table})
        self.assertEqual(template, "Total rose by {n1} ({n2}%), SME fell by {n3} [ref: run_python-1] in 2026Q3; "
                                   "5 books; user said 250.")
        self.assertEqual(bindings[0]["key"], {"book": "Total"})
        self.assertTrue(bindings[0]["plus"])
        self.assertEqual((bindings[1]["col"], bindings[1]["decimals"]), ("pct", 2))
        self.assertTrue(bindings[2]["abs"])                                         # "fell by 20" is -20
        self.assertEqual(literal, ["250"])

    def test_a_row_with_an_empty_text_cell_is_found_again(self):
        """The agent's result came from JSON (None), the replay's from pandas (NaN): both are the same empty cell."""
        seen = pd.DataFrame({"book": ["SME", "Loans"], "binding": [None, "DAILY"], "limit": [550.0, 1120.0]})
        _, bindings, _ = replay.bind_numbers("SME limit 550 [ref: run_python-1]", {"run_python-1": seen})
        self.assertEqual(bindings[0]["key"], {"book": "SME", "binding": ""})
        script = {"R": {"run_python-1": pd.DataFrame({"book": ["SME", "Loans"], "binding": [float("nan"), "DAILY"],
                                                      "limit": [550.0, 1120.0]})}, "FAILED": {}, "pd": pd,
                  "np": __import__("numpy")}
        exec(replay.RUNTIME + replay._FILL, script)
        self.assertEqual(script["cell"](bindings[0]), 550.0)

    def test_totals_in_the_notes_are_traced_by_position(self):
        """Code often prints a total in its notes rather than the result table: the replay recomputes it too."""
        table = pd.DataFrame({"division": ["A", "B"], "rwa": [100.0, 200.0]})
        notes = {"run_python-1": "Totals: before 250 after 300 (+20.00%)"}
        text = "Total RWA 300, up 20.00% [ref: run_python-1]; saved in work/20261029-093623-a720/t.csv."
        template, bindings, literal = replay.bind_numbers(text, {"run_python-1": table}, notes)
        self.assertEqual([b.get("note") for b in bindings], [1, 2])
        self.assertEqual(literal, [])                                         # the run id is not a number
        script = {"R": {}, "FAILED": {}, "NOTES": {"run_python-1": "Totals: before 260 after 312 (+20.00%)"}, "pd": pd,
                  "np": __import__("numpy")}
        exec(replay.RUNTIME + replay._FILL, script)
        self.assertEqual(script["fill"](template, bindings).split(";")[0],
                         "Total RWA 312 [was 300], up 20.00% [ref: run_python-1]")

    def test_braces_in_answers_survive_the_template(self):
        template, _, _ = replay.bind_numbers("Use {x} as is.", {})
        self.assertEqual(template.format(), "Use {x} as is.")


class Export(TempDirs):
    def test_cli_flag_writes_the_script(self):
        state = run("RWA change by book", analysis_conversation(), approve=lambda e: True,
                    toolkits=["files", "analysis", "datalake"])
        root = config.RUNS_DIR.parent                     # the temp folder holding runs/ and sandbox/
        cli = config.ROOT / "cli.py"
        out = subprocess.run([sys.executable, str(cli), "--export-script", state["run_id"]], capture_output=True,
                             text=True, env={**os.environ, "AGENT_HOME": str(root)}, cwd=config.ROOT, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        path = config.outputs_dir(state["run_id"]) / f"analysis_{state['run_id']}.py"
        self.assertEqual(out.stdout.strip(), str(path))
        self.assertTrue(path.read_text().startswith('"""Replay of the analysis'))


class FileOnlyConversation(TempDirs):
    def test_reading_the_users_file_is_a_replayable_step(self):
        from agent.backends.fake import FakeModel
        from tests.helpers import tool_call
        m = FakeModel({"planner": [PLAN], "executor": [tool_call("read_file", path="inputs/sample_capital.csv"),
                                                       j(thought="t", final="It has 3 periods.")],
                       "reviewer": [OK], "summariser": ["- s"] * 3})
        state = run("what is in this file", m, approve=lambda e: True)
        text = replay.build_script(state)
        self.assertIn("sample_capital = read_table('inputs/sample_capital.csv')", text)
        self.assertTrue(replay.has_steps(state))
        self.assertNotIn("Nothing to replay", text)


class ExportReplay(TempDirs):
    """An export is a replayable step: the script re-runs its SQL and checks the rows against the file."""
    def test_export_replays_against_a_signed_in_server(self):
        sql = f"SELECT location, amount FROM usage_limits WHERE scenario = 'USED' AND resource = 'RWA' AND {STD}"
        m = FakeModel({"planner": [PLAN], "reviewer": [OK], "summariser": ["- s"] * 5,
                       "executor": [j(thought="t", tool="export_query", input={"sql": sql, "name": "rows.csv"}),
                                    j(thought="t", final="The export is ready [ref: export_query-1].")]})
        state = run("Export RWA rows", m, approve=lambda e: True, toolkits=["datalake"])
        text = replay.build_script(state)
        self.assertIn("data export from sample_lake to a file for the user (export_query-1)", text)
        from agent import connectors
        srv = server.serve(0, connectors.enabled()["sample_lake"].path("data"), quiet=True,
                           users={"analyst": "test-pass"})
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        env = {**os.environ, "ANALYSIS_LAKE": "mock", "MOCK_LAKE_URL": f"http://127.0.0.1:{srv.server_address[1]}",
               "ANALYSIS_USER": "analyst", "ANALYSIS_INPUTS": str(config.inputs_dir())}
        out = subprocess.run([sys.executable, str(replay.export(state))], input="test-pass\n", capture_output=True,
                             text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertRegex(out.stdout, r"export_query-1\s+MATCH")


class TwoConnectors(TempDirs):
    """Each query goes back to the connector it ran on, so a conversation over two data sources replays."""
    def setUp(self):
        super().setUp()
        import shutil
        from pathlib import Path
        ext = Path(self._tmp) / "extensions"
        shutil.copytree(config.connectors_dir() / "sample_lake", ext / "connectors" / "sample_lake",
                        ignore=shutil.ignore_patterns("data"))
        fx = ext / "connectors" / "fx_lake"
        (fx / "datasets").mkdir(parents=True)
        (fx / "data").mkdir()
        (fx / "connector.py").write_text('NAME = "fx_lake"\nKIND = "sqlite"\nDESCRIPTION = "Synthetic FX rates."\n'
                                        'DATA = "data"\nABOUT = "All data is synthetic."\n')
        (fx / "datasets" / "fx_rates.md").write_text("---\nname: fx_rates\ndescription: Synthetic daily FX rates.\n"
                                                    "---\nColumns: as_of_date, currency, rate.\n")
        pd.DataFrame({"as_of_date": ["2026-03-26"] * 2, "currency": ["USD", "EUR"],
                      "rate": [151.25, 163.5]}).to_csv(fx / "data" / "fx_rates.csv", index=False)
        self._saved_ext = config.EXTENSIONS_DIR
        config.EXTENSIONS_DIR = ext
        from tests.helpers import LAKE
        from agent import connectors
        connectors.OVERRIDES["sample_lake"] = {"data": str(LAKE)}
        self.fx_data = fx / "data"

    def tearDown(self):
        config.EXTENSIONS_DIR = self._saved_ext
        super().tearDown()

    def test_each_query_replays_on_its_own_source(self):
        fx_sql = "SELECT currency, rate FROM fx_rates WHERE as_of_date = '2026-03-26'"
        m = FakeModel({"planner": [PLAN], "reviewer": [OK], "summariser": ["- s"] * 5,
                       "executor": [j(thought="t", tool="sql_query", input={"sql": SQL, "save_as": "two"}),
                                    j(thought="t", tool="sql_query", input={"sql": fx_sql, "save_as": "fx"}),
                                    j(thought="t", final="USD is 151.25 [ref: sql_query-2].")]})
        state = run("RWA and FX", m, approve=lambda e: True, toolkits=["datalake"])
        self.assertEqual(state["status"], "finished")
        text = replay.build_script(state)
        self.assertIn("two = run_sql(SQL, 'sample_lake')", text)
        self.assertIn("fx = run_sql(SQL, 'fx_lake')", text)
        self.assertIn("'fx_lake': {'kind': 'mock'", text)
        from agent import connectors
        urls = {}
        for name, data in (("SAMPLE_LAKE", connectors.enabled()["sample_lake"].path("data")), ("FX_LAKE", self.fx_data)):
            srv = server.serve(0, data, quiet=True)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            urls[f"ANALYSIS_{name}_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
        env = {**os.environ, **urls, "ANALYSIS_LAKE": "mock", "ANALYSIS_INPUTS": str(config.inputs_dir())}
        env.pop("MOCK_LAKE_URL", None)
        out = subprocess.run([sys.executable, str(replay.export(state))], capture_output=True, text=True, env=env,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertRegex(out.stdout, r"sql_query-1\s+MATCH")
        self.assertRegex(out.stdout, r"sql_query-2\s+MATCH")
        self.assertIn("USD is 151.25", out.stdout)
