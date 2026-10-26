"""Conversations: new instructions continue the same run; titles; the conversation list."""
from agent import config
from agent.backends.fake import FakeModel
from agent.loop import continue_with, rename, run, run_one_step
from agent.memory import list_conversations, load_state, new_state

from tests.helpers import OK, TempDirs, final, j, tool_call

READ = tool_call("read_file", step_done=True, path="inputs/sample_capital.csv")


class Continue(TempDirs):
    def model(self):
        return FakeModel({"planner": [j(plan=["Read the file"], title="Sample capital file"), j(plan=["Answer"])],
                          "executor": [READ, final("It has DemoBank figures [ref: read_file-1]"),
                                       final("Three periods [ref: read_file-1]")],
                          "reviewer": [OK, OK]})

    def test_a_new_instruction_continues_the_same_conversation(self):
        m = self.model()
        state = run("What is in inputs/sample_capital.csv?", m, approve=lambda e: True)   # reading it asks first
        run_id, facts = state["run_id"], list(state["facts"])
        continue_with(state, "How many periods does it cover?")
        self.assertEqual((state["run_id"], state["status"], state["plan"]), (run_id, "planning", []))
        self.assertEqual(state["events"][-1]["kind"], "task")
        run("", m, state=state)
        self.assertEqual(state["status"], "finished")
        self.assertEqual(state["final"], "Three periods [ref: read_file-1]")
        self.assertEqual(state["tasks"][0]["goal"], "What is in inputs/sample_capital.csv?")
        self.assertEqual(state["tasks"][0]["final"], "It has DemoBank figures [ref: read_file-1]")
        self.assertEqual(state["facts"][:len(facts)], facts)                       # earlier facts kept
        planner = [c for c in m.calls if c["purpose"] == "planner"][1]["messages"][-1]["content"]
        self.assertIn("What is in inputs/sample_capital.csv?", planner)            # earlier task visible
        executor = [c for c in m.calls if c["purpose"] == "executor"][-1]["messages"]
        text = "\n".join(x["content"] for x in executor)
        self.assertIn("DemoBank,2026Q3", text)                                     # earlier observation kept
        self.assertIn("How many periods does it cover?", text)
        self.assertEqual(load_state(run_id)["tasks"][0]["status"], "finished")

    def test_only_a_finished_or_stopped_conversation_takes_a_new_instruction(self):
        state = new_state("goal")
        with self.assertRaises(ValueError):
            continue_with(state, "next")

    def test_step_limit_counts_per_instruction(self):
        state = new_state("goal")
        state["status"], state["step_count"] = "finished", config.MAX_STEPS
        continue_with(state, "next")
        state["status"], state["plan"] = "running", ["answer"]
        run_one_step(state, FakeModel({"executor": [final("ok")], "reviewer": [OK]}))
        self.assertEqual(state["status"], "finished")

    def test_attachments_accumulate(self):
        state = new_state("goal", inputs=["inputs/a.csv"])
        state["status"] = "finished"
        continue_with(state, "next", inputs=["inputs/b.md", "inputs/a.csv"])
        self.assertEqual(state["inputs"], ["inputs/a.csv", "inputs/b.md"])


class Titles(TempDirs):
    def test_planner_names_the_conversation_once(self):
        m = FakeModel({"planner": [j(plan=["Read"], title="Sample capital file"), j(plan=["x"], title="Other")],
                       "executor": [final("a"), final("b")], "reviewer": [OK, OK]})
        state = run("What is in the file?", m)
        self.assertEqual(state["title"], "Sample capital file")
        continue_with(state, "and then?")
        run("", m, state=state)
        self.assertEqual(state["title"], "Sample capital file")

    def test_title_falls_back_to_the_first_instruction(self):
        state = new_state("Show the CET1 ratio trend for DemoBank over the last six quarters please", skill="usage_overview")
        run_one_step(state, FakeModel({}))
        self.assertEqual(state["title"], "Show the CET1 ratio trend for DemoBank over the…")

    def test_rename_and_list(self):
        a = new_state("first")
        rename(a, "  Capital review  ")
        b = new_state("second")
        rename(b, "")                                           # empty names are ignored
        convs = list_conversations()
        self.assertEqual(convs[0]["run_id"], b["run_id"])        # newest first
        self.assertEqual(load_state(a["run_id"])["title"], "Capital review")
        self.assertEqual({c["run_id"]: c["title"] for c in convs}[b["run_id"]], "second")


class RetitleAtAnswer(TempDirs):
    def test_the_answer_renames_the_conversation_unless_the_user_named_it(self):
        m = FakeModel({"planner": [j(plan=["Answer"], title="First guess")],
                       "executor": [j(thought="t", final="Done.", title="RWA breach sources FY2025")],
                       "reviewer": [OK]})
        state = run("Why did limits break?", m)
        self.assertEqual(state["title"], "RWA breach sources FY2025")
        rename(state, "My breach review")
        m2 = FakeModel({"planner": [j(plan=["Answer"])], "executor": [j(thought="t", final="Ok.", title="Other")],
                        "reviewer": [OK]})
        continue_with(state, "And leverage?")
        run("", m2, state=state)
        self.assertEqual(state["title"], "My breach review")                # the user's name is kept


class AnswerTiming(TempDirs):
    def test_time_to_answer_splits_agent_work_from_waiting_for_the_user(self):
        from agent.loop import answer_timing, describe_timing
        state = new_state("g")
        state["created"] = "2026-10-25T10:00:00"
        state["events"] = [
            {"kind": "plan", "ts": "2026-10-25T10:00:05", "payload": {}},
            {"kind": "approval", "ts": "2026-10-25T10:00:20", "payload": {"tool": "sql_query"}},
            {"kind": "user", "ts": "2026-10-25T10:01:20", "payload": {"approved": True, "tool": "sql_query"}},
            {"kind": "final", "ts": "2026-10-25T10:02:05", "payload": {"text": "x"}},
            {"kind": "task", "ts": "2026-10-25T10:05:00", "payload": {"goal": "next"}},
            {"kind": "final", "ts": "2026-10-25T10:05:30", "payload": {"text": "y"}}]
        t = answer_timing(state)
        self.assertEqual(t["2026-10-25T10:02:05"], {"elapsed": 125.0, "waiting": 60.0, "steps": 2})
        self.assertEqual(describe_timing(t["2026-10-25T10:02:05"]),
                         "Answered in 2 min 05 s (agent 1 min 05 s, waiting for you 1 min 00 s) · 2 steps")
        self.assertEqual(describe_timing(t["2026-10-25T10:05:30"]), "Answered in 30 s · 0 steps")
