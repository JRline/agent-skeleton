"""Tool time limits: a default for every tool, and a longer one for lake queries (they can run for 15 minutes)."""
import time

from pydantic import BaseModel

from agent import config
from agent.tools import TOOLS, ToolContext, dispatch, tool

from tests.helpers import TempDirs


class SleepArgs(BaseModel):
    seconds: float


class ToolTimeouts(TempDirs):
    def setUp(self):
        super().setUp()
        saved = config.TOOL_TIMEOUT_S
        self.addCleanup(setattr, config, "TOOL_TIMEOUT_S", saved)
        config.TOOL_TIMEOUT_S = 1

        @tool("_test_slow_default", "Sleeps.", SleepArgs)
        def slow_default(args, ctx):
            time.sleep(args.seconds)
            return "done"

        @tool("_test_slow_long", "Sleeps.", SleepArgs, timeout_s=lambda: 3)
        def slow_long(args, ctx):
            time.sleep(args.seconds)
            return "done"

        for name in ("_test_slow_default", "_test_slow_long"):
            self.addCleanup(TOOLS.pop, name, None)
        d = config.RUNS_DIR / "r1"
        d.mkdir(parents=True, exist_ok=True)
        self.ctx = ToolContext("r1", d)

    def test_the_default_limit_still_stops_a_slow_tool(self):
        obs = dispatch("_test_slow_default", {"seconds": 1.5}, self.ctx, "t-1")
        self.assertFalse(obs["ok"])
        self.assertIn("Timed out after 1s", obs["error"])

    def test_a_tool_with_its_own_limit_runs_past_the_default(self):
        obs = dispatch("_test_slow_long", {"seconds": 1.5}, self.ctx, "t-2")
        self.assertTrue(obs["ok"], obs)

    def test_sql_query_uses_the_sql_limit_read_at_call_time(self):
        self.assertEqual(TOOLS["sql_query"].timeout_s(), config.SQL_TIMEOUT_S)
        saved = config.SQL_TIMEOUT_S
        self.addCleanup(setattr, config, "SQL_TIMEOUT_S", saved)
        config.SQL_TIMEOUT_S = 1234
        self.assertEqual(TOOLS["sql_query"].timeout_s(), 1234)

    def test_the_sql_default_is_fifteen_minutes(self):
        import os
        if "SQL_TIMEOUT_S" in os.environ:
            self.skipTest("SQL_TIMEOUT_S is set in this environment")
        from agent import config
        self.assertEqual(config.Settings.model_fields["sql_timeout_s"].default, 900)
