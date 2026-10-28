# Instructions for coding assistants

This repo is a Python agent that runs on a text-only model (production: the GPT CLI). `docs/design.md` is the design spec;
read the section relevant to your task before changing code. `docs/setup.md` has setup steps; `docs/README.md` lists all documentation.

## Commands
- Tests: `python -m unittest discover -s tests -t .` (must pass before you finish any task)
- Demo run: `python cli.py --yes "Compute the latest capital ratio from inputs/sample_capital.csv"`
- UI: `streamlit run app.py`
- Backend check: `python smoke_model.py --backend ollama` (or `--backend claude`: Haiku via the Claude Code CLI)
- Mock data lake server (closer to production): `python mock_lake/server.py`, then run with
  `DATALAKE=agent.backends.datalake_mock`
- Replay script of a conversation: `python cli.py --export-script RUN_ID` (or the app's Export button)
- Analysis run: `python cli.py --backend claude "/usage_comparison RWA by division, FY2025Q3 end vs latest"` (follow-up: `--resume RUN_ID "..."`)
- Evals: `python eval.py --backend claude` (golden tasks in `tests/golden/`; results in `runs/evals/`). Run before and
  after any prompt, policy or model change.
- Context pack for the GPT CLI as maintainer: `python scripts/gpt_pack.py skill "..."` (also toolkit, golden,
  examples, view, diagnose --run RUN_ID). Procedures: `docs/maintenance.md`.

## Constraints (do not break these)
- The production environment is closed. Use only the packages in `requirements.txt` and the
  standard library. Ask before adding any dependency.
- Production uses the GPT CLI, wrapped as `call(text, effort) -> str` with effort `"none"` or `"medium"`.
  No native tool calling, no streaming, no system-role guarantees. Do not use features beyond this.
  The one optional extension is session memory mode (`MODEL_MEMORY=session`, docs/design.md §4a):
  `call(text, effort, channel)` and `new_session()`, for fixed model instances that keep their own memory.
- Every model call goes through `memory.call_model` so it is logged with purpose, effort and latency.
- `loop.py` stays UI-independent. `app.py` and `cli.py` only call `new_state`, `run_one_step`,
  `resolve_pending`, `load_state`, `abort` and `run`, plus `continue_with` (a follow-up instruction in the same
  conversation) and `rename` (the conversation title).
- `run_one_step` advances exactly one step and persists. Human input happens only via `resolve_pending`.
- Tools never raise into the loop: `dispatch` turns errors into `{"ok": false, "error": ...}`.
- Tools may only touch files inside `config.SANDBOX_DIR` (use `tools.sandbox_path`).
- Anything that writes, sends or deletes must be `risky=True` (needs approval). The one exception is
  per-run scratch tables in `sandbox/work/<run_id>/` (`config.work_dir`), which are not user-facing.
- The data lake is read-only: every query goes through `datalake.check_select` and a read-only connection.
- Arithmetic is done by tools, never by the model: `run_python` (model-written pandas code) or SQL.
- Model-written code runs only through `agent/pysandbox.py`: static check, user approval, then an isolated
  `python -I` process. It may only read data and must return `result`/`notes`; never `exec` it in the agent
  process and never let it write files. Allowed imports are `pysandbox.ALLOWED_MODULES` (shipped packages only).
- Every number in a final answer must trace to a tool result, a skill reference or the user's instruction
  (`loop.uncited_numbers`, checked before the LLM reviewer).
- Never put real or confidential data in tests, fixtures or `sandbox/`. Use synthetic data.
- Keep `docs/design.md` in sync when you change behaviour it describes.
- **Adding a tool, toolkit, plugin or skill: follow `docs/plugins.md` and only add files under `plugins/`
  (and their tests).** Do not change the infrastructure (`agent/`, `app.py`, `cli.py`, prompts, config) or the
  docs (`README.md`, `CLAUDE.md`, everything in `docs/`) unless the user specifically agrees.
- Tools are offered per session: basic toolkits by default; others only when the user enables them (sidebar,
  `/name`, `--tools`) or accepts the agent's suggestion (`awaiting_choice` → `resolve_pending(choice=...)`).

## Recipes

**Add a tool**: put it in a toolkit module under `plugins/tools/` (copy `_example_toolkit.py`), following
`docs/plugins.md`: a `TOOLKIT` dict, a Pydantic args model with field descriptions, then
```python
@tool("name", "What it returns, how big, and when NOT to use it.", ArgsModel, risky=False)
def name(args: ArgsModel, ctx: ToolContext):
    return "short result"          # or ToolOutput(preview, full)
```
Add `tests/test_plugin_<name>.py` for normal use, bad input and sandbox limits. Built-in toolkits live in
`agent/toolkits/` and are infrastructure: change them only when the user asks for it.

**Change a prompt** (`agent/prompts/*.md`): placeholders are `{{name}}`. Keep prompts short and
literal with one JSON example. Run `smoke_model.py` against Ollama before and after.

**Change loop behaviour** (`agent/loop.py`): add a FakeModel test in `tests/test_loop_fake_model.py`
first, scripting replies per purpose: `FakeModel({"planner": [...], "executor": [...], "reviewer": [...]})`.
Assert on `state["status"]`, events, and `model.efforts(purpose)`.

**Change the effort policy** (`agent/config.py` `EFFORT`, `loop.pick_effort`): update the effort
assertions in the loop tests.

**Add a skill** (`plugins/skills/<name>.md`, no code): front matter `name`, `description`, `toolkits`, `when_to_use`, and
`source` when it is written for one data source (e.g. `data lake view usage_limits`; leave it out for a general skill), then
`## Steps` (numbered; they become the plan), `## Checks` (bullets; added to the reviewer) and `## Output`.
Write steps in terms of the tools (sql_query, run_python, save_table) and keep skills general: parameters
(entity, dates, slice, breakdown) come from the request. `tests/test_skills.py` checks that every bundled skill parses.

**Add a data lake view**: locally, add a synthetic `mock_lake/data/<view>.csv` (never inside `sandbox/`: the lake
is remote and read-only) and describe it in `sandbox/references/datalake_dictionary.md`. In production, the view must be on the production allowlist (`DATALAKE_VIEWS`
or the module's `list_views`).

## Style
- Python 3.10+ type hints, small functions, docstrings that say why.
- No new abstraction layers or frameworks. Prefer plain functions and dicts in state (state must stay JSON-serialisable).
  The one agreed exception is the small plugin loader (`agent/plugins.py`) for toolkits and skills.
- Keep diffs focused on the task. Do not reformat unrelated code.

---

# Handover (from the Claude chat session, 2026-10-21)

## Context
- The agent supports regulatory capital analysis work,
  but this repo must only ever contain synthetic data.
- Production is a **closed environment**: the GPT CLI (the production GPT model), wrapped as `call(text, effort)` with
  effort `"none"` or `"medium"`, no native tool calling. Nothing here can be tested against it directly.
- Development happens on the owner's Mac (Apple Silicon Mac mini), project at `~/Coding/agent-skeleton`.
- Plan in three stages (see docs/setup.md): 1) FakeModel, 2) a local Ollama model as a stand-in for a
  small model, 3) production. **Stages 1 and 2 are done; the framework is ready to ship (2026-10-25).**
  After shipping, skills, tools and evals are built for real data inside the closed environment with a
  GPT CLI, following `docs/maintenance.md`. The framework (`agent/`, prompts, UI) should rarely change.
