"""IO cost: server-side limits, no repeated queries or parses, refusing over-large results, required filters."""
from unittest import mock

import pandas as pd

from agent import config, datalake, tablecache
from agent.datalake import Lake, LakeError, check_required_filters, get_lake, limit_sql
from agent.pysandbox import run_code
from agent.tools import ToolContext, dispatch

from tests.helpers import STD, TempDirs


class RecordingLake:
    """A DB-API stand-in that records the SQL the server would receive."""

    def __init__(self, rows=3):
        self.sent, self.rows, self.view_calls = [], rows, 0

    def connect(self):
        lake = self

        class Cursor:
            description = [("a",), ("b",)]

            def execute(self, sql):
                lake.sent.append(sql)

            def fetchmany(self, n):
                return [(i, i * 10) for i in range(min(n, lake.rows))]

        class Conn:
            def cursor(self):
                return Cursor()

            def close(self):
                pass

        return Conn()

    def views(self):
        self.view_calls += 1
        return ["v"]

    def lake(self, source):
        """A Lake on a connector whose connection kind is this recorder."""
        import types
        from agent.connectors import Connector, Dataset
        module = types.SimpleNamespace(connect=lambda c, login: self.connect(),
                                       list_datasets=lambda c, login: self.views(), source=lambda c: source)
        conn = Connector(name="rec", kind="rec", folder=config.EXTENSIONS_DIR, datasets={"v": Dataset("v", "rec")})
        conn.module = lambda: module
        return Lake(conn)


class LakeLimits(TempDirs):
    def test_every_query_asks_the_server_for_a_limited_number_of_rows(self):
        rec = RecordingLake()
        lake = rec.lake("rec-1")
        lake.query("SELECT a, b FROM v ORDER BY a")
        lake.describe("v")
        self.assertEqual(rec.sent[0], f"SELECT a, b FROM v ORDER BY a LIMIT {config.MAX_SQL_ROWS + 1}")
        self.assertEqual(rec.sent[1], "SELECT * FROM v LIMIT 0")             # columns only, no data rows

    def test_a_query_with_its_own_limit_is_wrapped(self):
        self.assertEqual(limit_sql("SELECT a FROM v LIMIT 10", 6), "SELECT * FROM (SELECT a FROM v LIMIT 10) AS limited_q LIMIT 6")

    def test_views_and_samples_are_fetched_once_per_session(self):
        rec = RecordingLake()
        lake = rec.lake("rec-2")
        for _ in range(3):
            lake.list_datasets()
            lake.describe("v")
        self.assertEqual(rec.view_calls, 1)
        self.assertEqual(len(rec.sent), 1)                       # one metadata query

    def test_large_views_need_their_filters(self):
        required = {"big": [["as_of_date", "fiscal_year"], ["scenario"]]}
        with self.assertRaisesRegex(LakeError, "needs a filter on as_of_date or fiscal_year"):
            check_required_filters("SELECT book, SUM(x) FROM big WHERE scenario = 'USED' GROUP BY book", required)
        with self.assertRaisesRegex(LakeError, "needs a filter on scenario"):
            check_required_filters("SELECT SUM(x) FROM big WHERE as_of_date = '2026-03-31'", required)
        check_required_filters("SELECT book, SUM(x) FROM big WHERE scenario = 'USED' "
                               "AND as_of_date IN ('2026-02-27', '2026-03-31') GROUP BY book", required)
        check_required_filters("SELECT SUM(x) FROM big WHERE fiscal_year = 'FY2025' AND scenario IN ('USED')", required)
        check_required_filters("SELECT * FROM small", required)                      # not a large view
        split = {"big": [["as_of_date"], ["scenario", "GROUP BY"]]}
        check_required_filters("SELECT scenario, SUM(x) FROM big WHERE as_of_date = '2026-03-31' GROUP BY scenario", split)
        with self.assertRaisesRegex(LakeError, r"scenario \(or GROUP BY it\)"):
            check_required_filters("SELECT SUM(x) FROM big WHERE as_of_date = '2026-03-31'", split)
        with self.assertRaisesRegex(LakeError, "as_of_date"):                         # dates must still be filtered
            check_required_filters("SELECT as_of_date, SUM(x) FROM big WHERE scenario = 'USED' GROUP BY as_of_date", split)

    def test_the_dictionary_marks_resources_and_the_lake_enforces_it(self):
        required = datalake.required_filters()["usage_limits"]
        self.assertEqual(required[0], ["partition_key"])                                     # the partition, always
        self.assertIn(["version", "GROUP BY"], required)
        self.assertIn(["method", "GROUP BY"], required)
        # the dictionary's own query for the latest dates passes: split by resource and version, partition filtered
        get_lake().query("SELECT resource, version, MAX(as_of_date) AS latest_date FROM usage_limits "
                         "WHERE partition_key = 'FY2025' AND scenario = 'USED' AND method = 'BOOKED' AND entity = 'DemoBank Group' "
                         "AND rule = 'JFSA B3' AND currency = 'JPY' GROUP BY resource, version")
        with self.assertRaisesRegex(LakeError, "version"):                          # flash + final would double count
            get_lake().query("SELECT SUM(amount) FROM usage_limits WHERE partition_key = 'FY2025' AND as_of_date = '2026-03-26' "
                             "AND scenario = 'USED' AND resource = 'RWA' AND method = 'BOOKED' "
                             "AND entity = 'DemoBank Group' AND rule = 'JFSA B3' AND currency = 'JPY'")


