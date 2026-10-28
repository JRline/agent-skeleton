"""Data lake sign-in: the user's ID and password reach the lake and nothing else (memory only)."""
import threading
from unittest import mock

from agent import config, credentials
from agent.backends.fake import FakeModel
from agent.datalake import LakeError, get_lake, needs_login
from agent.loop import resolve_pending, run_one_step
from agent.memory import new_state
from agent.tools import ToolContext, dispatch
from mock_lake import server

from tests.helpers import OK, STD, TempDirs, final, first_step, tool_call

SECRET = "s3cret-Pa55"
SQL = f"SELECT COUNT(*) AS n FROM usage_limits WHERE {STD} AND scenario = 'ALLOC' AND resource = 'RWA'"


class LakeLogin(TempDirs):
    def setUp(self):
        super().setUp()
        srv = server.serve(0, config.datalake_dir(), quiet=True, users={"analyst": SECRET})
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        for patch in (mock.patch.dict("os.environ", {"MOCK_LAKE_URL": f"http://127.0.0.1:{srv.server_address[1]}",
                                                     "MOCK_LAKE_LOGIN": "1"}),
                      mock.patch.object(config, "DATALAKE", "agent.backends.datalake_mock")):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(credentials.forget, "r1", "r2", credentials.DEFAULT)

    def test_the_users_own_login_reaches_the_lake(self):
        self.assertTrue(needs_login())
        credentials.use("r1", credentials.Login("analyst", SECRET))
        self.assertEqual(get_lake("r1").list_views(), ["usage_limits"])
        obs = dispatch("sql_query", {"sql": SQL, "save_as": "n"},
                       ToolContext(run_id="r1", run_dir=config.RUNS_DIR / "r1"), "sql_query-1")
        self.assertTrue(obs["ok"], obs)

    def test_without_a_login_the_message_says_how_to_sign_in(self):
        with self.assertRaises(LakeError) as cm:
            get_lake("r2").list_views()
        self.assertIn("Data lake sign-in", str(cm.exception))

    def test_a_wrong_password_is_refused_and_never_echoed(self):
        credentials.use("r1", credentials.Login("analyst", "wrong-pass"))
        obs = dispatch("sql_query", {"sql": SQL, "save_as": "n"},
                       ToolContext(run_id="r1", run_dir=config.RUNS_DIR / "r1"), "sql_query-1")
        self.assertFalse(obs["ok"])
        self.assertIn("Sign-in failed", obs["error"])
        self.assertNotIn("wrong-pass", obs["error"])

    def test_errors_that_echo_the_password_are_masked(self):
        credentials.use("r1", credentials.Login("analyst", SECRET))
        self.assertEqual(credentials.scrub(f"connect failed: UID=analyst;PWD={SECRET}"),
                         "connect failed: UID=analyst;PWD=***")
        self.assertNotIn(SECRET, repr(credentials.login_for("r1")))

    def test_the_password_is_never_saved_logged_or_checkpointed(self):
        state = new_state("How many limit rows are there?", toolkits=["datalake", "analysis"])
        credentials.use(state["run_id"], credentials.Login("analyst", SECRET))
        m = FakeModel({"executor": [tool_call("sql_query", sql=SQL, save_as="n"),
                                    final("There are rows [ref: sql_query-1].")], "reviewer": [OK]})
        first_step(state, m)
        self.assertEqual(state["status"], "awaiting_approval")
        resolve_pending(state, approved=True)
        self.assertTrue(state["events"][-1]["payload"]["obs"]["ok"], state["events"][-1])
        run_one_step(state, m)
        self.assertEqual(state["status"], "finished")
        for path in config.RUNS_DIR.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), path)
        self.assertNotIn(SECRET, str([c["messages"] for c in m.calls]))


class OdbcTemplate(TempDirs):
    def test_the_connection_string_carries_the_users_login_safely(self):
        from agent.backends import datalake_odbc_template as odbc
        text = odbc.connection_string(credentials.Login("analyst", "pa;ss}x"))
        self.assertTrue(text.startswith(odbc.LAKE_ODBC + ";UID={analyst};PWD="))
        self.assertTrue(text.endswith("PWD={pa;ss}}x}"))                # ; and } cannot break out of the value
        self.assertTrue(odbc.NEEDS_LOGIN)
