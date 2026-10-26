"""Toolkits, plugins, /commands and per-session tool selection."""
import shutil
import tempfile
from pathlib import Path

from agent import config, plugins
from agent.backends.fake import FakeModel
from agent.loop import resolve_pending, run_one_step
from agent.memory import build_messages, new_state
from agent.tools import TOOLS

from tests.helpers import STD, LakeOff, TempDirs, j

PLUGIN = '''
from pydantic import BaseModel, Field
from agent.tools import ToolContext, tool

TOOLKIT = {"name": "greeting", "description": "Says hello.", "basic": False, "guidance": "Greet politely."}


class HelloArgs(BaseModel):
    name: str = Field(description="Who to greet")


@tool("say_hello", "Return a greeting for one name.", HelloArgs)
def say_hello(args: HelloArgs, ctx: ToolContext):
    return f"Hello, {args.name}!"
'''


class Defaults(TempDirs):
    def test_builtin_toolkits_and_basic_set(self):
        plugins.load()
        self.assertEqual({"files", "docs", "analysis", "datalake"}, set(plugins.TOOLKITS))
        self.assertEqual(set(plugins.basic_toolkits()), {"files", "docs", "analysis", "datalake"})
        self.assertEqual(TOOLS["sql_query"].toolkit, "datalake")
        self.assertEqual(TOOLS["run_python"].toolkit, "analysis")
        self.assertEqual(TOOLS["ask_user"].toolkit, "core")
        for removed in ("calc", "capital_ratio", "add_column", "aggregate", "query_table"):
            self.assertNotIn(removed, TOOLS)

    def test_new_sessions_start_with_basic_toolkits(self):
        self.assertEqual(set(new_state("hi")["toolkits"]), {"files", "docs", "analysis", "datalake"})


class Toolkits(LakeOff):
    def test_prompt_shows_only_enabled_tools_and_their_guidance(self):
        system = build_messages(new_state("hi"))[0]["content"]
        self.assertIn("read_file", system)
        self.assertIn("ask_user", system)
        self.assertNotIn("sql_query", system)
        self.assertNotIn("describe_dataset once per view", system)
        system = build_messages(new_state("hi", toolkits=["datalake"]))[0]["content"]
        self.assertIn("sql_query", system)
        self.assertIn("describe_dataset once per view", system)

    def test_a_tool_that_does_not_exist_is_repaired_and_disabled_ones_are_not_listed(self):
        state = new_state("goal")
        state["status"], state["plan"] = "running", ["query"]
        m = FakeModel({"executor": [j(thought="q", tool="drop_tables", input={})] * 4})
        run_one_step(state, m)
        repair = m.calls[1]["messages"][-1]["content"]
        self.assertIn("Unknown tool 'drop_tables'", repair)
        self.assertNotIn("sql_query", repair.split("Use one of:")[1])


class MidRunSuggestion(LakeOff):
    SQL = j(thought="q", tool="sql_query", input={"sql": f"SELECT SUM(amount) AS rwa FROM usage_limits WHERE {STD} AND "
                                                         "as_of_date = '2026-03-26' AND scenario = 'USED' AND "
                                                         "resource = 'RWA'", "save_as": "c"})

    def running(self):
        state = new_state("CET1 for DemoBank")
        state["status"], state["plan"] = "running", ["get the data"]
        return state

    def test_calling_a_tool_from_a_disabled_toolkit_asks_the_user(self):
        state = self.running()
        m = FakeModel({"executor": [self.SQL, self.SQL]})
        ev = run_one_step(state, m)
        self.assertEqual(state["status"], "awaiting_choice")
        self.assertEqual(ev["payload"]["toolkits"], ["datalake"])
        self.assertEqual(len(m.calls), 1)                              # no wasted repair calls
        resolve_pending(state, choice={"toolkits": ["datalake"]})
        self.assertEqual(state["status"], "running")                   # continues, no re-plan
        run_one_step(state, m)
        self.assertEqual(state["status"], "awaiting_approval")          # a lake query is confirmed first
        resolve_pending(state, approved=True)
        self.assertTrue(state["events"][-1]["payload"]["obs"]["ok"])

    def test_bad_arguments_for_an_enabled_tool_are_repaired_not_suggested(self):
        """A real run asked twice to enable 'analysis' (already on) because run_python had data in `tables`."""
        state = self.running()
        bad = j(thought="t", tool="run_python",
                input={"code": "result = 1", "tables": {"raw": [["Common stock", 100]]}})
        good = j(thought="t", tool="run_python", input={"code": "result = 1"})
        m = FakeModel({"executor": [bad, good]})
        ev = run_one_step(state, m)
        self.assertNotEqual(state["status"], "awaiting_choice")
        self.assertEqual(ev["kind"], "approval")                       # repaired in the same step
        repair = m.calls[1]["messages"][-1]["content"]
        self.assertIn("tables maps a name to a sandbox file path", repair)   # the model sees the real error

    def test_declined_toolkit_is_not_offered_again(self):
        state = self.running()
        m = FakeModel({"executor": [self.SQL, self.SQL, self.SQL, self.SQL, self.SQL]})
        run_one_step(state, m)
        resolve_pending(state, choice={})
        self.assertIn("did not enable", state["turns"][-1]["content"])
        run_one_step(state, m)
        self.assertNotEqual(state["status"], "awaiting_choice")

    def test_file_tools_cannot_reach_lake_data(self):
        """The lake is remote: its (synthetic) data lives outside the sandbox, so no file path reaches it."""
        self.assertNotIn(config.SANDBOX_DIR.resolve(), config.datalake_dir().resolve().parents)
        for path in ["datalake/usage_limits.csv", "../mock_lake/data/usage_limits.csv",
                     str(config.datalake_dir() / "usage_limits.csv")]:
            state = self.running()
            run_one_step(state, FakeModel({"executor": [j(thought="r", tool="read_file", input={"path": path})]}))
            if state["status"] == "awaiting_approval":          # reading a file asks first; even approved, it fails
                resolve_pending(state, approved=True)
            self.assertFalse(state["events"][-1]["payload"]["obs"]["ok"], path)


