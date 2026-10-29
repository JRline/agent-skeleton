# Design spec — Python agent over a text-only model API

Design spec for a small, auditable agent that runs on a text-only model (production: the GPT CLI, effort `none` or
`medium`) through an existing API module. The agent runtime is **LangGraph / LangChain**: the stock parts do the
generic work (the model ⇄ tool loop, pausing for people, saving and resuming, summarising), and the code here adds
only what the domain and the text-only model need. Written so that a person or a coding agent can follow it section
by section.

---

## 1. Assumptions

- An API module already exists. It takes input text (or a message list) and returns output text. The only place
  that knows its real signature is the backend module behind `adapter.py` (§4).
- The production model has **no native tool calling**. Tools are invoked through a JSON text protocol (§5), added
  by one LangChain chat model (`chat_model.py`), so the rest of the agent is ordinary LangChain. A model with native
  tool calling can be swapped in instead (`LLM_BACKEND=langchain`).
- Effort is selectable per call: `none` (fast, cheap) or `medium` (better reasoning, slower).
- Python orchestrates everything: the graph, tools, checks, logging. The model only ever produces text.
- LangGraph and LangChain (and the packages they pull in) are approved in the closed environment, at pinned
  versions (`requirements.txt`).
- No confidential data is sent to the model unless the deployment is approved for it.

---

## 2. Architecture

```
   Streamlit UI (app.py) ── or ── CLI / eval / tests
            │  goal, approvals, answers, sign-in
┌───────────▼──────────────────────────────────────────────┐
│ Conversation driver (loop.py): one step at a time,        │
│ conversation record runs/<id>/state.json (events, facts)  │
└───────────▲──────────────────────────────────────────────┘
            │  invoke / Command(resume=...)
┌───────────▼──────────────────────────────────────────────┐
│ LangGraph agent (graph.py): one create_agent graph        │
│  Planner → Choice → model ⇄ tools → AnswerCheck            │
│  middleware: SessionPrompt · RequestCheck · RepeatGuard ·  │
│  HumanInTheLoop (approvals) · Summarization                │
│  tools: the toolkits as LangChain tools (lc_tools.py)      │
│  checkpoints: runs/checkpoints.sqlite (thread = run id)    │
└───────────▲──────────────────────────────────────────────┘
            │  LangChain chat model
┌───────────▼──────────────────────────────────────────────┐
│ TextChatModel (chat_model.py): tool calls and structured   │
│ output through a JSON reply protocol, repairs              │
└───────────▲──────────────────────────────────────────────┘
            │  complete(messages, effort, purpose)
┌───────────▼──────────────────────────────────────────────┐
│ Backend (adapter.py): retries, cancel, session memory      │
└───────────▲──────────────────────────────────────────────┘
            │  call(text, effort)
      production GPT model · OpenAI-compatible API · Ollama · Claude CLI · FakeModel
```

Rules:
- Everything above the backend is backend-independent; a native LangChain chat model replaces the bottom three
  boxes (`LLM_BACKEND=langchain`).
- Everything below the UI is UI-independent. `app.py` and `cli.py` only call `loop.py`, so the same agent runs from
  Streamlit, the CLI, eval or tests.
- Generic agent behaviour comes from LangGraph / LangChain; custom code is limited to what they do not provide
  (listed in §8).

---

## 3. Repository layout

```
app.py              # Streamlit UI: chat, step trace, approvals, data sign-in per connector, file upload (§12)
cli.py              # same agent from the command line
agent/              # the framework (fixed)
  graph.py          # the agent: one LangChain create_agent graph and its custom middleware (§8)
  loop.py           # conversation driver: run_one_step, resolve_pending, continue_with, run; events in state.json
  chat_model.py     # TextChatModel: a LangChain chat model over the text-only API (JSON tool calls, repairs) (§5)
  lc_tools.py       # the toolkits as LangChain tools (through tools.dispatch), ask_user, the model-call log
  checks.py         # number check, cited outputs, request history, result tables, answer timing
  credentials.py    # data sign-in per connector, held in memory only (§6)
  connectors/       # loads extensions/connectors/ (§6); built-in connection kinds: sqlite.py, mock.py, odbc.py
  datalake.py       # read-only SQL on datasets: check_select guard, one connector per query, server-side limits,
                    #   required filters, metadata cache
  extensions.py     # loads toolkits (built-in + extensions/toolkits/), catalogue, /command parsing, basic toolkits
  adapter.py        # ChatModel interface, ModuleChatModel, LangChainModel, get_model(backend)
  session.py        # session memory mode: send fixed prompt parts once (§4a)
  backends/         # models: fake.py, openai_api.py, ollama_api.py, claude_cli.py, session_sim.py, prod_template.py
  protocol.py       # finding the one JSON object in a reply (tolerant of fences and reasoning)
  tools.py          # @tool registry, shared helpers, dispatch (tools live in toolkits)
  toolkits/         # built-in toolkits, all basic: files, docs, analysis, datalake
  pysandbox.py      # model-written analysis code: static check, then an isolated run via _pyrunner.py
  tablecache.py     # each table file parsed once per version (memory + pickles in sandbox/work/.cache/)
  replay.py         # exports a conversation as one replayable analysis script (§13)
  export.py         # exports a conversation as one self-contained HTML page (§12)
  workbook.py       # Excel layout reader (table blocks, header rows, totals, formulas) and read_range
  outputs.py        # tool outputs as tables (shared by the UI and the HTML export)
  sqlfmt.py         # SQL for people: one clause per line (approval cards, Sources, replay); never changes what runs
  skills.py         # parse extensions/skills/*.md, list/render skills
  memory.py         # the conversation record (state.json), persistence, log.jsonl, prompt and context helpers
  prompts/          # system.md, planner.md, reviewer.md, action_review.md, finisher.md
  settings.py       # user prompt settings: role, answer style, template overrides (settings/prompts.json)
  ui/               # results_page.py (result tables), sources.py ([ref: …] → numbered Sources list)
  config.py         # budgets, limits, effort policy, paths, backend choice
extensions/         # added per environment (contract: docs/extensions.md); "_" names are templates, never loaded
  connectors/<name>/  connector.md (kind, sign-in, settings, shared notes), datasets/<dataset>.md (one per table),
                    #   optional connector.py (kind custom) and sample.py (synthetic data into a git-ignored data/)
  skills/           # Markdown recipes; may use datasets of several connectors
  toolkits/         # Python tools
  examples/         # worked plans for the planner (planner.md)
scripts/            # eval.py, smoke_model.py, setup_local.py, gpt_pack.py, mock_lake_server.py, sample_inputs.py
tests/              # unit tests (test_loop_fake_model.py: the agent end to end with FakeModel); golden/ for evals
docs/               # all documentation; gpt_prompts/ for scripts/gpt_pack.py
runs/               # runtime: one folder per conversation (state.json, log.jsonl, outputs/); checkpoints.sqlite
sandbox/            # runtime: the only folder tools may read or write
  inputs/           # input files (uploads land here)
  outputs/<run>/    # files written by write_file / save_table / export_query (approval needed)
  work/<run>/       # scratch tables from sql_query and run_python (no approval: not user-facing)
  context/          # user's context documents (definitions, methodology)
```

