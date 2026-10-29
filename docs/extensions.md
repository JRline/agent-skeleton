# Extending the agent: connectors, datasets, skills, toolkits (for people and coding agents)

Read this before adding anything to `extensions/`. It is the contract between extensions and the agent.

| Type | What it is | Where | Code? | Template |
|---|---|---|---|---|
| Connector | one connection to a data source (server, database, local sample) | `extensions/connectors/<name>/connector.py` | only for a new connection kind | `extensions/connectors/_template/` |
| Dataset | one table or view on a connector: meaning, filters, columns | `extensions/connectors/<connector>/datasets/<name>.md` | no | `…/_template/datasets/_template.md` |
| Skill | a recipe: steps, checks, output; may use datasets of several connectors | `extensions/skills/<name>.md` | no | `extensions/skills/_template.md` |
| Toolkit | Python tools the agent can call | `extensions/toolkits/<name>.py` | yes | `extensions/toolkits/_template.py` |
| Example | worked plans for the planner | `extensions/examples/planner.md` | no | the file itself |

Files and folders starting with `_` are templates: never loaded. With the GPT CLI, `python scripts/gpt_pack.py
<connector|dataset|skill|toolkit|examples> "<request>"` builds the context pack (docs/maintenance.md).

## Scope rule (do not skip)
When you are asked to add a connector, dataset, skill, toolkit or example, **only add files under `extensions/`**
and their tests (`tests/test_ext_<name>.py`). **Do not change the framework** (`agent/`, `app.py`, `cli.py`,
`agent/prompts/`, `agent/config.py`, `scripts/`) **or the docs** (`README.md`, `CLAUDE.md`, everything in `docs/`)
unless the user specifically agrees. If an extension seems to need a framework change, stop and ask; describe it.

