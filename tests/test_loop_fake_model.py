"""Step-machine tests with scripted FakeModel replies (no LLM)."""
import json
import shutil
import types
import unittest

from agent import config
from agent.adapter import ModuleChatModel
from agent.backends.fake import DemoFakeModel, FakeModel
from agent.loop import abort, load_state, resolve_pending, run, run_one_step
from agent.memory import build_messages, new_state, read_log

from tests.helpers import OK, PLAN, TempDirs, final, j, tool_call

# A read that needs no approval: the agent's own scratch table (reading the user's inputs/ files asks first).
READ = tool_call("read_file", step_done=True, path="work/sample_capital.csv")
LOOKUP = tool_call("recall", step_done=True, query="2026Q3")
WRITE = tool_call("write_file", name="out.md", content="hello")


class LoopCase(TempDirs):
    def setUp(self):
        super().setUp()
        (config.SANDBOX_DIR / "work").mkdir(parents=True, exist_ok=True)
        shutil.copy(config.inputs_dir() / "sample_capital.csv", config.SANDBOX_DIR / "work" / "sample_capital.csv")


def model(executor, reviewer=(OK,), planner=(PLAN,), summariser=("- summary",)):
    return FakeModel({"planner": list(planner), "executor": list(executor),
                      "reviewer": list(reviewer), "summariser": list(summariser)})


class HappyPath(LoopCase):
    def test_runs_to_finished_with_approval(self):
        m = model([READ, LOOKUP, WRITE, final("2026Q3 CET1 capital is 12850 [ref: read_file-1]")])
        events = []
        state = run("goal", m, inputs=["inputs/sample_capital.csv"], on_event=events.append,
                    approve=lambda e: True)
        self.assertEqual(state["status"], "finished")
        self.assertIn("12850", state["final"])
        self.assertEqual([e["kind"] for e in events],
                         ["plan", "tool", "tool", "approval", "user", "final"])
        self.assertTrue((config.outputs_dir(state["run_id"]) / "out.md").exists())
        self.assertEqual(state["step"], 2)                 # two steps marked done
        self.assertEqual(len(state["facts"]), 3)

    def test_effort_policy(self):
        m = model([READ, final()])
        run("goal", m)
        self.assertEqual(m.efforts("planner"), ["medium"])
        self.assertEqual(m.efforts("executor"), ["none", "none"])
        self.assertEqual(m.efforts("reviewer"), ["medium"])

    def test_everything_is_logged_and_persisted(self):
        state = run("goal", model([READ, final()]))
        kinds = [e["kind"] for e in read_log(state)]
        self.assertEqual(kinds.count("model"), 4)          # planner, 2 executor, reviewer
        self.assertIn("tool", kinds)
        self.assertIn("review", kinds)
        self.assertEqual(load_state(state["run_id"])["status"], "finished")

    def test_model_name_is_logged(self):
        state = run("goal", model([READ, final()]))
        self.assertEqual({e["model"] for e in read_log(state) if e["kind"] == "model"}, {"fake"})

    def test_module_backend_reports_model_per_effort(self):
        with_name = types.SimpleNamespace(call=lambda text, effort: "",
                                          model_name=lambda effort: f"small-{effort}")
        without = types.SimpleNamespace(call=lambda text, effort: "")
        self.assertEqual(ModuleChatModel(with_name, "ollama").model_id("medium"), "small-medium")
        self.assertEqual(ModuleChatModel(without, "prod").model_id("none"), "prod")

    def test_state_block_is_last_and_observation_fed_back(self):
        m = model([READ, final()])
        run("goal", m)
        second_call = [c for c in m.calls if c["purpose"] == "executor"][1]["messages"]
        self.assertEqual(second_call[0]["role"], "system")
        self.assertIn("OBSERVATION:", second_call[-1]["content"])
        self.assertTrue(second_call[-1]["content"].rstrip().endswith("Reply with one JSON object."))