---

## 4. Model adapter (`adapter.py`) and backends

The graph talks to a **LangChain chat model**. For the text-only production API that is `TextChatModel`
(`chat_model.py`, §5), which calls an `adapter.ChatModel`:

```python
class ChatModel:
    def complete(self, messages: list[dict], effort="none", stop=None, purpose="executor",
                 conversation=None) -> str: ...
    def model_id(self, effort="none") -> str: ...      # logged with every call
    def cancel(self) -> None: ...                       # Stop in the UI
```

`ModuleChatModel` wraps any module with `call(text, effort) -> str`: retries with backoff, stop sequences,
`model_name(effort)` for the log, `cancel()`, and the session memory mode (§4a). `purpose` (planner, executor,
action_review, reviewer, finisher, summariser) is fixed per role: each role gets its own copy of the chat model
(`TextChatModel.for_role`) with its effort from the policy (§9). FakeModel routes scripted replies by purpose.

Backends (`LLM_BACKEND`, `get_model`):

| Backend | What | Notes |
|---|---|---|
| `prod` | the module named by `PROD_API_MODULE` (`call(text, effort)`; copy `backends/prod_template.py`) | production |
| `chatgpt` | ChatGPT, or any OpenAI-compatible chat API (`backends/openai_api.py`: `API_URL`, `API_MODEL`, `API_MODEL_MEDIUM`, `API_KEY_FILE`, `API_EFFORT_PARAM`) | standard library only; the swap to a real API is these settings |
| `langchain` | any LangChain chat model, `LC_MODEL="provider:model"` (`init_chat_model`) | used **natively**: its own tool calling and structured output replace the JSON protocol; needs the provider package |
| `ollama` | a local model on the Mac | development |
| `claude` | Claude (Haiku by default) via `claude -p` | development; synthetic data only |
| `fake` | the scripted demo | tests and demos |

Contract: the caller always passes the full message list. With `MODEL_MEMORY=off` (the default) the adapter sends
all of it on every call (**stateless**). With `MODEL_MEMORY=session`, see §4a.

## 4a. Session memory mode (`session.py`, `MODEL_MEMORY=session`)

**Why.** Every stateless call re-sends the whole prompt: about 6k tokens per executor call, of which about 54%
(rules, tool list, toolkit rules, skill, the conversation's goal and files) is identical every time. When the
transport is slow, that repeated text costs time.

**Production constraints this is built for.**
- The model is reached through a **fixed set of instances**, each with a **fixed effort** and its **own memory**
  that persists between calls.
- Fresh instances (empty memory) exist only when a **new agent session** starts; the same instances serve every
  conversation in that session.
- An instance's memory is **compressed automatically** when it grows (assumed; if the backend drops old messages
  instead, the same refresh rules cover it). Compression can blur exact rules, so nothing may rely on old memory
  for exact formats.

**Channels: one instance per role, not per effort.** Splitting by effort alone would mix the reviewer with the
planner and request check, and split the executor whenever it recovers at medium.

| Channel | Effort | Serves | What each call sends |
|---|---|---|---|
| `executor` | none | executor actions and their repairs | fixed blocks once; then new turns + a one-line reminder |
| `check` | medium | request review (`action_review`) | the whole self-contained prompt, under a "new request" header |
| `review` | medium | planner, reviewer, finisher | the whole self-contained prompt, under a "new request" header |

The mapping is `config.CHANNEL_OF` / `config.CHANNEL_EFFORT`. The executor stays on `none` even when the stateless
policy would raise it to `medium` for recovery: its history lives in one instance.

**What the executor channel sends.** `session.Session.prepare()` takes the full message list the chat model built and
compares it with what the channel already holds (a fingerprint per fixed block, the conversation id, the number
of turns sent):
- **Fixed blocks** — the reply protocol, the tool list, the toolkit rules, the active skill (each `## ` section of
  the system prompt) and the conversation header (goal, input files) — are sent once; a block that changes (a
  toolkit enabled, a skill accepted, a follow-up goal) is sent again alone as `UPDATE (<block>)`.
- **New turns** since the last call (observations, feedback). The model's own replies are not repeated: its
  memory has them.
- **A reminder line** with the exact reply format and the enabled tool names on every call, so each call stays
  correct even if older memory has been compressed. (The plan is a fixed block of the system prompt.)
- **A repair** sends only the correction: the instance already holds its bad reply.

**Full send** (every fixed block, a `NEW CONVERSATION <id>` header and the recent turns) when:
the channel has not seen this conversation yet; the conversation has fewer turns than were sent (a step was
retried from its checkpoint); `resync()` was called (after a failed model call, a protocol error, or a request with a
missing required filter, i.e. when the memory may not match the saved state or the rules may have faded); or more
than `MEMORY_REFRESH_TOKENS` have been sent since the last full send (periodic refresh against compression).

**Other effects.** The graph leaves out `SummarizationMiddleware` (`ChatModel.remembers`): the backend holds the
history. Each model log line records `channel`, `send` (full / delta / repair) and
`sent_tokens` next to the full prompt's `prompt_tokens`, so the full prompt of any call can still be rebuilt.

**Backend contract.** Session mode switches on only if `MODEL_MEMORY=session` **and** the module's `call()`
accepts `channel`: `call(text, effort, channel)`. An optional `new_session()` starts fresh instances; the UI's
End session, each CLI run and each eval task start a new agent session. Modules without `channel` keep working
statelessly, so every existing backend and test is unchanged.

**Production stand-in** (`agent/backends/session_sim.py`). Behaves like the production stack — fixed instances per
channel with fixed effort, memory kept between calls, fresh only on `new_session()`, oldest half compressed into
notes past `SIM_MEMORY_TOKENS` — and answers through an inner backend (Claude Haiku via `claude_cli` by default).
It re-sends each channel's memory to the inner model, so it does not save IO itself; it tests the agent's side.

    LLM_BACKEND=prod PROD_API_MODULE=agent.backends.session_sim MODEL_MEMORY=session python scripts/eval.py --backend prod

**Measured on the stand-in (one run, 2026-10-26).** Executor calls after the first sent 0.5–1.8k tokens instead
of 5.8–9.5k (about 85–90% less); no summariser calls. The `check` and `review` channels still send their full
prompts (3.6–7.3k tokens); sending only what changed there (new request, new facts) is the next step if needed.

**Limits.** One run at a time per agent session (instances are shared). Whether less text means less time
depends on the production transport: measure with session mode on and off.

