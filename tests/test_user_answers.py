"""The user's confirmed choices outrank the plan. (A "latest" breach check once became a full-year scan: the
confirmation was cut to 120 characters in the facts, and the request check followed the skill's step instead.)"""
from agent.graph import first_prompt
from agent.backends.fake import FakeModel
from agent.loop import resolve_pending, run_one_step
from agent.graph import review_request
from agent.memory import new_state
from agent.skills import get_skill

from tests.helpers import OK, PLAN, TempDirs, final, j, tool_call

QUESTION = ("I plan to use: RWA and Leverage exposure; levels Group total, division and product, no region split; "
            "period = latest CLOSED date for each resource; version CLOSED; method BOOKED. Confirm or correct.")


class UserAnswers(TempDirs):
    def test_the_whole_question_and_answer_become_a_fact(self):
        m = FakeModel({"planner": [PLAN], "executor": [tool_call("ask_user", question=QUESTION), final()],
                       "reviewer": [OK]})
        state = new_state("check the latest breaches")
        run_one_step(state, m)                             # plan
        run_one_step(state, m)                             # ask_user
        resolve_pending(state, answer="Yes, confirmed.")
        fact = next(f for f in state["facts"] if f.startswith("USER ANSWER"))
        self.assertIn("period = latest CLOSED date for each resource", fact)     # past the old 120-char cut
        self.assertIn("-> Yes, confirmed.", fact)

    def test_request_check_and_executor_put_user_answers_above_the_plan(self):
        state = new_state("check the latest breaches")
        model = FakeModel({"action_review": [j(ok=True, issues=[], explain="Reads the latest date.")]})
        review_request(model, state, "sql_query", {"sql": "SELECT 1", "save_as": "x"})
        self.assertIn("The user's answers (USER ANSWER and USER REJECTED facts) override the plan step",
                      model.calls[0]["messages"][0]["content"])
        self.assertIn("The user's answers override the plan", first_prompt(state)[0]["content"])

    def test_a_rejection_reason_reaches_the_executor_and_the_facts(self):
        m = FakeModel({"planner": [PLAN], "executor": [tool_call("write_file", name="a.md", content="x"), final()]})
        state = new_state("goal")
        run_one_step(state, m)                             # plan
        run_one_step(state, m)                             # write_file waits for approval
        resolve_pending(state, approved=False, reason="Only the latest date is needed.")
        self.assertEqual(state["events"][-1]["payload"]["reason"], "Only the latest date is needed.")
        run_one_step(state, m)                             # the reason goes back to the model
        run_one_step(state, m)
        last = [c for c in m.calls if c["purpose"] == "executor"][-1]["messages"][-1]["content"]
        self.assertIn("because: Only the latest date is needed.", last)
        self.assertIn("USER REJECTED write_file because: Only the latest date is needed.", state["facts"])

    def test_a_rejection_without_reason_still_works(self):
        m = FakeModel({"planner": [PLAN], "executor": [tool_call("write_file", name="a.md", content="x"), final()]})
        state = new_state("goal")
        run_one_step(state, m)
        run_one_step(state, m)
        resolve_pending(state, approved=False)             # CLI and evals do not give reasons
        self.assertNotIn("reason", state["events"][-1]["payload"])
        run_one_step(state, m)
        run_one_step(state, m)
        last = [c for c in m.calls if c["purpose"] == "executor"][-1]["messages"][-1]["content"]
        self.assertIn("User rejected write_file. Choose another approach.", last)

    def test_breach_skills_are_split_by_question(self):
        # One skill doing both a latest check and a history made the request check reject query after query.
        history, status = get_skill("limit_breach_analysis"), get_skill("limit_status")
        self.assertIn("use limit_status", history.when_to_use)
        self.assertIn("use limit_breach_analysis", status.when_to_use)
        self.assertEqual(len(status.steps), 3)                                          # short on purpose

    def test_both_breach_skills_compare_per_component(self):
        # A sum over components once hid FX Options London's market-risk breach behind other components' headroom.
        for name in ("limit_breach_analysis", "limit_status"):
            self.assertIn("component_code as a key", " ".join(get_skill(name).steps), name)
        history = " ".join(get_skill("limit_breach_analysis").steps)
        self.assertIn("the latest as_of_date in the data before it", history)          # 2026-01-04 was a Sunday
        status = " ".join(get_skill("limit_status").steps)
        self.assertIn("headroom (limit - usage)", status)                              # no negative "excess"
        self.assertIn("latest CLOSED date of each resource; it needs no confirmation", status)

    def test_request_check_accepts_a_period_stated_in_the_goal(self):
        model = FakeModel({"action_review": [j(ok=True, issues=[], explain="x")]})
        review_request(model, new_state("Term Loans in Tokyo has been above its RWA limit since early January 2026."),
                       "sql_query", {"sql": "SELECT 1", "save_as": "x"})
        self.assertIn("A period stated in the GOAL", model.calls[0]["messages"][0]["content"])
