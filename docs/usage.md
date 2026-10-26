# Using the agent

How a session works, the tools and skills, the data lake and attachments, and the evals. For installation and settings, see `docs/setup.md`.

## Tools, skills and how a session works

- **Conversations**: after an answer, type a follow-up. It continues the same conversation (same run): earlier
  results, facts and work tables stay available and the screen keeps the whole exchange. **New conversation**
  starts a fresh one. Each conversation gets a short name from the agent (rename it with **Rename**); the
  sidebar **Conversations** list shows these names. CLI: `--resume RUN_ID "follow-up"`.
- **Toolkits** group tools: `files`, `docs`, `analysis` and `datalake` (read-only) are on when a session starts;
  plugin toolkits are off until you enable them. The model only sees the enabled tools, and prompts say a
  question answerable from general knowledge needs no tools.
- **Per session**: pick toolkits in the sidebar **Tools (this session)**, or type `/name` in the task
  (`/datalake`, `/sql_query`, `/usage_comparison`; see the **Commands** list). They stay on until **End session**.
  CLI: `--tools <toolkit>,…` or `/name` in the task; one invocation is one session.
- **Suggestions**: if the goal seems to need a toolkit or skill that is off, the agent pauses and suggests it.
  You tick what to use ("Use selected") or "Continue without". It never enables anything by itself.
- **Verbose** (sidebar, CLI `--verbose`): off shows only the steps (`🔧 sql_query · rows=6; …`), never data read
  in; on also shows thoughts, tool inputs, results and reviewer comments.
- **Add your own**: Python toolkits in `plugins/tools/`, Markdown skills in `plugins/skills/`. See `docs/plugins.md`
  (also the contract coding agents follow).
- **Prompt settings** (sidebar, collapsed): a role/persona, an answer style, and (advanced) the system,
  planner, reviewer, finisher and summariser templates with Reset. Saved in `settings/prompts.json`. An edited
  template must keep every `{{placeholder}}`. Guardrails live in code, so prompt edits can't remove them.

## Analysis: data lake, attachments and skills

- **Data lake** (`agent/datalake.py`): the remote, read-only data lake cluster. Tools `list_datasets`,
  `describe_dataset` and `sql_query` (one read-only SELECT; the result is saved as a work table in
  `sandbox/work/<run_id>/`). Nothing is ever loaded, uploaded or written to the lake, and attached files are
  never part of it: they are local inputs, read with `read_file` / `run_python`. In production it is a remote lake
  over ODBC: copy `agent/backends/datalake_odbc_template.py`, fill it in, and set `DATALAKE=<module>`.
  Writes are refused twice: by `check_select` and by the read-only connection (account).
  Locally, the synthetic lake data is in `mock_lake/data/` (outside the sandbox, so file tools can't see it):
  - `DATALAKE=sqlite` (default): an in-process, read-only SQLite copy. Used by the tests.
  - **Mock lake server**, closer to production: a separate process the agent reaches over HTTP, read-only on the
    server side, with optional token and simulated latency (see `mock_lake/server.py`). In one terminal
    `python mock_lake/server.py`, then start the agent with `DATALAKE=agent.backends.datalake_mock`.
  - What each view means is in `sandbox/references/datalake_dictionary.md`, a reference document the skills cite.
    `describe_dataset` returns a view's full entry (grain, required filters, calendar, hierarchies, measures).
  - The one view, `usage_limits`, is a realistic example of a usage-and-limits view: DemoBank Group's RWA and leverage
    exposure under JFSA Basel III in JPY millions, partitioned by fiscal year in `partition_key` (`FY2025`, 1 April to
    31 March, 244 Tokyo business days). Dimensions that must be filtered (or grouped) for correct numbers:
    `partition_key`, `entity`, `rule`, `currency`, `scenario` (USED / ALLOC), `resource` (RWA / Leverage
    exposure), `version` (CLOSED recommended; EARLY arrives first, some dates have CLOSED only, the newest EARLY
    only) and `method` (BOOKED; FRTB internal-model market risk also has DAILY and the 60-day AVG60, with BOOKED =
    max(DAILY, multiplier x AVG60)). Hierarchies as `<dimension>_generation_<n>`: measure (resource > risk type >
    approach, JFSA OV1 / LR2), region and book (group > division > business line > product); one `amount`
    column. No latest flag: each resource and version has its own latest date. `describe_dataset` lists the
    partitions. Rebuild with `python mock_lake/generate_view.py`.
  - Skills `usage_overview` (this view) and `file_summary` (any attached file) summarise data for an
    analyst: what it holds, how to read or query it correctly, and its key figures.
- **Data reads are confirmed, after a review**: before a lake query, a read of your files or analysis code is
  shown to you, the reviewer checks it against the goal and the view's documentation (required filters,
  aggregation level, load on the server, logic). Problems go back to the agent without asking you (after two
  rejections you are asked anyway, with the reviewer's notes). The approval card explains in plain words what the
  request does, with the SQL formatted one clause per line. While an approved query runs, the page shows what is
  running and the elapsed time (real lake queries can take minutes).
- **Vague questions are confirmed first**: when a question leaves a data choice open (above all the time range, or
  the resource, version or level), the agent proposes values and asks once before querying; **Yes, go ahead**
  confirms, or type a correction.
- **Required filters are background, not errors**: each view's required filters and default values (data
  dictionary `Required filter:` / `Default filters:`) are in the agent's standing guidance, and a query that still
  misses one is sent back to the agent before you are asked, never run.
