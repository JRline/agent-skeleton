"""Numbered citations and the Sources list under an answer; the lake vs local-files wording."""
from agent import config
from agent.loop import resolve_pending
from agent.memory import load_prompt, new_state, run_dir
from agent.skills import get_skill
from agent.toolkits.datalake import TOOLKIT as LAKE_TOOLKIT
from sources import number_sources, source_details

from tests.helpers import TempDirs


class Citations(TempDirs):
    def test_refs_become_numbered_links_and_repeat_the_same_number(self):
        text = "CET1 12.66% [ref: run_python-3]. RWA 101,500 [ref: sql_query-2, run_python-3]. See [doc: references/d.md]"
        body, sources = number_sources(text, "src-a")
        self.assertEqual(sources, [("ref", "run_python-3"), ("ref", "sql_query-2"), ("doc", "references/d.md")])
        self.assertIn("12.66% [[1]](#src-a-1).", body)
        self.assertIn("101,500 [[2]](#src-a-2)[[1]](#src-a-1).", body)
        self.assertNotIn("[ref:", body)

    def test_text_without_refs_is_unchanged(self):
        self.assertEqual(number_sources("No numbers here.", "x"), ("No numbers here.", []))

    def test_source_details_show_input_and_full_stored_output(self):
        state = new_state("g")
        state["events"].append({"kind": "tool", "step": 1, "payload": {
            "tool": "sql_query", "input": {"sql": "SELECT 1", "save_as": "x"},
            "obs": {"ok": True, "ref": "sql_query-1", "result": "preview only"}}})
        (run_dir(state) / "outputs").mkdir(parents=True, exist_ok=True)
        (run_dir(state) / "outputs" / "sql_query-1.txt").write_text("full output 42")
        d = source_details(state, "ref", "sql_query-1")
        self.assertEqual((d["title"], d["input"]["sql"], d["output"]), ("sql_query", "SELECT 1", "full output 42"))
        self.assertEqual(source_details(state, "ref", "nope-9")["title"], "not found in this conversation")

    def test_user_answers_are_sources_too(self):
        from agent.backends.fake import FakeModel
        from agent.loop import run_one_step
        from tests.helpers import PLAN, tool_call
        state = new_state("g")
        m = FakeModel({"planner": [PLAN], "executor": [tool_call("ask_user", question="Which period?")]})
        run_one_step(state, m)
        run_one_step(state, m)
        resolve_pending(state, answer="2026Q3")
        ref = state["events"][-1]["payload"]["obs"]["ref"]
        d = source_details(state, "ref", ref)
        self.assertIn("Which period?", d["output"])
        self.assertIn("2026Q3", d["output"])

    def test_documents_are_shown_from_the_sandbox(self):
        d = source_details(new_state("g"), "doc", "references/datalake_dictionary.md")
        self.assertIn("## usage_limits", d["output"])


class LakeIsRemote(TempDirs):
    def test_guidance_and_planner_keep_local_files_out_of_the_lake(self):
        guidance = LAKE_TOOLKIT["guidance"]()
        self.assertIn("remote, read-only", guidance)
        self.assertIn("never part of it", guidance)
        planner = load_prompt("planner", goal="g", inputs="inputs/x.xlsx", tool_names="", other_toolkits="",
                              skills="", earlier="", context="")
        self.assertIn("never in\nthe data lake and can never be loaded into it", planner)

    def test_data_profile_is_for_lake_views_only(self):
        use = get_skill("usage_overview").when_to_use
        self.assertIn("not for an attached or local file", use)
        self.assertIn("Not for questions about values", use)            # a value question is not a profile

    def test_the_dictionary_is_a_reference_document(self):
        self.assertTrue(config.datalake_dictionary().is_file())
        self.assertEqual(get_skill("usage_comparison").references, ["references/datalake_dictionary.md"])


class TablesAreNotRepeated(TempDirs):
    def test_the_model_is_told_listed_tables_show_as_grids(self):
        from agent.memory import load_prompt
        system = load_prompt("system", tools="", guidance="")
        self.assertIn('"tables": [{"name": "<work table name>", "title": ', system)
        self.assertIn("Do not also write a listed table out", system)
        self.assertIn("columns that qualify its numbers", system)
        for name in ("usage_comparison",):
            self.assertIn("Do not repeat the table in the text", get_skill(name).output, name)


class TableTitles(TempDirs):
    def test_titles_come_from_the_answer_or_the_table_name(self):
        from agent.loop import _result_tables
        state = new_state("g")
        config.work_dir(state["run_id"]).mkdir(parents=True, exist_ok=True)
        for name in ("rwa_by_division", "lev"):
            (config.work_dir(state["run_id"]) / f"{name}.csv").write_text("a\n1\n")
        out = _result_tables(state, [{"name": "rwa_by_division", "title": "RWA usage by division, 2026-03-26 (CLOSED, JPY m)"},
                                     "lev", "missing"])
        self.assertEqual([t["title"] for t in out], ["RWA usage by division, 2026-03-26 (CLOSED, JPY m)", "Lev"])
