---
name: my_skill
description: One line: what the skill produces.
toolkits: datalake, analysis
datasets: my_dataset
when_to_use: The user asks for ... Not for ... (say when it does not apply).
---
Files starting with "_" are not loaded: copy this to extensions/skills/<name>.md. Leave `datasets` out for a general
skill (e.g. any attached file). Datasets may come from different connectors: query each, combine with run_python.
## Steps
1. One sql_query on my_dataset at the dates the request names, aggregated to the level asked; save_as base.
2. run_python on base: the calculation, every number from the data.
3. Final answer from the result, listing the table in "tables".
## Checks
- What the reviewer must check (the right dates, filters, level).
## Output
What the answer looks like: headline first, then the key numbers.