DAY = f"as_of_date = '2026-03-26' AND scenario = 'USED' AND resource = 'RWA' AND {STD}"


class SqlTool(TempDirs):
    def ctx(self):
        return ToolContext(run_id="r1", run_dir=config.RUNS_DIR / "r1")

    def test_too_many_rows_is_refused_with_advice_to_aggregate(self):
        with mock.patch.object(config, "MAX_SQL_ROWS", 10):
            obs = dispatch("sql_query", {"sql": f"SELECT * FROM usage_limits WHERE {DAY}", "save_as": "all"},
                           self.ctx(), "sql_query-1")
        self.assertFalse(obs["ok"])
        self.assertIn("Aggregate in SQL", obs["error"])
        self.assertFalse((config.work_dir("r1") / "all.csv").exists())             # nothing truncated was saved

    def test_the_same_query_is_not_sent_twice(self):
        sql = f"SELECT division, SUM(amount) AS rwa FROM usage_limits WHERE {DAY} GROUP BY division"
        with mock.patch.object(Lake, "query", autospec=True, side_effect=Lake.query) as q:
            first = dispatch("sql_query", {"sql": sql, "save_as": "t"}, self.ctx(), "sql_query-1")
            again = dispatch("sql_query", {"sql": "  " + sql.replace(" ", "\n  ") + ";", "save_as": "t2"},
                             self.ctx(), "sql_query-2")
            self.assertEqual(q.call_count, 1)
            fresh = dispatch("sql_query", {"sql": sql, "save_as": "t3", "refresh": True}, self.ctx(), "sql_query-3")
            self.assertEqual(q.call_count, 2)
        self.assertTrue(first["ok"] and again["ok"] and fresh["ok"])
        self.assertIn("reused: the same query already ran as sql_query-1", again["result"])
        self.assertIn("saved as work/r1/t.csv", again["result"])                     # the first table


