"""Runs model-written analysis code: data in, one result out, nothing else (docs/design.md §6).

Two layers, so one mistake is not enough to do harm:
1. `check_code`: a static AST check before the user is even asked to approve. Imports only from
   ALLOWED_MODULES (standard analysis packages that ship with the project, so nothing is ever installed), no
   dunders, no eval/exec/open, reads only through pd.read_csv/read_excel/read_json, pd.ExcelFile or read_table, no writing
   methods (`.to_csv`, `np.save` ...).
2. `run_code`: a separate isolated Python process (`python -I`) in an empty temporary folder, with a time
   limit and restricted builtins (no `open`, `exec`, `eval`; `import` limited to ALLOWED_MODULES). Every read
   (`tables`, `read_table`, `pd.read_csv/excel/json`, `pd.ExcelFile`) is checked: sandbox files only, never the lake's storage,
   never URLs.
The code sets `result` (and optionally `notes`). The harness, not the code, turns that into the pre-agreed
JSON format and stores table results as work tables, so the code never writes its own output.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

from . import config

MAX_CODE_CHARS = 8000
# Standard analysis packages, all installed with requirements.txt (pandas brings numpy) or the standard library.
ALLOWED_MODULES = {"pandas", "numpy", "math", "statistics", "datetime", "collections", "itertools",
                   "functools", "re", "json", "decimal", "fractions", "operator", "string"}
_BANNED_SUBMODULES = {"io", "lib", "f2py", "distutils", "testing", "ctypeslib", "_libs", "plotting", "api"}
_ALLOWED_READS = {"read_csv", "read_excel", "read_json"}       # guarded in the runner: sandbox files only
MAX_STDOUT_CHARS = 2000
_BANNED_NAMES = {"open", "exec", "eval", "compile", "getattr", "setattr", "delattr", "globals", "locals",
                 "vars", "input", "breakpoint", "exit", "quit", "help", "memoryview", "__import__",
                 "os", "sys", "subprocess", "socket", "shutil", "pathlib", "io", "builtins"}
_BANNED_ATTRS = {"save", "savez", "savez_compressed", "savetxt", "load", "loadtxt", "fromfile", "tofile",
                 "genfromtxt", "memmap", "DataSource", "HDFStore", "ExcelWriter", "write", "writelines",
                 "system", "popen", "remove", "unlink", "rmdir", "mkdir", "chmod", "savefig"}
# `rename` is allowed: it is how pandas renames columns, and the file renames (os, pathlib, open) are blocked anyway.
# .to_* methods that only convert in memory; every other .to_* (to_csv, to_excel, to_sql ...) writes somewhere
_SAFE_TO = {"to_dict", "to_list", "to_numpy", "to_frame", "to_string", "to_datetime", "to_numeric",
            "to_timedelta", "to_period", "to_timestamp", "to_records", "to_pydatetime", "to_series", "to_flat_index"}


def module_allowed(name: str) -> bool:
    parts = name.split(".")
    return parts[0] in ALLOWED_MODULES and not (set(parts[1:]) & _BANNED_SUBMODULES)


def normalize_code(code: str) -> str:
    """Small models sometimes send code with literal \\n instead of line breaks."""
    if "\n" not in code.strip() and "\\n" in code:
        return code.replace("\\n", "\n").replace("\\t", "\t")
    return code


def check_code(code: str) -> str | None:
    """Return why the code is not allowed, or None if it passes. Runs before approval."""
    code = normalize_code(code)
    if len(code) > MAX_CODE_CHARS:
        return f"Code is too long ({len(code)} chars, max {MAX_CODE_CHARS}). Keep the analysis focused."
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Syntax error on line {e.lineno}: {e.msg}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad = [a.name for a in node.names if not module_allowed(a.name)]
            if bad:
                return f"Not allowed: import of {', '.join(bad)}. Allowed: {', '.join(sorted(ALLOWED_MODULES))}."
        if isinstance(node, ast.ImportFrom):
            if node.level or not module_allowed(node.module or ""):
                return (f"Not allowed: import from {node.module}. "
                        f"Allowed: {', '.join(sorted(ALLOWED_MODULES))}.")
        if isinstance(node, ast.Name) and (node.id.startswith("__") or node.id in _BANNED_NAMES):
            return f"'{node.id}' is not allowed in analysis code."
        if isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("_"):
                return f"Private or dunder attribute '{attr}' is not allowed."
            if attr.startswith("read_") and attr not in _ALLOWED_READS:
                return f"'{attr}' is not allowed: read with pd.read_csv/read_excel/read_json, pd.ExcelFile or read_table(path)."
            if attr in _BANNED_ATTRS or (attr.startswith("to_") and attr not in _SAFE_TO):
                return f"'.{attr}' is not allowed: analysis code must not write files; set `result` instead."
    return None


def _sandbox_file(path: str) -> Path:
    from .tools import ToolError, sandbox_path
    full = sandbox_path(path)
    lake = config.connectors_dir().resolve()
    if full == lake or lake in full.parents:
        raise ToolError(f"{path} is data lake storage: query it with sql_query and pass the saved table.")
    if not full.is_file():
        raise ToolError(f"File not found: {path}")
    return full


def run_code(code: str, tables: dict[str, str], run_id: str, save_as: str) -> dict:
    """Check, then run in an isolated process. Always returns the pre-agreed dict, never raises."""
    code = normalize_code(code)
    problem = check_code(code)
    if problem:
        return {"ok": False, "error": problem}
    try:
        files = {name: _sandbox_file(path) for name, path in tables.items()}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    from . import tablecache                 # parsed once per file version; the subprocess loads pickles
    files = {name: str(tablecache.pickle_for(full) or full) for name, full in files.items()}
    job = {"code": code, "tables": files, "cache": _precache_reads(code), "root": str(config.SANDBOX_DIR.resolve()),
           "lake": str(config.connectors_dir().resolve()), "max_rows": config.PY_MAX_ROWS,
           "allowed_modules": sorted(ALLOWED_MODULES), "banned_submodules": sorted(_BANNED_SUBMODULES),
           "cpu_seconds": config.PY_TIMEOUT_S, "workbook_module": str(Path(__file__).with_name("workbook.py")),
           "internal_modules": sorted(_INTERNAL_MODULES)}
    runner = Path(__file__).with_name("_pyrunner.py")
    with tempfile.TemporaryDirectory() as empty:
        try:
            proc = subprocess.run([sys.executable, "-I", str(runner)], input=json.dumps(job), cwd=empty,
                                  capture_output=True, text=True, timeout=config.PY_TIMEOUT_S,
                                  env={"LANG": "C.UTF-8"})
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"Analysis code timed out after {config.PY_TIMEOUT_S}s."}
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "error": f"Analysis process failed: {(proc.stderr or proc.stdout)[-400:]}"}
    out["stdout"] = (out.get("stdout") or "")[:MAX_STDOUT_CHARS]
    if out.get("ok") and out["result"]["type"] == "table":
        out["saved_as"] = _save_table(out["result"], run_id, save_as)
    return out


# Imported by pandas/datetime internals during analysis (e.g. date parsing imports _strptime); allowed at run time
# only, never in the code itself (the static check still refuses `import time`).
_INTERNAL_MODULES = {"_strptime", "time", "calendar", "locale", "_locale"}

_READ_CALLS = {"read_csv", "read_excel", "read_json", "read_table"}


def _precache_reads(code: str) -> dict[str, str]:
    """Pickles for the file reads written literally in the code, e.g. pd.read_excel('inputs/x.xlsx', header=None),
    keyed as the runner looks them up: '<resolved path>|<reader arguments>'. Reads it cannot see are parsed."""
    from . import tablecache
    out = {}
    for node in ast.walk(ast.parse(code)):
        if not (isinstance(node, ast.Call) and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name not in _READ_CALLS:
            continue
        try:
            kwargs = {k.arg: ast.literal_eval(k.value) for k in node.keywords}
            full = _sandbox_file(node.args[0].value)
        except Exception:
            continue
        kwargs = {} if name == "read_table" else kwargs
        pkl = tablecache.pickle_for(full, kwargs)
        if pkl:
            out[f"{full}|{tablecache.kwargs_key(kwargs)}"] = str(pkl)
    return out


def _save_table(result: dict, run_id: str, save_as: str) -> str:
    from .tools import _save_work
    df = pd.DataFrame(result["rows"], columns=result["columns"])
    return _save_work(df, type("Ctx", (), {"run_id": run_id})(), save_as)
