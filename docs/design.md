# Design spec — Python agent over a text-only model API

Design spec for a small, auditable agent that runs on a chat-only model
(the production GPT model, effort `none` or `medium`) through an existing API module.
Written so that a person or a coding agent can build it section by section.

---

## 1. Assumptions

- An API module already exists. It takes input text (or a message list) and returns output text.
  The only place that knows its real signature is `adapter.py` (§4).
- The model has **no native tool calling**. Tools are invoked through a JSON text protocol (§5).
- Effort is selectable per call: `none` (fast, cheap) or `medium` (better reasoning, slower).
- Python orchestrates everything: loop, tools, memory, validation, logging.
  The model only ever produces text.
- No confidential data is sent to the model unless the deployment is approved for it.

---

## 2. Architecture

```
   Streamlit UI (app.py) ── or ── CLI / tests
            │  goal, approvals
┌───────────▼──────────────────────────────┐
│ Python orchestrator                      │
│  ┌────────────┐ ┌────────┐ ┌──────────┐  │
│  │ Agent loop │ │ Tools  │ │ Memory   │  │
│  └────────────┘ └────────┘ └──────────┘  │
│  parsing · validation · retries · logs   │
└───────────▲──────────────────────────────┘
            │  complete(messages, effort)
┌───────────▼──────────────────────────────┐
│ Model adapter (wraps existing API module)│
└───────────▲──────────────────────────────┘
            │
      production GPT model (text only)
```

Rules:
- Everything above the adapter is backend-independent.
- Everything below the UI is UI-independent. `app.py` only calls `loop.py`,
  so the same agent runs from Streamlit, the CLI, or tests.

---

## 3. Repository layout

```
app.py            # Streamlit UI: chat, step trace, approvals, file upload (§12)
results_page.py   # Results view of a conversation's tables (opened only on request)
cli.py            # same agent from the command line
eval.py           # golden-task evals (tests/golden/*.json → runs/evals/)
agent/
  adapter.py      # ChatModel interface, ModuleChatModel wrapper, get_model(backend)
  backends/
    fake.py         # FakeModel (scripted, for tests) + DemoFakeModel (demo run)
    ollama_api.py   # local model on a Mac, same call(text, effort) as production. Thinking follows effort by
                    # default (off for none, on for medium; retried without it if the model can't think);
                    # OLLAMA_THINK=0 leaves the model's default
    claude_cli.py   # Claude (Haiku by default) via the Claude Code CLI on the Mac; development only
    prod_template.py# shape of the production module
    datalake_odbc_template.py # shape of the production data lake module (the production lake over ODBC)
    datalake_mock.py          # client for the mock lake server (DATALAKE=agent.backends.datalake_mock)
  protocol.py     # JSON parsing, validation, repair schedule
  tools.py        # @tool registry, shared helpers, dispatch (tools live in toolkits)
  toolkits/       # built-in toolkits, all basic: files, docs, analysis, datalake
  pysandbox.py    # model-written analysis code: static check, then an isolated run via _pyrunner.py
  plugins.py      # loads toolkits + plugins, catalogue, /command parsing, basic toolkits
  datalake.py     # read-only SQL access: check_select guard, server-side limits, required filters, metadata cache
  tablecache.py   # each table file parsed once per version (memory + pickles in sandbox/work/.cache/)
  replay.py       # exports a conversation as one replayable analysis script (§13)
  export.py       # exports a conversation as one self-contained HTML page (§12)
  workbook.py     # Excel layout reader (table blocks, header rows, totals, formulas) and read_range
  outputs.py      # tool outputs as tables (shared by the UI and the HTML export)
  sqlfmt.py       # SQL for people: one clause per line (approval cards, Sources, replay); never changes what runs
  skills.py       # parse plugins/skills/*.md, list/render skills
  memory.py       # state, persistence, logging, rolling summary, prompt builder
  loop.py         # step machine: plan → execute → review
  prompts/        # system.md, planner.md, reviewer.md, action_review.md, summariser.md, finisher.md
  settings.py     # user prompt settings: role, answer style, template overrides (settings/prompts.json)
  config.py       # budgets, limits, effort policy, paths, backend choice
plugins/          # extension point: tools/*.py (toolkits), skills/*.md, examples/, README.md (contract: docs/plugins.md)
runs/             # one folder per run: state.json, log.jsonl, outputs/
sandbox/          # the only folder tools may read or write
  inputs/         # input files (uploads land here)
  outputs/<run>/  # files written by write_file / save_table (approval needed)
  work/<run>/     # scratch tables from sql_query and run_python (no approval: not user-facing)
  work/.cache/    # parsed tables (pickles) written only by the agent (tablecache)
  context/        # user's context documents (definitions, methodology)
  references/     # reference documents skills cite, e.g. datalake_dictionary.md (what each lake view means)
mock_lake/        # local stand-in for the remote lake, never inside sandbox/
  data/           # synthetic CSVs, one per view
  server.py       # mock lake server: separate read-only process over HTTP (python mock_lake/server.py)
sources.py        # UI: [ref: …] / [doc: …] → numbered links and a Sources list under the answer
tests/
  test_protocol.py  test_tools.py  test_loop_fake_model.py  test_app.py  test_conversation.py
  test_datalake.py  test_mock_lake.py  test_io.py  test_pysandbox.py  test_replay.py  test_sources.py
  test_skills.py  test_plugins.py  test_settings.py  test_eval.py  test_ollama_backend.py  test_claude_cli.py
  golden/         # eval tasks
```

The executor instructions live in the STATE block built by `memory.render_state`, so there is no separate executor.md.

---

## 4. Model adapter (`adapter.py`)

One interface. The rest of the code calls nothing else.