class FileCache(TempDirs):
    def excel(self, rows=300):
        path = config.inputs_dir() / "big.xlsx"
        pd.DataFrame({"book": [f"b{i % 5}" for i in range(rows)], "rwa": range(rows)}).to_excel(path, index=False)
        return path

    def csv(self, rows=300):
        path = config.inputs_dir() / "big.csv"
        pd.DataFrame({"book": [f"b{i % 5}" for i in range(rows)], "rwa": range(rows)}).to_csv(path, index=False)
        return path

    def test_a_file_is_parsed_once_per_version(self):
        self.csv()
        ctx = ToolContext(run_id="r1", run_dir=config.RUNS_DIR / "r1")
        with mock.patch.object(tablecache, "read", side_effect=tablecache.read) as parse:
            for i in range(3):
                self.assertTrue(dispatch("read_file", {"path": "inputs/big.csv"}, ctx, f"read_file-{i}")["ok"])
            self.assertEqual(parse.call_count, 1)
            tablecache.clear_memory()                                               # e.g. a new app process
            dispatch("read_file", {"path": "inputs/big.csv"}, ctx, "read_file-9")
            self.assertEqual(parse.call_count, 1)                                   # the pickle is reused
            self.csv(rows=301)                                                      # the file changed
            dispatch("read_file", {"path": "inputs/big.csv"}, ctx, "read_file-10")
            self.assertEqual(parse.call_count, 2)

    def test_read_file_stores_a_slice_not_a_copy(self):
        self.csv(rows=500)
        ctx = ToolContext(run_id="r1", run_dir=config.RUNS_DIR / "r1")
        dispatch("read_file", {"path": "inputs/big.csv"}, ctx, "read_file-1")
        stored = (config.RUNS_DIR / "r1" / "outputs" / "read_file-1.txt").read_text()
        self.assertIn("shape=(500, 2)", stored)
        self.assertIn(f"first {config.READ_FILE_STORED_ROWS} of 500 rows", stored)
        self.assertLess(stored.count("\n"), config.READ_FILE_STORED_ROWS + 5)

    def test_analysis_code_gets_pickles_instead_of_parsing(self):
        self.excel()
        code = "import pandas as pd\nresult = len(pd.read_excel('inputs/big.xlsx', header=None)) + len(tables['t'])"
        with mock.patch.object(tablecache, "read", side_effect=tablecache.read) as parse:
            out = run_code(code, {"t": "inputs/big.xlsx"}, run_id="r1", save_as="x")
            again = run_code(code, {"t": "inputs/big.xlsx"}, run_id="r1", save_as="x")
        self.assertEqual(out["result"]["value"], 301 + 300)                         # header=None keeps row 0
        self.assertEqual(again["result"]["value"], 601)
        self.assertEqual(parse.call_count, 2)            # once per distinct read (default, header=None), not per run

    def test_work_tables_are_cached_when_written(self):
        ctx = ToolContext(run_id="r1", run_dir=config.RUNS_DIR / "r1")
        dispatch("sql_query", {"sql": f"SELECT division, SUM(amount) AS rwa FROM usage_limits WHERE {DAY} "
                                      "GROUP BY division", "save_as": "cc"}, ctx, "sql_query-1")
        with mock.patch.object(tablecache, "read", side_effect=tablecache.read) as parse:
            run_code("result = len(tables['cc'])", {"cc": "work/r1/cc.csv"}, run_id="r1", save_as="x")
        self.assertEqual(parse.call_count, 0)


class Evidence(TempDirs):
    def test_stored_outputs_are_indexed_once(self):
        from pathlib import Path
        from agent.loop import uncited_numbers
        from agent.memory import new_state, run_dir
        state = new_state("g")
        (run_dir(state) / "outputs").mkdir(parents=True, exist_ok=True)
        stored = run_dir(state) / "outputs" / "x-1.txt"
        stored.write_text("rwa 101500")
        real = Path.read_text
        reads = []
        with mock.patch.object(Path, "read_text", autospec=True,
                               side_effect=lambda p, *a, **k: (reads.append(p) if p == stored else None) or real(p, *a, **k)):
            for _ in range(3):
                self.assertEqual(uncited_numbers(state, "RWA 101,500 [ref: x-1]"), [])
        self.assertEqual(len(reads), 1)                            # read and indexed once, not per review

class DataReadsAreConfirmed(TempDirs):
    """Reading the lake or the user's files asks first; reading back this conversation's results does not."""

    def test_which_calls_need_approval(self):
        from agent.tools import TOOLS, needs_approval
        ctx = ToolContext(run_id="r1", run_dir=config.RUNS_DIR / "r1")
        sql = {"sql": f"SELECT SUM(amount) AS rwa FROM usage_limits WHERE {DAY}", "save_as": "t"}
        self.assertTrue(needs_approval(TOOLS["sql_query"], sql, ctx))
        self.assertTrue(needs_approval(TOOLS["read_file"], {"path": "inputs/sample_capital.csv"}, ctx))
        self.assertFalse(needs_approval(TOOLS["read_file"], {"path": "work/r1/t.csv"}, ctx))
        self.assertFalse(needs_approval(TOOLS["read_file"], {"path": "sandbox/outputs/r1/x.csv"}, ctx))
        self.assertFalse(needs_approval(TOOLS["describe_dataset"], {"view": "usage_limits"}, ctx))   # metadata only
        dispatch("sql_query", sql, ctx, "sql_query-1")
        self.assertFalse(needs_approval(TOOLS["sql_query"], sql, ctx))                 # answered from its saved result
        self.assertTrue(needs_approval(TOOLS["sql_query"], {**sql, "refresh": True}, ctx))

    def test_a_lake_query_waits_for_the_user_in_the_loop(self):
        from agent.backends.fake import FakeModel
        from agent.loop import resolve_pending, run_one_step
        from agent.memory import new_state
        from tests.helpers import OK, PLAN, final, tool_call
        query = tool_call("sql_query", step_done=True, sql=f"SELECT SUM(amount) AS rwa FROM usage_limits WHERE {DAY}",
                          save_as="t")
        m = FakeModel({"planner": [PLAN], "executor": [query, final("RWA 245463 [ref: sql_query-1]")],
                       "reviewer": [OK], "summariser": ["- s"]})
        state = new_state("latest RWA")
        run_one_step(state, m)
        ev = run_one_step(state, m)
        self.assertEqual((ev["kind"], ev["payload"]["tool"]), ("approval", "sql_query"))
        resolve_pending(state, approved=True)
        self.assertEqual(state["events"][-1]["payload"]["obs"]["ok"], True)
        run_one_step(state, m)
        self.assertEqual(state["status"], "finished")


