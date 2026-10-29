"""Datasets keyed by their own codes, joined to usage_limits through the user's local mapping table:
credit_exposures (same connector, sample_lake) and desk_risk (another connector, market_lake)."""
import os
import subprocess
import sys

import pandas as pd

from agent import config, connectors, datalake, replay
from agent.backends.fake import FakeModel
from agent.loop import run
from agent.tools import ToolContext, dispatch

from tests.helpers import OK, PLAN, STD, TempDirs, j

DATE = "2026-03-26"
CREDIT_USAGE = (f"SELECT product_id, location, component_code, SUM(amount) AS usage_rwa FROM usage_limits "
                f"WHERE {STD} AND scenario = 'USED' AND resource = 'RWA' AND as_of_date = '{DATE}' "
                "AND component_code IN ('CRD_STD', 'CRD_FND', 'CRD_ADV', 'CRD_SLT') "
                "GROUP BY product_id, location, component_code")
CREDIT = ("SELECT portfolio_code, approach, SUM(rwa) AS credit_rwa FROM credit_exposures WHERE partition_key = 'FY2025' "
          f"AND entity = 'DemoBank Group' AND as_of_date = '{DATE}' GROUP BY portfolio_code, approach")
MARKET_USAGE = (f"SELECT product_id, location, SUM(amount) AS market_rwa FROM usage_limits "
                f"WHERE {STD} AND scenario = 'USED' AND resource = 'RWA' AND as_of_date = '{DATE}' "
                "AND risk_type = 'Market risk' GROUP BY product_id, location")
ES = f"SELECT desk_code, value AS es FROM desk_risk WHERE metric = 'ES_10D_975' AND as_of_date = '{DATE}'"

RECONCILE = ("m = tables['map']\n"
             "m = m[m.source_system == 'credit']\n"
             "c = tables['credit'].merge(m, left_on='portfolio_code', right_on='source_code', how='left')\n"
             "c = c.groupby(['product_id', 'location', 'approach'], as_index=False)['credit_rwa'].sum()\n"
             "u = tables['usage'].rename(columns={'component_code': 'approach'})\n"
             "r = u.merge(c, on=['product_id', 'location', 'approach'], how='outer').fillna(0)\n"
             "r['difference'] = r['usage_rwa'] - r['credit_rwa']\n"
             "result = r\n"
             "notes = f\"total usage {r['usage_rwa'].sum():.0f} credit {r['credit_rwa'].sum():.0f} "
             "unmatched {int((r['difference'] != 0).sum())}\"")
RATIO = ("m = tables['map']\n"
         "m = m[m.source_system == 'market']\n"
         "es = tables['es'].merge(m, left_on='desk_code', right_on='source_code')\n"
         "es = es.groupby(['product_id', 'location'], as_index=False)['es'].sum()\n"
         "r = tables['rwa'].merge(es, on=['product_id', 'location'], how='inner')\n"
         "r['rwa_per_es'] = (r['market_rwa'] / r['es']).round(2)\n"
         "result = r.sort_values('rwa_per_es', ascending=False).reset_index(drop=True)\n"
         "notes = f\"{len(r)} desks matched of {len(tables['rwa'])} products with market RWA\"")
USAGE = (f"SELECT product_id, location, scenario, SUM(amount) AS amount FROM usage_limits "
         f"WHERE {STD} AND resource = 'RWA' AND as_of_date = '{DATE}' GROUP BY product_id, location, scenario")
THREE = ("u = tables['usage'].pivot_table(index=['product_id', 'location'], columns='scenario', values='amount',\n"
         "                                 aggfunc='sum').reset_index()\n"
         "u.columns = ['product_id', 'location', 'rwa_limit', 'rwa_usage']\n"
         "m = tables['map']\n"
         "c = tables['credit'].merge(m[m.source_system == 'credit'], left_on='portfolio_code', right_on='source_code')\n"
         "c = c.groupby(['product_id', 'location'], as_index=False)['credit_rwa'].sum()\n"
         "e = tables['es'].merge(m[m.source_system == 'market'], left_on='desk_code', right_on='source_code')\n"
         "e = e.groupby(['product_id', 'location'], as_index=False)['es'].sum()\n"
         "r = u.merge(c, on=['product_id', 'location'], how='left').merge(e, on=['product_id', 'location'], how='left')\n"
         "r['credit_share_pct'] = (100 * r['credit_rwa'] / r['rwa_usage']).round(2)\n"
         "result = r.sort_values(['product_id', 'location']).reset_index(drop=True)\n"
         "notes = f\"{len(r)} keys; credit found for {r['credit_rwa'].notna().sum()}, ES for {r['es'].notna().sum()}\"")


def saved(msgs, n=1):
    import re
    return re.findall(r"saved as (work/\S+?\.csv)", "\n".join(m["content"] for m in msgs))[-n]