- **Revised requests show what changed**: the card's explanation starts with the change, and a diff against the
  previous request of the same tool is shown.
- **Layout**: your request and the answer are highlighted cards; a finished instruction folds its steps into
  "How I got here", while the current one shows its steps as they happen.
- **Data reads ask for approval**: every lake query and every read of your files (`inputs/`) waits for your OK,
  with the SQL shown. Reading back this conversation's own results (work tables, a repeated query) and view
  documentation (`list_datasets`, `describe_dataset`: catalogue and dictionary, no data rows) do not ask.
  SQL extracts and aggregates; Python analyses the small extracts.
- **IO cost** (lake views can hold billions of rows, input files 50k+):
  - every lake query asks the server for at most `MAX_SQL_ROWS` + 1 rows (`LIMIT`, or the lake module's
    `limit_sql`); a result over the cap is refused with advice to filter and aggregate in SQL, never truncated;
  - views, columns and samples are fetched once per `LAKE_META_TTL_S`; the same SQL in a conversation reuses its
    saved table (`refresh: true` forces a new query);
  - large views can be marked `Required filter: \`col\`` in the data dictionary; unfiltered queries are refused;
  - each file version is parsed once (`agent/tablecache.py`: memory + pickles in `sandbox/work/.cache/`);
    `read_file`, `tables`, `read_table` and literal `pd.read_*` calls in analysis code reuse it;
  - `read_file` stores `READ_FILE_STORED_ROWS` rows under its ref, not a copy of the file; the citation check
    indexes each stored output once; the UI reads tables once per version and shows `UI_MAX_GRID_ROWS` rows.
- **Python analysis** (`run_python`, toolkit `analysis`): the model writes pandas code for every calculation,
  in one run rather than row by row. Safety, in layers:
  - a static check before you're asked (imports only from standard analysis packages that ship with the
    project — pandas, numpy, math, statistics, datetime, collections, itertools, functools, re, json, decimal —
    no `open`/`eval`/`exec`, no file writing);
  - **your approval**, with the code shown;
  - a separate time-limited `python -I` process in an empty folder, with `import` and reads
    (`pd.read_csv/excel/json`, `pd.ExcelFile`, `read_table`) limited to sandbox files, never lake storage or URLs.
  The code only returns `result` (+ `notes`); the agent turns it into a fixed JSON format and saves tables itself.
- **Result tables**: an answer can list tables; they show as interactive grids (sort, search, download) under
  the answer. **Create results page** (or `/results`) opens a separate page with every table of the conversation.
- **Sources**: `[ref: run_python-3]` and `[doc: file]` in an answer show as numbered links ([1], [2]) to a
  Sources list under it; each source opens to its tool, input (code or SQL) and full output. The stored
  answer, Save and Download keep the plain markers as the audit trail.