---

## 5. Reply protocol (`chat_model.py`, `protocol.py`)

The text-only model replies with exactly one JSON object per turn:

```json
{"thought": "short reasoning", "tool": "read_file", "input": {"path": "data.xlsx"}}
{"thought": "short reasoning", "final": "the deliverable", "tables": [{"name": "...", "title": "..."}], "title": "..."}
```

`TextChatModel` is an ordinary LangChain chat model, so `create_agent`, `ToolNode`, the middleware and
`with_structured_output` work unchanged on top of it:
- `bind_tools(tools)`: the tool list (name, description, arguments from each tool's Pydantic model) and the reply
  protocol are appended to the system prompt. A `tool` reply becomes an `AIMessage` with a tool call (the
  `thought` kept in `additional_kwargs`); a `final` reply becomes the answer, with `tables` and `title` kept.
- `with_structured_output(Schema)`: the reply must be one JSON object that validates against `Schema` (planner,
  request review, reviewer, finisher).
- Tool results go back to the model as `OBSERVATION: {"ok": …, "ref": …, "result": …}` user turns; the model's own
  calls as the JSON it wrote. Calls stop at `OBSERVATION:`.
- Parsing: `parse_json_markdown`, then `protocol.extract_json` for replies with reasoning or prose around the
  object (the last valid `{…}`; a cut-off block or two objects back to back are refused).
- Validation: the tool must be one offered to the model. A tool of a toolkit that is not enabled comes back marked
  `needs_toolkit`, so the user can be asked (§6). Arguments are validated against the tool's Pydantic model before
  the request is reviewed or shown for approval (§8).
- Repairs: an unusable reply gets the original messages plus only the latest bad reply (last 400 chars) and
  `Invalid output: <error>. Reply again with exactly one JSON object as specified.`, so the prompt does not grow:
  `MAX_REPAIRS` times at the role's effort, then once at `medium`. After that the step fails as a retryable
  `model_error` (§8).

A native LangChain model (`LLM_BACKEND=langchain`) skips all of this: it is given the same rules and tools and
uses its own tool calling.

---

## 6. Tools (`tools.py`, `lc_tools.py`)

### Registration

```python
TOOLS = {}

def tool(name, desc, args: type[BaseModel], risky=False):
    def wrap(fn):
        TOOLS[name] = dict(fn=fn, desc=desc, args=args, risky=risky)
        return fn
    return wrap
```

Each registry tool becomes a LangChain `StructuredTool` (`lc_tools.lc_tools`) with the same Pydantic args model,
so the tool list the model sees is rendered from `TOOLS` and code and prompt never drift apart. Toolkit authors
keep writing plain `@tool` functions (docs/extensions.md); they never touch LangChain.

### Toolkits (`agent/toolkits/`, `extensions/toolkits/`, `extensions.py`)
Tools are grouped in **toolkits**: one module with `TOOLKIT = {"name", "description", "basic", "guidance"}`
and tools registered with `@tool` (each tool records its toolkit; `ask_user` is `core`, always on).
`extensions.load()` imports `agent/toolkits/*.py`, then `extensions/toolkits/*.py` (`_*.py` skipped); one that fails
is listed in `extensions.ERRORS` instead of breaking the app; duplicate toolkit or tool names are load errors.
Built-in, all basic: `files`, `docs`, `analysis`, `datalake`. `guidance` is prompt text added only while the toolkit
is enabled. Data on a connector is never a file the tools can open: local sample data lives outside the sandbox
(`extensions/connectors/<name>/data/`) and goes only through the `datalake` toolkit, as it will in production. The
contract for every extension type is `docs/extensions.md`. **Rule:** an agent asked to add a connector, dataset,
skill, toolkit or example only adds files under `extensions/` (and tests); it does not change the framework
(`agent/`, `app.py`, `cli.py`, prompts, config, scripts) or the docs unless the user specifically agrees.

### Per-session tools, /commands and suggestions
- A session starts with the basic toolkits (`config.BASIC_TOOLKITS` overrides). The user adds or removes
  toolkits in the sidebar, with `/name` in the task (a toolkit, a tool → its toolkit, a skill → forced skill +
  its toolkits; `extensions.parse_commands`), or with CLI `--tools`. They stay on until End session (CLI: one
  invocation = one session). Each run records its toolkits in `state["toolkits"]`.
- The model is offered only enabled tools (`graph.SessionPrompt` filters each model call).
- **Suggestions, the user decides:** the planner may reply `"suggest": {"toolkits", "skill", "why"}` (once per
  run). Real, not-yet-enabled items pause the run in `awaiting_choice`; `resolve_pending(choice=...)` accepts
  only offered items: a skill → its steps and toolkits; toolkits only → re-plan once; nothing → keep the plan.
  Mid-run, if the executor calls a tool from a disabled toolkit, the run pauses with a one-toolkit suggestion
  (`replan: false`, an interrupt in `RequestCheck`): accepted → continue with it; declined → feedback, and that
  toolkit is not offered again. All of these pauses are LangGraph interrupts, answered with `resolve_pending`.

### Built-in tools (by toolkit: files = read_file, write_file, save_table; docs = recall, search_docs;
analysis = run_python; datalake = list_datasets, describe_dataset, sql_query, export_query; core = ask_user)

| Tool | Purpose | Returns | Risky |
|---|---|---|---|
| `read_file` | Read TXT/MD/JSON/CSV/XLSX in the sandbox | CSV: shape + first rows; Excel: the workbook layout (below); text: the start; and a `ref` | **yes** for the user's files; no for this conversation's `work/` and `outputs/` |
| `write_file` | Save text to `sandbox/outputs/<run>/` | path | **yes** |
| `save_table` | Deliver a work table to `sandbox/outputs/<run>/` | path | **yes** |
| `recall` / `search_docs` | Keyword search in the run's memory / context documents | snippets with source | no |
| `run_python` | Model-written pandas code for every calculation (see below) | agreed JSON result + work table | **yes** |
| `list_datasets` / `describe_dataset` | Datasets by connector with meaning, required filters, columns and partitions (catalogue and dataset files; no data rows) | text | no |
| `sql_query` | One read-only SELECT on datasets of one connector (filtered and aggregated) | preview + saved work table path | **yes**; no when a repeat is answered from its saved result |
| `export_query` | The rows of one read-only SELECT as a CSV for the user (an extract to download), up to `EXPORT_MAX_ROWS` | row count, columns, first rows + file path in `outputs/<run>/` | **yes** |
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

### Connectors and datasets (`agent/connectors/`, `datalake.py`)
Data sources are **remote and read-only**. Nothing is ever loaded, uploaded or written to them, and the user's
attached files are never part of them: they are local inputs, analysed with `read_file` / `run_python`. The toolkit
guidance and the planner prompt say so, after a run offered a dataset skill for an uploaded workbook.