- The owner prefers to go one stage at a time, with one focused task per coding session.

## Status (2026-10-25)
- Verified on the Mac (Python 3.14, pydantic 2.14, pandas 3.0, Streamlit 1.66): all unit tests pass, including
  the Streamlit AppTest ones; CLI and UI demos run; Ollama (qwen3:4b completes tasks; qwen3.5 needs Thinking
  "Off") and the `claude` backend (Haiku, used for evals) work.
- Evals (claude backend): about 0.8 on the 14 golden tasks; runs vary, so compare averages. The two
  `breach_source_*` tasks fail most often (step budget, source attribution).
- Prompts follow the published best practices (ordered decisions, positive rules, reasons, tags); planner worked
  examples are swappable (`plugins/examples/`). User answers and rejection reasons are facts that outrank the plan.

## Next tasks, in order
1. Pin `requirements.txt` to production's exact Python and package versions (ask the owner). The grid number
   format (`NumberColumn(format="localized")`) and `st.segmented_control` need a recent Streamlit: check them.
2. Production wrapper: copy `agent/backends/prod_template.py` (and the ODBC lake template) into modules that wrap
   the real API and lake. Done inside the closed environment, by the owner. Then `smoke_model.py --backend prod`.
3. Re-run the full eval against `prod` and re-tune prompts only if needed (one change at a time, before/after).
4. Optional: summarise old turns in larger batches (now nearly every step); tell the executor how many rows a
   cut-off preview holds; let a rejection reason come from the CLI too.

## Design decisions (keep unless the owner agrees otherwise)
- Plan → execute → review, with JSON actions validated by Pydantic. Chosen because small models are
  unreliable at open-ended tool use.
- Effort policy: `none` by default; `medium` for planner, reviewer, recovery after an error or repeat,
  and the last JSON-repair attempt. Every call is logged with effort and latency so this can be tuned.
- Step machine with persisted state, so Streamlit reruns, approvals and resume all use the same code.
- Arithmetic lives in tools (`run_python`, SQL), never in the model. A code check and the reviewer verify
  that numbers in the final answer trace to refs.
- Standard library + pydantic/pandas/openpyxl/streamlit only, because production is closed.

## Known limitations / gotchas
- Tool timeouts use a thread: a timed-out tool keeps running in the background. Tools get `TOOL_TIMEOUT_S` (30 s);
  `sql_query` gets `SQL_TIMEOUT_S` (900 s, lake queries can run 15 minutes). A query cut off or stopped by the user
  keeps running on the lake server until it ends: set the lake driver's own query timeout to match.
- `pysandbox` has no OS-level network or memory isolation (imports and readers are restricted instead, and a
  CPU/time limit applies). Review with security before production.
- `recall` and `search_docs` are keyword-only (no embeddings).
- `datalake.check_select` is a keyword/regex guard, not a SQL parser. In production the read-only lake
  account and view allowlist are the real protection; the guard is a second layer.
- The `claude` backend (Claude Code CLI) is for development only: prompts go to Anthropic, so synthetic data only.
  Each call starts a CLI process (a few seconds of overhead).
- Token counts are estimates (chars / 4).
- `DemoFakeModel` picks its next step by counting `OBSERVATION:` lines, so it only suits the short demo.
- A conversation gets the files attached in the message box or named in the message (matched by file name).
- `st.dataframe(use_container_width=True)` may warn on newer Streamlit; switch to the new width argument if so.
- `ollama_api.py` defaults to `qwen3:4b`; the owner should choose the model(s).