class HumanInTheLoop(LoopCase):
    def test_rejection_is_fed_back_and_file_not_written(self):
        m = model([WRITE, final("Skipped writing.")])
        state = run("goal", m, approve=lambda e: False)
        self.assertEqual(state["status"], "finished")
        self.assertFalse(config.outputs_dir(state["run_id"]).exists())
        self.assertIn("User rejected write_file", json.dumps(state["turns"]))

    def test_ask_user_pauses_and_resumes(self):
        m = model([tool_call("ask_user", question="Which period?"), final("Using 2026Q3.")])
        state = new_state("goal")
        run_one_step(state, m)                             # plan
        ev = run_one_step(state, m)                        # ask_user
        self.assertEqual((ev["kind"], state["status"]), ("ask_user", "awaiting_user"))
        ev = run_one_step(state, m)                        # waiting: no model call
        self.assertEqual(ev["kind"], "awaiting_user")
        self.assertEqual(len(m.efforts("executor")), 1)
        with self.assertRaises(ValueError):
            resolve_pending(state, answer="  ")
        resolve_pending(state, answer="2026Q3")
        self.assertEqual(state["status"], "running")
        self.assertIn("2026Q3", state["turns"][-1]["content"])
        run_one_step(state, m)
        self.assertEqual(state["status"], "finished")

    def test_resume_from_disk_while_awaiting_approval(self):
        m = model([WRITE, final()])
        state = new_state("goal")
        run_one_step(state, m)
        run_one_step(state, m)
        self.assertEqual(state["status"], "awaiting_approval")
        restored = load_state(state["run_id"])             # e.g. after a browser refresh or crash
        self.assertEqual(restored["pending"]["tool"], "write_file")
        resolve_pending(restored, approved=True)
        final_state = run("", m, state=restored)
        self.assertEqual(final_state["status"], "finished")
        self.assertTrue((config.outputs_dir(state["run_id"]) / "out.md").exists())


class Recovery(LoopCase):
    def test_bad_json_is_repaired(self):
        m = model(["I'll read the file.", READ, final()])
        events = []
        run("goal", m, on_event=events.append)
        tool_ev = [e for e in events if e["kind"] == "tool"][0]
        self.assertEqual(tool_ev["payload"]["attempts"], 2)
        self.assertEqual(m.efforts("executor")[:2], ["none", "none"])

    def test_tool_error_raises_effort_on_next_step(self):
        bad = tool_call("read_file", path="work/missing.csv")
        m = model([bad, READ, final()])
        run("goal", m)
        self.assertEqual(m.efforts("executor"), ["none", "medium", "none"])

    def test_repeated_action_not_rerun_then_aborts(self):
        m = model([READ, READ, READ])
        state = run("goal", m)
        self.assertEqual(state["status"], "aborted")
        self.assertEqual(sum(1 for e in state["events"] if e["kind"] == "tool"), 1)
        self.assertIn("Repeated", state["events"][-1]["payload"]["reason"])

    def test_reviewer_rejects_then_accepts(self):
        m = model([READ, final("no refs"), final("Ratio [ref: read_file-1]")],
                  reviewer=[j(ok=False, issues=["Numbers lack refs"]), OK])
        state = run("goal", m)
        self.assertEqual(state["status"], "finished")
        self.assertIn("ref", state["final"])
        self.assertIn("Numbers lack refs", json.dumps(state["turns"]))

    def test_reviewer_rejections_capped_with_warning(self):
        no = j(ok=False, issues=["x"])
        rounds = config.MAX_REVIEW_ROUNDS + 1
        m = model([final()] * rounds, reviewer=[no] * rounds)
        state = run("goal", m)
        self.assertEqual(state["status"], "finished")
        self.assertTrue(state["warnings"])

    def test_unparseable_replies_abort(self):
        m = model(["nope"] * 8)
        state = run("goal", m)
        self.assertEqual(state["status"], "aborted")

    def test_step_limit(self):
        old = config.MAX_STEPS
        config.MAX_STEPS = 2
        try:
            m = model([READ, LOOKUP, final()])
            state = run("goal", m)
        finally:
            config.MAX_STEPS = old
        self.assertEqual(state["status"], "aborted")
        self.assertIn("step limit", state["events"][-1]["payload"]["reason"])

    def test_planner_failure_falls_back_to_goal(self):
        m = model([final()], planner=["x"] * 4)
        state = run("my goal", m)
        self.assertEqual(state["plan"], ["my goal"])
        self.assertTrue(state["warnings"])


