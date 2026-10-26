"""Toolkit: Read, filter and save files in the sandbox (inputs/, outputs/)."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .. import config
from ..tools import (_FLOAT, ToolContext, ToolError, ToolOutput, tool, _load_table, quote_paths,
                     sandbox_path)

TOOLKIT = {
    "name": "files",
    "description": "Read input files; save text or tables for the user (saving needs approval).",
    "basic": True,     # enabled when a session starts
    "guidance": ("Read a file only when the goal needs its content. Use the exact paths from INPUT FILES."),
}


def _user_file(path: str):
    """Sandbox path for file tools. The lake's storage is off limits: locally it is CSV files, but in
    production it is a remote lake, so reading it as files would only work on this Mac and bypass the SQL guard."""
    full = sandbox_path(path)
    lake = config.datalake_dir().resolve()
    if full.suffix.lower() == ".csv" and (full == lake or lake in full.parents):
        raise ToolError(f"{path} is data lake storage. Query it with the datalake toolkit "
                        "(sql_query; the user enables it with /datalake).")
    return full


class ReadFileArgs(BaseModel):
    path: str = Field(description="Path inside the sandbox, exactly as listed in INPUT FILES")


def _reads_user_data(args: dict, ctx: ToolContext) -> bool:
    """Reading the user's files is IO the user confirms; reading back this conversation's own results
    (work tables, delivered outputs) is not."""
    path = str(args.get("path", "")).strip().lstrip("/").removeprefix("sandbox/")
    return not path.startswith(("work/", "outputs/"))


@tool("read_file", "Read a TXT/MD/JSON/CSV/XLSX file from the sandbox. CSV returns shape, columns and the first "
      "50 rows; an Excel workbook returns its layout: each sheet's table blocks (range, header rows, totals rows), "
      "the formulas behind them, a preview, and the read_range call to load each block in run_python; text "
      "returns the first 4k chars. Not for searching. Reading the user's files needs their approval; reading this "
      "conversation's work tables does not.",
      ReadFileArgs, risky=_reads_user_data, preview_chars=6000)
def read_file(args: ReadFileArgs, ctx: ToolContext):
    path = _user_file(args.path)
    if not path.is_file():
        # Small models often copy example paths; naming the real files lets them recover in one step.
        root = config.SANDBOX_DIR.resolve()
        files = sorted(str(p.resolve().relative_to(root)) for p in config.inputs_dir().glob("*")
                       if p.is_file() and not p.name.startswith("."))      # skip .DS_Store and the like
        raise ToolError(f"File not found: {args.path}. Available (exact names, spaces included): "
                        f"{quote_paths(files[:20]) or '(none)'}")
    if path.suffix.lower() in (".xlsx", ".xlsm"):        # report-style workbooks: describe the layout first
        from ..workbook import inspect
        layout = inspect(path).replace("{path}", args.path.removeprefix("sandbox/"))
        return ToolOutput(layout, layout)
    if path.suffix.lower() in (".csv", ".xls"):
        df = _load_table(path)
        head = f"shape={df.shape}; columns={list(df.columns)}\n"
        keep = config.READ_FILE_STORED_ROWS          # the file stays the source; no full copy per read
        tail = f"... first {keep} of {len(df)} rows; the full table is {args.path}\n" if len(df) > keep else ""
        preview = (head + df.head(50).to_csv(index=False))[:config.OBS_PREVIEW_CHARS]   # the larger limit is for layouts
        return ToolOutput(preview, head + df.head(keep).to_csv(index=False) + tail)
    text = path.read_text(errors="replace")
    return ToolOutput(text[:config.OBS_PREVIEW_CHARS], text)


class WriteFileArgs(BaseModel):
    name: str = Field(description="File name only, e.g. summary.md")
    content: str


@tool("write_file", "Save text to sandbox/outputs/<run_id>/<name>. Needs user approval.",
      WriteFileArgs, risky=True)
def write_file(args: WriteFileArgs, ctx: ToolContext):
    if not re.fullmatch(r"[\w.\- ]{1,100}", args.name) or args.name.startswith("."):
        raise ToolError("Use a plain file name with no folders.")
    out_dir = config.outputs_dir(ctx.run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / args.name
    path.write_text(args.content)
    return f"Saved {len(args.content)} chars to {path.relative_to(config.SANDBOX_DIR)}"


class SaveTableArgs(BaseModel):
    path: str = Field(description="Table to deliver, e.g. work/<run_id>/ratios.csv")
    name: str = Field(description="Output file name, e.g. cet1_trend.csv")


@tool("save_table", "Deliver a table to the user as sandbox/outputs/<run_id>/<name>. Needs user approval.",
      SaveTableArgs, risky=True)
def save_table(args: SaveTableArgs, ctx: ToolContext):
    if not re.fullmatch(r"[\w.\- ]{1,100}\.(csv|xlsx)", args.name):
        raise ToolError("Use a plain file name ending in .csv or .xlsx.")
    df = _load_table(sandbox_path(args.path))
    out_dir = config.outputs_dir(ctx.run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / args.name
    df.to_excel(path, index=False) if args.name.endswith(".xlsx") else df.to_csv(path, index=False,
                                                                                 float_format=_FLOAT)
    return f"Saved {len(df)} rows to {path.relative_to(config.SANDBOX_DIR)}"