class Commands(LakeOff):
    def test_slash_commands(self):
        out = plugins.parse_commands("Trend for DemoBank /usage_comparison, file inputs/a.csv /nope")
        self.assertEqual(out["skill"], "usage_comparison")
        self.assertEqual(out["toolkits"], ["datalake", "analysis"])
        self.assertEqual(out["unknown"], ["nope"])
        self.assertNotIn("/usage_comparison", out["goal"])
        self.assertIn("inputs/a.csv", out["goal"])
        self.assertEqual(plugins.parse_commands("run /sql_query please")["toolkits"], ["datalake"])
        self.assertEqual(plugins.parse_commands("use /datalake")["toolkits"], ["datalake"])

    def test_catalogue_lists_toolkits_tools_and_skills(self):
        kinds = {(i["kind"], i["name"]) for i in plugins.catalogue()}
        self.assertTrue({("toolkit", "datalake"), ("tool", "sql_query"), ("skill", "usage_comparison")} <= kinds)


class UserPlugins(TempDirs):
    def setUp(self):
        super().setUp()
        self._saved_plugins = config.PLUGINS_DIR
        tmp = Path(tempfile.mkdtemp())
        shutil.copytree(self._saved_plugins / "skills", tmp / "skills")
        (tmp / "tools").mkdir()
        config.PLUGINS_DIR = tmp
        self.tmp = tmp

    def tearDown(self):
        config.PLUGINS_DIR = self._saved_plugins
        plugins.TOOLKITS.pop("greeting", None)
        TOOLS.pop("say_hello", None)
        plugins.load(force=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def test_a_python_file_in_plugins_tools_becomes_a_toolkit(self):
        (self.tmp / "tools" / "greeting.py").write_text(PLUGIN)
        plugins.load(force=True)
        self.assertEqual(plugins.ERRORS, [])
        self.assertEqual(TOOLS["say_hello"].toolkit, "greeting")
        self.assertIn(("toolkit", "greeting"), {(i["kind"], i["name"]) for i in plugins.catalogue()})
        state = new_state("greet Ann", toolkits=["greeting"])
        state["status"], state["plan"] = "running", ["greet"]
        run_one_step(state, FakeModel({"executor": [j(thought="hi", tool="say_hello", input={"name": "Ann"})]}))
        self.assertEqual(state["events"][-1]["payload"]["obs"]["result"], "Hello, Ann!")

    def test_the_template_toolkit_works_when_copied(self):
        template = self._saved_plugins / "tools" / "_example_toolkit.py"
        (self.tmp / "tools" / "fx.py").write_text(template.read_text())
        plugins.load(force=True)
        try:
            self.assertEqual(plugins.ERRORS, [])
            state = new_state("convert", toolkits=["fx"])
            state["status"], state["plan"] = "running", ["convert"]
            run_one_step(state, FakeModel({"executor": [j(thought="c", tool="convert_amount",
                                                          input={"amount": 1000, "source": "USD", "target": "JPY"})]}))
            self.assertIn("151500.00 JPY", state["events"][-1]["payload"]["obs"]["result"])
        finally:
            plugins.TOOLKITS.pop("fx", None)
            TOOLS.pop("convert_amount", None)

    def test_a_broken_plugin_is_reported_not_fatal(self):
        (self.tmp / "tools" / "broken.py").write_text("raise RuntimeError('half written')")
        (self.tmp / "tools" / "nameless.py").write_text("X = 1")
        plugins.load(force=True)
        self.assertTrue(any("broken.py" in e and "half written" in e for e in plugins.ERRORS))
        self.assertTrue(any("nameless.py" in e and "TOOLKIT" in e for e in plugins.ERRORS))
        self.assertIn("files", plugins.TOOLKITS)
