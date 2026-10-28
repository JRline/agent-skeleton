---
name: my_dataset
description: One line: what one row is, what it measures, the unit (e.g. daily positions by desk, JPY millions).
partition_column: fiscal_year
required_filters: `fiscal_year`, `scenario` (or GROUP BY)
default_filters: fiscal_year = 'FY2025' AND scenario = 'ACTUAL'
---
### Grain and keys
- One row per ... (list every key column). Leaf rows only? Subtotal rows? Say which.
### Filters needed for correct numbers (each one double counts if left out)
- `scenario`: ... Never add the values together.
### Time and the latest date
- Date column, calendar, how to find the latest date.
### Hierarchies (first level = top)
- ...
### Measures and notes
- Units, how the amount is calculated, what is not in this dataset.
