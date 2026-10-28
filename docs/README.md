# Documentation

Every document for this project, by who needs it. Start with the first row that matches what you are doing.

## For people using or running the agent

| Document | What it covers |
|---|---|
| [setup.md](setup.md) | Install, the three stages (fake model, local model, production), swapping in a real model API, and every setting |
| [usage.md](usage.md) | How a session works: tools, skills, the data lake and its sign-in, attachments, approvals, and the evals |

## For people maintaining it (with the GPT CLI)

| Document | What it covers |
|---|---|
| [maintenance.md](maintenance.md) | Step-by-step procedures: add or update a skill, a toolkit, a data source, planner examples, golden tasks; diagnose a run; production settings; troubleshooting |
| [dataset_entry.md](dataset_entry.md) | The exact format of a dataset file (one per table or view), and its rules |
| [extensions.md](extensions.md) | The contract for connectors, datasets, skills, toolkits and planner examples: format, safety rules, tests |
| [gpt_prompts/](gpt_prompts/) | The prompts `scripts/gpt_pack.py` sends to the GPT CLI (one per maintenance task) |

## For understanding the design

| Document | What it covers |
|---|---|
| [design.md](design.md) | The full design spec: the LangGraph agent and what is stock or custom, the reply protocol, tools, data lake sign-in, memory, guardrails, effort policy, evals. Read-only: change it only with the owner's agreement. |

## For coding agents

[`CLAUDE.md`](../CLAUDE.md) at the project root: the rules any coding assistant must follow, recipes for common changes,
and the current status. It points back to [extensions.md](extensions.md) and [design.md](design.md).

## Where else documentation lives

- [`README.md`](../README.md): what the project is, a quick start, and a map of these documents.
- [`extensions/README.md`](../extensions/README.md): a short note on the extension folders and their templates.
- `extensions/connectors/<name>/datasets/*.md`: the dataset files themselves (read by the agent at run time).