- A **connector** is one connection: `extensions/connectors/<name>/connector.md` with front matter `name`, `kind`,
  `description`, `needs_login` and the kind's settings, and a body of notes every dataset shares. Built-in kinds
  (`agent/connectors/`): `odbc` (pyodbc; the connection string without the sign-in comes from the environment
  variable named by `connection_env`), `mock` (the mock lake server over HTTP, `url`), `sqlite` (a folder of CSVs,
  `data`, as a locked in-memory database). `kind: custom` loads `connector.py` from the folder. A kind provides
  `connect(connector, login)` and `list_datasets(connector, login)`, optionally `partitions`, `limit_sql`, `source`.
- A **dataset** is one table or view: `datasets/<name>.md` in its connector's folder, front matter `name`,
  `description`, `partition_column`, `required_filters`, `default_filters`, then its documentation
  (`docs/dataset_entry.md`). **Only described datasets can be queried**: the files are the allowlist (intersected
  with what the server lists). Dataset names are unique across the enabled connectors.
- `CONNECTORS` picks the enabled connectors (default all); `CONNECTOR_<NAME>_<KEY>` overrides a front matter value
  (e.g. `CONNECTOR_SAMPLE_LAKE_KIND=mock`). A connector that fails to load is reported in `connectors.ERRORS`.
- **One connector per query:** `datalake.connector_for(sql)` finds the connector of the datasets a query reads; a
  query that mixes connectors is refused with advice to query each and combine the extracts with `run_python`.
- **Development:** the `sample_lake` connector holds the synthetic data (`sample.py` builds `data/*.csv`, git-ignored;
  `scripts/setup_local.py` and the tests call it). `kind: sqlite` by default (tests, quick runs);
  `CONNECTOR_SAMPLE_LAKE_KIND=mock` reaches the same data through the **mock lake server**
  (`scripts/mock_lake_server.py`, standard library): a separate process on localhost over HTTP (`GET /datasets`,
  `POST /query`), read-only (SQLite authorizer) whatever the client sends, with optional per-user sign-in
  (`MOCK_LAKE_USERS`), token (`MOCK_LAKE_TOKEN`) and latency (`MOCK_LAKE_LATENCY_MS`); when it is down, tools return
  "Data lake server not reachable … Start it with: python scripts/mock_lake_server.py".

- **Test datasets keyed by their own codes**, to exercise joins across sources: `credit_exposures` on
  `sample_lake` (month-end credit RWA and EAD by `portfolio_code`, approach and rating grade) and `desk_risk` on
  a second connector, `market_lake` (its own data folder; in production its own connection string,
  `connection_env: MARKET_ODBC`): daily VaR, stressed VaR and ES by `desk_code`. Neither knows `product_id`: the
  user's local mapping file `inputs/book_mapping.csv` (source_system, source_code, product_id, location; written
  with the sample inputs) links them to `usage_limits`, and the agent joins the extracts in `run_python`. Credit RWA
  summed over grades equals `usage_limits` (USED, CLOSED, BOOKED, the credit components) on the mapped keys, so a
  join can be checked exactly (`tests/test_join_sources.py`). `build_samples` runs a connector's `sample.py`
  whenever one of its documented datasets has no data file.

**Sign-in (`credentials.py`).** A connector with `needs_login: yes` gets its own **Data sign-in: <name>** section in
the sidebar (user ID, password; the CLI: `--sign-in <name>=<user id>`, password asked without echo). The sign-in is
checked with one catalogue call, then:
- held only in this process's memory: per browser session in Streamlit's session state, and lent per conversation
  and connector (`credentials.use(run_id, connector, login)`) for the time it runs; never written to `state.json`,
  `log.jsonl`, the checkpoints, settings or a prompt, so the model never sees it; `Login`'s repr hides the password;
- passed to the connection kind as `connect(connector, login)`; the `odbc` kind builds
  `<connection string>;UID={user};PWD={password}` (values braced, so `;` or `}` in a password cannot break it);
- masked in every error text (`credentials.scrub`), since drivers may echo the connection string;
- forgotten on Sign out, End session, or when the process ends. Without a sign-in, the tools say how to sign in.
The metadata cache is per user (a user may see a different catalogue). Use an encrypted connection in production;
the mock server's HTTP basic auth is for local tests only.

**The sample dataset `usage_limits`** (`extensions/connectors/sample_lake/`) mirrors a typical usage-and-limits table: DemoBank
Group's RWA and leverage exposure under JFSA Basel III in JPY millions, Tokyo business days of FY2025.
- Partitioned by fiscal year in `partition_key` (e.g. `FY2025`); `describe_dataset` lists the partitions from the
  catalogue (`Lake.partitions`; a connection kind may supply `partitions`), so the agent never guesses a year.
- Dimensions needed for correct numbers: `partition_key`, `entity`, `rule`, `currency`, `scenario` (USED / ALLOC),
  `resource` (RWA / Leverage exposure), `version` (CLOSED recommended; EARLY arrives first, some dates
  have CLOSED only, the newest EARLY only) and `method` (BOOKED; FRTB internal-model market risk also has DAILY and
  the 60-day AVG60, with BOOKED = max(DAILY, multiplier x AVG60), as for IMCC). Leaving any out double counts.
- Hierarchies as `<dimension>_generation_<n>`: measure (resource > risk type > approach, JFSA OV1 / LR2), region
  and book (group > division > business line > product); one `amount` column; leaf rows only.
- No latest flag: each resource and version has its own latest date (CLOSED lags EARLY, leverage lags RWA). Writes are refused twice: by
`check_select` (one SELECT/WITH statement, no DML/DDL keywords, documented datasets only) and by a read-only
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

**IO cost.** Datasets can hold billions of rows and input files 50k+, so the design avoids needless reads:
- Every query asks the server for at most `MAX_SQL_ROWS` + 1 rows (`datalake.limit_sql` appends `LIMIT`, or
  wraps a query that has its own; a connection kind may supply `limit_sql` for its dialect). Columns come from a
  zero-row query; `describe_dataset` returns no data rows.
- A result over `MAX_SQL_ROWS` is refused, not truncated: nothing is saved and the model is told to filter (WHERE)
  and aggregate (GROUP BY, SUM/COUNT/AVG) in SQL to the level the goal needs, then run it again (or, when the
  user wants the rows themselves as a file, to use `export_query`).