## Connectors (`extensions/connectors/<name>/connector.py`)
```python
NAME = "lake_odbc"
KIND = "odbc"              # odbc | mock | sqlite | custom (the functions below, defined in this file)
DESCRIPTION = "The production data lake, read-only."
NEEDS_LOGIN = True         # each user signs in with their own user ID and password
CONNECTION_STRING = "DRIVER={Lake ODBC Driver};SERVER=lake.example.internal;PORT=9996;DATABASE=analytics"  # no UID/PWD
DIALECT = "trino"
ABOUT = """Notes every dataset of this connector shares (calendar, units, how dates work)."""
```
- Built-in kinds live in `agent/connectors/`: `odbc` (pyodbc; UID and PWD come from the user's sign-in),
  `mock` (the local mock lake server, `URL`), `sqlite` (a folder of CSVs, `DATA`). Set `DIALECT` to the server's SQL
  dialect as sqlglot names it (e.g. trino, tsql, oracle, postgres): queries are read and row limits written in it. Use `custom` only when none
  fits: `KIND = "custom"` and `connector.py` itself defines `connect(connector, login)`, `list_datasets(connector, login)` and optionally
  `partitions(connector, dataset, column, login)`, `limit_sql(sql, n)`, `source(connector)`. Read-only, always.
- Sign-in: with `NEEDS_LOGIN = True` the sidebar shows **Data sign-in: <name>** (CLI `--sign-in name=user`). The
  login is held in memory only, per conversation and connector, never written anywhere, masked in errors.
- Settings: `CONNECTORS=a,b` picks the enabled connectors (default all); `CONNECTOR_<NAME>_<KEY>` overrides any
  constant (e.g. `CONNECTOR_SAMPLE_LAKE_KIND=mock`). Never put a user ID, password or token in a file.
- Optional `sample.py` with `write()` builds synthetic data for local runs (`scripts/setup_local.py` calls it), and
  optional `inputs()` returns sample input files that go with it (e.g. a mapping table); data folders are git-ignored.
- Sample connectors, datasets and their data are extensions only: the core (`agent/`, `app.py`, `cli.py`, `scripts/`,
  prompts) never names one; it finds them through `agent.connectors`.

## Datasets (`extensions/connectors/<connector>/datasets/<name>.md`)
One file per table or view; **only described datasets can be queried** (the files are the allowlist). Dataset
names are unique across the enabled connectors, so a query's datasets say which connector runs it; one query
reads one connector (combine connectors with `run_python`). The format is in `docs/dataset_entry.md`:
front matter `name`, `description` (one line), `partition_column`, `required_filters`, `default_filters`, then the
sections (grain and keys, filters needed for correct numbers, time, hierarchies, measures).

## How extensions connect to the agent
- `agent/extensions.py` loads built-in toolkits (`agent/toolkits/*.py`) and then `extensions/toolkits/*.py`.
  Skills are read from `extensions/skills/*.md`, connectors and datasets by `agent/connectors`.
- An extension that fails to load is listed in `extensions.ERRORS` / `connectors.ERRORS` and shown in the UI; the
  agent keeps working.
- Every toolkit, tool and skill appears in one catalogue: the sidebar **Tools** list, the **Commands** list
  (`/name`), the CLI `--tools`, and the planner's list of things it may **suggest**.
- A session starts with the toolkits marked `"basic": True`. Others are enabled by the user (sidebar, `/name`,
  `--tools`) or by accepting the agent's suggestion. The model only sees tools that are enabled, plus the
  `guidance` of their toolkits.

## Toolkit contract (`extensions/toolkits/<name>.py`)
Start from `extensions/toolkits/_template.py`.
```python
from pydantic import BaseModel, Field
from agent.tools import ToolContext, ToolError, ToolOutput, tool   # also: sandbox_path, _load_table

TOOLKIT = {"name": "fx", "description": "One line for users and the planner.",
           "basic": False, "guidance": "Prompt rules for this toolkit, shown only when it is enabled."}

class ConvertArgs(BaseModel):
    amount: float = Field(description="...")       # every field gets a description: it is shown to the model

@tool("convert_amount", "What it returns, how big, and when NOT to use it.", ConvertArgs, risky=False)
def convert_amount(args: ConvertArgs, ctx: ToolContext):
    return "short result"                          # or ToolOutput(preview="what the model sees", full="stored under the ref")
```
Rules:
- `TOOLKIT["name"]` and every tool name are unique (a clash is a load error). Names: letters, digits, `_`.
- Deterministic Python. **All arithmetic happens in the tool**; the model never computes numbers.
- Files: only inside the sandbox, via `sandbox_path(path)`. Scratch tables go to `config.work_dir(ctx.run_id)`;
  anything delivered to the user (outputs, sends, deletes) must be `risky=True` (the user approves it).
- Errors: raise `ToolError("clear message the model can act on")`. `dispatch` turns any exception into an
  observation; never let a tool crash the loop. Tools run with a timeout (`config.TOOL_TIMEOUT_S`, 30 s); a tool that legitimately runs longer passes
  `timeout_s=lambda: <seconds>` to `@tool` (as `sql_query` does with `config.SQL_TIMEOUT_S`).
- Keep previews small (the model sees `preview`, truncated); put full data in `full` or a work table.
- Use only the standard library and the packages in `requirements.txt` (production is closed).
- Write plain `@tool` functions: the agent turns every registered tool into a LangChain tool by itself
  (`agent/lc_tools.py`), so a toolkit never imports LangChain or LangGraph.
- Synthetic data only, in code, tests and `sandbox/`.

## Skill contract (`extensions/skills/<name>.md`, no code)
```markdown
---
name: rwa_variance
description: One line for users and the planner.
toolkits: datalake, analysis        # enabled when the skill is used
datasets: usage_limits, fx_rates    # optional: the datasets it is written for (any connectors); omit for a general skill
references: context/method.md       # optional: other sandbox documents (methodology)
when_to_use: When the planner should suggest it (and when not).
---
## Steps
1. Numbered steps written in terms of tools (they become the plan).
## Checks
- Bullets added to the reviewer's checklist.
## Output
What the final answer looks like.
```
Steps should make tools do every calculation (SQL for extraction, one `run_python` for the analysis), and ask
for a ref after every number. A skill runs when the user picks it or types `/name`, or accepts the agent's
suggestion. Once it is accepted, its steps and its datasets' documentation are in the agent's prompt (until then
the agent sees one line). Optional `references:` lists other sandbox documents (methodology) for the CONTEXT.

**General or tied to datasets.** A skill that knows datasets' fields and rules (filters, versions, hierarchies)
lists them in `datasets:` and is only offered for questions about them; they may come from different connectors
(query each, then combine the saved extracts with one `run_python`). A general skill (e.g. summarising any
attached file) has no `datasets:` and must not assume any dataset's fields.

**Make skills general.** A skill describes a kind of analysis, not one question: take the entity, dates, slice
and breakdown from the request (with sensible defaults, and ask_user only when they can't be inferred). Prefer
one skill per analysis pattern (e.g. "compare a measure between two dates") over one per report or per number.
Tools should be equally general: no tool for a single calculation that `run_python` can do.

## Tests (required for every extension)
Add `tests/test_ext_<name>.py`. A toolkit: load it (`extensions.load(force=True)`), assert `extensions.ERRORS == []`,
call each tool through `dispatch(...)` for normal use, bad input and sandbox limits. A skill: `get_skill(name)` has
steps and checks. A connector or dataset: `connectors.enabled()` has it and `connectors.ERRORS == []`
(`tests/test_skills.py` already checks that every skill's datasets exist and that the templates parse). Run
`python -m unittest discover -s tests -t .`.

## Planner examples (`extensions/examples/planner.md`)
A few goals with the planner's ideal reply, filled into the planner prompt's `{{examples}}` slot. Use this
environment's skill names only; `tests/test_examples.py` checks the format. Procedure: `docs/maintenance.md` (D).

## Checklist
- [ ] Only files under `extensions/` and `tests/` changed.
- [ ] `TOOLKIT` has name, description, basic, guidance; tool and arg descriptions are clear.
- [ ] Writes outside `sandbox/work/` are `risky=True`; no arithmetic left to the model.
- [ ] No user ID, password or token in any file; connections are read-only.
- [ ] Tests pass; the UI shows no "Extension not loaded" warning.
