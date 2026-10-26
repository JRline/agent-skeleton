# How to write a data-source entry

The agent learns a data lake view only from its entry in the data dictionary
(`sandbox/references/datalake_dictionary.md`, one `## <view name>` section per view). The executor reads it before
querying, the request check uses it to reject a query with a missing filter, and skills point to it. A wrong or
missing line here becomes a wrong number in an answer, so every section follows the same format.

Each environment keeps its own dictionary with its real views; the repository only has a synthetic example.
To draft an entry with the GPT CLI: `python scripts/gpt_pack.py view "<column list and notes>" --out pack.md`
(the pack includes this file). Then check every line marked **TO CONFIRM** with the data owner.

## The format

Keep the headings and their order. The first two lines after the title are read by `list_datasets`, so keep them
to one line each.

```markdown
## <view name>
<One sentence: what one row holds, for which entity, in which unit.>
Required filter: `<column>`, `<column>` (or GROUP BY), ...
Default filters: <column> = '<value>' AND <column> = '<value>' ...
### Grain and keys
- Partitioned by <what> in `<partition column>`. Always filter on it.
- One row per `<key>` x `<key>` x ... (name the leaf of each hierarchy).
- Value column(s), unit, and whether rows exist for zero amounts. Say whether the view holds leaf rows only.
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
- Anything not in the view (and where it is instead).
```

## Rules

1. **Write only what is true for the real view.** Mark anything unconfirmed as `TO CONFIRM`; never guess.
2. **Every filter that double counts gets a line**, with the reason. These lines are what the request check uses.
3. **Name the default for every filter** (e.g. the recommended version), so a "latest" question needs no
   confirmation.
4. **State the grain** at which comparisons are valid (e.g. limits set per component, location and product). Skills
   compare at that grain; a sum over a coarser level can hide a breach.
5. **Give the exact query for the latest date**, filters included.
6. **Use the column and value names exactly as the view has them.** Skills and evals copy them.
7. **No data rows, no credentials, no server names.** The entry describes the view; it does not contain it.

## After writing or changing an entry

1. Add the view to the allowlist (`DATALAKE_VIEWS`, or the lake module's `list_views`).
2. If the partition column has a different name, set `LAKE_PARTITION_COLUMN`.
3. Skills for this view name it in `source:` (e.g. `source: data lake view <view name>`) and use the names above.
4. Add golden tasks for the view (`docs/maintenance.md`, procedure E) and run the evals.

## Example (synthetic)

The section `## usage_limits` in `sandbox/references/datalake_dictionary.md` follows this format; use it as the
model for a new entry.