```python
from typing import Literal
import time, existing_api_module as api   # <- your module

Effort = Literal["none", "medium"]

class ChatModel:
    def complete(self, messages: list[dict], effort: Effort = "none",
                 stop: list[str] | None = None, retries: int = 3) -> str:
        text = self._render(messages)            # adapt if the module accepts a message list
        for attempt in range(retries):
            try:
                out = api.call(text, effort=effort)   # <- the one real call
                return self._apply_stop(out, stop)
            except Exception:
                if attempt == retries - 1: raise
                time.sleep(2 ** attempt)

    @staticmethod
    def _render(messages):
        """Flatten roles if the module only takes a single string."""
        parts = [f"{m['role'].upper()}:\n{m['content']}" for m in messages]
        parts.append("Reply as ASSISTANT. Output only your reply.")
        return "\n\n".join(parts)

    @staticmethod
    def _apply_stop(text, stop):
        if not stop: return text
        cuts = [text.find(s) for s in stop if s in text]
        return text[:min(cuts)] if cuts else text
```

In the skeleton, `complete()` also takes `purpose` (planner, executor, reviewer, summariser).
Real backends ignore it; FakeModel uses it to route scripted replies and the log records it.
Backends are chosen with `LLM_BACKEND=fake|ollama|claude|prod`; `prod` imports the module named by
`PROD_API_MODULE`, which must expose `call(text, effort) -> str`. `claude` runs `claude -p` (Claude Code CLI,
own tools off, our system prompt, empty temp folder; effort none→low, medium→medium) for fast development
tests with a real model. Its prompts go to Anthropic, so synthetic data only. Backends may expose
`model_name(effort)` (logged) and `cancel()` (used by Stop).

Contract: the adapter is **stateless**. The caller passes the full message list on every call.
If the module accepts role-tagged messages natively, pass them through instead of `_render`.

---

## 5. Protocol (`protocol.py`)

The model must reply with exactly one JSON object per turn.

```json
{"thought": "short reasoning", "tool": "read_file", "input": {"path": "data.xlsx"}}
{"thought": "short reasoning", "final": "the deliverable"}
```

Rules given to the model:
1. Output one JSON object only. No prose outside it.
2. Use exactly one of `tool` or `final`.
3. Use only listed tools, with arguments matching their schema.
4. Never invent tool results. Wait for `OBSERVATION:`.
5. If information is missing, use `ask_user` rather than guessing. Never use `ask_user` to ask permission:
   risky tools ask for approval themselves.
6. If data has several periods and the goal doesn't say which, use the latest and name it in `final`.

Python side:
- `extract_json(text)`: take the last valid `{…}` block, ignoring reasoning and quoted JSON before it. Tolerate code fences.
  Reject a broken or cut-off block after it, and two objects back to back (two actions at once).
- Validate with Pydantic: the tool exists, the arguments match the schema, exactly one of `tool`/`final`.
- On failure, retry from the original messages plus only the latest bad reply (last 400 chars) and
  `"Invalid output: <error>. Reply with one JSON object per the protocol, and no other text."`, so the prompt doesn't grow.
  At most 2 repair attempts with effort `none`, then escalate one attempt to `medium`, then abort the step.
- Always call with `stop=["OBSERVATION:"]`.

---

## 6. Tools (`tools.py`)

### Registration

```python
TOOLS = {}

def tool(name, desc, args: type[BaseModel], risky=False):
    def wrap(fn):
        TOOLS[name] = dict(fn=fn, desc=desc, args=args, risky=risky)
        return fn
    return wrap
```

The system prompt is rendered from `TOOLS`: name, description, JSON schema, one example.
Code and prompt never drift apart.

### Toolkits and plugins (`agent/toolkits/`, `plugins/`, `plugins.py`)
Tools are grouped in **toolkits**: one module with `TOOLKIT = {"name", "description", "basic", "guidance"}`
and tools registered with `@tool` (each tool records its toolkit; `ask_user` is `core`, always on).
`plugins.load()` imports `agent/toolkits/*.py`, then `plugins/tools/*.py` (`_*.py` skipped); a plugin that fails
is listed in `plugins.ERRORS` instead of breaking the app; duplicate toolkit or tool names are load errors.
Built-in, all basic: `files`, `docs`, `analysis`, `datalake`. `guidance` is prompt text added only
while the toolkit is enabled. Lake data is never a file the tools can open: it lives outside the sandbox
(`mock_lake/data/`), and goes only through the `datalake` toolkit, as it will in production. The contract for new toolkits and skills is
`docs/plugins.md`. **Rule:** an agent asked to add a tool, toolkit, plugin or skill only adds files under
`plugins/` (and tests); it does not change the infrastructure (`agent/`, `app.py`, `cli.py`, prompts, config)
or the docs unless the user specifically agrees.

### Per-session tools, /commands and suggestions
- A session starts with the basic toolkits (`config.BASIC_TOOLKITS` overrides). The user adds or removes
  toolkits in the sidebar, with `/name` in the task (a toolkit, a tool → its toolkit, a skill → forced skill +
  its toolkits; `plugins.parse_commands`), or with CLI `--tools`. They stay on until End session (CLI: one
  invocation = one session). Each run records its toolkits in `state["toolkits"]`.
- The model sees only enabled tools; `parse_action` validates against them.
- **Suggestions, the user decides:** the planner may reply `"suggest": {"toolkits", "skill", "why"}` (once per
  run). Real, not-yet-enabled items pause the run in `awaiting_choice`; `resolve_pending(choice=...)` accepts
  only offered items: a skill → its steps and toolkits; toolkits only → re-plan once; nothing → keep the plan.
  Mid-run, if the executor calls a tool from a disabled toolkit, the run pauses with a one-toolkit suggestion
  (`replan: false`): accepted → continue with it; declined → feedback, and that toolkit is not offered again.

### Built-in tools (by toolkit: files = read_file, write_file, save_table; docs = recall, search_docs;
analysis = run_python; datalake = list_datasets, describe_dataset, sql_query; core = ask_user)

