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
from mock_lake import server

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
        srv = server.serve(0, config.datalake_dir(), quiet=True)
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
        self.assertIn("\np['change'] = p['after'] - p['before']\n", text)        # approved code, unchanged, top level
        self.assertIn("two = run_sql(SQL)", text)                                   # named after its saved table
        self.assertLess(text.index("two = run_sql(SQL)"), text.index("# %% Support code"))   # analysis first
        self.assertIn("# %% Not replayed: run_python attempt", text)               # the failed attempt
        self.assertIn("#     result = missing_name", text.replace("# result", "#     result"))
        self.assertLess(text.index("# %% Step 1 · data extraction"), text.index("# %% Step 2 · analysis"))
        self.assertIn("# %% Check", text)

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
