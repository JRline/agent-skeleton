"""Toolkits, plugins, /commands and per-session tool selection."""
import shutil
import tempfile
from pathlib import Path

from agent.graph import first_prompt
from agent import config, extensions
from agent.backends.fake import FakeModel
from agent.loop import resolve_pending, run_one_step
from agent.memory import new_state
from agent.tools import TOOLS

from tests.helpers import STD, LakeOff, TempDirs, j, first_step

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
        extensions.load()
        self.assertEqual({"files", "docs", "analysis", "datalake"}, set(extensions.TOOLKITS))
        self.assertEqual(set(extensions.basic_toolkits()), {"files", "docs", "analysis", "datalake"})
        self.assertEqual(TOOLS["sql_query"].toolkit, "datalake")
        self.assertEqual(TOOLS["run_python"].toolkit, "analysis")
        self.assertEqual(TOOLS["ask_user"].toolkit, "core")
        for removed in ("calc", "capital_ratio", "add_column", "aggregate", "query_table"):
            self.assertNotIn(removed, TOOLS)

    def test_new_sessions_start_with_basic_toolkits(self):
        self.assertEqual(set(new_state("hi")["toolkits"]), {"files", "docs", "analysis", "datalake"})


class Toolkits(LakeOff):
    def test_prompt_shows_only_enabled_tools_and_their_guidance(self):
        system = first_prompt(new_state("hi"))[0]["content"]
        tools = system.split("## Tools", 1)[1]
        self.assertIn("read_file", tools)
        self.assertIn("ask_user", tools)
        self.assertNotIn("sql_query", tools)                          # no full docs until enabled
        self.assertNotIn("describe_dataset once per dataset", system)
        others = system.split("## Other toolkits", 1)[1].split("## Tools", 1)[0]
        self.assertIn("- datalake: ", others)                          # one line, so the agent can ask for it
        self.assertIn("Tools: list_datasets, describe_dataset, sql_query.", others)
        system = first_prompt(new_state("hi", toolkits=["datalake"]))[0]["content"]
        self.assertIn("sql_query", system)
        self.assertIn("describe_dataset once per dataset", system)

    def test_a_tool_that_does_not_exist_is_repaired_and_disabled_ones_are_not_listed(self):
        state = new_state("goal")
        m = FakeModel({"executor": [j(thought="q", tool="drop_tables", input={})] * 4})
        first_step(state, m, plan=["query"])
        repair = m.calls[2]["messages"][-1]["content"]
        self.assertIn("Unknown tool 'drop_tables'", repair)
        self.assertNotIn("sql_query", repair.split("Use one of:")[1])


class MidRunSuggestion(LakeOff):
    SQL = j(thought="q", tool="sql_query", input={"sql": f"SELECT SUM(amount) AS rwa FROM usage_limits WHERE {STD} AND "
                                                         "as_of_date = '2026-03-26' AND scenario = 'USED' AND "
                                                         "resource = 'RWA'", "save_as": "c"})

    def running(self):
        state = new_state("CET1 for DemoBank")
        return state

    def test_calling_a_tool_from_a_disabled_toolkit_asks_the_user(self):
        state = self.running()
        m = FakeModel({"executor": [self.SQL, self.SQL]})
        ev = first_step(state, m, plan=["get the data"])
        self.assertEqual(state["status"], "awaiting_choice")
        self.assertEqual(ev["payload"]["toolkits"], ["datalake"])
        self.assertEqual(len(m.calls), 2)                              # plan, then no wasted repair calls
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
        ev = first_step(state, m)
        self.assertNotEqual(state["status"], "awaiting_choice")
        self.assertEqual(ev["kind"], "approval")                       # repaired in the same step
        repair = [c for c in m.calls if c["purpose"] == "executor"][1]["messages"][-1]["content"]
        self.assertIn("tables maps a name to a sandbox file path", repair)   # the model sees the real error

    def test_declined_toolkit_is_not_offered_again(self):
        state = self.running()
        m = FakeModel({"executor": [self.SQL, self.SQL, self.SQL, self.SQL, self.SQL]})
        first_step(state, m)
        resolve_pending(state, choice={})
        run_one_step(state, m)
        self.assertIn("did not enable", [c for c in m.calls if c["purpose"] == "executor"][1]["messages"][-1]["content"])
        self.assertNotEqual(state["status"], "awaiting_choice")

    def test_file_tools_cannot_reach_lake_data(self):
        """The lake is remote: its (synthetic) data lives outside the sandbox, so no file path reaches it."""
        self.assertNotIn(config.SANDBOX_DIR.resolve(), (config.connectors_dir() / "sample_lake" / "data").resolve().parents)
        for path in ["datalake/usage_limits.csv", "../extensions/connectors/sample_lake/data/usage_limits.csv",
                     str((config.connectors_dir() / "sample_lake" / "data") / "usage_limits.csv")]:
            state = self.running()
            first_step(state, FakeModel({"executor": [j(thought="r", tool="read_file", input={"path": path})]}))
            if state["status"] == "awaiting_approval":          # reading a file asks first; even approved, it fails
                resolve_pending(state, approved=True)
            self.assertFalse(state["events"][-1]["payload"]["obs"]["ok"], path)