- An export (`export_query`, `Lake.export`) is the one way past `MAX_SQL_ROWS`, for a user who wants the data itself,
  not an analysis. The rows stream from the cursor (`fetchmany` in chunks) straight into a CSV in
  `outputs/<run>/`, so the table is never held in memory or sent to the model: the model gets the row count, the
  columns and the first rows. The server is asked for at most `EXPORT_MAX_ROWS` + 1 rows (default 5,000,000);
  over that, or when the server cuts the result at its own limit, the partial file is removed and the export
  refused. The same checks as `sql_query` apply (`check_select`, required filters, request review, approval).
  The file is attached to the answer even if the model does not list it under `tables` (`loop._exports`); a
  name without `.csv` gets it. The UI shows its first `UI_MAX_GRID_ROWS` rows and offers the
  download up to `UI_MAX_DOWNLOAD_MB` (a larger file is shown by its path on disk).
- Required filters are standing background, not errors: each dataset file's `default_filters` (recommended
  values, e.g. `partition_key = 'FY2025' AND … AND version = 'CLOSED' AND method = 'BOOKED'`) and the required filters are
  rendered into the datalake toolkit's guidance (`datalake.dataset_rules`; a toolkit's `guidance` may be a function)
  so the agent knows them before writing SQL. `sql_query`'s `check` (`datalake.static_problem`) runs before
  approval: a query missing one goes back to the executor as a `precheck` correction and never runs.
- Each dataset file lists its required filters (`required_filters: \`partition_key\`, \`scenario\` (or GROUP
  BY), …`, `datalake.required_filters`): every group must be filtered on one of its columns, and a group marked
  "(or GROUP BY)" may instead be split by in GROUP BY (values never added together). The partition must always be
  filtered. A query that breaks this is refused before it reaches the server. A keyword check, like
  `check_select`.
- Datasets, columns and partitions are cached per connector and user for `LAKE_META_TTL_S`, not fetched on every call or rerun.
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

1. The arguments are validated with the tool's Pydantic model as soon as the model asks (`RequestCheck`); an error
   goes straight back to the model.
2. If the call needs approval (`tools.needs_approval`: `risky` is True, or a function of the arguments, e.g. a lake
   query yes, a repeat answered from its saved result no), the request is reviewed first (§8) and then waits for
   the user (`HumanInTheLoopMiddleware`, one interrupt per call).
3. `ToolNode` runs the LangChain tool, which calls `tools.dispatch`: inside try/except with a timeout
   (`TOOL_TIMEOUT_S`; `sql_query`: `SQL_TIMEOUT_S`). An error becomes an observation, never a crash; its text is
   masked for lake passwords.
4. Store the full output in `runs/<id>/outputs/<ref>.txt`. Return a truncated preview plus the `ref`.
5. Observation format: `{"ok": true|false, "ref": "...", "result": "..."}`.

Description rules: say what comes back and how big it is, say when *not* to use the tool,
and keep names distinct.

---

## 7. Memory and context

| Layer | Content | Where |
|---|---|---|
| System prompt | rules and the enabled toolkits' rules (`prompts/system.md`); the accepted skill and its reference documents, or the plan; the current step; other toolkits as one line each | every model call (`graph.system_prompt` via `SessionPrompt`), one `## ` block each; the chat model adds the full tool list (enabled toolkits only) and the reply protocol |
| Instruction | goal (or follow-up with earlier instructions), input files, the user's own context docs | the first message of each instruction (`graph.intro`) |
| Conversation | tool calls, observations, feedback, the user's answers and rejection reasons | the graph's `messages`, checkpointed. The model sees the last `KEEP_FULL_RESULTS` (3) tool results and calls in full, older results as one line with their ref (a `describe_dataset` result keeps its heading, required and default filters, columns and partitions, which a join over several datasets still needs) and older calls with the start of their SQL / code (`graph.compact`); past `PROMPT_BUDGET_TOKENS` `SummarizationMiddleware` folds old messages into a summary |
| Context docs | user's definitions / methodology (`state.context`); a skill's references | the user's: small ones inline in the instruction (≤ `CONTEXT_INLINE_TOKENS`), larger via `search_docs`; a skill's: in the system prompt once the skill is accepted |
| Facts | one line per tool result, the user's answers (whole) and rejection reasons, with refs | the conversation record; given to the request reviewer, answer reviewer and finisher |
| Long-term | full tool outputs, logs, documents | only via `recall` / `read_file` / cited outputs |

### Summaries until enabled or accepted
The executor gets the full documentation of a toolkit (each tool's description and arguments, the toolkit's rules)
only once the toolkit is enabled, and of a skill (its steps, checks and reference documents) only once it is
accepted, by the user directly or by their yes to a suggestion. Until then it sees one line per toolkit (description
and tool names, so it can ask for one by calling it) and the planner sees one line per skill. Basic toolkits count
as enabled from the start. The request check gets only the current plan step (`graph.current_step`: tool calls the
model marks `step_done`, or a repeated call), and the checkers do not get a dataset's description again when it
is already among the accepted skill's datasets. `describe_dataset` likewise leaves the documentation out then.

Measured on Haiku (2026-10-29, same three golden tasks, tokens per model call): `file_average` 1,790 (main 1,906),
`rwa_latest_total` 3,584 (main 3,365), `rwa_limit_status_now` 4,055 (main 4,117); executor calls 2.4k / 4.4k / 4.7k
against main's 2.7k / 4.6k / 5.7k. Session memory mode (§4a) cuts the executor's share further.

### Two stores
- **LangGraph checkpoints** (`runs/checkpoints.sqlite`, thread = run id): the graph's own state after every node
  (messages, plan, toolkits, pending interrupts). This is what resumes a run, in the same process or another.
- **The conversation record** (`runs/<id>/state.json`, `memory.py`): what people and the other tools read —
  status, title, instructions, plan, facts, the pending approval / question / suggestion, answers, warnings, result
  tables and the event list the UI, the HTML export, the replay script and eval are built from.

```json
{
  "run_id": "20261028-…", "status": "awaiting_approval", "title": "…", "goal": "…", "tasks": [],
  "inputs": ["inputs/x.xlsx"], "context": [], "toolkits": ["files", "analysis"], "skill": null,
  "plan": ["…"], "step_count": 3, "facts": ["read_file(…) -> … [ref: read_file-1]"],
  "pending": {"tool": "run_python", "input": {…}, "interrupt": "<LangGraph interrupt id>", "tool_call_id": "…"},
  "final": null, "warnings": [], "results": [], "events": [{"kind": "plan", "step": 0, "ts": "…", "payload": {…}}]
}
```

