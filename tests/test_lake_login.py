"""Data lake sign-in: the user's ID and password reach the lake and nothing else (memory only)."""
import threading
from unittest import mock

from agent import config, credentials
from agent.backends.fake import FakeModel
from agent.datalake import LakeError, get_lake, needs_login
from agent.loop import resolve_pending, run_one_step
from agent.memory import new_state
from agent.tools import ToolContext, dispatch
from scripts import mock_lake_server as server

from tests.helpers import OK, STD, TempDirs, final, first_step, tool_call

SECRET = "s3cret-Pa55"
SQL = f"SELECT COUNT(*) AS n FROM usage_limits WHERE {STD} AND scenario = 'ALLOC' AND resource = 'RWA'"


class LakeLogin(TempDirs):
    def setUp(self):
        super().setUp()
        srv = server.serve(0, (config.connectors_dir() / "sample_lake" / "data"), quiet=True, users={"analyst": SECRET})
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        for patch in (mock.patch.dict("os.environ", {"MOCK_LAKE_URL": f"http://127.0.0.1:{srv.server_address[1]}",
                                                     "CONNECTOR_SAMPLE_LAKE_NEEDS_LOGIN": "yes"}),
                      mock.patch.dict("os.environ", {"CONNECTOR_SAMPLE_LAKE_KIND": "mock"})):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(credentials.forget, "r1", "r2", credentials.DEFAULT)

    def test_the_users_own_login_reaches_the_lake(self):
        self.assertEqual(needs_login(), ["sample_lake"])
        credentials.use("r1", "sample_lake", credentials.Login("analyst", SECRET))
        self.assertEqual(get_lake("r1", "sample_lake").list_datasets(), ["credit_exposures", "usage_limits"])
        obs = dispatch("sql_query", {"sql": SQL, "save_as": "n"},
                       ToolContext(run_id="r1", run_dir=config.RUNS_DIR / "r1"), "sql_query-1")
        self.assertTrue(obs["ok"], obs)

    def test_without_a_login_the_message_says_how_to_sign_in(self):
        with self.assertRaises(LakeError) as cm:
            get_lake("r2", "sample_lake").list_datasets()
        self.assertIn("Data sign-in", str(cm.exception))

    def test_a_wrong_password_is_refused_and_never_echoed(self):
        credentials.use("r1", "sample_lake", credentials.Login("analyst", "wrong-pass"))
        obs = dispatch("sql_query", {"sql": SQL, "save_as": "n"},
                       ToolContext(run_id="r1", run_dir=config.RUNS_DIR / "r1"), "sql_query-1")
        self.assertFalse(obs["ok"])
        self.assertIn("Sign-in failed", obs["error"])
        self.assertNotIn("wrong-pass", obs["error"])

    def test_errors_that_echo_the_password_are_masked(self):
        credentials.use("r1", "sample_lake", credentials.Login("analyst", SECRET))
        self.assertEqual(credentials.scrub(f"connect failed: UID=analyst;PWD={SECRET}"),
                         "connect failed: UID=analyst;PWD=***")
        self.assertNotIn(SECRET, repr(credentials.login_for("r1", "sample_lake")))

    def test_the_password_is_never_saved_logged_or_checkpointed(self):
        state = new_state("How many limit rows are there?", toolkits=["datalake", "analysis"])
        credentials.use(state["run_id"], "sample_lake", credentials.Login("analyst", SECRET))
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
        from agent.connectors import Connector, odbc
        conn = Connector(name="lake", kind="odbc", folder=config.EXTENSIONS_DIR,
                         settings={"connection_string": "DRIVER={D};SERVER=s"})
        text = odbc.connection_string(conn, credentials.Login("analyst", "pa;ss}x"))
        self.assertEqual(text, "DRIVER={D};SERVER=s;UID={analyst};PWD={pa;ss}}x}")   # ; and } cannot break out

    def test_an_odbc_connector_without_a_connection_string_says_so(self):
        from agent.connectors import Connector, odbc
        conn = Connector(name="lake", kind="odbc", folder=config.EXTENSIONS_DIR)
        with self.assertRaisesRegex(RuntimeError, "no CONNECTION_STRING"):
            odbc.connection_string(conn, None)

    def test_a_custom_connector_is_its_own_connection_code(self):
        from pathlib import Path
        from agent import connectors
        folder = Path(self._tmp) / "my_lake"
        (folder / "datasets").mkdir(parents=True)
        (folder / "connector.py").write_text('KIND = "custom"\nDESCRIPTION = "Synthetic."\n'
                                             'def list_datasets(connector, login=None):\n    return ["a"]\n')
        conn = connectors._load(folder)
        self.assertEqual((conn.name, conn.kind, conn.description), ("my_lake", "custom", "Synthetic."))
        self.assertEqual(conn.module().list_datasets(conn), ["a"])