- **Downloads under each answer** (nothing to dig out of a working folder): the answer (`.md`), the whole
  conversation (`.html`: requests with their files, folded steps with SQL and code, answers with their tables,
  sources and timing; one self-contained page, `agent/export.py`, CLI `--export-html RUN_ID`), and the analysis
  script (`.py`, below; CLI `--export-script RUN_ID`). Each answer also shows how long it took ("Answered in
  3 min 05 s (agent 1 min 40 s, waiting for you 1 min 25 s) · 7 steps").
- **Analysis script**: one file that replays the whole conversation, built from what actually ran (not written by
  the model). It has `# %%` cells, so `python <file>` runs it end to end and VS Code, Spyder or Jupyter open it
  as cells. The analysis comes first, as plain code; the support code for the checks comes after it:
  - **Setup**: `run_sql` for the internal lake server (`ANALYSIS_LAKE=odbc`, DSN in `LAKE_DSN`) or the mock
    lake server (`ANALYSIS_LAKE=mock`); input files from `ANALYSIS_INPUTS`.
  - **Steps**: every read of your files, every successful `sql_query` and every approved `run_python` code,
    unchanged and in order, each with the reviewer's plain explanation as a comment and its result in a named
    variable (e.g. `rwa_by_division = run_sql(SQL)`); each instruction and your answers to the agent's questions
    as headings. Failed or rejected attempts are kept as comments.
  - **Answers**: each answer as a template; every number traced to a result cell (by step, row name and column)
    is recomputed, and a changed number shows as `2,017 [was 1,917]`. Untraced numbers stay as written.
  - **Check**: each step against what the agent saw (shape, column totals, digest): `MATCH` or `DIFF: …`.
    The exit code is 1 when anything differs, e.g. the lake data was restated.
- **Attachments** (any file): the paperclip in the message box, or CLI `--input`. They become input files of the
  conversation, shown as 📎 file names under the request they came with; attached text files can also be searched
  with `search_docs`.
- **Excel workbooks**: `read_file` describes the layout (`agent/workbook.py`): each sheet's table blocks (range,
  header rows joined into names, totals rows, title and note text), the formulas behind them as patterns
  ("E5:E8 = Dn-Bn, row by row"; formulas without saved values are flagged), a preview, and the
  `read_range(path, sheet, range, header_rows)` call that loads a block in analysis code.
- **Conversation title**: each final answer renames the conversation from what it has become, unless you named it.
- **Reference documents belong to skills** (`references:` in the skill file, e.g. the data dictionary): while
  the skill is active they are the run's CONTEXT. Small ones go into the prompt; larger ones are searched
  with `search_docs`.
- **Skills** (`plugins/skills/*.md`): step lists written in Markdown. The agent may suggest one, or you pick it
  (sidebar "Skill", `/skill_name`, CLI `--skill`). Its steps become the plan, its toolkits are enabled, and its
  checks are added to the reviewer.
  A skill is either general or tied to one data source (front matter `source:`), and the planner only offers a
  source's skills for questions about that source. Bundled: `file_summary` (general: any attached file);
  for the `usage_limits` view: `usage_overview`, `usage_comparison`, `limit_breach_analysis` (finds
  limit breaches over a period and their source: a usage increase, a limit cut at a quarter reset, or the
  internal-model 60-day average) and `limit_status` (usage, limit and headroom per key at the latest
  date: is anything over its limit now). See CLAUDE.md "Add a skill".
- **Citation check**: before the AI reviewer, code checks that every number in an answer appears in a tool result,
  a skill reference or your instruction (rounding allowed). Numbers without a source go back to the agent.

## Evals (golden tasks)

`tests/golden/*.json` are synthetic tasks with known answers (numbers with tolerance, keywords, tool budgets).
`python eval.py --backend claude` (or `fake`, `ollama`, `prod`) runs them as fresh conversations, auto-approving
and accepting suggestions, and writes `runs/evals/<time>-<backend>.json` and `.md`: score per task, failed checks,
steps, tool calls, model calls and seconds, repairs, compared with the previous run of the same backend.
Run it before and after every prompt, policy or model change.
