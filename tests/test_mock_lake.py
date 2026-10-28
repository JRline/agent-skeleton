"""The mock lake server (a separate, read-only process) and the agent's client for it."""
import json
import threading
import urllib.request
from unittest import mock

from agent import config
from agent import connectors
from agent.connectors import mock as mock_kind
from agent.datalake import LakeError, get_lake
from agent.tools import ToolContext, dispatch
from scripts import mock_lake_server as server

from tests.helpers import STD, TempDirs


class MockLakeServer(TempDirs):
    def start(self, **kw):
        srv = server.serve(0, (config.connectors_dir() / "sample_lake" / "data"), quiet=True, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        env = mock.patch.dict("os.environ", {"MOCK_LAKE_URL": url})
        env.start()
        self.addCleanup(env.stop)
        lake = mock.patch.dict("os.environ", {"CONNECTOR_SAMPLE_LAKE_KIND": "mock"})
        lake.start()
        self.addCleanup(lake.stop)
        return url

    def test_views_and_queries_go_through_the_server(self):
        self.start()
        lake = get_lake()
        self.assertEqual(lake.list_datasets(), ["usage_limits"])
        df, truncated = lake.query(f"SELECT SUM(amount) AS rwa FROM usage_limits WHERE as_of_date = '2026-03-26' "
                                   f"AND scenario = 'USED' AND resource = 'RWA' AND {STD}")
        self.assertEqual(df.iloc[0].tolist(), [245463])
        self.assertFalse(truncated)

    def test_the_sql_tool_works_unchanged(self):
        self.start()
        ctx = ToolContext(run_id="r1", run_dir=config.RUNS_DIR / "r1")
        obs = dispatch("sql_query", {"sql": f"SELECT COUNT(*) AS n FROM usage_limits WHERE {STD} AND scenario = 'ALLOC' "
                                            "AND resource = 'RWA'", "save_as": "n"}, ctx, "sql_query-1")
        self.assertTrue(obs["ok"], obs)
        self.assertTrue((config.work_dir("r1") / "n.csv").exists())

    def test_the_server_refuses_writes_whatever_the_client_sends(self):
        """The agent's SQL guard is bypassed here on purpose: the server is the read-only account."""
        url = self.start()
        for sql in ["DROP TABLE usage_limits", "DELETE FROM usage_limits",
                    "INSERT INTO usage_limits (entity) VALUES ('x')", "CREATE TABLE t (a)",
                    "ATTACH DATABASE '/tmp/x.db' AS x", "SELECT 1; DROP TABLE usage_limits"]:
            req = urllib.request.Request(url + "/query", data=json.dumps({"sql": sql}).encode())
            with self.assertRaises(urllib.error.HTTPError, msg=sql) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 400)
            cm.exception.close()
        self.assertIn("usage_limits", get_lake().list_datasets())               # still there

    def test_the_agent_guard_still_applies_first(self):
        self.start()
        with self.assertRaises(LakeError):
            get_lake().query("DELETE FROM usage_limits")

    def test_token_is_required_when_set(self):
        self.start(token="synthetic-dev-token")
        conn = connectors.enabled()["sample_lake"]
        with self.assertRaisesRegex(RuntimeError, "Not authorised"):
            mock_kind.list_datasets(conn)
        with mock.patch.dict("os.environ", {"MOCK_LAKE_TOKEN": "synthetic-dev-token"}):
            self.assertIn("usage_limits", mock_kind.list_datasets(conn))

    def test_server_down_gives_a_clear_message(self):
        with mock.patch.dict("os.environ", {"MOCK_LAKE_URL": "http://127.0.0.1:9"}), \
                mock.patch.dict("os.environ", {"CONNECTOR_SAMPLE_LAKE_KIND": "mock"}):
            ctx = ToolContext(run_id="r1", run_dir=config.RUNS_DIR / "r1")
            obs = dispatch("list_datasets", {}, ctx, "list_datasets-1")
        self.assertFalse(obs["ok"])
        self.assertIn("python scripts/mock_lake_server.py", obs["error"])

    def test_lake_data_is_outside_the_sandbox(self):
        self.assertNotIn(config.SANDBOX_DIR.resolve(), (config.connectors_dir() / "sample_lake" / "data").resolve().parents)