### Rules
- Token budget per call, e.g. 6k, well below the model limit. Small models degrade with long prompts.
- The record is saved before every step (the graph's checks read it) and after; `log.jsonl` gets every model call,
  tool call, review and user action. Runs resume after a crash or a refresh.
- No secrets in either store (§6 sign-in).

---

## 8. The agent (`graph.py`) and the conversation driver (`loop.py`)

Pattern: **plan → execute (tool loop) → review**, more reliable than open-ended tool use on a small model. It is one
LangChain `create_agent` graph:

```
Planner.before_agent → Choice.before_agent → model ⇄ tools → AnswerCheck.after_agent → end
                                               ↑ RequestCheck.after_model (may send a call back to the model)
                                                         AnswerCheck may send the answer back to the model ↑
```

| Part | Stock or custom | What it does |
|---|---|---|
| `create_agent`, `ToolNode` | stock | the model ⇄ tool loop and routing |
| `HumanInTheLoopMiddleware` (per call) | stock | approve, or reject with a reason, each call that needs approval (`tools.needs_approval`); the card text is the request reviewer's explanation |
| `SummarizationMiddleware` | stock | folds old messages into a summary past the budget (left out in session memory mode) |
| `interrupt()` / `Command(resume=…)` | stock | pauses for questions (`ask_user`), suggestions and approvals; resumed by the driver |
| SQLite checkpointer, breakpoints | stock | saves after every node; `interrupt_after` the plan, each tool call and each answer check make one invoke one step |
| `Planner` / `Choice` | custom | plan with the session's tools and skills (`planner.md`, `make_plan`); a suggested skill or toolkit waits for the user; toolkits accepted → plan again with them |
| `SessionPrompt` | custom | system prompt per call (rules, toolkit rules, skill or plan), only enabled tools offered, `medium` effort after a failed call |
| `RequestCheck` | custom | before approval: disabled toolkit → ask the user; arguments valid; the tool's static check; then the request reviewer for `REVIEWED_TOOLS` |
| `RepeatGuard` | custom | an identical repeat of a call that worked is not run again; after `MAX_REPEATS` the finisher answers from the facts |
| `AnswerCheck` | custom | every number traces to a source (`checks.uncited_numbers`, code), then the reviewer; issues go back to the model up to `MAX_REVIEW_ROUNDS` times |

### Step-wise driving (required by the UI)
`loop.run_one_step(state, model)` runs the graph to its next breakpoint or pause and turns what happened into
events in the conversation record (`plan`, `tool`, `precheck`, `approval`, `user`, `ask_user`, `suggest`, `review`,
`final`, `abort`, `model_error`), then persists and returns the last event. `resolve_pending(state, approved=…,
reason=…, answer=…, choice=…)` records the human's response and resumes the graph with `Command(resume=…)`: an
approved request runs at once (the UI shows it as a background job), anything else with the next step. Streamlit,
the CLI, eval and tests all drive these same functions; `run(goal, model, approve, ask, choose)` drives them to the
end. `continue_with` starts a follow-up instruction on the same thread (earlier messages, facts, refs and work tables
stay); `rename` names the conversation; `abort` stops it.

`state["status"]`: `planning` → (`awaiting_choice` →) `running` → `awaiting_approval` | `awaiting_user` | `awaiting_choice` → `running` … → `finished` | `aborted`

### Scope confirmation
When the goal leaves a data choice open that cannot be inferred with confidence, above all the time range (real
data spans 13-14 years) or the resource, scenario, version or level, the agent asks once before the first data
query, proposing the values it would use; the user confirms ("Yes, go ahead" in the UI) or corrects. The planner
makes this the first step ("Confirm with the user: …"), the system prompt (rule 2) and `ask_user` say how, and the
request reviewer rejects a query built on an unconfirmed open choice. A precise goal uses the documented defaults
and does not ask. The user's answer is kept whole as a fact (`USER ANSWER to: … -> …`), and answers and rejection
reasons outrank the plan. Evals answer such questions with "Yes, confirmed"; golden tasks may check `expect_ask` /
`max_asks`.

### Request review before approval
Every `sql_query`, `read_file` and `run_python` request (`config.REVIEWED_TOOLS`) is checked by the request reviewer
(`action_review.md`, effort `medium`, `graph.review_request`) before the user sees it: it serves the step, applies
every required filter with the recommended values, aggregates to the level needed without scanning more than
needed, does not repeat or re-query what an extract already gives (a check is done in Python), and the analysis
logic is right. A rejected request goes back to the model in the same step (a `precheck` event) without asking the
user; after `MAX_ACTION_REJECTIONS` the user is asked anyway, with the reviewer's notes on the card. An approved
request carries the reviewer's plain-language `explain`, shown on the approval card. The reviewer also gets the full
SQL / code of earlier requests and the previous request of the same tool (from the graph's own history, so a
request sent back moments ago counts): a revised request's `explain` starts with "Changed from the previous request:
…" (shown first and in bold on the card), and the approval event stores `previous`, which the card offers as a diff.
Bad arguments and static problems (`Tool.check`: unsafe analysis code, a missing required filter) are sent back the
same way, before the reviewer and before the user. The review of the final answer stays, for what can only be seen
in results.

### Stop conditions
An answer accepted by the checks · the step budget per instruction, counted by the driver:
`config.step_budget(plan)` = max(`MAX_STEPS` (15), `STEPS_PER_PLAN_STEP` (3) x plan steps), so a 7-step join plan
gets 21; the stop message says a follow-up instruction carries on from the results so far · repeated action
(finisher, else abort) · user abort.

A backend failure (model unreachable after the adapter's retries, or replies that stay outside the protocol after
the repairs) is not a stop: the step records a `model_error` event, and retrying resumes the graph from its last
checkpoint (UI: Retry button; CLI: `--resume`). Whatever the graph saved before the failure is recorded, not redone.

User abort (UI Stop) also cancels the model call in flight (`ChatModel.cancel()`); the driver never records a result
that arrives after the abort.

---

## 9. Effort policy (`config.py`)

Default to `none`. Spend `medium` only where reasoning quality changes the outcome. Each role has its own copy of the
chat model with its effort (`TextChatModel.for_role`), so the policy is the `EFFORT` table:

| Call | Effort | Why |
|---|---|---|
| Planner | medium | Decomposition errors propagate to every step |
| Executor step (routine tool choice) | none | Narrow, well-specified decisions |
| Executor step after a failed call | medium | Recovery needs reasoning (`SessionPrompt`) |
| JSON repair (first `MAX_REPAIRS` tries) / last try | none / medium | Formatting only, then one careful try |
| Summarising old messages | none | Compression, low risk |
| Reviewer | medium | The final quality gate |
| Request review (before each approval) | medium | Catches wrong filters and needless lake load before the user is asked |
| Finisher (repeating executor) | medium | Last chance to answer from the facts |

Log the effort and latency of every call so the policy can be tuned with data. A native LangChain model
(`LLM_BACKEND=langchain`) is called without an effort.

---

## 10. Prompts (`prompts/`)

- **system.md**: the agent's rules and `{{guidance}}` (toolkit rules); the tool list and the reply protocol (§5) are
  added by the chat model, so a native LangChain model gets the same rules without the JSON format. A final answer
  may list `"tables"` as `{"name", "title"}`; they are shown under the answer as titled grids, so the answer must
  not also write them out (rule 8), a table keeps the columns that qualify its numbers (date, version, resource,
  unit), and SQL in an answer goes in a ```sql block, one clause per line. Rule 10: fiscal years run 1 April to
  31 March. The user's answers and rejection reasons override the plan (rule 7).
- **planner.md**: "Break the goal into the fewest steps: 1–7, a simple question needs one. Plain-word actions,
  not tool names." Shape-only example `{"plan": ["<step>", ...]}`. `plan()` drops copied `<step>` placeholders
  and rejects a plan made only of tool names; then the goal is the single step. It must not plan steps that
  re-check extracted data with another query, and suggests a skill only when the goal asks for what it produces. A skill written for certain datasets is never suggested
  for a request that names a dataset it does not cover (`checks._datasets_outside`).
- **reviewer.md**: "Check FINAL against GOAL and this checklist: complete, consistent with facts,
  numbers traceable to refs, no unsupported claims. Output `{\"ok\": bool, \"issues\": [...]}`."
  It also gets the CONTEXT, the active skill's checks, and the full stored output (with the tool input) of
  every ref cited in FINAL (`cited_outputs`, up to `REVIEW_CITED_CHARS`): facts keep only short previews,
  which made the reviewer reject correct answers. Numbers quoted from context docs are cited `[doc: <file>]`.
  FINAL sits between `<<<FINAL` and `FINAL>>>` marker lines, and the checklist after it is headed "REVIEW
  CHECKLIST (instructions for you, not part of FINAL)": without the markers Haiku read the checklist as part of
  the answer and rejected it. It also gets the documentation of the datasets the agent read (a valid source for
  units, filter values, calendars and names) and the tables shown under the answer. It reports a problem only
  when it makes the answer wrong: a wrong or unsourced number, a claim contradicting the facts or documentation,
  the wrong date / version / scenario / filter, a direction contradicting the numbers, or an unanswered part of
  the goal. Missing caveats, method notes, scope wording and unrequested checks are not problems (they cost
  review rounds in evals).
- **action_review.md**: the request review before approval (above, §8): `{"ok", "issues", "explain"}`.
- **Citation check** (`checks.uncited_numbers`, code, before the LLM reviewer): every number in FINAL (except dates,
  periods, list markers, small counts ≤12 and years; rounding allowed) must appear in the evidence: the goal and
  earlier goals, stored outputs (`outputs/*.txt`), context docs, and what the model was shown — tool previews
  (e.g. read_file's `shape=(48, 5)`, which the stored output lacks) and the user's answers to `ask_user`.
  Stored CSV outputs are read both ways ("1,234" as one number, and every value between commas), since a row
  like `Credit risk,4,148898` would otherwise read as 4,148 and 898.
  Misses go back to the executor without an LLM call.
- Summaries of old messages use `SummarizationMiddleware`'s own prompt (LangChain).
- **finisher.md**: "Write the final answer to the GOAL using only the FACTS. Cite every number as [ref]."

The planner gets the enabled tools, the other toolkits and the skills (names and descriptions only) and the
CONTEXT, and replies `{"plan": [...], "suggest": {"toolkits": [...], "skill": "<name>|none", "why": "..."}}`.
Prompts are neutral: a question answerable from general knowledge needs no tools.

### Skills (`extensions/skills/*.md`, `skills.py`)
Front matter `name`, `description`, `toolkits`, `datasets` (the datasets a skill is written for, from any enabled
connectors; none for a general skill; the skill list shows `[datasets: …]` or `[general]` and the planner offers a
skill only for questions about its datasets; once accepted, the datasets' files are in the prompt), `references`
(other context documents, e.g. a methodology note), `when_to_use`; sections `## Steps` (become the plan), `## Checks`
(added to the reviewer checklist) and `## Output`. The planner may suggest a skill (the user decides), or the
user forces one with the sidebar or `/name` (then no planner call; its toolkits are enabled). Write steps in terms of tools (sql_query, run_python, save_table) and compute changes in SQL or run_python so
the model never subtracts numbers itself. Keep skills general: entity, dates, slice and breakdown come from the
request. Bundled: `file_summary` (general: any attached file - layout, formula logic, key figures, at most one
Python run); for the `usage_limits` dataset: `usage_overview` (at most two small queries on the latest partition,
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
- No secrets in prompts, state, logs or checkpoints. The data sign-in is held in memory only and masked in
  errors (§6); an API key is read from a file outside the repository (`API_KEY_FILE`).
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
- The conversation lives in `st.session_state` **and** on disk (`runs/<id>/state.json` plus the graph's checkpoints).
- With auto-run on, each rerun calls `run_one_step` once (in a background job), renders the events, then reruns
  until the agent finishes or waits for a human. With it off, a "Next step" button does the same.
- Approvals, questions and suggestions are buttons and inputs that call `resolve_pending`, then rerun. A rejection
  needs a reason.
- A step that can take long (an approved lake query can take minutes, an agent step, analysis code) runs in a
  background thread (`start_job`); every rerun meanwhile shows what is running and the elapsed time and offers no
  actions, so the same query is never started twice.
- For long runs or many users, move execution to a background worker and have the UI
  poll `log.jsonl` (see "Scaling" below).

### Skeleton

```python
from agent.loop import new_state, run_one_step, resolve_pending, continue_with

s = ss.state
if s and needs_login():                              # this conversation's queries use this session's sign-in
    credentials.use(s["run_id"], ss.get("lake_login"))
if s and s["status"] == "awaiting_approval":
    if st.button("Approve"): start_job("Running", lambda: resolve_pending(s, approved=True))
    reason = st.text_area("Why reject?")
    if st.button("Send rejection") and reason: resolve_pending(s, approved=False, reason=reason); st.rerun()
elif s and s["status"] == "awaiting_user":
    if ans := st.text_input("Your answer"): resolve_pending(s, answer=ans); st.rerun()
elif s and s["status"] in ("planning", "running"):
    if ss.autorun or st.button("Next step"): start_job("Step", lambda: run_one_step(s, model))
if goal := st.chat_input("Describe the task"):
    if s: continue_with(s, goal)
    else: ss.state = new_state(goal)
    st.rerun()
```

### UI rules
- **Verbose** switch (off by default; CLI `--verbose`). Off: plan, one line per step (`step_summary`: rows, shape,
  saved path, or size — never data read in), questions, approvals (details collapsed), final answer. On: thoughts,
  tool inputs, results and reviewer issues. The full trace is always in `log.jsonl` and **Model calls**.
- Sidebar **Tools (this session)**, **Skill** (Auto = suggestions only), **Commands** (every `/name`), **Data lake
  sign-in** (when the lake needs it: user ID and password, Sign in checks them, Sign out forgets them; the password
  field is cleared when sent), plugin load warnings, **End session** (tools back to basic, skill to Auto, sign-in
  forgotten, screen cleared; runs stay saved).
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
- Each answer shows its timing (`checks.answer_timing`): "Answered in 3 min 05 s (agent 1 min 40 s, waiting for you
  1 min 25 s) · 7 steps", from the instruction's start to the final event, with waiting = time between an
  approval, question or suggestion and the user's response.
- Each request shows the files attached with it (📎 names; `first_files` and the `task` event's `files`).
- At each accepted final answer the agent may rename the conversation (`"title"` in the final reply,
  `checks._retitle`), unless the user renamed it (`rename` sets `title_by_user`).
- Citations: `[ref: …]` and `[doc: …]` in an answer show as numbered links ([1], [2]) to a **Sources** list under
  it (`sources.py`); each source opens to its tool, input (code/SQL) and full stored output (`run_python` tables
  as a grid, ask_user as question + answer). The stored answer and the downloads keep the plain markers.
- Raw model replies are shown only on request (a checkbox under the model-call table), to compare models.
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
`{ts, step, kind: model|tool|review|user|model_error|abort, purpose, effort, model, prompt_tokens, latency_ms, repairs,
input, output, ref}` (session mode adds `channel`, `send`, `sent_tokens`). Model calls are logged by a LangChain
callback (`lc_tools.ModelLog`) on every call the graph makes, with the raw reply; tool calls by the tool wrapper;
reviews and user actions by the graph and the driver. `model` is the model that answered (the backend's optional
`model_name(effort)`, else the backend name). The graph's checkpoints (`runs/checkpoints.sqlite`) hold every step's
state as well. This is enough to replay a run, debug failures, and evidence how a result was produced.

**Replay script** (`replay.py`; the "analysis script (.py)" download, or `cli.py --export-script RUN_ID`),
assembled by the harness from what ran, never by the model. Plain Python with `# %%` cells (runs end to end, or
cell by cell in VS Code / Spyder / Jupyter). The analysis comes first as plain code; support code comes after:
- Setup: `SOURCES`, one entry per connector the queries used (each query is sent back to the connector that holds
  its datasets: `run_sql(SQL, "sample_lake")`). Kind `odbc` (connection string without the sign-in in the variable
  the connector's `connection_env` names, default `LAKE_ODBC`; user ID in `ANALYSIS_USER`) or `mock` (the mock lake
  server at the source's `url`, else `MOCK_LAKE_URL`). Any setting can be overridden with
  `ANALYSIS_<SOURCE>_<SETTING>` (e.g. `ANALYSIS_SAMPLE_LAKE_URL`); `ANALYSIS_LAKE=mock` sends every query to the
  mock server. The password is asked once per source and never stored. A result the server cut at its own row
  limit is an error, not a short table. Inputs from `ANALYSIS_INPUTS`; steps run in a scratch folder with their
  original paths.
- Every read of the user's files, every successful `sql_query` and `export_query` (the export is written where the
  agent wrote it and checked against that file) and every approved `run_python` code unchanged (indented one
  level), in order, each inside `with step("<ref>"):` with the reviewer's `explain` as a comment and its result in
  a variable named after its saved table or file (e.g. `rwa_by_division = run_sql(SQL, "sample_lake")`); each
  instruction and the user's answers as headings; failed or rejected attempts as comments. `read_range` is
  included when a step uses it.
- The steps hold no checks. A step that stops with an error (e.g. approved code meeting a new limit breach it was
  not written for) has its traceback printed and is recorded in `FAILED`; the script goes on, and later steps that
  need its table fail the same way.
- Then one "support code" cell (result normalising, fingerprints, answer rebuilding), the answers, and last the
  one Checks cell.
- Each answer as a template: numbers are bound to the result cell they came from (step ref, row by its text
  columns, an empty cell counting as "", column), or else to their position in the step's `notes` (totals the
  code printed there), and recomputed; a changed number shows as `2,017 [was 1,917]`, one from a failed step as
  `1,917 [step run_python-5 failed]`; untraced numbers (dates, counts, run IDs, the user's own) stay literal.
- The Checks cell holds every check, with a comment saying it changes nothing and may be skipped: each step
  against what the agent saw (rows, column sums, digest) as `MATCH`, `DIFF: …` or `FAILED: <error>`; exit code 1
  on any of them. It re-queries the live sources; it does not ship data. The connector of a query is found from
  its datasets when the script is exported; a dataset no longer configured falls back to the first connector.

---

## 14. Testing

- **FakeModel**: replays scripted replies per purpose. Covers the whole graph without API calls
  (`test_loop_fake_model.py`: statuses, approvals with reasons, questions, resume from disk and in a new process,
  repairs, repeats and the finisher, the number check, review rounds, step limit, model errors, Stop, summarising,
  follow-ups).
- UI smoke tests with `streamlit.testing.v1.AppTest` and FakeModel: a goal runs to `finished`, approvals, rejection
  reasons, verbose off prints no data, data sign-in per connector (wrong then right password, sign out).
- Reply protocol tests: malformed JSON, two objects, unknown tool, disabled toolkit, repairs and their size.
- Tool tests: deterministic outputs, truncation, sandbox enforcement, time limits.
- Data tests: the guard rejects writes, multiple statements and unknown datasets; one connector per query; the
  connection refuses writes;
  row cap; per-user sign-in reaches the lake and never reaches state, logs, checkpoints, prompts or errors.
- Skill/context tests: bundled skills parse; suggestions (accept skill, accept toolkits → re-plan, decline);
  forced skill skips the planner; context inline vs listed.
- Plugin tests: built-in toolkits and basic set; only enabled tools offered; /commands; a plugin file is discovered
  (the `_template.py` toolkit works when copied); broken extensions are reported; every skill's datasets exist and
  the templates parse; mid-run suggestion; file
  tools refuse lake storage.
- Backend tests without network: Ollama against a local stand-in server, Claude CLI against a fake `claude` script,
  an OpenAI-compatible stand-in server (`chatgpt`), a native LangChain model with tool calling (`langchain`), the
  session memory stand-in.
- Golden tasks (`scripts/eval.py`): representative goals with expected facts. Re-run after prompt, policy, model or
  framework changes.

---

## 15. Build order (how this was built; also the order to read it)

1. `adapter.py` and a backend, plus `scripts/smoke_model.py` against the real module.
2. `chat_model.py` (reply protocol, repairs) plus tests.
3. `tools.py` and the toolkits; `lc_tools.py` turns them into LangChain tools.
4. `graph.py`: `create_agent` with the stock middleware, then the custom middleware one by one, each with a
   FakeModel test.
5. `loop.py`: the driver and the conversation record, plus `cli.py`.
6. `app.py` on top of the driver: trace, approvals, uploads, sign-in, resume.
7. Skills, golden-task evaluation, replay and export.
8. Optional: background worker for long runs; parallel sub-agents (LangGraph subgraphs).
