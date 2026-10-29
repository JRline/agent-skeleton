# Maintaining the agent with the GPT CLI

For whoever keeps the agent working after it ships: adding skills and tools for real data, keeping the evals
green, and finding out why a run went wrong. The helper is the GPT CLI, which cannot see the project, so
every task starts by pasting it a **context pack** that `scripts/gpt_pack.py` builds for you.

Setup and settings are in `docs/setup.md`; the design is in `docs/design.md`.

## What you change, and what stays fixed

| You change (per environment, no approval needed) | Stays fixed (change only with the owner's approval) |
|---|---|
| Connectors and their datasets: `extensions/connectors/<name>/` | The LangGraph agent and guardrails: `agent/` |
| Skills: `extensions/skills/*.md` | Prompts: `agent/prompts/*.md` |
| Toolkits: `extensions/toolkits/*.py` and their tests | The UI and CLI: `app.py`, `cli.py` |
| Planner worked examples: `extensions/examples/planner.md` | Packages: `requirements.txt` (production is closed) |
| Golden tasks: `tests/golden/*.json` | The docs: `docs/`, `CLAUDE.md`, `README.md` |

Each type has a template (names starting with `_` are never loaded) and a section in `docs/extensions.md`.

The fixed part enforces the rules that keep answers trustworthy (arithmetic in tools, every number traced to a
source, approvals for anything that writes, read-only data sources). A skill or tool can never switch them off.

## The loop for every change

1. **Pack.** `python scripts/gpt_pack.py <task> "what you want" --out pack.md` (tasks below).
2. **Ask.** Give `pack.md` to the GPT CLI as the prompt (for example, pipe the file into it). If it is too long
   for one prompt, send it in two parts (each `=====` block is a separate file).
3. **Save.** Copy each file from the reply to the path it names. Read it once: does it say what you meant?
4. **Test.** `python -m unittest discover -s tests -t .` must pass. A failure: send the error to the GPT CLI
   in the same session and ask for a fix.
5. **Evaluate.** `python scripts/eval.py --backend prod` (all golden tasks, about 20-30 minutes) or
   `--only id1,id2` for the tasks you touched. The score must not drop. See "Evals" below.
6. **Try it.** Ask the question in the UI (`streamlit run app.py`) with **Verbose** on and check the plan, the
   requests and the answer.
7. **Record.** Add a golden task for the new behaviour (procedure E), so it stays tested.

Never paste real or confidential data into anything that leaves the closed environment. Packs contain code,
skills, dataset files and eval questions only, never data rows, unless you add them yourself.

## Procedures

### A. Add or update a skill
A skill is a Markdown recipe for one kind of analysis (for example "compare a resource between two dates").

1. Decide what it produces and when it should be suggested. One skill per analysis pattern, not per report.
2. `python scripts/gpt_pack.py skill "Add a skill that ... It should be used when ... and not when ..." --like usage_comparison --out pack.md`
   (`--like` picks the existing skill shown as the model to follow: choose the closest one).
3. Save the reply as `extensions/skills/<name>.md`. Check that the steps:
   - name only tools in the pack's tool list, and do all arithmetic in SQL or `run_python`;
   - take the period and scope from the request (a period stated in it, "latest", or an open one confirmed with
     the user);
   - compare things at the grain the data is defined at (see the data dictionary).
4. Loop steps 4-7. In the UI, also check that the planner suggests the skill for a matching question and does
   **not** suggest it for a near miss. If it misfires, sharpen `when_to_use` or add a planner example (D).

### B. Add or update a toolkit (Python tools)
Only when a skill cannot do it with `sql_query` and `run_python` (for example a new data source or a fixed
calculation that must be exact every time).

1. `python scripts/gpt_pack.py toolkit "Add a tool that ..." --out pack.md`
2. Save both files from the reply: `extensions/toolkits/<name>.py` and `tests/test_ext_<name>.py`.
3. Run the tests. Restart the UI; the toolkit appears in the sidebar **Tools** list and under **Commands**.
   An "Extension not loaded" warning shows the import error.
4. Anything that writes, sends or deletes must be `risky=True`. Check this yourself.

### C. Add a data source (connector) or a dataset
A connector is one connection (a server, a database); a dataset is one table or view on it. Formats:
`docs/extensions.md` (connectors) and `docs/dataset_entry.md` (datasets).

**A new connector** (once per data source):
1. `python scripts/gpt_pack.py connector "Add a connector for <server>: <driver>, users sign in with their own ID" --out pack.md`
2. Save `extensions/connectors/<name>/connector.md`. Use a built-in kind (`odbc` for most servers). Set the
   settings it names, e.g. the environment variable with the connection string without user or password.
3. Restart the UI: with `needs_login: yes` the sidebar shows **Data sign-in: <name>**. Sign in and check that
   **Datasets** lists nothing yet (no dataset files) and no "Extension not loaded" warning appears.

**A new dataset** (one per table):
1. Copy the dataset's column list and notes from the source system.
2. `python scripts/gpt_pack.py dataset "Add the dataset X. Columns: ... Notes: ..." --connector <name> --out pack.md`
3. Check every line marked **TO CONFIRM** with the data owner, then save
   `extensions/connectors/<name>/datasets/<dataset>.md`. The file is the allowlist: only described datasets can
   be queried. Its name must be unique across the enabled connectors.
4. Write or update the skills that use it (`datasets:` in the skill, A) and add golden tasks for it (E).

### D. Update the planner's worked examples
The planner sees a few example goals with the reply it should give. They steer which skill it suggests.

1. `python scripts/gpt_pack.py examples "Add an example where ... should (not) suggest ..." --out pack.md`
2. Save `extensions/examples/planner.md`. Keep 3-5 varied examples, using only this environment's skill names.
   `tests/test_examples.py` checks the format and that no example copies a golden task.
3. Run the evals: examples change every plan.

### E. Golden tasks and evals
A golden task is a question plus the facts a correct answer must contain (`tests/golden/<id>.json`).

1. `python scripts/gpt_pack.py golden "Add tasks for ..." --out pack.md`
2. **Compute every expected number yourself** (a query, or a run whose answer you checked) before saving. Never
   keep a value the GPT marked TO CHECK without checking it.
3. Run: `python scripts/eval.py --backend prod --only <new ids>`, then the whole set.
4. Results go to `runs/evals/<time>-<backend>.md`, compared with the previous run. The table lists failed checks
   per task. Runs vary: if a task fails once, run it again before changing anything.

Fields: `instruction`, `inputs`, `expect_numbers` (`value`, `tol`), `expect_keywords` (a list inside means "any
one of"), `expect_ask`, `max_asks`, `max_tool_calls`, `max_steps`.

### F. Find out why a run went wrong
1. Find the run id: the UI header shows it, and each run is a folder under `runs/`.
2. `python scripts/gpt_pack.py diagnose "what was wrong, in your words" --run <run id> --out pack.md`
3. The GPT replies with the first wrong step, the cause and the smallest fix. Prefer a fix in a skill, an
   example or a dataset file. A fix in a prompt or in `agent/` needs the owner (G).
4. Add a golden task that would have caught it (E).

Useful files in `runs/<run id>/`: `log.jsonl` (every model and tool call, with purpose, effort and latency),
`state.json` (plan, facts, status). In the UI: **Verbose**, and **Model calls (effort and latency)** under a run.
`python cli.py --export-script <run id>` writes one Python file that replays the whole analysis.

### G. Change a prompt or the framework (owner approval)
1. Run the full eval first, so you have a baseline.
2. Make one change at a time. Prompts: `agent/prompts/*.md` (short, literal, one JSON shape; keep every
   `{{placeholder}}`). Users can also adjust wording in the UI's prompt settings, saved in `settings/`.
3. Run the unit tests and the full eval again; compare the two reports. Keep the change only if the score holds
   and no task got worse for a reason you can explain.
4. Update `docs/design.md` when the change alters behaviour it describes.

## Production configuration

| Variable | Production value | Meaning |
|---|---|---|
| `LLM_BACKEND` | `prod` | Use the production chat API |
| `PROD_API_MODULE` | your wrapper module | Module with `call(text, effort) -> str` (copy `agent/backends/prod_template.py`) |
| `MODEL_MEMORY` | `session` if the wrapper has `channel` | Send fixed prompt parts once per instance (docs/design.md §4a); `off` sends everything each call |
| `MEMORY_REFRESH_TOKENS` | `20000` | Session mode: re-send everything after this much text (lower it if answers drift after compression) |
| `CONNECTORS` | e.g. `lake_odbc` | The connectors to enable (default: every folder in `extensions/connectors/`; leave out `sample_lake` in production) |
| `LAKE_ODBC` (or the name in a connector's `connection_env`) | your driver, server, port, database | An `odbc` connector's connection string without the sign-in (UID/PWD come from each user's sign-in) |
| `AGENT_HOME` | project folder | Where `runs/`, `sandbox/` and `settings/` live |
| `AGENT_EXTENSIONS` (`AGENT_SKILLS`, `AGENT_TOOLKITS`, `AGENT_EXAMPLES`) | default `extensions/...` | Point to another folder of extensions (or one type) |
| `BASIC_TOOLKITS` | e.g. `files,analysis,datalake,docs` | Toolkits on when a session starts |
| `AGENT_MAX_STEPS` | `15` | Tool steps per instruction before it stops (at least; longer plans get more) |
| `AGENT_STEPS_PER_PLAN_STEP` | `3` | Steps per plan step, so a 7-step plan gets 21 |
| `SQL_TIMEOUT_S` | `900` | Longest a lake query may run (15 minutes); set the lake driver's timeout to match |
| `TOOL_TIMEOUT_S` | `30` | Time limit for every other tool call |

Check that `python scripts/smoke_model.py --backend prod` passes after any change to the wrapper or the model.

## Troubleshooting

| Symptom | Where to look | Usual fix |
|---|---|---|
| Answers a different period or scope than asked | `USER ANSWER` facts, the skill's steps | The skill must take the period from the request (A) |
| A breach or total looks wrong | the SQL in the run | Compare at the grain the dataset file defines (per measure leaf, per level); fix the skill |
| Many steps, then "Reached the step limit" | `log.jsonl`: repeated `review` rejections | A skill step that does not fit the question; make it conditional ("only if ...") |
| Suggests the wrong skill | the planner reply in `log.jsonl` | Sharpen `when_to_use`; add a counter-example (D) |
| "File not found" for a file the user attached | the tool error lists the exact names | The name has a space or typo; it must be copied exactly (already in the prompts) |
| The reviewer keeps rejecting a good answer | `review` events | Check the cited outputs hold the numbers; a skill check may be too strict |
| An extension does not appear | UI warning "Extension not loaded" | The error is shown (import error, broken front matter, a dataset name used twice); fix and restart |
| Every call fails | `scripts/smoke_model.py --backend prod` | The wrapper module or the API; nothing in skills |
| "Not signed in to <connector>" / "Sign-in failed" | the sidebar's Data sign-in | Sign in again (sign-ins are memory only and end with the session); check the account |
| "This query reads datasets of several connectors" | the SQL | Query each connector separately and combine the extracts with run_python (say so in the skill) |
| A run stopped with "Model call failed" and resumes oddly | `runs/checkpoints.sqlite`, `log.jsonl` | Retry resumes from the last checkpoint; deleting the run folder alone does not reset a thread (start a new conversation) |
