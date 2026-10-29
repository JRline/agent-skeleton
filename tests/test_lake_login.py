"""Data lake sign-in: the user's ID and password reach the lake and nothing else (memory only)."""

from agent import config, connectors, credentials
from agent.connectors import odbc
from agent.backends.fake import FakeModel
from agent.datalake import LakeError, get_lake, needs_login
from agent.loop import resolve_pending, run_one_step
from agent.memory import new_state
from agent.tools import ToolContext, dispatch

from tests.helpers import OK, STD, TempDirs, final, first_step, tool_call

SECRET = "s3cret-Pa55"
SQL = f"SELECT COUNT(*) AS n FROM usage_limits WHERE {STD} AND scenario = 'ALLOC' AND resource = 'RWA'"


class LakeLogin(TempDirs):
    def setUp(self):
        super().setUp()
        self.serve_mock(use=True, users={"analyst": SECRET})
        connectors.OVERRIDES["sample_lake"]["needs_login"] = True
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

    def test_a_connector_without_a_connection_is_reported_at_load_with_the_reason(self):
        from pathlib import Path
        from unittest import mock
        root = Path(self._tmp) / "conns"
        cases = {"lower": 'KIND = "odbc"\nconnection_string = "DRIVER={D};SERVER=s"\n',
                 "near": 'KIND = "odbc"\nCONN_STR = "DRIVER={D};SERVER=s"\n',
                 "dsn": 'KIND = "odbc"\nDSN = "LakeDSN"\n', "nokind": 'DESCRIPTION = "Synthetic."\n'}
        for name, body in cases.items():
            (root / name).mkdir(parents=True)
            (root / name / "connector.py").write_text(body)
        (root / "old").mkdir()
        (root / "old" / "connector.md").write_text("---\nkind: odbc\n---\n")
        with mock.patch.dict("os.environ", {"LAKE_ODBC": "DRIVER={D};SERVER=s"}):
            errors = {}
            for name in [*cases, "old"]:
                try:
                    connectors._load(root / name)
                except (ValueError, FileNotFoundError) as e:
                    errors[name] = str(e)
        self.assertNotIn("dsn", errors)                                    # DSN = "<name>" is enough
        self.assertIn("Rename connection_string to CONNECTION_STRING", errors["lower"])
        self.assertIn("Rename CONN_STR", errors["near"])
        self.assertIn("LAKE_ODBC is set in the environment but not read", errors["near"])
        self.assertIn("no KIND", errors["nokind"])
        self.assertIn("connector.md is no longer read", errors["old"])

    def test_pyodbc_gets_the_connection_string_and_nothing_else(self):
        """No readonly, autocommit or timeout attributes: some drivers reject them."""
        import sys
        import types
        from unittest import mock
        from agent.connectors import Connector
        calls = []
        fake = types.SimpleNamespace(connect=lambda *a, **k: calls.append((a, k)) or object(), Error=Exception,
                                     drivers=lambda: [])
        conn = Connector(name="lake", kind="odbc", folder=config.EXTENSIONS_DIR,
                         settings={"connection_string": "DRIVER={D};SERVER=s"})
        with mock.patch.dict(sys.modules, {"pyodbc": fake}):
            odbc.connect(conn, credentials.Login("analyst", "p"))
        self.assertEqual(calls, [(("DRIVER={D};SERVER=s;UID={analyst};PWD={p}",), {})])

    def test_a_dsn_alone_makes_the_connection_string(self):
        from agent.connectors import Connector
        conn = Connector(name="lake", kind="odbc", folder=config.EXTENSIONS_DIR, settings={"dsn": "LakeDSN"})
        self.assertEqual(odbc.connection_string(conn, credentials.Login("a", "p")), "DSN=LakeDSN;UID={a};PWD={p}")

    def test_a_missing_dsn_names_the_driver_and_what_is_installed(self):
        from agent.connectors import Connector, odbc
        conn = Connector(name="lake", kind="odbc", folder=config.EXTENSIONS_DIR,
                         settings={"connection_string": "Driver={Lake ODBC 2.1};Server=s;Database=d"})
        text = odbc.driver_help(conn, ["Lake ODBC Driver 2.1", "SQLite3"])
        self.assertIn("'Lake ODBC 2.1'", text)
        self.assertIn("Installed drivers: Lake ODBC Driver 2.1, SQLite3", text)
        conn.settings["connection_string"] = "SERVER=s;DATABASE=d"
        self.assertIn("neither DRIVER", odbc.driver_help(conn, []))

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