class Commands(LakeOff):
    def test_slash_commands(self):
        out = extensions.parse_commands("Trend for DemoBank /usage_comparison, file inputs/a.csv /nope")
        self.assertEqual(out["skill"], "usage_comparison")
        self.assertEqual(out["toolkits"], ["datalake", "analysis"])
        self.assertEqual(out["unknown"], ["nope"])
        self.assertNotIn("/usage_comparison", out["goal"])
        self.assertIn("inputs/a.csv", out["goal"])
        self.assertEqual(extensions.parse_commands("run /sql_query please")["toolkits"], ["datalake"])
        self.assertEqual(extensions.parse_commands("use /datalake")["toolkits"], ["datalake"])

    def test_catalogue_lists_toolkits_tools_and_skills(self):
        kinds = {(i["kind"], i["name"]) for i in extensions.catalogue()}
        self.assertTrue({("toolkit", "datalake"), ("tool", "sql_query"), ("skill", "usage_comparison")} <= kinds)


class UserToolkits(TempDirs):
    def setUp(self):
        super().setUp()
        self._saved_plugins, self._saved_toolkits = config.EXTENSIONS_DIR, config.TOOLKITS_DIR
        tmp = Path(tempfile.mkdtemp())
        shutil.copytree(self._saved_plugins / "skills", tmp / "skills")
        (tmp / "toolkits").mkdir()
        config.EXTENSIONS_DIR, config.TOOLKITS_DIR = tmp, tmp / "toolkits"
        self.tmp = tmp

    def tearDown(self):
        config.EXTENSIONS_DIR, config.TOOLKITS_DIR = self._saved_plugins, self._saved_toolkits
        extensions.TOOLKITS.pop("greeting", None)
        TOOLS.pop("say_hello", None)
        extensions.load(force=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def test_a_python_file_in_extensions_toolkits_becomes_a_toolkit(self):
        (self.tmp / "toolkits" / "greeting.py").write_text(PLUGIN)
        extensions.load(force=True)
        self.assertEqual(extensions.ERRORS, [])
        self.assertEqual(TOOLS["say_hello"].toolkit, "greeting")
        self.assertIn(("toolkit", "greeting"), {(i["kind"], i["name"]) for i in extensions.catalogue()})
        state = new_state("greet Ann", toolkits=["greeting"])
        first_step(state, FakeModel({"executor": [j(thought="hi", tool="say_hello", input={"name": "Ann"})]}), plan=["greet"])
        self.assertEqual(state["events"][-1]["payload"]["obs"]["result"], "Hello, Ann!")

    def test_the_template_toolkit_works_when_copied(self):
        template = self._saved_plugins / "toolkits" / "_template.py"
        (self.tmp / "toolkits" / "fx.py").write_text(template.read_text())
        extensions.load(force=True)
        try:
            self.assertEqual(extensions.ERRORS, [])
            state = new_state("convert", toolkits=["fx"])
            first_step(state, FakeModel({"executor": [j(thought="c", tool="convert_amount",
                                                          input={"amount": 1000, "source": "USD", "target": "JPY"})]}))
            self.assertIn("151500.00 JPY", state["events"][-1]["payload"]["obs"]["result"])
        finally:
            extensions.TOOLKITS.pop("fx", None)
            TOOLS.pop("convert_amount", None)

    def test_a_broken_plugin_is_reported_not_fatal(self):
        (self.tmp / "toolkits" / "broken.py").write_text("raise RuntimeError('half written')")
        (self.tmp / "toolkits" / "nameless.py").write_text("X = 1")
        extensions.load(force=True)
        self.assertTrue(any("broken.py" in e and "half written" in e for e in extensions.ERRORS))
        self.assertTrue(any("nameless.py" in e and "TOOLKIT" in e for e in extensions.ERRORS))
        self.assertIn("files", extensions.TOOLKITS)
