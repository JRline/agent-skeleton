# Extending the agent: toolkits and skills (for coding agents)

Read this before adding a tool, toolkit, plugin or skill. It is the contract between plugins and the agent.

## Scope rule (do not skip)
When you are asked to create a new tool, toolkit, plugin or skill, **only add files under `plugins/`**
(`plugins/tools/*.py`, `plugins/skills/*.md`) and their tests (`tests/test_plugin_<name>.py`).
**Do not change the infrastructure layer** (`agent/`, `app.py`, `cli.py`, `agent/prompts/`, `agent/config.py`,
`smoke_model.py`) **or the docs** (`README.md`, `CLAUDE.md`, everything in `docs/`) unless the user
specifically agrees. If the plugin seems to need an infrastructure change, stop and ask; describe the change.

## How plugins connect to the agent
- `agent/plugins.py` loads built-in toolkits (`agent/toolkits/*.py`) and then `plugins/tools/*.py`
  (files starting with `_` are skipped). Skills are read from `plugins/skills/*.md`.
- A plugin that fails to import is listed in `plugins.ERRORS` and shown in the UI; the agent keeps working.
- Every toolkit, tool and skill appears in one catalogue: the sidebar **Tools** list, the **Commands** list
  (`/name`), the CLI `--tools`, and the planner's list of things it may **suggest**.
- A session starts with the toolkits marked `"basic": True`. Others are enabled by the user (sidebar, `/name`,
  `--tools`) or by accepting the agent's suggestion. The model only sees tools that are enabled, plus the
  `guidance` of their toolkits.

## Toolkit contract (`plugins/tools/<name>.py`)
Start from `plugins/tools/_example_toolkit.py`.
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
- Synthetic data only, in code, tests and `sandbox/`.

## Skill contract (`plugins/skills/<name>.md`, no code)
```markdown
---
name: rwa_variance
description: One line for users and the planner.
toolkits: datalake, analysis        # enabled when the skill is used
references: references/datalake_dictionary.md  # optional: context documents
source: data lake view usage_limits    # optional: the one data source it is written for; omit for a general skill
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
suggestion. Optional front matter `references:` lists sandbox documents (data dictionary, methodology) that
become the run's CONTEXT while the skill is active.

**General or tied to a source.** A skill that knows one source's fields and rules (a data lake view's filters,
versions, hierarchies) names it in `source:` and is only offered for questions about that source. A general skill
(e.g. summarising any attached file) has no `source:` and must not assume any source's fields.

**Make skills general.** A skill describes a kind of analysis, not one question: take the entity, dates, slice
and breakdown from the request (with sensible defaults, and ask_user only when they can't be inferred). Prefer
one skill per analysis pattern (e.g. "compare a measure between two dates") over one per report or per number.
Tools should be equally general: no tool for a single calculation that `run_python` can do.

## Tests (required for every plugin)
Add `tests/test_plugin_<name>.py`: load the plugin (`plugins.load(force=True)`), assert `plugins.ERRORS == []`,
call the tool through `dispatch(...)` for normal use, bad input and sandbox limits, and for a skill assert that
`agent.skills.get_skill(name)` has steps and checks. Run `python -m unittest discover -s tests -t .`.

## Planner examples (`plugins/examples/planner.md`)
A few goals with the planner's ideal reply, filled into the planner prompt's `{{examples}}` slot. Use this
environment's skill names only; `tests/test_examples.py` checks the format. Procedure: `docs/maintenance.md` (D).

## Checklist
- [ ] Only files under `plugins/` and `tests/` changed.
- [ ] `TOOLKIT` has name, description, basic, guidance; tool and arg descriptions are clear.
- [ ] Writes outside `sandbox/work/` are `risky=True`; no arithmetic left to the model.
- [ ] Tests pass; the plugin shows up under **Commands** in the UI with no "Plugin not loaded" warning.
