"""Toolkit: Data analysis in Python (pandas) written by the model, checked, approved by the user, then run."""
from __future__ import annotations

import json

from pydantic import BaseModel, Field, field_validator

from ..pysandbox import check_code, run_code
from ..tools import ToolContext, ToolError, ToolOutput, tool

TOOLKIT = {
    "name": "analysis",
    "description": "Analyse tables with Python (pandas): joins, pivots, changes, ratios, breakdowns, statistics.",
    "basic": True,      # enabled when a session starts
    "guidance": ("Do all calculations with run_python in as few runs as possible: compute the whole result at "
                 "once (never row by row) and set `result` (a DataFrame for tables) and `notes`. Pass data tables "
                 "in `tables`, or read sandbox files with pd.read_csv/read_excel/read_json or pd.ExcelFile, or one table "
                 "block of a workbook with read_range(path, sheet, range, header_rows) as read_file's layout gives "
                 "it, using the "
                 "path exactly as listed (inputs/x.csv or work/<run>/x.csv; no 'sandbox/' prefix needed). You may import pandas, numpy, math, statistics, datetime, "
                 "collections, itertools, functools, re, json, decimal. Data analysis only: never write files. "
                 "Never type data values into the code or into `tables` (not even from a preview): always read them "
                 "from the file, so the numbers come from the data. "
                 "Each run needs the user's approval, so keep code short and readable, with a short comment above each "
                 "block saying in plain words what it does. Round shares and percentages to 2 decimals. Analyse the small, already "
                 "aggregated extracts saved by earlier steps (pass them in `tables`); never pull detail rows to "
                 "aggregate in Python, and never re-read a saved result only to cite it: cite the step that "
                 "produced it."),
}


class RunPythonArgs(BaseModel):
    code: str = Field(description="Python (pandas) analysis code with real line breaks. Sets `result` and "
                                  "optionally `notes`. Standard analysis imports only; no file writing")
    tables: dict[str, str] = Field(default_factory=dict,
                                   description='Tables to load, name -> sandbox path, e.g. {"rwa": "work/<run>/rwa.csv"}. '
                                               'Paths only, never data')
    save_as: str = Field("analysis", description="Short name for the result table, e.g. rwa_change")

    @field_validator("tables", mode="before")
    @classmethod
    def _paths_only(cls, value):
        """Models paste preview rows here; say what to do instead of pydantic's 'should be a valid string'."""
        if isinstance(value, dict) and not all(isinstance(v, str) for v in value.values()):
            raise ValueError("tables maps a name to a sandbox file path, e.g. {\"cap\": \"inputs/x.xlsx\"}, never data. "
                             "Pass the file's path (or read it in the code with pd.read_excel) instead of typing values.")
        return value


def _check(args: RunPythonArgs) -> str | None:
    return check_code(args.code)


@tool("run_python", "Run data-analysis code on tables and return its result in a fixed JSON format; table "
      "results are also saved as a work table. Needs user approval. Use for every calculation.",
      RunPythonArgs, risky=True, check=_check)
def run_python(args: RunPythonArgs, ctx: ToolContext):
    out = run_code(args.code, args.tables, ctx.run_id, args.save_as)
    if not out["ok"]:
        raise ToolError(out["error"] + (f"\nstdout: {out['stdout']}" if out.get("stdout") else ""))
    preview = dict(out)
    if out["result"]["type"] == "table":
        preview["result"] = {**out["result"], "rows": out["result"]["rows"][:30],
                             "row_count": len(out["result"]["rows"])}
    return ToolOutput(json.dumps(preview, default=str), json.dumps(out, default=str))
