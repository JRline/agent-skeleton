"""Executed by agent/pysandbox.py in a separate `python -I` process. Not imported by the agent.

Reads one job (JSON) from stdin, runs the code with restricted builtins, and prints one JSON line:
{"ok": true, "result": {"type": "table", "columns": [...], "rows": [...]} | {"type": "value", "value": ...},
 "notes": "...", "stdout": "..."} or {"ok": false, "error": "...", "stdout": "..."}.
"""
import builtins
import contextlib
import io
import json
import math
import statistics
import sys
from pathlib import Path

try:
    import resource
except ImportError:          # not on Windows
    resource = None

import numpy as np
import pandas as pd

BLOCKED_BUILTINS = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "exit", "quit",
                    "help", "globals", "locals", "vars", "getattr", "setattr", "delattr", "memoryview"}


# Unguarded readers for the runner's own use; the code only gets the guarded versions (see main).
# ExcelFile (list sheets, parse each) also takes a path, so it is guarded the same way.
READERS = {"read_csv": pd.read_csv, "read_excel": pd.read_excel, "read_json": pd.read_json,
           "ExcelFile": pd.ExcelFile}


def load(path: Path):
    suffix = path.suffix.lower()
    if suffix == ".parquet":                  # parsed once by the agent (agent/tablecache.py)
        return pd.read_parquet(path)
    if suffix == ".csv":
        return READERS["read_csv"](path)
    if suffix in (".xlsx", ".xlsm", ".xls"):
        return READERS["read_excel"](path)
    if suffix == ".json":
        return READERS["read_json"](path)
    raise ValueError(f"Not a table file: {path.name}")


def _workbook(job):
    """agent/workbook.py, loaded by path: the isolated process has no package path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("workbook", job["workbook_module"])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def to_plain(value):
    if isinstance(value, (np.generic,)):
        return value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    return value


def serialise(result, max_rows):
    if isinstance(result, pd.Series):
        result = result.reset_index()
    if isinstance(result, list) and result and all(isinstance(r, dict) for r in result):
        result = pd.DataFrame(result)
    if isinstance(result, dict) and result and all(isinstance(v, (list, tuple)) for v in result.values()):
        result = pd.DataFrame(result)
    if isinstance(result, pd.DataFrame):
        df = result.reset_index(drop=not any(result.index.names)) if not isinstance(result.index, pd.RangeIndex) else result
        df = df.head(max_rows)
        rows = [[to_plain(v) for v in row] for row in df.astype(object).itertuples(index=False, name=None)]
        return {"type": "table", "columns": [str(c) for c in df.columns], "rows": rows}
    if isinstance(result, dict):
        return {"type": "value", "value": {str(k): to_plain(v) for k, v in result.items()}}
    if isinstance(result, (list, tuple)):
        return {"type": "value", "value": [to_plain(v) for v in result]}
    return {"type": "value", "value": to_plain(result)}


def main():
    job = json.loads(sys.stdin.read())
    if resource is not None:
        with contextlib.suppress(Exception):
            resource.setrlimit(resource.RLIMIT_CPU, (job["cpu_seconds"], job["cpu_seconds"]))
    root, lake = Path(job["root"]), Path(job["lake"])
    allowed, banned_parts = set(job["allowed_modules"]), set(job["banned_submodules"])

    def sandbox_file(path):
        """Only plain sandbox paths: no URLs, no file objects, nothing outside, never the lake's storage."""
        if not isinstance(path, str) or "://" in path:
            raise PermissionError(f"{path!r}: read sandbox files by path, e.g. 'inputs/data.csv'")
        full = (root / path.lstrip("/").removeprefix("sandbox/")).resolve()
        if root not in full.parents or full == lake or lake in full.parents:
            raise PermissionError(f"{path} is outside the allowed sandbox files")
        if not full.is_file():
            raise FileNotFoundError(f"No sandbox file {path!r}. Use the path exactly as listed, relative to the "
                                    "sandbox: 'inputs/<file name>' or 'work/<run>/<table>.csv'.")
        return full

    cache = job.get("cache", {})                            # Parquet copies of reads the agent saw in the code

    def cached(full, kwargs):
        copy = cache.get(f"{full}|{json.dumps(kwargs, sort_keys=True, default=str)}")
        return pd.read_parquet(copy) if copy else None

    def read_table(path):
        """Read-only loader for CSV/XLSX/JSON inside the sandbox (never the lake's storage)."""
        full = sandbox_file(path)
        df = cached(full, {})
        return df if df is not None else load(full)

    def guard_reader(name, reader):
        def guarded(path, *args, **kwargs):
            full = sandbox_file(path)
            if name != "ExcelFile" and not args:
                df = cached(full, kwargs)
                if df is not None:
                    return df
            return reader(full, *args, **kwargs)
        return guarded

    for name, reader in READERS.items():                    # the only readers the checker lets through
        setattr(pd, name, guard_reader(name, reader))

    real_import = builtins.__import__

    internal = set(job.get("internal_modules", []))

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        parts = name.split(".")
        if name in internal:                    # library internals (date parsing); the static check refuses them
            return real_import(name, globals, locals, fromlist, level)
        if level or parts[0] not in allowed or set(parts[1:]) & banned_parts:
            raise ImportError(f"import of {name} is not allowed in analysis code")
        return real_import(name, globals, locals, fromlist, level)

    safe_builtins = {k: v for k, v in vars(builtins).items() if k not in BLOCKED_BUILTINS}
    safe_builtins["__import__"] = guarded_import
    def read_range(path, sheet, cell_range, header_rows=1):
        """One table block of an Excel sheet (from read_file's layout) as a DataFrame."""
        return _workbook(job).read_range(sandbox_file(path), sheet, cell_range, header_rows)

    env = {"__builtins__": safe_builtins, "pd": pd, "np": np, "math": math, "statistics": statistics,
           "read_table": read_table, "read_range": read_range}
    stdout = io.StringIO()
    try:
        env["tables"] = {name: load(Path(p)) for name, p in job["tables"].items()}   # checked by the parent
        with contextlib.redirect_stdout(stdout):
            exec(compile(job["code"], "<analysis>", "exec"), env)  # noqa: S102 - checked and isolated
        notes = str(env.get("notes", ""))[:2000]
        if "result" in env:
            result = serialise(env["result"], job["max_rows"])
        elif stdout.getvalue().strip():            # common with small models: they print instead of setting result
            result = {"type": "value", "value": stdout.getvalue().strip()[-4000:]}
            notes = (notes + " " if notes else "") + "No `result` was set; the printed output is the result."
        else:
            raise ValueError("The code must set `result` (a table, value or dict).")
        out = {"ok": True, "result": result, "notes": notes}
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if isinstance(e, FileNotFoundError) and "Use the path" not in msg:   # an unguarded reader in the empty cwd
            msg += (" Read files only with pd.read_csv/read_excel/read_json, pd.ExcelFile or read_table, using "
                    "sandbox paths like 'inputs/<file name>'.")
        out = {"ok": False, "error": msg[:600]}
    out["stdout"] = stdout.getvalue()[-4000:]
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
