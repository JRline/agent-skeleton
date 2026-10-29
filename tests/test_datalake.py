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

    def test_the_query_is_parsed_not_searched_for_keywords(self):
        """Columns named like keywords are fine; a dataset hidden in a subquery or after a comment is still found."""
        check_select("SELECT entity AS set_name, period AS copy FROM capital_components -- delete later", VIEWS)
        for sql in ["SELECT * FROM (SELECT * FROM secret_view) AS s",
                    "SELECT * FROM capital_components WHERE entity IN (SELECT entity FROM /* x */ secret_view)",
                    "WITH c AS (SELECT * FROM secret_view) SELECT * FROM c"]:
            with self.assertRaisesRegex(LakeError, "secret_view", msg=sql):
                check_select(sql, VIEWS)
        with self.assertRaisesRegex(LakeError, "Only SELECT"):
            check_select("PRAGMA table_info(capital_components)", VIEWS)

    def test_row_limits_follow_the_dialect_and_never_raise_a_query_limit(self):
        from agent.datalake import limit_sql
        self.assertEqual(limit_sql("SELECT a FROM t", 5), "SELECT a FROM t LIMIT 5")
        self.assertEqual(limit_sql("SELECT a FROM t", 5, "tsql"), "SELECT TOP 5 a FROM t")
        self.assertIn("FETCH FIRST 5 ROWS ONLY", limit_sql("SELECT a FROM t", 5, "oracle"))
        wrapped = limit_sql("SELECT a FROM t ORDER BY a LIMIT 3", 5001)
        self.assertIn("LIMIT 3", wrapped)                               # the query's own limit is kept
        self.assertTrue(wrapped.endswith("LIMIT 5001"))

    def test_required_filters_are_found_in_the_parsed_query(self):
        from agent.datalake import check_required_filters
        req = {"capital_components": [["period"], ["entity", "GROUP BY"]]}
        check_required_filters("SELECT entity, SUM(x) FROM capital_components WHERE period IN ('2026Q3') "
                               "GROUP BY entity", req)
        check_required_filters("SELECT * FROM capital_components c JOIN rwa_by_risk_type r ON c.entity = r.entity "
                               "AND c.period = '2026Q3' WHERE c.entity = 'A'", req)
        with self.assertRaisesRegex(LakeError, "period"):               # named in SELECT only: not a filter
            check_required_filters("SELECT period, entity FROM capital_components GROUP BY period, entity", req)


USAGE_RWA = f"scenario = 'USED' AND resource = 'RWA' AND {STD}"


class SQLiteLake(TempDirs):
    def test_views_describe_and_query(self):
        lake = datalake.get_lake(None, "sample_lake")
        self.assertEqual(lake.list_datasets(), ["credit_exposures", "usage_limits"])
        cols = lake.describe("usage_limits")
        self.assertIn("component", cols)
        self.assertIn("amount", cols)
        df, truncated = lake.query(f"SELECT division, SUM(amount) AS rwa FROM usage_limits "
                                   f"WHERE as_of_date = '2026-03-26' AND {USAGE_RWA} GROUP BY division "
                                   f"ORDER BY division")
        self.assertEqual(df.to_dict("records")[0], {"division": "Corporate & Investment Banking", "rwa": 122815})
        self.assertFalse(truncated)

    def test_each_resource_and_version_has_its_own_latest_date(self):
        df, _ = datalake.get_lake(None, "sample_lake").query("SELECT resource, version, MAX(as_of_date) AS latest FROM usage_limits "
                                          "WHERE partition_key = 'FY2025' AND scenario = 'USED' AND method = 'BOOKED' "
                                          "AND entity = 'DemoBank Group' AND rule = 'JFSA B3' AND currency = 'JPY' "
                                          "GROUP BY resource, version ORDER BY resource, version")
        self.assertEqual(df.values.tolist(), [["Leverage exposure", "CLOSED", "2026-03-24"],
                                              ["Leverage exposure", "EARLY", "2026-03-27"],
                                              ["RWA", "CLOSED", "2026-03-26"], ["RWA", "EARLY", "2026-03-31"]])

    def test_partitions_come_from_the_catalogue(self):
        self.assertEqual(datalake.get_lake(None, "sample_lake").partitions("usage_limits"), ["FY2025"])

    def test_row_cap(self):
        df, truncated = datalake.get_lake(None, "sample_lake").query(f"SELECT * FROM usage_limits WHERE as_of_date = '2026-03-26' "
                                                  f"AND {USAGE_RWA}", max_rows=5)
        self.assertEqual(len(df), 5)
        self.assertTrue(truncated)

    def test_connection_itself_refuses_writes(self):
        conn = datalake.get_lake(None, "sample_lake").connect()
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM usage_limits")