class RequestReview(TempDirs):
    """The reviewer checks a data read or analysis request before the user is asked."""

    def setUp(self):
        super().setUp()
        from tests.helpers import tool_call
        self.query = tool_call("sql_query", step_done=True,
                               sql=f"SELECT SUM(amount) AS rwa FROM usage_limits WHERE {DAY}", save_as="t")

    def model(self, reviews, executor_extra=()):
        from agent.backends.fake import FakeModel
        from tests.helpers import OK, PLAN, final
        return FakeModel({"planner": [PLAN], "executor": [self.query] * 4 + list(executor_extra) + [final("x")],
                          "action_review": list(reviews), "reviewer": [OK], "summariser": ["- s"] * 5})

    def test_a_rejected_request_goes_back_to_the_agent_without_asking(self):
        from agent.loop import run_one_step
        from agent.memory import new_state
        from tests.helpers import j
        m = self.model([j(ok=False, issues=["Use version = 'CLOSED'"], explain=""),
                        j(ok=True, issues=[], explain="Totals RWA usage on 2026-03-26 (final).")])
        state = new_state("latest RWA")
        run_one_step(state, m)                                       # plan
        ev = run_one_step(state, m)                                  # sent back, revised, then the user is asked
        self.assertEqual([e["kind"] for e in state["events"]][-2:], ["precheck", "approval"])
        self.assertEqual((ev["kind"], state["status"]), ("approval", "awaiting_approval"))
        second = [c for c in m.calls if c["purpose"] == "executor"][1]["messages"][-1]["content"]
        self.assertIn("Use version = 'CLOSED'", second)
        self.assertEqual(ev["payload"]["explain"], "Totals RWA usage on 2026-03-26 (final).")
        self.assertEqual(m.efforts("action_review"), ["medium", "medium"])

    def test_after_repeated_rejections_the_user_decides_with_the_notes(self):
        from agent.loop import run_one_step
        from agent.memory import new_state
        from tests.helpers import j
        no = j(ok=False, issues=["Scans every date"], explain="")
        m = self.model([no, no, no])
        state = new_state("latest RWA")
        run_one_step(state, m)
        run_one_step(state, m)
        self.assertEqual([e["kind"] for e in state["events"]], ["plan", "precheck", "precheck", "approval"])
        self.assertEqual(state["events"][-1]["payload"]["reviewer_notes"], ["Scans every date"])

    def test_writes_are_not_reviewed_as_requests(self):
        """export_query is reviewed for its query (it reads the lake); its only write is the user's own file."""
        from agent.config import REVIEWED_TOOLS
        self.assertEqual(set(REVIEWED_TOOLS), {"sql_query", "export_query", "run_python", "read_file"})


class SqlFormat(TempDirs):
    def test_formatting_changes_layout_only(self):
        from agent.sqlfmt import format_sql
        sql = ("SELECT a, SUM(amount) AS s FROM usage_limits WHERE partition_key='FY2025' AND version IN ('CLOSED','EARLY') "
               "AND as_of_date BETWEEN '2025-04-01' AND '2025-06-30' AND entity = 'A, B' GROUP BY a ORDER BY s DESC")
        out = format_sql(sql)
        self.assertEqual(out.splitlines()[:4], ["SELECT", "    a,", "    SUM(amount) AS s", "FROM usage_limits"])
        self.assertIn("    AND as_of_date BETWEEN '2025-04-01' AND '2025-06-30'", out)
        self.assertIn("'A, B'", out)                                             # strings untouched
        squash = lambda s: "".join(s.split()).replace(",", "").lower()           # noqa: E731
        self.assertEqual(squash(out), squash(sql))


