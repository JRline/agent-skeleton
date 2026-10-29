# How to write a dataset file

A dataset is one table or view on a data source. The agent learns it only from its file,
`extensions/connectors/<connector>/datasets/<dataset>.md` (one file per dataset). `list_datasets` shows its front
matter, `describe_dataset` returns the whole file, the request check uses `required_filters` to reject a query with a
missing filter, and a skill that lists the dataset puts the file into the agent's prompt once accepted. A wrong or
missing line here becomes a wrong number in an answer, so every file follows the same format. Only described
datasets can be queried: the files are the allowlist.

Each environment keeps its own connectors and datasets; the repository only has a synthetic example
(`sample_lake/datasets/usage_limits.md`). To draft one with the GPT CLI:
`python scripts/gpt_pack.py dataset "<column list and notes>" --connector <name> --out pack.md`. Then check every
line marked **TO CONFIRM** with the data owner.

## The format

Keep the front matter keys and the headings in this order. `description` is read by `list_datasets`: one line.
Copy `extensions/connectors/_template/datasets/_template.md`.

```markdown
---
name: <dataset name, exactly as the server has it>
description: <One sentence: what one row holds, for which entity, in which unit.>
partition_column: <partition column, if the dataset is partitioned>
required_filters: `<column>`, `<column>` or `<column>`, `<column>` (or GROUP BY), ...
default_filters: <column> = '<value>' AND <column> = '<value>' ...
---
### Grain and keys
- Partitioned by <what> in `<partition column>`. Always filter on it.
- One row per `<key>` x `<key>` x ... (name the leaf of each hierarchy).
- Value column(s), unit, and whether rows exist for zero amounts. Say whether the dataset holds leaf rows only.
### Filters needed for correct numbers (each one double counts if left out)
- `<column>`: its values and meaning. Why leaving it out double counts. Which value is the default.
### Time and the latest date
- The date column, its calendar (business days?), and how to find the latest date (the exact query).
- Month and quarter ends: which day is used.
- Whether amounts are balances (never add across dates) or flows.
### Hierarchies (first level = top)
- <Hierarchy>: `<level 1>` > `<level 2>` > ... ; `<leaf key>` is the leaf key. List the values of small levels.
### Measures and notes
- Units, and the grain at which limits or targets are set (compare at that grain).
- Anything not in the dataset (and where it is instead).
```

## Rules

1. **Write only what is true for the real dataset.** Mark anything unconfirmed as `TO CONFIRM`; never guess.
2. **Every filter that double counts gets a line**, with the reason. These lines are what the request check uses.
3. **Name the default for every filter** (e.g. the recommended version), so a "latest" question needs no
   confirmation.
4. **State the grain** at which comparisons are valid (e.g. limits set per component, location and product). Skills
   compare at that grain; a sum over a coarser level can hide a breach.
5. **Give the exact query for the latest date**, filters included.
6. **Use the column and value names exactly as the dataset has them.** Skills and evals copy them.
7. **No data rows, no credentials, no server names.** The file describes the dataset; it does not contain it.

## After writing or changing a dataset file

1. Put it in the folder of the connector that serves it (a new connector first, if needed: docs/extensions.md).
   Its name must be unique across the enabled connectors.
2. Skills for this dataset list it in `datasets:` and use the names above.
3. Add golden tasks for the dataset (`docs/maintenance.md`, procedure E) and run the evals.

Notes every dataset of a connector shares (calendar, units) go in `ABOUT` in its `connector.py`, once. Sign-in is
not part of a dataset: a connector that needs each user's own login says `NEEDS_LOGIN = True`; never put a user ID or
password in any file.