| Tool | Purpose | Returns | Risky |
|---|---|---|---|
| `read_file` | Read TXT/MD/JSON/CSV/XLSX in the sandbox | CSV: shape + first rows; Excel: the workbook layout (below); text: the start; and a `ref` | **yes** for the user's files; no for this conversation's `work/` and `outputs/` |
| `write_file` | Save text to `sandbox/outputs/<run>/` | path | **yes** |
| `save_table` | Deliver a work table to `sandbox/outputs/<run>/` | path | **yes** |
| `recall` / `search_docs` | Keyword search in the run's memory / context documents | snippets with source | no |
| `run_python` | Model-written pandas code for every calculation (see below) | agreed JSON result + work table | **yes** |
| `list_datasets` / `describe_dataset` | Views with meaning, required filters, columns and partitions (catalogue and dictionary; no data rows) | text | no |
| `sql_query` | One read-only SELECT on the lake (filtered and aggregated) | preview + saved work table path | **yes**; no when a repeat is answered from its saved result |
| `ask_user` | Pause and ask the human | the answer | no |

**Arithmetic is done by code, never by the model.** One tool, `run_python`, replaces the per-operation tools of
the first version (calc, add_column, aggregate…): the model writes the whole calculation once, not row by row.
- Static check before approval (`pysandbox.check_code`, AST): imports only from `ALLOWED_MODULES` (pandas, numpy,
  math, statistics, datetime, collections, itertools, functools, re, json, decimal, …; shipped packages only),
  no dunders, no `open/exec/eval/compile/getattr/…`, reads only via `pd.read_csv/read_excel/read_json`,
  `pd.ExcelFile` or `read_table`, no writing methods (`.to_csv`, `np.save`, …).
- The user approves the code (the card shows it), then it runs in a separate `python -I` process in an empty
  folder with restricted builtins, a CPU/time limit and guarded readers: sandbox files only (with or without a
  `sandbox/` prefix), never the lake's data, never URLs. No OS-level network or memory isolation yet.
- Data in: `tables` (name → sandbox path, never data) and file reads. Result out, in the agreed format: the code
  sets `result` (+ `notes`); the harness returns `{"ok", "result": {"type": "table"|"value", …}, "notes", "stdout"}`
  and saves a table result as a work table. The code never writes files; printed output becomes the result if
  `result` is unset. The guidance tells the model never to type data values into code.

### Data lake (`datalake.py`)
The data lake is a **remote, read-only data lake cluster**. Nothing is ever loaded, uploaded or written to it, and the
user's attached files are never part of it: they are local inputs, analysed with `read_file` / `run_python`. The
toolkit guidance, the planner prompt ("INPUT FILES … are never in the data lake") and the `usage_overview` skill
(lake views only) all say so, after a run offered a lake skill for an uploaded workbook.
Production: the lake via SQL over ODBC (a module shaped like `datalake_odbc_template.py`, `DATALAKE=<module>`).
Development, from the synthetic CSVs in `mock_lake/data/` (outside the sandbox, `config.LAKE_DATA_DIR`):
- `DATALAKE=sqlite` (default, tests): an in-process, read-only in-memory SQLite copy.
- `DATALAKE=agent.backends.datalake_mock`: the **mock lake server** (`mock_lake/server.py`, standard library),
  a separate process on localhost reached over HTTP (`GET /views`, `POST /query`), shaped like the production
  module (DB-API `connect()` + `list_views()`). The server itself is read-only (SQLite authorizer) whatever the
  client sends, can require a token (`MOCK_LAKE_TOKEN`) and add latency (`MOCK_LAKE_LATENCY_MS`); when it is
  down, tools return "Data lake server not reachable … Start it with: python mock_lake/server.py".
What each view means is in `sandbox/references/datalake_dictionary.md` (`datalake.view_notes`), a reference
document the skills cite.

**The sample view `usage_limits`** (`mock_lake/generate_view.py`) mirrors a typical usage-and-limits view: DemoBank
Group's RWA and leverage exposure under JFSA Basel III in JPY millions, Tokyo business days of FY2025.
- Partitioned by fiscal year in `partition_key` (e.g. `FY2025`); `describe_dataset` lists the partitions from the
  catalogue (`Lake.partitions`; a lake module may supply `partitions(view)`), so the agent never guesses a year.
- Dimensions needed for correct numbers: `partition_key`, `entity`, `rule`, `currency`, `scenario` (USED / ALLOC),
  `resource` (RWA / Leverage exposure), `version` (CLOSED recommended; EARLY arrives first, some dates
  have CLOSED only, the newest EARLY only) and `method` (BOOKED; FRTB internal-model market risk also has DAILY and
  the 60-day AVG60, with BOOKED = max(DAILY, multiplier x AVG60), as for IMCC). Leaving any out double counts.
- Hierarchies as `<dimension>_generation_<n>`: measure (resource > risk type > approach, JFSA OV1 / LR2), region
  and book (group > division > business line > product); one `amount` column; leaf rows only.
- No latest flag: each resource and version has its own latest date (CLOSED lags EARLY, leverage lags RWA). Writes are refused twice: by
`check_select` (one SELECT/WITH statement, no DML/DDL keywords, allowed views only) and by a read-only
connection (SQLite authorizer; read-only lake account).
Query results are saved as work tables, and later tools take the path: the model never retypes numbers.

**Breach scenarios in the sample data** (`BREACHES`, `LIMIT_CUTS` in the generator), each with a different source,
for testing breach analysis: a usage spike (FX Options London, FRTB-SA, 2026-03-02..13), an internal-model spike
whose 60-day average keeps the breach going after the spike (Equity Derivatives Hong Kong, 2025-11-10..12-30), a
leverage spike (Repo Tokyo, gross SFT assets, 2026-02-02..20), and a limit cut at a quarter reset with usage flat
(Term Loans Tokyo, A-IRB, limit 29,810 -> 22,110 from FY2025Q4).

