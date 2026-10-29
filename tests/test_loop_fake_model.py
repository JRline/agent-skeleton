"""The LangGraph agent driven step by step with scripted FakeModel replies (no LLM)."""
import json
import shutil
import types
import unittest

from agent import config
from agent.adapter import ModuleChatModel
from agent.backends.fake import DemoFakeModel, FakeModel
from agent.loop import abort, load_state, resolve_pending, run, run_one_step
from agent.memory import new_state, read_log

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


def executor_prompts(m) -> list[list[dict]]:
    return [c["messages"] for c in m.calls if c["purpose"] == "executor"]


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
        self.assertTrue((config.RUNS_DIR / "checkpoints.sqlite").exists())    # the graph's own checkpoints

    def test_model_name_purpose_and_latency_are_logged(self):
        state = run("goal", model([READ, final()]))
        calls = [e for e in read_log(state) if e["kind"] == "model"]
        self.assertEqual({e["model"] for e in calls}, {"fake"})
        self.assertEqual([e["purpose"] for e in calls], ["planner", "executor", "executor", "reviewer"])
        self.assertTrue(all("latency_ms" in e and e["prompt_tokens"] > 0 for e in calls))

    def test_module_backend_reports_model_per_effort(self):
        with_name = types.SimpleNamespace(call=lambda text, effort: "",
                                          model_name=lambda effort: f"small-{effort}")
        without = types.SimpleNamespace(call=lambda text, effort: "")
        self.assertEqual(ModuleChatModel(with_name, "ollama").model_id("medium"), "small-medium")
        self.assertEqual(ModuleChatModel(without, "prod").model_id("none"), "prod")

    def test_prompt_has_rules_tools_protocol_and_the_observation(self):
        m = model([READ, final()])
        run("goal", m)
        second = executor_prompts(m)[1]
        self.assertEqual(second[0]["role"], "system")
        for part in ("## Toolkit rules", "## Tools", "run_python", "## Reply protocol", "## Plan"):
            self.assertIn(part, second[0]["content"])
        self.assertEqual(second[1]["content"], "GOAL: goal")
        self.assertTrue(second[-1]["content"].startswith("OBSERVATION: "))
        self.assertIn('"read_file"', second[-2]["content"])          # its own call, as the JSON it wrote


class HumanInTheLoop(LoopCase):
    def test_rejection_reason_is_fed_back_and_file_not_written(self):
        m = model([WRITE, final("Skipped writing.")])
        state = run("goal", m, approve=lambda e: "write it to the shared folder instead")
        self.assertEqual(state["status"], "finished")
        self.assertFalse(config.outputs_dir(state["run_id"]).exists())
        self.assertIn("write it to the shared folder instead", executor_prompts(m)[-1][-1]["content"])
        self.assertIn("USER REJECTED write_file because: write it to the shared folder instead", state["facts"])
        user = [e for e in state["events"] if e["kind"] == "user"][0]["payload"]
        self.assertEqual((user["approved"], user["reason"]), (False, "write it to the shared folder instead"))

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
        run_one_step(state, m)                             # the answer goes back to the model
        run_one_step(state, m)
        self.assertEqual(state["status"], "finished")
        self.assertIn("2026Q3", executor_prompts(m)[-1][-1]["content"])
        self.assertTrue(any(f.startswith("USER ANSWER to: Which period?") for f in state["facts"]))

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

    def test_resume_in_a_new_process(self):
        """Another process has no compiled graph yet: the checkpoint on disk is enough."""
        from agent import loop
        m = model([WRITE, final()])
        state = new_state("goal")
        run_one_step(state, m)
        run_one_step(state, m)
        loop._GRAPHS.clear()
        loop._ACTIVE.clear()
        restored = load_state(state["run_id"])
        resolve_pending(restored, approved=True)
        self.assertEqual(run("", model([final()], planner=()), state=restored)["status"], "finished")
        self.assertTrue((config.outputs_dir(state["run_id"]) / "out.md").exists())


