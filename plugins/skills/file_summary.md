---
name: file_summary
description: Summarise an attached file (CSV or Excel workbook) - what it is, how it is laid out (sheets, table blocks, header rows, totals), the logic in its formulas, and its key figures.
toolkits: files, analysis
when_to_use: The user asks what an attached file contains, to summarise or explain it, or how it is built. Not for a data lake view.
---
## Steps
1. read_file the file. For a workbook this gives each sheet's table blocks (range, header rows, totals rows), the formulas behind them and the read_range call for each block; for a CSV, its columns and first rows. Most of the summary comes from here.
2. Only if the summary needs figures the layout does not show (totals, changes, ranges): one run_python that reads each needed block with read_range (or the CSV) and computes them all at once; save_as key_figures. Skip this step for a short file whose values are all in the preview.
3. Give the final answer.

## Checks
- The layout described matches read_file (sheets, table ranges, header and totals rows).
- Formulas are explained in words where they define a figure (e.g. "Change = 2026Q3 minus 2026Q2", "Total = sum of rows 5-7").
- Figures cite the read_file or run_python ref; totals rows are not added to the rows they total.

## Output
Short sections: **What it is** (one or two sentences); **Layout** (per sheet: its tables with their ranges and what each holds; titles and notes); **How the figures are built** (the formula logic in words, if any); **Key figures** (a small table or bullets); **Points to note** (only what a reader must know to use the file correctly, e.g. totals rows, units, labels that do not match the dates).