### Excel workbooks (`workbook.py`)
Real workbooks are report-style: titles, multi-row headers, several tables per sheet, totals rows, formulas.
`read_file` on `.xlsx` returns `workbook.inspect`: per sheet, its table blocks (row bands split by empty rows and
columns, so side-by-side tables are separate; a one-cell first row is a title; header rows detected and joined
into column names, spanning headers filled), label columns, totals rows ("do not add them"), the formulas as
patterns ("E5:E8 =Dn-Bn (row by row)", "B8 =SUM(B5:B7)"; formula cells with no saved value are flagged), a short
preview, and the `read_range(path, sheet, range, header_rows)` call that loads a block. Analysis code gets
`read_range` (guarded to sandbox files; loaded by path in the isolated runner). The layout is read from the first
`MAX_SCAN_ROWS` rows; openpyxl only. Library-internal imports (`_strptime`, `time`, `calendar`, `locale`) are
allowed at run time only, so pandas date parsing works; the static check still refuses them in the code.

**IO cost.** Views can hold billions of rows and input files 50k+, so the design avoids needless reads:
- Every query asks the server for at most `MAX_SQL_ROWS` + 1 rows (`datalake.limit_sql` appends `LIMIT`, or
  wraps a query that has its own; a lake module may supply `limit_sql` for its dialect). Columns come from a
  zero-row query; `describe_dataset` returns no data rows.
- A result over `MAX_SQL_ROWS` is refused, not truncated: nothing is saved and the model is told to filter (WHERE)
  and aggregate (GROUP BY, SUM/COUNT/AVG) in SQL to the level the goal needs, then run it again.
- Required filters are standing background, not errors: the dictionary's `Default filters:` line (recommended
  values, e.g. `partition_key = 'FY2025' AND … AND version = 'CLOSED' AND method = 'BOOKED'`) and the required filters are
  rendered into the datalake toolkit's guidance (`datalake.view_rules`; a toolkit's `guidance` may be a function)
  so the agent knows them before writing SQL. `sql_query`'s `check` (`datalake.static_problem`) runs before
  approval: a query missing one goes back to the executor as a `precheck` correction and never runs.
- The data dictionary lists each view's required filters (`Required filter: \`partition_key\`, \`scenario\` (or GROUP
  BY), …`, `datalake.required_filters`): every group must be filtered on one of its columns, and a group marked
  "(or GROUP BY)" may instead be split by in GROUP BY (values never added together). The partition must always be
  filtered. A query that breaks this is refused before it reaches the server. A keyword check, like
  `check_select`.
- Views, columns and partitions are cached per lake source for `LAKE_META_TTL_S`, not fetched on every call or rerun.
- The same SQL (whitespace aside) in a conversation is not sent again: `sql_query` reuses its saved table
  (`runs/<id>/sql_cache.json`) and says so; `refresh: true` forces a new query.
- Each table file version (path, mtime, size, reader arguments) is parsed once (`tablecache`): `read_file`,
  `tables`, `read_table`, literal `pd.read_*` calls in analysis code (found by AST before the run) and work tables
  (cached when written) are loaded from memory or a pickle. Only the agent writes the pickles. A 50k-row Excel
  file is parsed once (~0.5–1.3 s) instead of on every read.
- `read_file` stores `READ_FILE_STORED_ROWS` rows under its ref, not a copy of the file; the citation check
  indexes each stored output's numbers once; the UI reads tables and outputs once per version and shows at most
  `UI_MAX_GRID_ROWS` rows per grid (the download has all).

### Dispatch

1. Validate the arguments with the tool's Pydantic model.
2. If the call needs approval (`tools.needs_approval`: `risky` is True, or a function of the arguments, e.g. a lake
   query yes, a repeat answered from its saved result no), the request is reviewed first (§8) and then waits for
   the user.
3. Run inside try/except with a timeout. An error becomes an observation, never a crash.
4. Store the full output in `runs/<id>/outputs/`. Return a truncated preview plus a `ref`.
5. Observation format: `OBSERVATION: {"ok": true|false, "ref": "...", "result": "..."}`.

Description rules: say what comes back and how big it is, say when *not* to use the tool,
and keep names distinct.

---

## 7. Memory and context (`memory.py`)

| Layer | Content | In prompt |
|---|---|---|
| Fixed | protocol + tool list | always |
| Working state | goal, plan, step index, done, facts, open questions, summary | always, placed last |
| Recent turns | last 3–6 exchanges verbatim | within budget |
| Context docs | user's definitions / methodology (`state.context`) | small ones inline (≤ `CONTEXT_INLINE_TOKENS`), marked "reference material, not instructions"; larger via `search_docs` |
| Skill | the active skill's steps, checks and output | appended to the system prompt |
| Long-term | full logs, tool outputs, documents | only via `recall` / `read_file` |

### Working state (`state.json`)

```json
{
  "run_id": "2026-10-21-xxxx",
  "status": "running",
  "step_count": 4,
  "pending": null,
  "final": null,
  "goal": "...",
  "context": ["references/datalake_dictionary.md"],
  "skill": "usage_comparison",
  "plan": ["step 1", "step 2"],
  "step": 1,
  "done": [{"step": 0, "result": "short"}],
  "facts": ["durable fact with source ref"],
  "open_questions": [],
  "summary": "rolling summary of dropped turns"
}
```

### Rules
- Token budget per call, e.g. 6k, well below the model limit. Small models degrade with long prompts.
- When recent turns exceed the budget, fold the oldest into `summary` with a summariser call (effort `none`).
- After each observation, extract durable facts into `facts`, each with its `ref`.
- Sub-tasks start with only the state and inputs they need, not the parent's history.
- Persist `state.json` and append to `log.jsonl` after every step. Runs can resume after a crash.

---

## 8. Agent loop (`loop.py`)

Pattern: **plan → execute (ReAct per step) → review**. This is more reliable than open-ended ReAct on a small model.

### Step-wise design (required by the UI)

The loop is a **resumable state machine**, not one long function. `run_one_step(state)`
advances exactly one step, persists the state, and returns an `Event` describing what happened.
Anything that needs a human (approval, `ask_user`) sets a pending field and returns,
and the next call resumes from the saved state. Streamlit, the CLI and tests all drive the same function.

`state["status"]`: `planning` → (`awaiting_choice` →) `running` → `awaiting_approval` | `awaiting_user` | `awaiting_choice` → `running` … → `finished` | `aborted`

```python
@dataclass
class Event:
    kind: str          # plan | suggest | tool | observation | final | review | approval | ask_user | error | abort
    payload: dict