def unreachable(messages):
    raise ConnectionError("Connection refused")


class ModelFailure(LoopCase):
    def test_planner_failure_is_retryable_event(self):
        m = model([READ, final()], planner=(unreachable, PLAN))
        state = new_state("goal")
        ev = run_one_step(state, m)
        self.assertEqual(ev["kind"], "model_error")
        self.assertIn("Connection refused", ev["payload"]["error"])
        self.assertEqual(state["status"], "planning")      # nothing advanced
        self.assertEqual(load_state(state["run_id"])["events"][-1]["kind"], "model_error")
        run_one_step(state, m)                              # retry succeeds
        self.assertEqual(state["status"], "running")

    def test_failed_step_is_rolled_back(self):
        m = model([READ, final(), final()], reviewer=(unreachable, OK))
        state = new_state("goal")
        while not state["events"] or state["events"][-1]["kind"] != "model_error":
            if state["status"] == "awaiting_approval":         # reading an input file asks first
                resolve_pending(state, approved=True)
            else:
                run_one_step(state, m)
        self.assertEqual(state["step_count"], 1)            # the final-answer step did not count
        self.assertEqual(state["status"], "running")
        run_one_step(state, m)                              # retry reruns executor + reviewer
        self.assertEqual(state["status"], "finished")
        self.assertIn("model_error", [e["kind"] for e in read_log(state)])

    def test_run_stops_instead_of_looping(self):
        state = run("goal", model([READ, final()], planner=(unreachable, unreachable)))
        self.assertEqual(state["status"], "planning")
        self.assertEqual(state["events"][-1]["kind"], "model_error")


class Planner(LoopCase):
    def test_copied_placeholder_plan_falls_back_to_goal(self):
        state = new_state("What is CET1?")
        run_one_step(state, model([final()], planner=(j(plan=["<step>", "<step>"]),)))
        self.assertEqual(state["plan"], ["What is CET1?"])

    def test_plan_of_bare_tool_names_falls_back_to_goal(self):
        state = new_state("What is in this file?")
        run_one_step(state, model([final()], planner=(j(plan=["read_file", "recall", "search_docs"]),)))
        self.assertEqual(state["plan"], ["What is in this file?"])

    def test_repeating_a_successful_call_moves_the_plan_on(self):
        unfinished = tool_call("read_file", path="work/sample_capital.csv")        # step_done false
        m = model([unfinished, unfinished, final("The file has 3 rows [ref: read_file-1]")],
                  planner=(j(plan=["Read the file"]),))
        state = run("What is in the file?", m)
        self.assertEqual(state["status"], "finished")
        third = [c for c in m.calls if c["purpose"] == "executor"][2]["messages"][-1]["content"]
        self.assertIn("All plan steps are done", third)

    def test_one_step_plan_is_kept(self):
        state = new_state("What is CET1?")
        run_one_step(state, model([final()], planner=(j(plan=["Answer the question"]),)))
        self.assertEqual(state["plan"], ["Answer the question"])