class SampleData(TempDirs):
    def test_every_code_is_mapped_and_the_credit_view_adds_up_to_usage(self):
        mapping = pd.read_csv(config.inputs_dir() / "book_mapping.csv")
        self.assertFalse(mapping.duplicated(["source_system", "source_code"]).any())
        credit, _ = datalake.get_lake(None, "sample_lake").query(CREDIT)
        usage, _ = datalake.get_lake(None, "sample_lake").query(CREDIT_USAGE)
        es, _ = datalake.get_lake(None, "market_lake").query(ES)
        self.assertTrue(set(credit.portfolio_code) <= set(mapping.source_code))
        self.assertTrue(set(es.desk_code) <= set(mapping.source_code))
        joined = credit.merge(mapping, left_on="portfolio_code", right_on="source_code")
        joined = joined.groupby(["product_id", "location", "approach"])["credit_rwa"].sum()
        expected = usage.set_index(["product_id", "location", "component_code"])["usage_rwa"]
        expected.index.names = joined.index.names
        pd.testing.assert_series_equal(joined.sort_index(), expected.sort_index(), check_names=False)

    def test_datasets_of_two_connectors_cannot_be_joined_in_one_query(self):
        d = config.RUNS_DIR / "r1"
        d.mkdir(parents=True)
        sql = ("SELECT u.product_id, d.value FROM usage_limits u JOIN desk_risk d ON u.as_of_date = d.as_of_date "
               f"WHERE {STD.replace('partition_key', 'u.partition_key')} AND d.metric = 'ES_10D_975'")
        obs = dispatch("sql_query", {"sql": sql, "save_as": "x"}, ToolContext("r1", d), "sql_query-1")
        self.assertFalse(obs["ok"])
        self.assertIn("several connectors", obs["error"])
        self.assertIn("run_python", obs["error"])


class JoinConversations(TempDirs):
    """The agent queries each source, then joins the extracts with the mapping file in run_python; the replay
    script sends each query back to its own source."""

    def converse(self, goal, queries, code, final):
        steps = [j(thought="t", tool="sql_query", input={"sql": sql, "save_as": name}) for name, sql in queries]
        names = [name for name, _ in queries]
        python = lambda msgs: j(thought="t", tool="run_python", input={       # noqa: E731
            "code": code, "save_as": "joined",
            "tables": {"map": "inputs/book_mapping.csv",
                       **{n: saved(msgs, len(names) - i) for i, n in enumerate(names)}}})
        m = FakeModel({"planner": [PLAN], "reviewer": [OK], "summariser": ["- s"] * 8,
                       "executor": steps + [python, lambda msgs: j(thought="t", final=final(msgs))]})
        state = run(goal, m, approve=lambda e: True, toolkits=["files", "analysis", "datalake"])
        self.assertEqual(state["status"], "finished", state.get("error"))
        return state

    def result(self, state, ref):
        return replay.agent_result(state, ref)

    def replay(self, state):
        """Each connector's own mock server at its own URL, all at once, as the script's SOURCES name them."""
        for name in connectors.enabled():
            self.serve_mock(name)
        env = {**os.environ, "ANALYSIS_INPUTS": str(config.inputs_dir())}
        return subprocess.run([sys.executable, str(replay.export(state))], capture_output=True, text=True, env=env,
                              timeout=60)

    def test_credit_view_on_the_same_connector_reconciles_to_usage(self):
        state = self.converse("Reconcile credit RWA to usage", [("usage", CREDIT_USAGE), ("credit", CREDIT)],
                              RECONCILE, lambda msgs: "Credit RWA reconciles to usage [ref: run_python-3].")
        r = self.result(state, "run_python-3")
        self.assertGreater(len(r), 10)
        self.assertEqual(float(r["difference"].abs().sum()), 0.0)
        self.assertFalse(r["credit_rwa"].eq(0).any())                  # every usage row found its portfolio
        out = self.replay(state)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.count("MATCH"), 3)

    def test_market_view_on_another_connector_joins_through_the_mapping(self):
        state = self.converse("Market RWA per unit of ES by desk", [("rwa", MARKET_USAGE), ("es", ES)], RATIO,
                              lambda msgs: "Market RWA per ES by desk [ref: run_python-3].")
        r = self.result(state, "run_python-3")
        self.assertEqual(len(r), len(self.result(state, "sql_query-1")))  # every product with market RWA has a desk
        self.assertTrue((r["rwa_per_es"] > 0).all())
        text = replay.build_script(state)
        self.assertIn("run_sql(SQL, 'sample_lake')", text)
        self.assertIn("run_sql(SQL, 'market_lake')", text)
        out = self.replay(state)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.count("MATCH"), 3)

    def test_three_datasets_over_two_connectors_join_into_one_table(self):
        """usage_limits and credit_exposures (sample_lake) and desk_risk (market_lake), keyed together by the
        user's mapping: usage and limit, the credit part of usage, and the desk's ES, per product and location."""
        state = self.converse("RWA usage, credit RWA and ES by product and location",
                              [("usage", USAGE), ("credit", CREDIT), ("es", ES)], THREE,
                              lambda msgs: "Usage, credit RWA and ES by product and location [ref: run_python-4].")
        r = self.result(state, "run_python-4")
        credit, _ = datalake.get_lake(None, "sample_lake").query(CREDIT)
        es, _ = datalake.get_lake(None, "market_lake").query(ES)
        self.assertEqual(float(r["credit_rwa"].sum()), float(credit["credit_rwa"].sum()))   # nothing lost in the join
        self.assertAlmostEqual(float(r["es"].sum()), float(es["es"].sum()), places=6)
        self.assertEqual(int(r["es"].notna().sum()), len(es))                              # every desk found its key
        self.assertTrue((r["rwa_usage"] <= r["rwa_limit"] * 1.5).all())
        self.assertTrue((r["credit_share_pct"].dropna() <= 100).all())        # credit is a part of the usage
        text = replay.build_script(state)
        self.assertEqual(text.count("run_sql(SQL, 'sample_lake')"), 2)
        self.assertEqual(text.count("run_sql(SQL, 'market_lake')"), 1)
        out = self.replay(state)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.count("MATCH"), 4)