def run_one_step(state) -> Event:
    if state["status"] == "planning":
        state["plan"] = plan(state["goal"])                      # effort: medium
        state["status"] = "running"
        return persist(state, Event("plan", {"plan": state["plan"]}))

    if state["status"] in ("awaiting_approval", "awaiting_user"):
        return Event(state["status"], state["pending"])          # wait for the human

    if state["step_count"] >= MAX_STEPS:
        return abort(state, "max steps reached")

    reply = model.complete(build_prompt(state), effort=pick_effort(state),
                           stop=["OBSERVATION:"])
    action = parse_and_validate(reply)                           # repairs inside
    state["step_count"] += 1

    if action.final:
        verdict = review(action.final, state)                    # effort: medium
        if verdict.ok:
            state["status"], state["final"] = "finished", action.final
            return persist(state, Event("final", {"text": action.final}))
        add_feedback(state, verdict.issues)
        return persist(state, Event("review", {"issues": verdict.issues}))

    if is_repeat(state, action):
        add_feedback(state, "Repeated action; change approach.")
    if action.tool == "ask_user":
        state["status"], state["pending"] = "awaiting_user", action.input
        return persist(state, Event("ask_user", action.input))
    if TOOLS[action.tool]["risky"]:
        state["status"], state["pending"] = "awaiting_approval", action.dict()
        return persist(state, Event("approval", action.dict()))

    obs = dispatch(action)
    update_state(state, action, obs)                             # facts, step progress
    return persist(state, Event("tool", {"action": action.dict(), "obs": obs}))

def resolve_pending(state, approved: bool | None = None, answer: str | None = None):
    """Called by the UI/CLI when the human responds."""
    pending = state.pop("pending")
    if state["status"] == "awaiting_user":
        update_state(state, pending, {"ok": True, "result": answer})
    elif approved:
        update_state(state, pending, dispatch(pending))
    else:
        add_feedback(state, f"User rejected {pending['tool']}. Choose another approach.")
    state["status"] = "running"
    persist(state)

def run(goal, on_event=print, approve=cli_approve, ask=cli_ask):
    """Convenience driver for the CLI and tests."""
    state = new_state(goal)
    while state["status"] not in ("finished", "aborted"):
        ev = run_one_step(state); on_event(ev)
        if state["status"] == "awaiting_approval": resolve_pending(state, approved=approve(ev))
        if state["status"] == "awaiting_user":     resolve_pending(state, answer=ask(ev))
    return state
```

### Scope confirmation
When the goal leaves a data choice open that cannot be inferred with confidence, above all the time range (real
data spans 13-14 years) or the resource, scenario, version or level, the agent asks once before the first data
query, proposing the values it would use; the user confirms ("Yes, go ahead" in the UI) or corrects. The planner
makes this the first step ("Confirm with the user: …"), the system prompt (rule 5) and `ask_user` say how, and the
request reviewer rejects a query built on an unconfirmed open choice. A precise goal uses the documented defaults
and does not ask. Evals answer such questions with "Yes, confirmed"; golden tasks may check `expect_ask` /
`max_asks`.

### Request review before approval
Every `sql_query`, `read_file` and `run_python` request (`config.REVIEWED_TOOLS`) is checked by the reviewer
(`action_review.md`, effort `medium`) before the user sees it: it serves the step, applies every required filter
with the recommended values, aggregates to the level needed without scanning more than needed, does not repeat
or re-query what an extract already gives (a check is done in Python), and the analysis logic is right. A
rejected request goes back to the executor as feedback without asking the user (and is not counted as a repeat);
after `MAX_ACTION_REJECTIONS` the user is asked anyway, with the reviewer's notes on the card. An approved request
carries the reviewer's plain-language `explain`, shown on the approval card. The reviewer also gets the full SQL /
code of earlier requests and the previous request of the same tool: a revised request's `explain` starts with
"Changed from the previous request: …" (shown first and in bold on the card), and the approval event stores
`previous`, which the card offers as a diff, folded by default.
Static problems (`Tool.check`: unsafe analysis code, a missing required filter) are sent back the same way,
before the reviewer and before the user. The result review of the final
answer stays, for what can only be seen in results.

### Stop conditions
`final` accepted by the reviewer · `MAX_STEPS` (default 15) · token/cost budget · repeated action ×2 · user abort.

A backend failure (model unreachable after the adapter's retries) is not a stop: `run_one_step` rolls the step back,
records a `model_error` event, and the step can be retried (UI: Retry button; CLI: `--resume`).

User abort (UI Stop) also cancels the model call in flight (`ChatModel.cancel()`; Ollama stops generating).
`run_one_step` checks after every model call and never acts on a reply that arrives after the abort.

Repeats: the first repeat of a call that succeeded marks the current plan step done (small models often
never set `step_done`), so STATE points at the next step or the final answer. A second repeat in a row aborts,
unless all plan steps are done and there are facts: then `finish()` makes one plain request (`finisher.md`:
goal + facts with refs → `{"final": ...}`, effort `medium`). Its answer still goes through the reviewer and is
marked `via: finisher`; if it is unusable, the run aborts as before.

---

## 9. Effort policy (`config.py`)

Default to `none`. Spend `medium` only where reasoning quality changes the outcome.

| Call | Effort | Why |
|---|---|---|
| Planner | medium | Decomposition errors propagate to every step |
| Executor step (routine tool choice) | none | Narrow, well-specified decisions |
| Executor step after an error or repeat | medium | Recovery needs reasoning |
| JSON repair (first 2 tries) | none | Formatting only |
| Summariser / fact extraction | none | Compression, low risk |
| Reviewer | medium | The final quality gate |
| Request review (before each approval) | medium | Catches wrong filters and needless lake load before the user is asked |
| Finisher (stuck executor, plan done) | medium | Last chance to answer from the facts |
| Chat mode | none | Direct chat; reasoning models think anyway |
| Re-plan after a failed step | medium | Same as planner |

```python
def pick_effort(state):
    return "medium" if state.get("last_error") or state.get("repeat_flag") else "none"
