# Plugins: add tools and skills without touching the agent

- `tools/` — Python toolkits. Copy `tools/_example_toolkit.py` to a new file (no leading `_`), give it a
  unique `TOOLKIT["name"]`, write the tools. It appears in the sidebar **Tools** list after a restart.
- `skills/` — Markdown skills: front matter (`name`, `description`, `toolkits`, `when_to_use`) plus
  `## Steps`, `## Checks`, `## Output`. Use one with the sidebar **Skill** picker or `/skill_name` in the task.

- `examples/` — worked examples for the planner (`examples/planner.md`): goals with the reply the planner
  should give. Swap them per environment so they name that environment's skills.

Maintaining these with the GPT CLI: `docs/maintenance.md` and `python scripts/gpt_pack.py`.
The contract (format, tests, and the rule to leave the infrastructure alone) is in `docs/plugins.md`.