class Recovery(LoopCase):
    def test_bad_json_is_repaired(self):
        m = model(["I'll read the file.", READ, final()])
        events = []
        run("goal", m, on_event=events.append)
        tool_ev = [e for e in events if e["kind"] == "tool"][0]
        self.assertEqual(tool_ev["payload"]["attempts"], 2)
        self.assertEqual(m.efforts("executor")[:2], ["none", "none"])
        self.assertIn("Invalid output", executor_prompts(m)[1][-1]["content"])

    def test_tool_error_raises_effort_on_next_step(self):
        bad = tool_call("read_file", path="work/missing.csv")
        m = model([bad, READ, final()])
        run("goal", m)
        self.assertEqual(m.efforts("executor"), ["none", "medium", "none"])

    def test_repeated_action_not_rerun_then_aborts(self):
        m = FakeModel({"planner": [PLAN], "executor": [READ] * 3, "finisher": ["no json"] * 4})
        state = run("goal", m)
        self.assertEqual(state["status"], "aborted")
        self.assertEqual(sum(1 for e in state["events"] if e["kind"] == "tool"
                             and e["payload"]["obs"].get("ok")), 1)
        self.assertIn("Repeated", state["events"][-1]["payload"]["reason"])

    def test_reviewer_rejects_then_accepts(self):
        m = model([READ, final("no refs"), final("Ratio [ref: read_file-1]")],
                  reviewer=[j(ok=False, issues=["Numbers lack refs"]), OK])
        state = run("goal", m)
        self.assertEqual(state["status"], "finished")
        self.assertIn("ref", state["final"])
        self.assertIn("Numbers lack refs", executor_prompts(m)[-1][-1]["content"])
        self.assertEqual([e["kind"] for e in state["events"]][-2:], ["review", "final"])

    def test_reviewer_rejections_capped_with_warning(self):
        no = j(ok=False, issues=["x"])
        rounds = config.MAX_REVIEW_ROUNDS + 1
        m = model([final()] * rounds, reviewer=[no] * rounds)
        state = run("goal", m)
        self.assertEqual(state["status"], "finished")
        self.assertTrue(state["warnings"])

    def test_unparseable_replies_stop_with_a_retryable_error(self):
        m = model(["nope"] * 8)
        state = run("goal", m)
        self.assertEqual(state["events"][-1]["kind"], "model_error")
        self.assertIn("outside the protocol", state["events"][-1]["payload"]["error"])
        self.assertEqual(m.efforts("executor"), ["none", "none", "none", "medium"])

    def test_step_limit(self):
        old = config.MAX_STEPS, config.STEPS_PER_PLAN_STEP
        config.MAX_STEPS, config.STEPS_PER_PLAN_STEP = 2, 0
        try:
            m = model([READ, LOOKUP, final()])
            state = run("goal", m)
        finally:
            config.MAX_STEPS, config.STEPS_PER_PLAN_STEP = old
        self.assertEqual(state["status"], "aborted")
        self.assertIn("step limit", state["events"][-1]["payload"]["reason"])
        self.assertIn("follow-up instruction", state["events"][-1]["payload"]["reason"])

    def test_a_longer_plan_gets_more_steps(self):
        """A join over several datasets needs more steps than a one-table question."""
        old = config.MAX_STEPS, config.STEPS_PER_PLAN_STEP
        config.MAX_STEPS, config.STEPS_PER_PLAN_STEP = 2, 1
        try:
            short = run("goal", model([READ, LOOKUP, final()], planner=[j(plan=["Do it"])]))
            long = run("goal", model([READ, LOOKUP, final()], planner=[j(plan=["Read", "Look up", "Answer"])]))
        finally:
            config.MAX_STEPS, config.STEPS_PER_PLAN_STEP = old
        self.assertEqual(short["status"], "aborted")
        self.assertEqual(long["status"], "finished")
        self.assertEqual(config.step_budget(["a"] * 7), max(config.MAX_STEPS, 7 * config.STEPS_PER_PLAN_STEP))

    def test_the_users_step_limit_is_exact_and_a_follow_up_can_change_it(self):
        from agent.loop import continue_with
        state = run("goal", model([READ, LOOKUP, final(), LOOKUP, final()]), max_steps=1)
        self.assertEqual((state["status"], state["max_steps"]), ("aborted", 1))
        self.assertIn("your step limit", state["events"][-1]["payload"]["reason"])
        continue_with(state, "carry on", max_steps=5)                    # raised for the follow-up
        state = run("carry on", model([LOOKUP, final()]), state=state)
        self.assertEqual((state["status"], state["max_steps"]), ("finished", 5))
        continue_with(state, "again")                                    # kept when not given
        self.assertEqual(state["max_steps"], 5)
        state = run("again", model([final()]), state=state)
        continue_with(state, "once more", max_steps=0)                   # 0: automatic again
        self.assertIsNone(state["max_steps"])
        self.assertEqual(config.step_budget(["a"], 500), config.MAX_STEPS_LIMIT)

    def test_planner_failure_falls_back_to_goal(self):
        m = model([final()], planner=["x"] * 4)
        state = run("my goal", m)
        self.assertEqual(state["plan"], ["my goal"])
        self.assertEqual(state["status"], "finished")