```

Log the effort and latency of every call so the policy can be tuned with data.

---

## 10. Prompts (`prompts/`)

- **system.md**: role, protocol rules (§5), rendered tool list, "state is authoritative" note. The final shape
  may list `"tables"` as `{"name", "title"}`; they are shown under the answer as titled grids, so the answer must
  not also write them out (rule 11), a table keeps the columns that qualify its numbers (date, version, resource,
  unit), and SQL in an answer goes in a ```sql block, one clause per line. Rule 12: fiscal years run 1 April to
  31 March.
- **planner.md**: "Break the goal into the fewest steps: 1–7, a simple question needs one. Plain-word actions,
  not tool names." Shape-only example `{"plan": ["<step>", ...]}`. `plan()` drops copied `<step>` placeholders
  and rejects a plan made only of tool names; then the goal is the single step. It must not plan steps that
  re-check extracted data with another query, and suggests a skill only when the goal asks for what it produces.
- **executor.md**: "Current step: {step}. Choose one tool or finish this step. Use facts in STATE."
- **reviewer.md**: "Check FINAL against GOAL and this checklist: complete, consistent with facts,
  numbers traceable to refs, no unsupported claims. Output `{\"ok\": bool, \"issues\": [...]}`."
  It also gets the CONTEXT, the active skill's checks, and the full stored output (with the tool input) of
  every ref cited in FINAL (`cited_outputs`, up to `REVIEW_CITED_CHARS`): facts keep only short previews,
  which made the reviewer reject correct answers. Numbers quoted from context docs are cited `[doc: <file>]`.
  FINAL sits between `<<<FINAL` and `FINAL>>>` marker lines, and the checklist after it is headed "REVIEW
  CHECKLIST (instructions for you, not part of FINAL)": without the markers Haiku read the checklist as part of
  the answer and rejected it. It also gets the documentation of the views the agent read (a valid source for
  units, filter values, calendars and names) and the tables shown under the answer. It reports a problem only
  when it makes the answer wrong: a wrong or unsourced number, a claim contradicting the facts or documentation,
  the wrong date / version / scenario / filter, a direction contradicting the numbers, or an unanswered part of
  the goal. Missing caveats, method notes, scope wording and unrequested checks are not problems (they cost
  review rounds in evals).