class CitationCheck(LoopCase):
    def test_invented_number_goes_back_without_a_reviewer_call(self):
        m = model([READ, final("2026Q3 CET1 capital is 12850 and the ratio is 99.12% [ref: read_file-1]"),
                   final("2026Q3 CET1 capital is 12,850 [ref: read_file-1]")])
        state = run("goal", m)
        self.assertEqual(state["status"], "finished")
        self.assertEqual(len(m.efforts("reviewer")), 1)                       # only for the second answer
        feedback = [t["content"] for t in state["turns"] if t["content"].startswith("FEEDBACK")]
        self.assertIn("99.12", feedback[0])
        self.assertTrue(any(e["kind"] == "review" and e.get("check") == "citations" for e in read_log(state)))

    def test_rounding_dates_periods_and_the_users_numbers_are_fine(self):
        from agent.loop import uncited_numbers
        state = new_state("Use a 7% requirement for DemoBank")
        (config.RUNS_DIR / state["run_id"] / "outputs").mkdir(parents=True, exist_ok=True)
        (config.RUNS_DIR / state["run_id"] / "outputs" / "x-1.txt").write_text('{"ratio": 12.660098522, "rwa": 101500}')
        text = ("On 2026-09-30 (2026Q3) the ratio was 12.66% (12.7% rounded) on RWA of 101,500, against the 7% "
                "requirement, over 6 quarters [ref: x-1]. 1. First point")
        self.assertEqual(uncited_numbers(state, text), [])
        self.assertEqual(uncited_numbers(state, "Headroom is 5.71 pp [ref: x-1]"), ["5.71"])

    def test_numbers_in_previews_and_user_answers_count(self):
        """The stored output of read_file is the raw file; its shape line (48 rows) is only in the preview."""
        from agent.loop import uncited_numbers
        state = new_state("what is in this file")
        state["events"] += [
            {"kind": "tool", "step": 1, "payload": {"obs": {"ok": True, "ref": "read_file-1",
                                                            "result": "shape=(48, 5); columns=['a']"}}},
            {"kind": "user", "step": 2, "payload": {"answer": "use a 250 threshold"}}]
        self.assertEqual(uncited_numbers(state, "The file has 48 rows [ref: read_file-1], threshold 250."), [])


class ReviewerSeesCitedOutputs(LoopCase):
    def test_full_output_of_cited_refs_is_given_to_the_reviewer(self):
        rows = "\n".join(f"Bank{i},2026Q3,operational,{1000 + i}" for i in range(60))
        (config.SANDBOX_DIR / "work" / "rwa.csv").write_text("entity,period,risk_type,rwa\n" + rows + "\n")
        read_work = tool_call("read_file", step_done=True, path="work/rwa.csv")
        m = model([read_work, final("Bank59 2026Q3 operational RWA [ref: read_file-1]")],
                  planner=(j(plan=["Read"]),))
        state = run("What is in the file?", m)
        self.assertNotIn("Bank59,2026Q3,operational", "".join(state["facts"]))   # cut from the preview
        reviewer = [c for c in m.calls if c["purpose"] == "reviewer"][0]["messages"][-1]["content"]
        self.assertIn("Bank59,2026Q3,operational", reviewer)
        self.assertIn('input: {"path": "work/rwa.csv"}', reviewer)   # what produced it


class Finisher(LoopCase):
    STUCK = tool_call("read_file", path="work/sample_capital.csv")        # step_done false, repeated

    def _model(self, finisher):
        return FakeModel({"planner": [j(plan=["Read the file"])], "executor": [self.STUCK] * 3,
                          "finisher": list(finisher), "reviewer": [OK]})

    def test_stuck_model_gets_one_simple_final_answer_call(self):
        m = self._model([j(final="DemoBank data for 3 periods [ref: read_file-1]")])
        state = run("What is in the file?", m)
        self.assertEqual(state["status"], "finished")
        self.assertIn("3 periods", state["final"])
        self.assertEqual(m.efforts("finisher"), ["medium"])
        prompt = [c for c in m.calls if c["purpose"] == "finisher"][0]["messages"][-1]["content"]
        self.assertIn("[ref: read_file-1]", prompt)                  # facts with refs are given
        self.assertEqual(state["events"][-1]["payload"].get("via"), "finisher")

    def test_unusable_finisher_reply_still_aborts(self):
        state = run("What is in the file?", self._model(["no json"] * 4))
        self.assertEqual(state["status"], "aborted")
        self.assertIn("Repeated", state["events"][-1]["payload"]["reason"])

    def test_not_used_while_plan_steps_remain(self):
        m = model([READ, READ, READ])                                  # 3-step plan, step 2 not done
        run("goal", m)
        self.assertEqual(m.efforts("finisher"), [])