class RequestReviewContext(TempDirs):
    def test_the_reviewer_sees_earlier_requests_in_full(self):
        from agent.backends.fake import FakeModel
        from agent.loop import resolve_pending, run_one_step
        from agent.memory import new_state
        from tests.helpers import OK, PLAN, final, tool_call
        first = tool_call("sql_query", sql=f"SELECT SUM(amount) AS rwa FROM usage_limits WHERE {DAY}", save_as="t")
        code = tool_call("run_python", code="result = len(tables['t'])", tables={"t": "work/x/t.csv"})
        m = FakeModel({"planner": [PLAN], "executor": [first, code, final("x")], "reviewer": [OK],
                       "summariser": ["- s"] * 5})
        state = new_state("latest RWA")
        run_one_step(state, m)
        run_one_step(state, m)
        resolve_pending(state, approved=True)
        run_one_step(state, m)
        reviews = [c["messages"][-1]["content"] for c in m.calls if c["purpose"] == "action_review"]
        self.assertEqual(len(reviews), 2)                                   # the query, then the code
        earlier = reviews[1].split("EARLIER REQUESTS", 1)[1].split("REQUEST (between", 1)[0]
        self.assertIn(f"SELECT SUM(amount) AS rwa FROM usage_limits WHERE {DAY}", earlier)   # in full, not a preview
        self.assertIn("rows=1", earlier)


class ViewRulesUpFront(TempDirs):
    """Required filters are standing background for the agent, so a query never fails on them."""

    def test_the_agent_knows_each_views_filters_and_defaults_before_querying(self):
        from agent.memory import toolkit_guidance
        rules = toolkit_guidance(["datalake"])
        self.assertIn("Dataset usage_limits: every query must filter on partition_key; scenario (or GROUP BY it)", rules)
        self.assertIn("Start from WHERE partition_key = 'FY2025' AND entity = 'DemoBank Group'", rules)
        self.assertIn("version = 'CLOSED' AND method = 'BOOKED'", rules)

    def test_a_query_missing_a_filter_goes_back_before_the_user_is_asked(self):
        from agent.backends.fake import FakeModel
        from agent.loop import run_one_step
        from agent.memory import new_state
        from tests.helpers import OK, PLAN, final, tool_call
        bad = tool_call("sql_query", sql="SELECT SUM(amount) FROM usage_limits WHERE partition_key = 'FY2025'", save_as="t")
        good = tool_call("sql_query", sql=f"SELECT SUM(amount) AS rwa FROM usage_limits WHERE {DAY}", save_as="t")
        m = FakeModel({"planner": [PLAN], "executor": [bad, good, final("x")], "reviewer": [OK],
                       "summariser": ["- s"] * 3})
        state = new_state("latest RWA")
        run_one_step(state, m)
        run_one_step(state, m)
        ev = state["events"][-2]
        self.assertEqual((ev["kind"], ev["payload"]["check"]), ("precheck", "static"))
        self.assertIn("needs a filter on scenario", ev["payload"]["issues"][0])
        self.assertFalse(any(e["kind"] == "tool" and not (e["payload"].get("obs") or {}).get("ok", True)
                             for e in state["events"]))                  # no failed query anywhere
        self.assertEqual(state["events"][-1]["kind"], "approval")


class RevisedRequest(TempDirs):
    def test_a_revised_request_carries_the_previous_one_to_the_reviewer_and_the_card(self):
        from agent.backends.fake import FakeModel
        from agent.loop import run_one_step
        from agent.memory import new_state
        from tests.helpers import OK, PLAN, final, j, tool_call
        first = tool_call("sql_query", sql=f"SELECT SUM(amount) AS rwa FROM usage_limits WHERE {DAY}", save_as="t")
        revised = tool_call("sql_query", sql=f"SELECT division, SUM(amount) AS rwa FROM usage_limits WHERE {DAY} "
                                             "GROUP BY division", save_as="t2")
        m = FakeModel({"planner": [PLAN], "executor": [first, revised, final("x")], "reviewer": [OK],
                       "action_review": [j(ok=False, issues=["Break it down by division"], explain=""),
                                         j(ok=True, issues=[], explain="Changed from the previous request: by division.")],
                       "summariser": ["- s"] * 3})
        state = new_state("RWA by division")
        run_one_step(state, m)
        ev = run_one_step(state, m)                              # first request sent back, revised one asks
        self.assertEqual([e["kind"] for e in state["events"]][-2:], ["precheck", "approval"])
        self.assertIn("SELECT SUM(amount) AS rwa", ev["payload"]["previous"])
        self.assertTrue(ev["payload"]["explain"].startswith("Changed from the previous request"))
        second_review = [c for c in m.calls if c["purpose"] == "action_review"][1]["messages"][-1]["content"]
        self.assertIn("PREVIOUS REQUEST of the same tool", second_review)
        self.assertIn("SELECT SUM(amount) AS rwa FROM usage_limits", second_review.split("PREVIOUS REQUEST", 1)[1])
