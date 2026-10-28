"""LangGraph prototype (lgagent/): scripted replies, no LLM. Same guarantees as the step machine's tests."""
import json
import shutil

from agent import config
from lgagent.chat_model import TextChatModel
from lgagent.graph import build, sqlite_checkpointer
from lgagent.run import run

from tests.helpers import OK, PLAN, TempDirs, final, j, tool_call

READ = tool_call("read_file", path="work/sample_capital.csv")
WRITE = tool_call("write_file", name="out.md", content="hello")


class Scripted:
    """call(text, effort) for TextChatModel: answers from one queue per role, told apart by the prompt text."""

    ROLES = {"Plan how to achieve the GOAL": "planner", "You are reviewing an agent's FINAL": "reviewer",
             "You check a request before it is shown": "action_review"}

    def __init__(self, executor, planner=(PLAN,), reviewer=(OK,), action_review=()):
        self.queues = {"executor": list(executor), "planner": list(planner), "reviewer": list(reviewer),
                       "action_review": list(action_review)}
        self.calls = []

    def __call__(self, text, effort="none"):
        role = next((r for k, r in self.ROLES.items() if k in text), "executor")
        self.calls.append({"role": role, "effort": effort, "text": text})
        return self.queues[role].pop(0)

    def texts(self, role):
        return [c["text"] for c in self.calls if c["role"] == role]


class LangGraphAgent(TempDirs):
    def setUp(self):
        super().setUp()
        (config.SANDBOX_DIR / "work").mkdir(parents=True, exist_ok=True)
        shutil.copy(config.inputs_dir() / "sample_capital.csv", config.SANDBOX_DIR / "work" / "sample_capital.csv")

    def go(self, script, **kw):
        return run("What is the 2026Q3 CET1 capital?", TextChatModel(call=script), **kw)

    def log(self, state):
        path = config.RUNS_DIR / state["run_id"] / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_tool_approval_review_and_log(self):
        script = Scripted([READ, WRITE, final("2026Q3 CET1 capital is 12850 [ref: read_file-1]")])
        asked = []
        state = self.go(script, approve=lambda v: asked.append(v["name"]) or True)
        self.assertEqual(state["status"], "finished")
        self.assertIn("12850", state["final"])
        self.assertEqual(asked, ["write_file"])                         # only the risky tool asks
        self.assertTrue((config.RUNS_DIR / state["run_id"] / "outputs" / "read_file-1.txt").exists())
        purposes = [e["purpose"] for e in self.log(state) if e["kind"] == "model"]
        self.assertEqual(purposes[0], "planner")
        self.assertIn("reviewer", purposes)
        self.assertEqual({c["effort"] for c in script.calls if c["role"] == "planner"}, {"medium"})
        self.assertEqual({c["effort"] for c in script.calls if c["role"] == "executor"}, {"none"})

    def test_rejection_reason_reaches_the_model(self):
        script = Scripted([WRITE, final("Nothing written.")])
        state = self.go(script, approve=lambda v: "write it to the shared folder instead")
        self.assertEqual(state["status"], "finished")
        self.assertIn("write it to the shared folder instead", script.texts("executor")[-1])
        self.assertFalse((config.SANDBOX_DIR / "outputs").exists() and any((config.SANDBOX_DIR / "outputs").rglob("out.md")))

    def test_uncited_number_goes_back_before_the_reviewer(self):
        script = Scripted([READ, final("CET1 capital is 99999 [ref: read_file-1]"),
                           final("2026Q3 CET1 capital is 12850 [ref: read_file-1]")])
        state = self.go(script)
        self.assertEqual(state["status"], "finished")
        self.assertIn("12850", state["final"])
        self.assertIn("99999", script.texts("executor")[-1])            # the feedback named the number
        self.assertEqual(len(script.texts("reviewer")), 1)              # the reviewer saw only the fixed answer

    def test_question_to_the_user(self):
        script = Scripted([tool_call("ask_user", question="Which quarter?"), final("You said 2026Q3.")])
        state = self.go(script, ask=lambda v: "2026Q3 please")
        self.assertEqual(state["status"], "finished")
        self.assertIn("USER ANSWER: 2026Q3 please", script.texts("executor")[-1])

    def test_bad_reply_is_repaired(self):
        script = Scripted(["I will read the file.", READ, final("2026Q3 CET1 capital is 12850 [ref: read_file-1]")])
        state = self.go(script)
        self.assertEqual(state["status"], "finished")
        self.assertIn("Invalid output", script.texts("executor")[1])

    def test_paused_run_resumes_in_a_new_graph(self):
        script = Scripted([WRITE, final("Written.")])
        model = TextChatModel(call=script)

        class Pause(Exception):
            pass

        def stop(v):
            raise Pause
        with self.assertRaises(Pause):
            run("Write a note", model, approve=stop, thread_id="t1", graph=build(model, sqlite_checkpointer()))
        state = run(None, model, thread_id="t1", graph=build(model, sqlite_checkpointer()))   # e.g. another process
        self.assertEqual(state["status"], "finished")
        self.assertTrue(list(config.SANDBOX_DIR.rglob("out.md")))

    def test_step_limit_ends_the_run(self):
        script = Scripted([READ] * 5)
        state = self.go(script, max_steps=2)
        self.assertEqual(state["status"], "aborted")

    def test_request_check_sends_a_bad_query_back(self):
        bad = tool_call("sql_query", sql="SELECT * FROM usage_limits", save_as="x")
        script = Scripted([bad, final("No query was run.")],
                          action_review=[j(ok=False, issues=["missing filters"], explain="")])
        state = self.go(script, approve=lambda v: self.fail("must not ask the user"))
        self.assertEqual(state["status"], "finished")
        self.assertIn("was not sent", script.texts("executor")[-1])