def unreachable(messages):
    raise ConnectionError("Connection refused")


class ModelFailure(LoopCase):
    def test_planner_failure_is_retryable_event(self):
        m = model([READ, final()], planner=(unreachable, PLAN))
        old, config.MODEL_RETRIES = config.MODEL_RETRIES, 1
        try:
            state = new_state("goal")
            ev = run_one_step(state, m)
        finally:
            config.MODEL_RETRIES = old
        self.assertEqual(ev["kind"], "model_error")
        self.assertIn("Connection refused", ev["payload"]["error"])
        self.assertEqual(state["status"], "planning")      # nothing advanced
        self.assertEqual(load_state(state["run_id"])["events"][-1]["kind"], "model_error")
        run_one_step(state, m)                              # retry succeeds
        self.assertEqual(state["status"], "running")

    def test_failed_step_resumes_from_the_checkpoint(self):
        m = model([READ, final(), final()], reviewer=(unreachable, OK))
        state = new_state("goal")
        while not state["events"] or state["events"][-1]["kind"] != "model_error":
            run_one_step(state, m)
        self.assertEqual(state["step_count"], 1)
        self.assertEqual(state["status"], "running")
        run_one_step(state, m)                              # retry: executor + reviewer again
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
        self.assertIn("99.12", executor_prompts(m)[-1][-1]["content"])
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

    def test_tables_listed_with_the_answer_are_given_to_the_reviewer(self):
        m = model([tool_call("run_python", code="result = 1", save_as="by_division"),
                   j(final="See the grid below.", tables=["by_division"])])

        def save(e):
            config.work_dir(state["run_id"]).mkdir(parents=True, exist_ok=True)
            (config.work_dir(state["run_id"]) / "by_division.csv").write_text("division,rwa\nMarkets,66363\n")
            return True
        state = new_state("g")
        run("g", m, state=state, approve=save)
        prompt = [c for c in m.calls if c["purpose"] == "reviewer"][0]["messages"][-1]["content"]
        self.assertIn("shown to the user under the answer", prompt)
        self.assertIn("Markets,66363", prompt)


    def test_an_export_is_shown_with_the_answer_even_if_not_listed(self):
        from tests.helpers import STD
        sql = f"SELECT location, amount FROM usage_limits WHERE scenario = 'USED' AND resource = 'RWA' AND {STD}"
        m = model([tool_call("export_query", sql=sql, name="rwa_rows"),
                   final("The export is ready [ref: export_query-1].")])
        state = run("Export the RWA rows", m, approve=lambda e: True)
        ev = state["events"][-1]["payload"]
        self.assertEqual(ev["tables"], [f"outputs/{state['run_id']}/rwa_rows.csv"])   # .csv added to the name
        self.assertEqual(state["results"][0]["path"], ev["tables"][0])


class Finisher(LoopCase):
    STUCK = tool_call("read_file", path="work/sample_capital.csv")        # repeated

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


