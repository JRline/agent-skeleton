"""The context packs for the GPT CLI as maintainer: every task builds, and each carries what it needs."""
import importlib.util
from pathlib import Path

from agent.backends.fake import FakeModel
from agent.loop import run_one_step
from agent.memory import new_state

from tests.helpers import PLAN, TempDirs, final

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gpt_pack", ROOT / "scripts" / "gpt_pack.py")
gpt_pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpt_pack)


class GptPack(TempDirs):
    def test_every_task_has_a_prompt_with_the_request_slot(self):
        for task in gpt_pack.TASKS:
            self.assertIn("{request}", (ROOT / "docs" / "gpt_prompts" / f"{task}.md").read_text(), task)

    def test_skill_pack_carries_contract_tools_example_and_dictionary(self):
        pack = gpt_pack.build("skill", "Compare leverage by region", like="limit_breach_analysis")
        self.assertIn("Compare leverage by region", pack)
        for part in ("docs/plugins.md", "TOOLS THE AGENT HAS", "sql_query",
                     "plugins/skills/limit_breach_analysis.md", "datalake_dictionary.md"):
            self.assertIn(part, pack)

    def test_other_packs_build(self):
        for task in ("toolkit", "golden", "examples", "view"):
            self.assertIn("THE REQUEST", gpt_pack.build(task, "x"), task)

    def test_diagnose_pack_shows_the_run(self):
        state = new_state("latest breaches")
        run_one_step(state, FakeModel({"planner": [PLAN], "executor": [final()]}))
        pack = gpt_pack.build("diagnose", "it went wrong", run=state["run_id"])
        self.assertIn("goal: latest breaches", pack)
        self.assertIn("planner (medium", pack)
        with self.assertRaises(SystemExit):
            gpt_pack.build("diagnose", "no run given")
