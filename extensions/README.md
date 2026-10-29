# Extensions: everything added per environment

Four types, one folder each. Copy the template (names starting with `_` are never loaded), fill it in, restart.

- `connectors/<name>/` — one connection to a data source: `connector.py` (kind, sign-in, connection string) and
  `datasets/<dataset>.md`, one file per table or view. Template: `connectors/_template/`.
- `skills/<name>.md` — Markdown recipes (steps, checks, output) that may use datasets of several connectors.
  Template: `skills/_template.md`.
- `toolkits/<name>.py` — Python tools the agent can call. Template: `toolkits/_template.py`.
- `examples/planner.md` — worked plans for the planner, naming this environment's skills.

The contract (formats, tests, and the rule to leave the framework alone) is `docs/extensions.md`. Maintaining
these with the GPT CLI: `docs/maintenance.md` and `python scripts/gpt_pack.py`.