class StopDuringModelCall(LoopCase):
    """Stop is clicked in the UI while a slow model call is still running (same state dict)."""

    def _stopped_while_calling(self, reply_or_error):
        def reply(messages):
            abort(state, "Stopped by user.")
            if isinstance(reply_or_error, Exception):
                raise reply_or_error
            return reply_or_error
        state = new_state("goal")
        m = FakeModel({"planner": [PLAN], "executor": [reply]})
        run_one_step(state, m)                             # plan
        run_one_step(state, m)
        return state

    def test_reply_after_stop_is_not_acted_on(self):
        state = self._stopped_while_calling(READ)
        self.assertEqual(state["status"], "aborted")
        self.assertEqual(state["events"][-1]["kind"], "abort")
        self.assertNotIn("tool", [e["kind"] for e in state["events"]])

    def test_cancelled_call_does_not_undo_the_stop(self):
        state = self._stopped_while_calling(ConnectionError("cancelled"))
        self.assertEqual(state["status"], "aborted")
        self.assertEqual(load_state(state["run_id"])["status"], "aborted")


class Memory(LoopCase):
    def test_long_conversations_are_summarised(self):
        old = config.PROMPT_BUDGET_TOKENS, config.RECENT_TURNS
        config.PROMPT_BUDGET_TOKENS, config.RECENT_TURNS = 200, 2
        try:
            calcs = [tool_call("recall", query=f"term {i}") for i in range(5)]
            m = model(calcs + [final()], summariser=["- earlier calcs done"] * 5)
            state = run("goal", m)
        finally:
            config.PROMPT_BUDGET_TOKENS, config.RECENT_TURNS = old
        self.assertEqual(state["status"], "finished")
        self.assertEqual(set(m.efforts("summariser")), {"none"})
        self.assertIn("earlier calcs done", json.dumps(executor_prompts(m)[-1]))

    def test_follow_up_keeps_the_conversation(self):
        from agent.loop import continue_with
        m = model([READ, final("CET1 is 12850 [ref: read_file-1]"), final("Still 12850 [ref: read_file-1]")],
                  planner=(PLAN, PLAN), reviewer=(OK, OK))
        state = run("goal", m)
        continue_with(state, "and again?")
        run("", m, state=state)
        self.assertEqual(state["status"], "finished")
        last = executor_prompts(m)[-1]
        self.assertIn("NEW INSTRUCTION: and again?", last[-1]["content"])
        self.assertTrue(any("OBSERVATION" in x["content"] for x in last))        # earlier results still there


class Demo(LoopCase):
    def test_demo_fake_model_runs_end_to_end(self):
        state = run("demo", DemoFakeModel(), inputs=["inputs/sample_capital.csv"], approve=lambda e: True)
        self.assertEqual(state["status"], "finished")
        self.assertIn("12.66%", state["final"])


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


if __name__ == "__main__":
    unittest.main()