class StopDuringModelCall(LoopCase):
    """Stop is clicked in the UI while a slow model call is still running (same state dict)."""

    def _stopped_while_calling(self, reply_or_error):
        def reply(messages):
            abort(state, "Stopped by user.")
            if isinstance(reply_or_error, Exception):
                raise reply_or_error
            return reply_or_error
        state = new_state("goal")
        state["status"], state["plan"] = "running", ["read"]
        run_one_step(state, FakeModel({"executor": [reply]}))
        return state

    def test_reply_after_stop_is_not_acted_on(self):
        state = self._stopped_while_calling(READ)
        self.assertEqual(state["status"], "aborted")
        self.assertNotIn("tool", [e["kind"] for e in read_log(state)])
        self.assertEqual(state["step_count"], 0)

    def test_cancelled_call_does_not_undo_the_stop(self):
        state = self._stopped_while_calling(ConnectionError("cancelled"))
        self.assertEqual(state["status"], "aborted")
        self.assertEqual(load_state(state["run_id"])["status"], "aborted")


class Memory(LoopCase):
    def test_old_turns_are_summarised(self):
        old = config.RECENT_TURNS
        config.RECENT_TURNS = 2
        try:
            calcs = [tool_call("recall", query=f"term {i}") for i in range(5)]
            m = model(calcs + [final()], summariser=["- earlier calcs done"] * 5)
            state = run("goal", m)
        finally:
            config.RECENT_TURNS = old
        self.assertEqual(state["status"], "finished")
        self.assertTrue(m.efforts("summariser"))
        self.assertEqual(set(m.efforts("summariser")), {"none"})
        self.assertIn("earlier calcs", state["summary"])
        self.assertLessEqual(len(state["turns"]), 2 * 2 + 2)

    def test_prompt_contains_rendered_tools(self):
        state = new_state("goal")
        msgs = build_messages(state)
        self.assertIn("run_python", msgs[0]["content"])
        self.assertNotIn("{{tools}}", msgs[0]["content"])


class Demo(LoopCase):
    def test_demo_fake_model_runs_end_to_end(self):
        state = run("demo", DemoFakeModel(), inputs=["inputs/sample_capital.csv"], approve=lambda e: True)
        self.assertEqual(state["status"], "finished")
        self.assertIn("12.66%", state["final"])


if __name__ == "__main__":
    unittest.main()


class ReviewerSeesShownTables(LoopCase):
    def test_tables_listed_with_the_answer_are_given_to_the_reviewer(self):
        from agent.loop import review
        state = new_state("g")
        config.work_dir(state["run_id"]).mkdir(parents=True, exist_ok=True)
        (config.work_dir(state["run_id"]) / "by_division.csv").write_text("division,rwa\nMarkets,66363\n")
        m = FakeModel({"reviewer": [OK]})
        review(state, m, "See the grid below [ref: x-1].", tables=[f"work/{state['run_id']}/by_division.csv"])
        prompt = m.calls[0]["messages"][-1]["content"]
        self.assertIn("shown to the user under the answer", prompt)
        self.assertIn("Markets,66363", prompt)


class CsvEvidence(LoopCase):
    def test_values_in_csv_outputs_count_even_next_to_other_numbers(self):
        """A real eval run flagged 148,898: the stored CSV row 'Credit risk (excl. CCR),4,148898' was read as 4,148."""
        from agent.loop import uncited_numbers
        state = new_state("RWA by risk type")
        out = config.RUNS_DIR / state["run_id"] / "outputs"
        out.mkdir(parents=True, exist_ok=True)
        (out / "sql_query-1.txt").write_text("rows=2\nrisk,leaves,rwa\nCredit risk (excl. CCR),4,148898\n"
                                             "Amounts below thresholds (250% RW),1,857\n")
        self.assertEqual(uncited_numbers(state, "Credit 148,898 and thresholds 857 [ref: sql_query-1]"), [])
        self.assertEqual(uncited_numbers(state, "Credit 148,899 [ref: sql_query-1]"), ["148,899"])