- **action_review.md**: the request review before approval (above, §8): `{"ok", "issues", "explain"}`.
- **Citation check** (`loop.uncited_numbers`, code, before the LLM reviewer): every number in FINAL (except dates,
  periods, list markers, small counts ≤12 and years; rounding allowed) must appear in the evidence: the goal and
  earlier goals, stored outputs (`outputs/*.txt`), context docs, and what the model was shown — tool previews
  (e.g. read_file's `shape=(48, 5)`, which the stored output lacks) and the user's answers to `ask_user`.
  Stored CSV outputs are read both ways ("1,234" as one number, and every value between commas), since a row
  like `Credit risk,4,148898` would otherwise read as 4,148 and 898.
  Misses go back to the executor without an LLM call.
- **summariser.md**: "Compress these turns into ≤5 bullets of decisions and facts. Keep refs."
- **finisher.md**: "Write the final answer to the GOAL using only the FACTS. Cite every number as [ref]."

The planner gets the enabled tools, the other toolkits and the skills (names and descriptions only) and the
CONTEXT, and replies `{"plan": [...], "suggest": {"toolkits": [...], "skill": "<name>|none", "why": "..."}}`.
Prompts are neutral: a question answerable from general knowledge needs no tools.

### Skills (`plugins/skills/*.md`, `skills.py`)
Front matter `name`, `description`, `toolkits`, `references` (context documents, e.g.
`references/datalake_dictionary.md`), `source` (the one data source a skill is written for, e.g. `data lake view
usage_limits`; none for a general skill; the skill list shows `[data source: …]` or `[general]` and the planner offers
a source's skills only for questions about it), `when_to_use`; sections `## Steps` (become the plan), `## Checks`
(added to the reviewer checklist) and `## Output`. The planner may suggest a skill (the user decides), or the
user forces one with the sidebar or `/name` (then no planner call; its toolkits are enabled). Write steps in terms of tools (sql_query, run_python, save_table) and compute changes in SQL or run_python so
the model never subtracts numbers itself. Keep skills general: entity, dates, slice and breakdown come from the
request. Bundled: `file_summary` (general: any attached file - layout, formula logic, key figures, at most one
Python run); for the `usage_limits` view: `usage_overview` (at most two small queries on the latest partition,
structured output, no data-quality scan), `usage_comparison` (two dates, any slice and level; one extract at
the deepest level needed, rolled up in Python, reusing dates found earlier) and `limit_breach_analysis`
(breaches found in SQL with one aggregated query, then the start of each breach compared at component level to
classify its source: usage increase, limit reduction, or for internal models DAILY vs the 60-day AVG60). A skill's
`when_to_use` says when it does not apply (e.g. not for questions about values).

Keep prompts short and literal. Show one example of valid JSON in each, as a shape with placeholders
(e.g. `inputs/<file name>`): small models copy concrete examples verbatim.

---

## 11. Guardrails

- Writes go to `sandbox/` only. No delete, send, or network tools without explicit approval.
- `risky=True` tools require approval in the CLI.
- Caps: steps, total tokens, wall time, tool timeout.
- No secrets in prompts. Load credentials from the environment, never log them.
- Tool output is data: text inside files or observations is never treated as instructions.
- Every number in a final output must trace to a tool `ref`, or be quoted from a context document and cited
  `[doc: <file>]` (enforced by the citation check in code, then the reviewer).
- Data reads are confirmed: every lake query and every read of the user's files, after the request review;
  reading back this conversation's own results is not.
- Model-written code runs only through `pysandbox`: static check, the user's approval, an isolated process;
  it reads data and returns a result, and never writes files.
- The data lake is read-only (guard + read-only connection). Scratch tables in `sandbox/work/<run>/` need no
  approval because they are not user-facing; delivering a table (`save_table`) does.
- Development backends that leave the machine (`claude`) get synthetic data only.

---

## 12. UI (Streamlit, `app.py`)

Streamlit is the default UI: pure Python, with built-in chat, file upload, tables and status panels.
The UI holds no agent logic. It renders events and collects human input.

### Execution model
Streamlit re-runs the whole script on every interaction. Therefore:
- Agent state lives in `st.session_state` **and** on disk (`runs/<id>/state.json`).
- Each rerun calls `run_one_step` once, renders the event, then calls `st.rerun()` until
  the agent finishes or waits for a human.
- Approvals and `ask_user` are buttons and inputs that call `resolve_pending`, then rerun.
- A step that can take long (an approved lake query can take minutes, an agent step, analysis code) runs in a
  background thread (`start_job`); every rerun meanwhile shows what is running and the elapsed time and offers no
  actions, so the same query is never started twice.
- For long runs or many users, move execution to a background worker and have the UI
  poll `log.jsonl` (see "Scaling" below).

### Skeleton

```python
import streamlit as st
from agent.loop import new_state, run_one_step, resolve_pending, load_state
from agent.adapter import ChatModel

st.set_page_config(page_title="Agent", layout="wide")

@st.cache_resource
def get_model():               # created once per server process, not per rerun
    return ChatModel()

ss = st.session_state
ss.setdefault("state", None)
ss.setdefault("events", [])
ss.setdefault("autorun", True)

with st.sidebar:
    files = st.file_uploader("Inputs", accept_multiple_files=True)   # saved to sandbox/inputs
    ss.autorun = st.toggle("Auto-run steps", value=ss.autorun)
    run_id = st.text_input("Resume run id")
    if st.button("Resume") and run_id:
        ss.state, ss.events = load_state(run_id), []
    if st.button("Stop") and ss.state:
        ss.state["status"] = "aborted"

for ev in ss.events:                                   # replay trace on every rerun
    with st.chat_message("assistant"):
        st.caption(ev.kind)
        st.json(ev.payload, expanded=False)

if goal := st.chat_input("Describe the task"):
    ss.state, ss.events = new_state(goal, inputs=files), []
    st.rerun()

s = ss.state
if s and s["status"] == "awaiting_approval":
    p = s["pending"]
    st.warning(f"Approve `{p['tool']}` with {p['input']}?")
    c1, c2 = st.columns(2)
    if c1.button("Approve"): resolve_pending(s, approved=True);  st.rerun()
    if c2.button("Reject"):  resolve_pending(s, approved=False); st.rerun()

elif s and s["status"] == "awaiting_user":
    if ans := st.text_input(s["pending"].get("question", "Agent question")):
        resolve_pending(s, answer=ans); st.rerun()

elif s and s["status"] in ("planning", "running"):
    if ss.autorun or st.button("Next step"):
        with st.status(f"Step {s['step_count'] + 1}…"):
            ss.events.append(run_one_step(s))
        st.rerun()

elif s and s["status"] == "finished":
    st.success("Done")
    st.markdown(s["final"])
    st.download_button("Download result", s["final"], file_name="result.md")
```

### UI rules
- **Verbose** switch (off by default; CLI `--verbose`). Off: plan, one line per step (`step_summary`: rows, shape,
  saved path, or size — never data read in), questions, approvals (details collapsed), final answer. On: thoughts,
  tool inputs, results and reviewer issues. The full trace is always in `log.jsonl` and **Model calls**.
- Sidebar **Tools (this session)**, **Skill** (Auto = suggestions only), **Commands** (every `/name`), plugin load
  warnings, **End session** (tools back to basic, skill to Auto, screen cleared; runs stay saved).
- A suggestion is a card with checkboxes: Use selected / Continue without. Accepted toolkits stay on for the session.
- Risky tools never run without a button click, even with auto-run on. The approval card shows the reviewer's
  plain-language explanation, any reviewer notes, and the SQL formatted one clause per line (`sqlfmt`) or the code.
- Data is never shown as raw text: query and file results are grids with a caption (rows, saved path), and the
  one-line step summary reads "9 rows × 2 columns, saved as …". Result tables under an answer carry the title
  the agent gave them.
- Layout: the user's request and the answer are highlighted cards ("Your request", "Answer"); a finished
  instruction folds its steps into one "How I got here · N steps" expander between them (nested expanders become
  captioned blocks there), while the current instruction shows its steps as they happen. A question card offers
  "Yes, go ahead" to confirm the agent's proposal, plus a field to correct it. A revised request's card leads
  with the change in its summary (bold) and keeps "What changed since the previous request (diff)" folded.
- Citation anchors have a scroll margin, so a [n] link lands with its source below Streamlit's sticky header.
- Uploaded files go to `sandbox/inputs/` and are referenced by path. They are never pasted whole into prompts.
- A run gets only the files picked in the sidebar, uploaded, or named in the task text; a plain question gets none.
- Stop run cancels the call in flight; New run clears the screen (the run stays saved and resumable).
- Show the effort level and latency per model call in the trace (from `log.jsonl`).
- A finished answer offers downloads only (nothing to fetch from a working folder): the answer (`.md`), the whole
  conversation (`.html`, `export.py`: requests with their files, folded steps with SQL and code, answers with
  titled tables, numbered sources and timing; one self-contained page with inline styles and no scripts; CLI
  `--export-html`) and the analysis script (`.py`, §13; omitted with a note when nothing was read or run). They
  are built once per conversation state. (macOS may quarantine browser downloads; open with right-click > Open.)
- Each answer shows its timing (`loop.answer_timing`): "Answered in 3 min 05 s (agent 1 min 40 s, waiting for you
  1 min 25 s) · 7 steps", from the instruction's start to the final event, with waiting = time between an
  approval, question or suggestion and the user's response.
- Each request shows the files attached with it (📎 names; `first_files` and the `task` event's `files`).
- At each accepted final answer the agent may rename the conversation (`"title"` in the final reply,
  `loop._retitle`), unless the user renamed it (`rename` sets `title_by_user`).
- Citations: `[ref: …]` and `[doc: …]` in an answer show as numbered links ([1], [2]) to a **Sources** list under
  it (`sources.py`); each source opens to its tool, input (code/SQL) and full stored output (`run_python` tables
  as a grid, ask_user as question + answer). The stored answer and the downloads keep the plain markers.
- Raw model replies are shown only on request (a checkbox under the model-call table), to compare models.
- Chat mode (sidebar) talks to the model directly via `loop.chat_turn`: no plan, tools or review, so its numbers are unchecked. Chats are persisted and logged like runs (purpose `chat`).
- With the Ollama backend, the sidebar picks one model (installed models only) for every call; effort still switches thinking.
- Refreshing the page must not lose work: the run id is shown and runs can be resumed.

### Scaling
- One user / a few users: the step-wise rerun loop above is enough.
- Long runs or several users: run agents in a worker process (a queue plus a `run(goal)` driver).
  The UI only writes goals and approvals to the run folder and polls `log.jsonl`.
- Wider internal use: deploy behind the firm's authentication and approved hosting.
  Don't run a shared instance from a laptop.

---

## 13. Logging and audit

`runs/<run_id>/log.jsonl` gets one line per event:
`{ts, step, kind: model|tool|review|user, effort, model, prompt_tokens, latency_ms, input, output, ref}`.
`model` is the model that answered (from the backend's optional `model_name(effort)`, else the backend name).
This is enough to replay a run, debug failures, and evidence how a result was produced.

**Replay script** (`replay.py`; the "analysis script (.py)" download, or `cli.py --export-script RUN_ID`),
assembled by the harness from what ran, never by the model. Plain Python with `# %%` cells (runs end to end, or
cell by cell in VS Code / Spyder / Jupyter). The analysis comes first as plain code; support code comes after:
- Setup: `run_sql` for the production lake over ODBC (`ANALYSIS_LAKE=odbc`, `LAKE_DSN`) or the mock lake server
  (`ANALYSIS_LAKE=mock`); inputs from `ANALYSIS_INPUTS`; steps run in a scratch folder with their original paths.
- Every read of the user's files, every successful `sql_query` and every approved `run_python` code unchanged,
  in order, at top level, each with the reviewer's `explain` as a comment and its result in a variable named after
  its saved table or file (e.g. `rwa_by_division = run_sql(SQL)`); each instruction and the user's answers as
  headings; failed or rejected attempts as comments. `read_range` is included when a step uses it.
- Then one "support code" cell (result normalising, fingerprints, answer rebuilding), the answers and the check.
- Each answer as a template: numbers are bound to the result cell they came from (step ref, row by its text
  columns, column) and recomputed; a changed number shows as `2,017 [was 1,917]`; untraced numbers stay literal.
- A check of each step against what the agent saw (rows, column sums, digest): `MATCH` / `DIFF: …`; exit code 1
  on any difference. It re-queries the live lake; it does not ship data.

---

## 14. Testing

- **FakeModel**: replays scripted replies. Covers the loop without API calls.
- Step-machine tests: each status transition, approval accept/reject, `ask_user`, resume from `state.json`.
- UI smoke test with `streamlit.testing.v1.AppTest` and FakeModel: a goal runs to `finished`, and an approval button appears for risky tools.
- Protocol tests: malformed JSON, two actions, unknown tool, bad arguments, prose outside JSON.
- Tool tests: deterministic outputs, truncation, sandbox enforcement.
- Data lake tests: the guard rejects writes, multiple statements and unknown views; the connection refuses writes; row cap.
- Skill/context tests: bundled skills parse; suggestions (accept skill, accept toolkits → re-plan, decline);
  forced skill skips the planner; context inline vs listed.
- Plugin tests: built-in toolkits and basic set; only enabled tools in the prompt; /commands; a plugin file is
  discovered (the `_example_toolkit.py` template works when copied); broken plugins are reported; mid-run
  suggestion; file tools refuse lake storage. UI: verbose off prints no data; /commands persist until End session.
- Backend tests without network: Ollama against a local stand-in server, Claude CLI against a fake `claude` script.
- Golden tasks: 5–10 representative goals with expected facts. Re-run after prompt or policy changes.

---

## 15. Build order

1. `adapter.py` plus a smoke test against the real module.
2. `protocol.py` plus tests.
3. `tools.py` with `read_file`, `calc`, `ask_user`, `write_file`.
4. `memory.py` (state, prompt builder, persistence).
5. `loop.py` as the step machine (`run_one_step`, `resolve_pending`), tested with FakeModel, plus `cli.py`.
6. Add planner and reviewer, then the effort policy.
7. `app.py` Streamlit UI on top of the step machine: trace, approvals, uploads, resume.
8. Add `recall`, domain tools, and golden-task evaluation.
9. Optional: background worker for long runs; parallel sub-agents (one orchestrator instance per sub-task, merged by the parent).
