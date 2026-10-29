"""export_query: leaf rows beyond the sql_query cap streamed to a CSV for the user; only a preview reaches the model."""
from unittest import mock

import pandas as pd

from agent import config
from agent.tools import ToolContext, dispatch

from tests.helpers import STD, TempDirs

LEAVES = f"SELECT as_of_date, location, product_id, amount FROM usage_limits WHERE scenario = 'USED' AND resource = 'RWA' AND {STD}"


class ExportQuery(TempDirs):
    def ctx(self):
        d = config.RUNS_DIR / "r1"
        d.mkdir(parents=True, exist_ok=True)
        return ToolContext("r1", d)

    def export(self, sql=LEAVES, name="rwa_leaves.csv"):
        return dispatch("export_query", {"sql": sql, "name": name}, self.ctx(), "export_query-1")

    def test_rows_beyond_the_query_cap_go_to_a_file_not_the_prompt(self):
        obs = self.export()
        self.assertTrue(obs["ok"], obs.get("error"))
        path = config.outputs_dir("r1") / "rwa_leaves.csv"
        df = pd.read_csv(path)
        self.assertGreater(len(df), config.MAX_SQL_ROWS)               # sql_query would have refused this
        self.assertEqual(list(df.columns), ["as_of_date", "location", "product_id", "amount"])
        self.assertIn(f"rows={len(df)}", obs["result"])
        self.assertIn("saved as outputs/r1/rwa_leaves.csv", obs["result"])
        self.assertLess(len(obs["result"]), 3000)                     # the model sees a preview only

    def test_sql_query_over_the_cap_points_to_export(self):
        obs = dispatch("sql_query", {"sql": LEAVES, "save_as": "x"}, self.ctx(), "sql_query-1")
        self.assertFalse(obs["ok"])
        self.assertIn("export_query", obs["error"])

    def test_over_the_export_cap_leaves_no_partial_file(self):
        with mock.patch.object(config, "EXPORT_MAX_ROWS", 1000):
            obs = self.export()
        self.assertFalse(obs["ok"])
        self.assertIn("more than 1,000 rows", obs["error"])
        self.assertEqual(list(config.outputs_dir("r1").glob("*.csv")), [])

    def test_an_earlier_export_is_never_overwritten(self):
        sql = LEAVES + " AND as_of_date = '2025-04-01'"
        self.assertTrue(self.export(sql)["ok"])
        obs = self.export(sql)
        self.assertIn("outputs/r1/rwa_leaves_2.csv", obs["result"])
        self.assertTrue((config.outputs_dir("r1") / "rwa_leaves.csv").exists())

    def test_the_same_guards_as_sql_query(self):
        self.assertIn("Only SELECT", self.export("DROP TABLE usage_limits")["error"])
        missing = self.export("SELECT amount FROM usage_limits WHERE scenario = 'USED'")
        self.assertFalse(missing["ok"])
        self.assertIn("partition_key", missing["error"])
        self.assertIn("plain file name", self.export(name="../escape.csv")["error"])

    def test_needs_approval(self):
        from agent.tools import TOOLS
        self.assertTrue(TOOLS["export_query"].risky)
