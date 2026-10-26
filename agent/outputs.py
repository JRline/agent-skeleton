"""Tool outputs as tables: run_python's agreed JSON, and 'rows=N; columns=[...]; saved as path' + CSV from queries
and files. Shared by the UI (sources.py) and the conversation export, so data is never shown as raw text."""
from __future__ import annotations

import json
import re

import pandas as pd


def python_table(output: str) -> pd.DataFrame | None:
    try:
        result = json.loads(output).get("result") or {}
    except (ValueError, AttributeError):
        return None
    return pd.DataFrame(result["rows"], columns=result["columns"]) if result.get("type") == "table" else None


_TABLE_HEAD = re.compile(r"^(rows=(\d+)|shape=\((\d+), \d+\)); columns=\[.*?\](?:; saved as (\S+))?(.*)$")


def parse_output(output: str) -> tuple[str, pd.DataFrame | None, str]:
    """A stored or previewed tool output as (caption, table or None, remaining text). Query and file results are
    'rows=N; columns=[...]; saved as path' plus CSV: shown as a grid with a plain caption, never as raw text."""
    table = python_table(output)
    if table is not None:
        out = json.loads(output)
        bits = [f"{len(table):,} rows"] + ([f"saved as `{out['saved_as']}`"] if out.get("saved_as") else [])
        return " · ".join(bits), table, str(out.get("notes") or "")
    head, _, body = output.partition("\n")
    m = _TABLE_HEAD.match(head)
    if not m:
        return "", None, output
    from io import StringIO
    csv = "\n".join(ln for ln in body.splitlines() if not ln.startswith("... first "))
    try:
        df = pd.read_csv(StringIO(csv)) if csv.strip() else pd.DataFrame()
    except Exception:
        return "", None, output
    rows = int(m.group(2) or m.group(3))
    bits = [f"{rows:,} rows" + (f" (first {len(df):,} shown)" if len(df) < rows else "")]
    if m.group(4):
        bits.append(f"saved as `{m.group(4)}`")
    note = m.group(5).strip(" ()")
    return " · ".join(bits), df, note
