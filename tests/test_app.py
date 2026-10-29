"""Streamlit UI smoke test with the fake backend. Skipped if Streamlit isn't installed."""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from agent import config
from tests.helpers import TempDirs

try:
    import streamlit as st
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover
    AppTest = None

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@unittest.skipIf(AppTest is None, "streamlit not installed")
class AppSmoke(TempDirs):
    def setUp(self):
        super().setUp()
        os.environ["LLM_BACKEND"] = "fake"

    def lake_off(self):
        """Start the session without the datalake toolkit, to test enabling it."""
        saved = config.BASIC_TOOLKITS
        config.BASIC_TOOLKITS = ["files", "docs", "analysis"]
        self.addCleanup(lambda: setattr(config, "BASIC_TOOLKITS", saved))

    def test_goal_runs_to_approval_then_finishes(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        self.assertFalse(at.exception)
        at.chat_input(key="goal").set_value("Compute the latest capital ratio").run()
        self.assertFalse(at.exception)
        # autorun drives plan -> read_file -> capital_ratio, then stops for approval
        self.assertEqual(at.session_state["state"]["status"], "awaiting_approval")
        at.button(key="approve").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["state"]["status"], "finished")
        self.assertTrue(any("12.66%" in m.value for m in at.markdown))      # the answer is shown
        self.assertFalse(at.chat_input(key="goal").disabled)                # ready for a follow-up

    def test_the_step_limit_set_in_the_sidebar_stops_the_conversation(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.number_input(key="max_steps").set_value(1).run()
        at.chat_input(key="goal").set_value("Compute the latest capital ratio").run()
        self.assertEqual(at.session_state["state"]["status"], "awaiting_approval")   # the one step it may take
        at.button(key="approve").click().run()
        self.assertFalse(at.exception)
        state = at.session_state["state"]
        self.assertEqual(state["max_steps"], 1)
        self.assertEqual(state["status"], "aborted")
        self.assertIn("your step limit", state["events"][-1]["payload"]["reason"])

    def test_refs_are_numbered_links_with_a_sources_list(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.chat_input(key="goal").set_value("Compute the latest capital ratio").run()
        at.button(key="approve").click().run()
        self.assertIn("[ref:", at.session_state["state"]["final"])            # stored text keeps the audit markers
        answer = next(m.value for m in at.markdown if "12.66%" in m.value)
        self.assertNotIn("[ref:", answer)
        self.assertRegex(answer, r"\[\[1\]\]\(#src-[\w-]+-1\)")
        self.assertTrue(any(m.value == "**Sources**" for m in at.markdown))
        self.assertTrue(any(e.label.startswith("[1] ") for e in at.expander))

    def test_answer_downloads_and_timing(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.chat_input(key="goal").set_value("Compute the latest capital ratio").run()
        at.button(key="approve").click().run()
        self.assertFalse(at.exception)
        files = at.session_state["exports"]                      # what the three download buttons serve
        self.assertIn("Your request", files["html"])
        self.assertIn("# %% Step 1 · analysis", files["script"])
        self.assertTrue(any(c.value.startswith("Answered in") for c in at.caption))
        self.assertFalse(config.outputs_dir(at.session_state["state"]["run_id"]).exists())   # nothing to dig out

    def test_finished_requests_fold_their_steps_between_request_and_answer(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.chat_input(key="goal").set_value("Compute the latest capital ratio").run()
        self.assertFalse(any(e.label.startswith("How I got here") for e in at.expander))   # live: steps visible
        at.button(key="approve").click().run()
        self.assertTrue(any(e.label.startswith("How I got here") for e in at.expander))
        shown = " ".join(m.value for m in at.markdown)
        self.assertIn('class="req-mark">Your request', shown)
        self.assertIn('class="ans-mark">Answer', shown)

    def test_raw_replies_only_on_request(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.chat_input(key="goal").set_value("Compute the latest capital ratio").run()
        shown = len(at.code)                               # tool inputs and approval only
        at.checkbox(key="show_raw").check().run()
        self.assertFalse(at.exception)
        self.assertGreater(len(at.code), shown)            # one block per model call

    def test_model_failure_shows_retry_instead_of_crashing(self):
        target = "agent.backends.fake.DemoFakeModel.complete"
        with mock.patch(target, side_effect=ConnectionError("Connection refused")):
            at = AppTest.from_file(APP, default_timeout=30)
            at.run()
            at.chat_input(key="goal").set_value("Compute the latest capital ratio").run()
        self.assertFalse(at.exception)
        self.assertIn("Connection refused", at.error[0].value)
        self.assertEqual(at.session_state["state"]["status"], "planning")
        at.button(key="retry").click().run()                # backend is back
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["state"]["status"], "awaiting_approval")

    def test_files_named_in_the_message_are_attached(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.toggle(key="autorun_toggle").set_value(False).run()
        at.chat_input(key="goal").set_value("Summarise sample_capital.csv").run()
        self.assertEqual(at.session_state["state"]["inputs"], ["inputs/sample_capital.csv"])
        self.assertTrue(any("📎 `sample_capital.csv`" in m.value for m in at.markdown))   # shown with the request

    def test_skill_goes_into_the_run_with_its_references(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.toggle(key="autorun_toggle").set_value(False).run()
        self.assertIn("usage_comparison", at.selectbox(key="skill").options)
        at.selectbox(key="skill").set_value("usage_overview").run()
        at.chat_input(key="goal").set_value("Profile usage_limits").run()
        self.assertFalse(at.exception)
        state = at.session_state["state"]
        self.assertEqual(state["skill"], "usage_overview")
        self.assertIn("datalake", state["toolkits"])

    def test_code_approval_tables_and_results_page_on_request(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.chat_input(key="goal").set_value("Compute the latest capital ratio").run()
        self.assertEqual(at.session_state["state"]["status"], "awaiting_approval")
        self.assertTrue(any("cet1_ratio_pct" in c.value and c.language == "python" for c in at.code))
        at.button(key="approve").click().run()
        state = at.session_state["state"]
        self.assertEqual(state["status"], "finished")
        results = [d for d in at.dataframe if "period" in d.value.columns]    # (the other grid is Model calls)
        self.assertEqual(len(results), 2)                  # in source [1] (run_python) and under the answer
        self.assertEqual(list(results[0].value.columns),
                         ["period", "cet1_ratio_pct", "requirement_pct", "headroom_pp"])
        self.assertEqual(len(at.get("link_button")), 0)                      # no results page until asked
        at.button(key="create_results").click().run()
        self.assertEqual(len(at.get("link_button")), 1)
        page = AppTest.from_file(APP, default_timeout=30)
        page.query_params["results"] = state["run_id"]
        page.run()
        self.assertFalse(page.exception)
        self.assertIn("Results", page.title[0].value)
        self.assertEqual(len(page.dataframe), 1)

    def test_results_command(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.chat_input(key="goal").set_value("Compute the latest capital ratio").run()
        at.button(key="approve").click().run()
        at.chat_input(key="goal").set_value("/results").run()
        self.assertEqual(len(at.get("link_button")), 1)
        self.assertEqual(at.session_state["state"]["tasks"], [])           # not a new instruction

    def test_prompt_settings(self):
        from agent import settings
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.text_area(key="pref_role").set_value("You are a capital analyst.")
        at.button(key="pref_save").click().run()
        self.assertEqual(settings.load()["role"], "You are a capital analyst.")
        at.checkbox(key="pref_advanced").check().run()
        at.selectbox(key="pref_template").set_value("system").run()
        at.text_area(key="pref_text_system").set_value("Too short, no placeholders.")
        at.button(key="pref_template_save").click().run()
        self.assertTrue(any("{{guidance}}" in e.value for e in at.error))              # refused, explained
        self.assertEqual(settings.load()["templates"], {})

    def test_verbose_off_shows_steps_but_no_data(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.chat_input(key="goal").set_value("Compute the latest capital ratio").run()
        at.button(key="approve").click().run()
        shown = " ".join(e.value for e in list(at.text) + list(at.markdown) + list(at.code))
        self.assertIn("Approved `run_python` · table 3", shown)       # one line per step
        self.assertNotIn('"2026Q1", 12.54', shown)                    # never raw result text
        grids = lambda: sum("cet1_ratio_pct" in d.value.columns for d in at.dataframe)   # noqa: E731
        quiet = grids()                                               # the answer's table and its source
        at.toggle(key="verbose").set_value(True).run()
        self.assertEqual(grids(), quiet + 1)                          # the step's result, as a grid
        shown = " ".join(e.value for e in list(at.text) + list(at.markdown) + list(at.code))
        self.assertNotIn('"2026Q1", 12.54', shown)

    def test_slash_command_adds_tools_for_the_session_until_it_ends(self):
        self.lake_off()
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        self.assertNotIn("datalake", at.session_state["session_toolkits"])
        at.toggle(key="autorun_toggle").set_value(False).run()
        at.chat_input(key="goal").set_value("/datalake which views exist?").run()
        self.assertEqual(at.session_state["state"]["goal"], "which views exist?")
        self.assertIn("datalake", at.session_state["state"]["toolkits"])
        self.assertIn("datalake", at.multiselect(key="session_toolkits").value)   # kept for later runs
        at.button(key="end_session").click().run()
        self.assertNotIn("datalake", at.multiselect(key="session_toolkits").value)

    def test_suggestion_card_lets_the_user_decide(self):
        self.lake_off()
        def fake(self, messages, effort="none", stop=None, purpose="executor", conversation=None):
            replies = {"planner": '{"plan": ["Answer"], "suggest": {"toolkits": ["datalake"], "skill": "none", '
                                  '"why": "Needs lake data."}}',
                       "reviewer": '{"ok": true, "issues": []}'}
            return replies.get(purpose, '{"thought": "done", "final": "Answer."}')
        with mock.patch("agent.backends.fake.DemoFakeModel.complete", new=fake):
            at = AppTest.from_file(APP, default_timeout=30)
            at.run()
            at.chat_input(key="goal").set_value("RWA by risk type").run()
            self.assertEqual(at.session_state["state"]["status"], "awaiting_choice")
            self.assertTrue(at.checkbox(key="suggest_datalake").value)
            at.button(key="use_suggestion").click().run()
        self.assertFalse(at.exception)
        self.assertIn("datalake", at.session_state["state"]["toolkits"])
        self.assertIn("datalake", at.multiselect(key="session_toolkits").value)

    def test_stop_and_new_conversation(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.toggle(key="autorun_toggle").set_value(False).run()
        at.chat_input(key="goal").set_value("demo").run()
        self.assertTrue(at.chat_input(key="goal").disabled)            # busy: no new message yet
        at.button(key="stop").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["state"]["status"], "aborted")
        at.button(key="new_conversation").click().run()
        self.assertIsNone(at.session_state["state"])

    @staticmethod
    def quick_model(final_text="Answer."):
        def fake(self, messages, effort="none", stop=None, purpose="executor", conversation=None):
            replies = {"planner": '{"title": "Capital questions", "plan": ["Answer"]}',
                       "reviewer": '{"ok": true, "issues": []}'}
            return replies.get(purpose, json.dumps({"thought": "done", "final": final_text}))
        return mock.patch("agent.backends.fake.DemoFakeModel.complete", new=fake)

    def test_a_follow_up_continues_the_conversation_and_it_is_named(self):
        with self.quick_model():
            at = AppTest.from_file(APP, default_timeout=30)
            at.run()
            at.chat_input(key="goal").set_value("What is CET1?").run()
            first = at.session_state["state"]
            self.assertEqual((first["status"], first["title"]), ("finished", "Capital questions"))
            at.chat_input(key="goal").set_value("And Tier 1?").run()
        state = at.session_state["state"]
        self.assertFalse(at.exception)
        self.assertEqual(state["run_id"], first["run_id"])
        self.assertEqual([t["goal"] for t in state["tasks"]], ["What is CET1?"])
        self.assertEqual(state["status"], "finished")
        self.assertIn("Capital questions", at.title[0].value)
        shown = " ".join(m.value for m in at.markdown)
        self.assertIn("What is CET1?", shown)                          # earlier exchange still on screen
        self.assertIn("And Tier 1?", shown)
        self.assertIn("Capital questions", at.selectbox(key="resume_id").options)   # listed by title

    def test_rename(self):
        with self.quick_model():
            at = AppTest.from_file(APP, default_timeout=30)
            at.run()
            at.chat_input(key="goal").set_value("What is CET1?").run()
            at.text_input(key="rename_text").set_value("CET1 basics").run()
            at.button(key="rename_save").click().run()
        self.assertEqual(at.session_state["state"]["title"], "CET1 basics")
        self.assertIn("CET1 basics", at.title[0].value)

    def test_latex_brackets_are_rendered_as_math(self):
        with self.quick_model(r"ratio \[ \frac{a}{b} \] and \(x\)"):
            at = AppTest.from_file(APP, default_timeout=30)
            at.run()
            at.chat_input(key="goal").set_value("formula").run()
        self.assertTrue(any("$$ \\frac{a}{b} $$ and $x$" in m.value for m in at.markdown))

    def test_one_model_picker_lists_installed_models_only(self):
        saved = {k: os.environ.get(k) for k in ("OLLAMA_MODEL", "OLLAMA_MODEL_MEDIUM")}
        os.environ["OLLAMA_MODEL"] = "not-installed:1b"
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        with mock.patch("agent.backends.ollama_api.list_models", return_value=["a:4b", "b:8b"]):
            st.cache_data.clear()
            at = AppTest.from_file(APP, default_timeout=30)
            at.run()
            at.selectbox(key="backend").set_value("ollama").run()
            self.assertEqual(os.environ["OLLAMA_MODEL"], "a:4b")     # unknown default falls back
            at.selectbox(key="model").set_value("b:8b").run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key="model").options, ["a:4b", "b:8b"])
        self.assertEqual((os.environ["OLLAMA_MODEL"], os.environ["OLLAMA_MODEL_MEDIUM"]), ("b:8b", "b:8b"))

    def test_thinking_choice_sets_ollama_think(self):
        saved = os.environ.get("OLLAMA_THINK")
        os.environ.pop("OLLAMA_THINK", None)
        self.addCleanup(lambda: os.environ.pop("OLLAMA_THINK", None) if saved is None
                        else os.environ.__setitem__("OLLAMA_THINK", saved))
        with mock.patch("agent.backends.ollama_api.list_models", return_value=["a:4b"]):
            st.cache_data.clear()
            at = AppTest.from_file(APP, default_timeout=30)
            at.run()
            at.selectbox(key="backend").set_value("ollama").run()
            self.assertEqual(at.segmented_control(key="ollama_think").value, "1")   # by effort
            self.assertEqual(os.environ["OLLAMA_THINK"], "1")
            at.segmented_control(key="ollama_think").set_value("never").run()
        self.assertFalse(at.exception)
        self.assertEqual(os.environ["OLLAMA_THINK"], "never")

    def test_reject_path(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        at.chat_input(key="goal").set_value("demo").run()
        at.button(key="reject").click().run()                       # opens the reason form, rejects nothing yet
        self.assertEqual(at.session_state["state"]["status"], "awaiting_approval")
        send = next(b for b in at.button if b.label == "Send rejection")
        send.click().run()                                          # no reason: refused with a message
        self.assertEqual(at.session_state["state"]["status"], "awaiting_approval")
        self.assertTrue(any("give a reason" in e.value for e in at.error))
        at.text_area(key="reject_reason").set_value("Not needed for this goal.")
        next(b for b in at.button if b.label == "Send rejection").click().run()
        self.assertFalse(at.exception)
        self.assertIn(at.session_state["state"]["status"], ("running", "finished"))
        rejected = next(e for e in at.session_state["state"]["events"] if e["payload"].get("approved") is False)
        self.assertEqual(rejected["payload"]["reason"], "Not needed for this goal.")


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(AppTest is None, "streamlit not installed")
class LakeSignIn(TempDirs):
    """A lake that needs the user's own login: the sidebar asks for it and keeps it in memory only."""

    def setUp(self):
        super().setUp()
        from agent import connectors
        self.serve_mock(use=True, users={"analyst": "s3cret-Pa55"})
        connectors.OVERRIDES["sample_lake"]["needs_login"] = True
        patch = mock.patch.dict("os.environ", {"LLM_BACKEND": "fake"})
        patch.start()
        self.addCleanup(patch.stop)

    def sign_in(self, at, password):
        at.text_input(key="user_sample_lake").set_value("analyst")
        at.text_input(key="password_sample_lake").set_value(password)
        at.button(key="FormSubmitter:sign_in_sample_lake-Sign in").click().run()

    def test_wrong_then_right_password(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        self.sign_in(at, "nope")
        self.assertTrue(any("Sign-in failed" in e.value for e in at.error))
        self.assertEqual(at.session_state["lake_logins"] if "lake_logins" in at.session_state else {}, {})
        self.sign_in(at, "s3cret-Pa55")
        self.assertFalse(at.exception)
        self.assertTrue(any("Signed in as analyst" in s.value for s in at.success))
        self.assertTrue(any("usage_limits" in m.value for m in at.markdown))   # the lake's views, as this user
        at.button(key="sign_out_sample_lake").click().run()
        self.assertEqual(at.session_state["lake_logins"], {})
