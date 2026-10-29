# Instructions for coding assistants

This repo is a Python agent that runs on a text-only model (production: the GPT CLI), built on LangGraph / LangChain.
`docs/design.md` is the design spec;
read the section relevant to your task before changing code. `docs/setup.md` has setup steps; `docs/README.md` lists all documentation.

## Commands
- Tests: `python -m unittest discover -s tests -t .` (must pass before you finish any task)
- Demo run: `python cli.py --yes "Compute the latest capital ratio from inputs/sample_capital.csv"`
- UI: `streamlit run app.py`
- Backend check: `python scripts/smoke_model.py --backend ollama` (or `--backend claude`: Haiku via the Claude Code CLI;
  `--backend chatgpt`: ChatGPT or any OpenAI-compatible API, see `agent/backends/openai_api.py`)
- Mock lake server (closer to production): `python scripts/mock_lake_server.py` (every connector with local data, each
  at the `URL` in its connector.py), then set `KIND = "mock"` in that connector.py (per-user sign-in: start the server
  with `MOCK_LAKE_USERS=analyst:secret` and set `NEEDS_LOGIN = True`)
- Replay script of a conversation: `python cli.py --export-script RUN_ID` (or the app's Export button)
- Analysis run: `python cli.py --backend claude "/usage_comparison RWA by division, FY2025Q3 end vs latest"` (follow-up: `--resume RUN_ID "..."`)
- Evals: `python scripts/eval.py --backend claude` (golden tasks in `tests/golden/`; results in `runs/evals/`). Run before and
  after any prompt, policy or model change.
- Context pack for the GPT CLI as maintainer: `python scripts/gpt_pack.py skill "..."` (also toolkit, golden,
  connector, dataset, examples, diagnose --run RUN_ID). Procedures: `docs/maintenance.md`.

## Constraints (do not break these)
- The production environment is closed. Use only the packages in `requirements.txt` and the
  standard library. Ask before adding any dependency.
- Production uses the GPT CLI, wrapped as `call(text, effort) -> str` with effort `"none"` or `"medium"`.
  No native tool calling, no streaming, no system-role guarantees. Do not use features beyond this.
  The one optional extension is session memory mode (`MODEL_MEMORY=session`, docs/design.md §4a):
  `call(text, effort, channel)` and `new_session()`, for fixed model instances that keep their own memory.
- The agent is one LangChain `create_agent` graph (`agent/graph.py`). Use stock LangGraph / LangChain parts where
  they fit; add custom middleware only for what they do not provide, and say why in its docstring.
- Every model call goes through a role copy of the chat model (`TextChatModel.for_role`, or the native model) so the
  `ModelLog` callback logs it with purpose, effort and latency.
- `loop.py` stays UI-independent. `app.py` and `cli.py` only call `new_state`, `run_one_step`,
  `resolve_pending`, `load_state`, `abort` and `run`, plus `continue_with` (a follow-up instruction in the same
  conversation) and `rename` (the conversation title).
- `run_one_step` advances the graph to its next breakpoint or pause and persists. Human input happens only via
  `resolve_pending` (LangGraph `Command(resume=...)`).
- The data lake sign-in (`agent/credentials.py`) lives in memory only: never write it to state, logs, checkpoints,
  settings, prompts or error text.
- Connections (connection string, URL) are set only in each connector's `connector.py`, never in environment
  variables: the agent connects to several sources at once.
- No sample connector, dataset or sample data is hard-coded in the core (`agent/`, `app.py`, `cli.py`, `scripts/`,
  prompts): they are extension modules under `extensions/connectors/<name>/` (`connector.py`, `datasets/*.md`,
  `sample.py` with `write()` and optional `inputs()`). Core code finds them through `agent.connectors`; only tests
  and extensions name them.
- Tools never raise into the loop: `dispatch` turns errors into `{"ok": false, "error": ...}`.
- Tools may only touch files inside `config.SANDBOX_DIR` (use `tools.sandbox_path`).
- Anything that writes, sends or deletes must be `risky=True` (needs approval). The one exception is
  per-run scratch tables in `sandbox/work/<run_id>/` (`config.work_dir`), which are not user-facing.
- Data sources are read-only: every query goes through `datalake.check_select`, on documented datasets of one
  connector, over a read-only connection.
- Arithmetic is done by tools, never by the model: `run_python` (model-written pandas code) or SQL.
- Model-written code runs only through `agent/pysandbox.py`: static check, user approval, then an isolated
  `python -I` process. It may only read data and must return `result`/`notes`; never `exec` it in the agent
  process and never let it write files. Allowed imports are `pysandbox.ALLOWED_MODULES` (shipped packages only).
- Every number in a final answer must trace to a tool result, a skill reference or the user's instruction
  (`loop.uncited_numbers`, checked before the LLM reviewer).
- Never put real or confidential data in tests, fixtures or `sandbox/`. Use synthetic data.
- Keep `docs/design.md` in sync when you change behaviour it describes.
- **Adding a connector, dataset, skill, toolkit or example: follow `docs/extensions.md` and only add files under
  `extensions/`
  (and their tests).** Do not change the infrastructure (`agent/`, `app.py`, `cli.py`, prompts, config) or the
  docs (`README.md`, `CLAUDE.md`, everything in `docs/`) unless the user specifically agrees.
- Tools are offered per session: basic toolkits by default; others only when the user enables them (sidebar,
  `/name`, `--tools`) or accepts the agent's suggestion (`awaiting_choice` → `resolve_pending(choice=...)`).

## Recipes

**Add a tool**: put it in a toolkit module under `extensions/toolkits/` (copy `_template.py`), following
`docs/extensions.md`: a `TOOLKIT` dict, a Pydantic args model with field descriptions, then
```python
@tool("name", "What it returns, how big, and when NOT to use it.", ArgsModel, risky=False)
def name(args: ArgsModel, ctx: ToolContext):
    return "short result"          # or ToolOutput(preview, full)
```
Add `tests/test_plugin_<name>.py` for normal use, bad input and sandbox limits. Built-in toolkits live in
`agent/toolkits/` and are infrastructure: change them only when the user asks for it.

**Change a prompt** (`agent/prompts/*.md`): placeholders are `{{name}}`. Keep prompts short and
literal with one JSON example. Run `scripts/smoke_model.py` against Ollama before and after.

**Change agent behaviour** (`agent/graph.py` middleware, `agent/loop.py` driver): add a FakeModel test in
`tests/test_loop_fake_model.py` first, scripting replies per purpose:
`FakeModel({"planner": [...], "executor": [...], "reviewer": [...]})` (`tests.helpers.first_step` plans and takes
the first step). Assert on `state["status"]`, events, and `model.efforts(purpose)`.

**Change the effort policy** (`agent/config.py` `EFFORT`; recovery effort in `graph.SessionPrompt`): update the effort
assertions in the loop tests.

**Add a skill** (`extensions/skills/<name>.md`, no code; copy `_template.md`): front matter `name`, `description`,
`toolkits`, `when_to_use`, and `datasets` when it is written for datasets (any connectors; leave it out for a general
skill), then
`## Steps` (numbered; they become the plan), `## Checks` (bullets; added to the reviewer) and `## Output`.
Write steps in terms of the tools (sql_query, run_python, save_table) and keep skills general: parameters
(entity, dates, slice, breakdown) come from the request. `tests/test_skills.py` checks that every bundled skill parses.

**Add a data source or dataset**: a connector is a folder `extensions/connectors/<name>/` (copy `_template/`):
`connector.py` (constants: KIND odbc / mock / sqlite / custom, NEEDS_LOGIN, CONNECTION_STRING, ...) and one `datasets/<dataset>.md` per
table, in the format of `docs/dataset_entry.md`. Only described datasets can be queried. Synthetic local data comes
from the connector's optional `sample.py` (git-ignored `data/`, never inside `sandbox/`).

## Style
- Python 3.10+ type hints, small functions, docstrings that say why.
- No new abstraction layers or frameworks. Prefer plain functions and dicts in state (state must stay JSON-serialisable).
  The agreed exceptions are the small loaders for extensions (`agent/extensions.py`, `agent/connectors/`).
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
  examples are swappable (`extensions/examples/`). User answers and rejection reasons are facts that outrank the plan.

## Status of branch `langgraph-prototype` (2026-10-28)
- The agent runs on LangGraph 1.2 / LangChain 1.4: one `create_agent` graph (`agent/graph.py`) with stock approvals,
  interrupts, checkpoints and summarising, and custom middleware for planning, the session prompt, the request
  check, repeats and the answer check. `agent/loop.py` drives it one step at a time and keeps `state.json` and its
  events, so the UI, export, replay and eval are unchanged. The step machine is gone (it is on `main`).
- Also new on the branch: session memory mode, data lake sign-in (memory only; ODBC template with UID/PWD), and two
  ways to swap in a real model (`LLM_BACKEND=chatgpt`, `LLM_BACKEND=langchain`). The CLI asks for a rejection reason.
- Verified 2026-10-29: all 283 unit tests pass (Python 3.14 venv with requirements.txt), CLI demo runs, and two
  golden tasks on Haiku (`file_average`, `rwa_latest_total`) score 1.00. The full golden eval has not been run on
  this branch yet.
- Extensions layout (2026-10-29): `plugins/` became `extensions/` with four types: `connectors/<name>/`
  (connector.py + one `datasets/<dataset>.md` per table; built-in kinds sqlite, mock, odbc in `agent/connectors/`),
  `skills/` (list `datasets:` from any connectors), `toolkits/`, `examples/`; each with a `_template`. "View" is now
  "dataset". Sign-in is per connector (sidebar, CLI `--sign-in name=user`). Root holds only `app.py` and `cli.py`;
  scripts moved to `scripts/`, UI helpers to `agent/ui/`; `mock_lake/` and the data dictionary are gone.
- Leaner prompts (2026-10-29): toolkits and skills are one-line summaries until enabled or accepted, then their
  full docs; the request check gets the current step only; old tool results and calls are shortened (refs kept);
  no repeated dataset docs. Three Haiku tasks score 1.00 at 1.8k / 3.6k / 4.1k tokens per call, level with main
  (1.9k / 3.4k / 4.1k). 289 unit tests pass.
- Exports (2026-10-29): `export_query` (datalake toolkit) streams the rows of one approved SELECT to a CSV in
  `sandbox/outputs/<run>/`, past the `MAX_SQL_ROWS` analysis cap, up to `EXPORT_MAX_ROWS` (5,000,000); the model
  sees only the row count and first rows. The file is always attached to the answer.
  The `api` backend is now called `chatgpt`. 300 unit tests pass.
- Replay check (2026-10-29): full golden eval on Haiku scored 0.91 (15 tasks). All 15 replay scripts run, every
  step MATCHes, and every number in the answers is traced (notes totals included); against restated data the
  affected tasks report DIFF and `[was …]`. Fixed: exports, empty-cell row keys, notes totals, run IDs, sign-in.
  Since then: each query replays on its own connector (`SOURCES`), and a step that fails on changed data is
  recorded and skipped (`with step(ref)`), with every check in one final Checks cell.
- Join test data (2026-10-29): `credit_exposures` (sample_lake) and `desk_risk` (new `market_lake` connector) are keyed
  by their own codes and join to `usage_limits` through `inputs/book_mapping.csv` in run_python. Live Haiku runs of
  both joins finished correctly (credit reconciles to 0 on 30 keys; market replay MATCHes on two sources).
  Then: a three-dataset join test; the step budget scales with the plan (3 per plan step, at least 15); old
  dataset descriptions keep filters and columns; a dataset skill is not suggested for datasets it does not cover.
  Live Haiku: the credit reconciliation (12 steps) and the three-dataset join (10 steps) finish. 313 unit tests pass.
  Golden eval on the production stand-in (session_sim, MODEL_MEMORY=session, Haiku): 0.93 (main: 0.74 on the same
  backend). Only `rwa_limit_status_now` fails (step cap 8; it ran 4 queries for a one-query skill). A golden task's
  `max_steps` stays an exact cap in the eval (no plan scaling).
  Users can set a conversation's step limit (sidebar "Step limit per instruction", CLI `--max-steps`; 0 = automatic).
- Libraries over custom code (2026-10-29): sqlglot parses SQL (guard, tables, required filters, row limits in the
  connector's `dialect`, formatting); Parquet table cache (no pickles); Jinja2 page template for the HTML export;
  tenacity retries; pydantic-settings reads and checks the environment once (config.Settings). All pinned in
  requirements.txt.

## Next tasks, in order
1. Pin `requirements.txt` to production's exact Python and package versions (ask the owner). The grid number
   format (`NumberColumn(format="localized")`) and `st.segmented_control` need a recent Streamlit: check them.
2. Production wrapper: copy `agent/backends/prod_template.py` (and the ODBC lake template) into modules that wrap
   the real API, and a connector folder for the real lake (`extensions/connectors/_template/`). Done inside the
   closed environment, by the owner. Then `scripts/smoke_model.py --backend prod`.
3. Re-run the full eval against `prod` and re-tune prompts only if needed (one change at a time, before/after).
4. On this branch: run the full golden eval on the LangGraph engine and compare with `main`'s recent runs; get the
   extra packages approved; then decide whether to merge.
5. Optional: tell the executor how many rows a
   cut-off preview holds.

## Design decisions (keep unless the owner agrees otherwise)
- Plan → execute → review, with JSON actions validated by Pydantic. Chosen because small models are
  unreliable at open-ended tool use.
- Effort policy: `none` by default; `medium` for planner, reviewer, recovery after an error or repeat,
  and the last JSON-repair attempt. Every call is logged with effort and latency so this can be tuned.
- LangGraph runtime (2026-10-28, branch `langgraph-prototype`): one `create_agent` graph with checkpoints and
  breakpoints replaced the custom step machine, so Streamlit reruns, approvals and resume all use the same code and
  less of it is ours. The previous step machine is on `main`.
- Arithmetic lives in tools (`run_python`, SQL), never in the model. A code check and the reviewer verify
  that numbers in the final answer trace to refs.
- Standard library + pydantic/pandas/openpyxl/streamlit + langchain/langgraph (pinned) only, because production is
  closed.

## Known limitations / gotchas
- Tool timeouts use a thread: a timed-out tool keeps running in the background. Tools get `TOOL_TIMEOUT_S` (30 s);
  `sql_query` gets `SQL_TIMEOUT_S` (900 s, lake queries can run 15 minutes). A query cut off or stopped by the user
  keeps running on the lake server until it ends: set the lake driver's own query timeout to match.
- `pysandbox` has no OS-level network or memory isolation (imports and readers are restricted instead, and a
  CPU/time limit applies). Review with security before production.
- `recall` and `search_docs` are keyword-only (no embeddings).
- `datalake.check_select` parses the query with sqlglot (one read-only query on documented datasets). In production the read-only lake
  account and the dataset files (the allowlist) are the real protection; the guard is a second layer.
- The `claude` backend (Claude Code CLI) is for development only: prompts go to Anthropic, so synthetic data only.
  Each call starts a CLI process (a few seconds of overhead).
- Token counts are estimates (chars / 4).
- `DemoFakeModel` picks its next step by counting `OBSERVATION:` lines, so it only suits the short demo.
- A conversation gets the files attached in the message box or named in the message (matched by file name).
- `st.dataframe(use_container_width=True)` may warn on newer Streamlit; switch to the new width argument if so.
- `ollama_api.py` defaults to `qwen3:4b`; the owner should choose the model(s).
