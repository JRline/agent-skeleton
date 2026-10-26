"""Read-only data lake: SQL guard and the local SQLite stand-in (synthetic CSVs)."""
import sqlite3

from agent import datalake
from agent.datalake import LakeError, check_select

from tests.helpers import STD, TempDirs

VIEWS = ["capital_components", "rwa_by_risk_type"]


class SqlGuard(TempDirs):
    def test_plain_select_and_cte_are_allowed(self):
        check_select("SELECT entity, period FROM capital_components WHERE period = '2026Q3';", VIEWS)
        check_select("WITH t AS (SELECT entity, SUM(rwa) AS rwa FROM rwa_by_risk_type GROUP BY entity) "
                     "SELECT * FROM t JOIN capital_components c ON c.entity = t.entity", VIEWS)

    def test_writes_and_multiple_statements_are_rejected(self):
        for sql in ["DELETE FROM capital_components",
                    "SELECT 1; DROP TABLE capital_components",
                    "UPDATE capital_components SET cet1_capital = 0",
                    "SELECT * INTO copy FROM capital_components",
                    "/* hi */ CREATE TABLE x (a)"]:
            with self.assertRaises(LakeError, msg=sql):
                check_select(sql, VIEWS)

    def test_unknown_view_is_rejected(self):
        with self.assertRaisesRegex(LakeError, "secret_view"):
            check_select("SELECT * FROM secret_view", VIEWS)

    def test_keywords_inside_strings_do_not_trip_the_guard(self):
        check_select("SELECT * FROM capital_components WHERE entity = 'Drop Bank'", VIEWS)


USAGE_RWA = f"scenario = 'USED' AND resource = 'RWA' AND {STD}"


class SQLiteLake(TempDirs):
    def test_views_describe_and_query(self):
        lake = datalake.get_lake()
        self.assertEqual(lake.list_views(), ["usage_limits"])
        cols = lake.describe("usage_limits")
        self.assertIn("component", cols)
        self.assertIn("amount", cols)
        df, truncated = lake.query(f"SELECT division, SUM(amount) AS rwa FROM usage_limits "
                                   f"WHERE as_of_date = '2026-03-26' AND {USAGE_RWA} GROUP BY division "
                                   f"ORDER BY division")
        self.assertEqual(df.to_dict("records")[0], {"division": "Corporate & Investment Banking", "rwa": 122815})
        self.assertFalse(truncated)

    def test_each_resource_and_version_has_its_own_latest_date(self):
        df, _ = datalake.get_lake().query("SELECT resource, version, MAX(as_of_date) AS latest FROM usage_limits "
                                          "WHERE partition_key = 'FY2025' AND scenario = 'USED' AND method = 'BOOKED' "
                                          "AND entity = 'DemoBank Group' AND rule = 'JFSA B3' AND currency = 'JPY' "
                                          "GROUP BY resource, version ORDER BY resource, version")
        self.assertEqual(df.values.tolist(), [["Leverage exposure", "CLOSED", "2026-03-24"],
                                              ["Leverage exposure", "EARLY", "2026-03-27"],
                                              ["RWA", "CLOSED", "2026-03-26"], ["RWA", "EARLY", "2026-03-31"]])

    def test_partitions_come_from_the_catalogue(self):
        self.assertEqual(datalake.get_lake().partitions("usage_limits"), ["FY2025"])

    def test_row_cap(self):
        df, truncated = datalake.get_lake().query(f"SELECT * FROM usage_limits WHERE as_of_date = '2026-03-26' "
                                                  f"AND {USAGE_RWA}", max_rows=5)
        self.assertEqual(len(df), 5)
        self.assertTrue(truncated)

    def test_connection_itself_refuses_writes(self):
        conn = datalake.get_lake().connect()
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM usage_limits")