class LeanPrompts(LoopCase):
    """Summaries until a toolkit is enabled or a skill accepted; the current step only; old results shortened."""

    def test_an_accepted_skill_brings_its_steps_and_references_only_then(self):
        from agent.memory import new_state
        state = new_state("Compare RWA by division between two dates")
        m = FakeModel({"planner": [j(plan=["Answer"], suggest={"skill": "usage_comparison", "why": "fits"})],
                       "executor": [final("x")], "reviewer": [OK]})
        run_one_step(state, m)
        self.assertEqual(state["status"], "awaiting_choice")           # suggested: not in any prompt yet
        resolve_pending(state, choice={"skill": "usage_comparison"})
        run_one_step(state, m)
        system = executor_prompts(m)[0][0]["content"]
        self.assertIn("## SKILL: usage_comparison", system)
        self.assertIn("## Skill references", system)
        self.assertNotIn("CONTEXT", executor_prompts(m)[0][1]["content"])

    def test_the_request_check_sees_the_current_step_only(self):
        sql = "SELECT SUM(amount) AS rwa FROM usage_limits WHERE " + __import__("tests.helpers").helpers.STD + \
              " AND scenario = 'USED' AND resource = 'RWA' AND as_of_date = '2026-03-26'"
        m = FakeModel({"planner": [j(plan=["Read the file", "Query the total", "Answer"])],
                       "executor": [tool_call("read_file", step_done=True, path="work/sample_capital.csv"),
                                    tool_call("sql_query", sql=sql, save_as="t")]})
        from agent.memory import new_state
        state = new_state("goal")
        for _ in range(3):
            run_one_step(state, m)
        self.assertEqual(state["status"], "awaiting_approval")
        review = [c for c in m.calls if c["purpose"] == "action_review"][0]["messages"][-1]["content"]
        step = review.split("CURRENT PLAN STEP:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("Query the total", step)
        self.assertNotIn("Read the file", step)
        self.assertIn("## Current step\n2. Query the total", executor_prompts(m)[-1][0]["content"])

    def test_old_results_are_shortened_but_keep_their_ref(self):
        from agent.graph import compact
        from langchain_core.messages import ToolMessage
        big = [ToolMessage(json.dumps({"ok": True, "ref": f"read_file-{i}", "result": "head line\n" + "x" * 2000}),
                           tool_call_id=str(i)) for i in range(5)]
        out = compact(big, keep=3)
        self.assertTrue(all(len(m.content) < 400 for m in out[:2]))
        self.assertIn('"ref": "read_file-0"', out[0].content)
        self.assertIn("head line", out[0].content)
        self.assertEqual([m.content for m in out[2:]], [m.content for m in big[2:]])
        self.assertGreater(len(big[0].content), 2000)                  # the graph's own copy stays whole

    def test_an_old_dataset_description_keeps_what_sql_needs(self):
        """Filters and columns survive shortening, so a join over several datasets does not look them up again."""
        from agent import connectors
        from agent.graph import compact
        from langchain_core.messages import ToolMessage
        doc = connectors.dataset_doc("usage_limits") + "\n\ncolumns: partition_key, as_of_date, amount"
        msgs = [ToolMessage(json.dumps({"ok": True, "ref": "describe_dataset-1", "result": doc}), tool_call_id="0",
                            name="describe_dataset")]
        msgs += [ToolMessage(json.dumps({"ok": True, "ref": f"read_file-{i}", "result": "x" * 2000}),
                             tool_call_id=str(i)) for i in range(1, 4)]
        out = json.loads(compact(msgs, keep=3)[0].content)["result"]
        self.assertLess(len(out), len(doc) / 2)
        for part in ("## usage_limits", "Required filter: `partition_key`", "Default filters:", "columns: partition_key"):
            self.assertIn(part, out)

    def test_checkers_do_not_get_the_dictionary_twice(self):
        from agent.checks import _view_docs
        from agent.memory import new_state
        state = new_state("x", skill="usage_comparison")
        out = config.RUNS_DIR / state["run_id"] / "outputs"
        out.mkdir(parents=True, exist_ok=True)
        (out / "describe_dataset-1.txt").write_text("view docs")
        state["events"].append({"kind": "tool", "payload": {"tool": "describe_dataset", "input": {"dataset": "usage_limits"},
                                                            "obs": {"ok": True, "ref": "describe_dataset-1"}}})
        self.assertEqual(_view_docs(state), "")                         # the skill's dataset documentation covers it
        state["skill"] = None
        self.assertIn("view docs", _view_docs(state))


class LeanerHistory(LoopCase):
    def test_old_calls_keep_only_the_start_of_their_sql_or_code(self):
        from agent.graph import compact
        from langchain_core.messages import AIMessage
        calls = [AIMessage("", tool_calls=[{"name": "run_python", "args": {"code": f"# step {i}\n" + "x = 1\n" * 100},
                                            "id": str(i)}]) for i in range(4)]
        out = compact(calls, keep=3)
        self.assertLess(len(out[0].tool_calls[0]["args"]["code"]), 200)
        self.assertTrue(out[0].tool_calls[0]["args"]["code"].startswith("# step 0"))
        self.assertEqual(out[3].tool_calls, calls[3].tool_calls)

    def test_a_small_cited_result_is_never_cut_by_a_large_one(self):
        from agent.checks import cited_outputs
        from agent.memory import new_state
        state = new_state("g")
        out = config.RUNS_DIR / state["run_id"] / "outputs"
        out.mkdir(parents=True, exist_ok=True)
        (out / "sql_query-1.txt").write_text("row\n" * 5000)
        (out / "sql_query-2.txt").write_text("rows=1\ntotal\n245463\n")
        text = cited_outputs(state, "Big [ref: sql_query-1], total 245,463 [ref: sql_query-2]")
        self.assertIn("245463", text)
        self.assertIn("…[cut]", text)
        self.assertLessEqual(len(text), config.REVIEW_CITED_CHARS + 300)
