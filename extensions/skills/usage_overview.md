---
name: usage_overview
description: High-level summary of the usage_limits dataset (RWA and leverage usage and limits) for an analyst - what it holds, the filters needed for correct numbers, which dates are loaded for each resource and version, the hierarchy outline, and current totals. Two small queries at most.
toolkits: datalake, analysis
datasets: usage_limits
when_to_use: Only when the user asks to profile, summarise or describe the usage_limits dataset itself - what it contains, how to query it correctly, which dates are loaded. Not for questions about values in it (totals, breakdowns, changes, limits, breaches), and not for an attached or local file (use file_summary).
---
## Steps
1. describe_dataset for the dataset: it gives the grain, the required filters and their values, the hierarchies, the measures and the partitions (partition_key). Most of the summary comes from here, with no query.
2. One sql_query on the latest partition only (partition_key = the newest listed), all other required filters at their recommended values except those you GROUP BY: MIN(as_of_date), MAX(as_of_date) and COUNT(DISTINCT as_of_date) per resource (resource), scenario and version. Never scan every partition or every row to check quality. save_as loaded_dates.
3. One sql_query at the latest CLOSED date of each resource from step 2 (as_of_date IN those dates), USED and ALLOC, method BOOKED: SUM(amount) per resource, scenario and division, keeping as_of_date in the result. save_as totals_by_division.
4. Give the final answer from the documentation and the two tables.

## Checks
- Every lake query filters on the partition and the required columns (or groups by them), and there are no more than two.
- Dates, row counts and totals cite the sql_query refs; filter values and definitions cite the documentation.
- Each table shown keeps the columns that say what its numbers are (resource, scenario, version, date).

## Output
Use these sections, each short, with markdown tables where the content is tabular:
1. **What it is**: one sentence (content, unit, grain).
2. **Filters you need**: a table with columns Filter | Values to use | Why (what goes wrong without it).
3. **Loaded dates**: a table with columns Resource | Scenario | Version | First date | Latest date | Dates loaded, from step 2. Then one line on which date to use for "latest" (the latest CLOSED date unless the user wants flash).
4. **Hierarchies**: one line per hierarchy, top level to leaf.
5. **Current totals**: the headline totals per resource at its latest CLOSED date (usage and limit); list the step 3 table in "tables" with a title like "Usage and limit by division at the latest CLOSED date (JPY m)".
6. **Example query**: one correct query for a common question, in a ```sql block, one clause per line, with every required filter.
