"""Export a conversation as one Python script that replays its analysis (traceability for reviewers).

The script is assembled by the harness from what actually ran, never written by the model: every successful
`sql_query` exactly as it ran, every approved `run_python` code exactly as approved, in order, followed by a
check of each step against what the agent saw (MATCH / DIFF) and each answer as a template whose numbers are
filled from the replayed results. Failed or rejected attempts appear as comments, so the audit trail is complete.

The file is plain Python with `# %%` cells: `python analysis_<run>.py` runs it end to end, and VS Code, Spyder or
Jupyter open it as cells. It needs pandas plus a connection to the lake: the internal lake server over ODBC in
production, or the mock lake server when testing. It re-queries the live lake; it does not ship the data.
"""
from __future__ import annotations

import json
import numbers
import re
import textwrap
from datetime import datetime
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .memory import read_log, run_dir
from .pysandbox import normalize_code
from .sqlfmt import format_sql

# Helpers shared by the export (to fingerprint what the agent saw) and the script (to check the replay): one
# source, embedded verbatim in the script and executed here, so both sides compute the same digest.
RUNTIME = r'''
def plain(v):
    """numpy scalars and timestamps as plain Python values, as run_python's JSON output has them."""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v.item() if hasattr(v, "item") else v


def to_result(result):
    """The agreed result shapes of run_python: a DataFrame for tables, otherwise a plain value."""
    if isinstance(result, pd.Series):
        result = result.reset_index()
    if isinstance(result, list) and result and all(isinstance(r, dict) for r in result):
        result = pd.DataFrame(result)
    if isinstance(result, dict) and result and all(isinstance(v, (list, tuple)) for v in result.values()):
        result = pd.DataFrame(result)
    if isinstance(result, pd.DataFrame):
        if not isinstance(result.index, pd.RangeIndex):
            result = result.reset_index(drop=not any(result.index.names))
        result.columns = [str(c) for c in result.columns]
        return result
    if isinstance(result, dict):
        return {str(k): plain(v) for k, v in result.items()}
    if isinstance(result, (list, tuple)):
        return [plain(v) for v in result]
    return plain(result)


def fingerprint(value):
    """Shape, column totals and a digest of a result, independent of row order and float noise."""
    import hashlib
    if not isinstance(value, pd.DataFrame):
        return {"value": value if isinstance(value, (int, float, str, bool, type(None))) else str(value)}
    df = value.copy()
    df.columns = [str(c) for c in df.columns]
    sums = {c: round(float(pd.to_numeric(df[c], errors="coerce").sum()), 6) for c in df.columns
            if pd.to_numeric(df[c], errors="coerce").notna().all() and len(df)}
    cells = df.apply(lambda col: col.map(lambda v: "" if v is None or (not isinstance(v, str) and pd.isna(v))
                                         else f"{v:.6g}" if isinstance(v, float) else str(plain(v))))
    text = "\n".join(sorted(",".join(row) for row in cells.astype(str).itertuples(index=False, name=None)))
    digest = hashlib.sha256((",".join(df.columns) + "\n" + text).encode()).hexdigest()[:16]
    return {"rows": len(df), "columns": list(df.columns), "sums": sums, "digest": digest}


def compare(expected, actual):
    """'MATCH' or a short description of what differs."""
    got = fingerprint(actual)
    if got == expected:
        return "MATCH"
    if "value" in expected:
        return f"DIFF: value {expected['value']!r} -> {got.get('value', 'a table')!r}"
    if "rows" not in got:
        return f"DIFF: expected a table, got {got['value']!r}"
    notes = []
    if got["columns"] != expected["columns"]:
        notes.append(f"columns {expected['columns']} -> {got['columns']}")
    if got["rows"] != expected["rows"]:
        notes.append(f"rows {expected['rows']} -> {got['rows']}")
    notes += [f"sum({c}) {v:,.6g} -> {got['sums'][c]:,.6g}" for c, v in expected["sums"].items()
              if c in got["sums"] and abs(got["sums"][c] - v) > 1e-6 * max(1.0, abs(v))]
    return "DIFF: " + ("; ".join(notes) or "values differ")
'''
_runtime: dict = {"pd": pd}
exec(RUNTIME, _runtime)                    # noqa: S102 - our own constant code, shared with the script
fingerprint, to_result = _runtime["fingerprint"], _runtime["to_result"]

_NOT_DATA = re.compile(r"\[(?:ref|doc):[^\]]*\]|\b\d{4}-\d{2}-\d{2}\b|\b\d{4}Q[1-4]\b|(?m:^\s*\d+[.)]\s)|\b\d{8}\b")
_NUMBER = re.compile(r"\+?(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|\+?(?<![\w.])-?\d+(?:\.\d+)?")
_REF = re.compile(r"\[ref:\s*([^\]]+)\]")


# ---------------------------------------------------------------- what ran

def collect_steps(state: dict) -> list[dict]:
    """The conversation in order: instructions, replayable steps (reads of the user's files, queries, analysis
    code, each with the reviewer's plain explanation), attempts that failed, the user's answers, final answers."""
    items, pending, task_no = [], {}, 1
    first = state["tasks"][0]["goal"] if state.get("tasks") else state["goal"]
    items.append({"kind": "task", "n": 1, "goal": first})
    for ev in state["events"]:
        kind, p = ev["kind"], ev["payload"]
        if kind == "task":
            task_no += 1
            items.append({"kind": "task", "n": task_no, "goal": p["goal"]})
        elif kind == "approval":
            pending = {"tool": p["tool"], "input": p.get("input") or {}, "explain": p.get("explain") or ""}
        elif kind == "tool" or (kind == "user" and "approved" in p):
            tool = p.get("tool")
            inp = p.get("input") if kind == "tool" else pending.get("input", {})
            explain = pending.get("explain", "") if kind == "user" else ""
            obs = p.get("obs") or {}
            if tool == "read_file" and obs.get("ok") and _user_file(inp.get("path", "")):
                items.append({"kind": "read_file", "ref": obs["ref"], "input": inp, "explain": explain})
                continue
            if tool not in ("sql_query", "run_python"):
                continue
            if obs.get("ok"):
                items.append({"kind": tool, "ref": obs["ref"], "input": inp, "explain": explain})
            else:
                why = obs.get("error") or ("rejected by the user" if not p.get("approved", True) else "failed")
                items.append({"kind": "attempt", "tool": tool, "input": inp, "why": str(why)[:300]})
        elif kind == "user" and "answer" in p:
            items.append({"kind": "answer", "question": p.get("question", ""), "answer": p["answer"]})
        elif kind == "final":
            items.append({"kind": "final", "task": task_no, "text": p["text"]})
    return items


def _user_file(path: str) -> bool:
    path = path.strip().lstrip("/").removeprefix("sandbox/")
    return bool(path) and not path.startswith(("work/", "outputs/"))


def has_steps(state: dict) -> bool:
    """Whether the conversation read data or ran analysis, i.e. whether a replay script has anything to replay."""
    return any(it["kind"] in ("read_file", "sql_query", "run_python") for it in collect_steps(state))


def agent_result(state: dict, ref: str):
    """What the agent saw for a step, from its stored full output: a DataFrame or a value."""
    text = (run_dir(state) / "outputs" / f"{ref}.txt").read_text()
    if ref.startswith("sql_query"):
        head, _, body = text.partition("\n")
        return pd.read_csv(StringIO(body)) if body.strip() else pd.DataFrame(columns=_columns(head))
    out = json.loads(text)["result"]
    if out["type"] == "table":
        return pd.DataFrame(out["rows"], columns=out["columns"])
    return to_result(out["value"])


def _columns(head: str) -> list[str]:
    m = re.search(r"columns=(\[.*?\])", head)
    return json.loads(m.group(1).replace("'", '"')) if m else []


def _truncated(state: dict, ref: str) -> int | None:
    head = (run_dir(state) / "outputs" / f"{ref}.txt").read_text().partition("\n")[0]
    m = re.search(r"rows=(\d+);.*TRUNCATED", head)
    return int(m.group(1)) if m else None


def _saved_path(state: dict, item: dict) -> str | None:
    """Where the step stored its table, so later steps that read it by path find it in the replay too."""
    text = (run_dir(state) / "outputs" / f"{item['ref']}.txt").read_text()
    if item["kind"] == "sql_query":
        m = re.search(r"saved as (\S+?\.csv)", text.partition("\n")[0])
        return m.group(1) if m else None
    return json.loads(text).get("saved_as")


# ---------------------------------------------------------------- answer → template

def bind_numbers(text: str, results: dict) -> tuple[str, list[dict], list[str]]:
    """Replace each traceable number in an answer with {nK}, bound to the result cell it came from.
    Cells of refs cited in the same paragraph are tried first, then the rest of the answer's refs, then any step.
    Returns (template, bindings, numbers left as written)."""
    skip = [m.span() for m in _NOT_DATA.finditer(text)]
    cited_all = [r.strip() for m in _REF.finditer(text) for r in m.group(1).split(",")]
    out, bindings, literal, last = [], [], [], 0
    paragraphs = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"(?s).+?(?:\n\s*\n|$)", text)]
    for m in _NUMBER.finditer(text):
        token, (start, end) = m.group(0), m.span()
        if any(a <= start < b for a, b in skip):
            continue
        plus = token.startswith("+")
        number = token.lstrip("+")
        decimals = len(number.split(".")[1]) if "." in number else 0
        value = float(number.replace(",", ""))
        maybe_not_data = decimals == 0 and (abs(value) <= 12 or 1900 <= abs(value) <= 2100)   # a count or a year
        if maybe_not_data and ("," not in number or abs(value) <= 12):
            continue
        para = next((p for a, b, p in paragraphs if a <= start < b), text)
        near = [r.strip() for mm in _REF.finditer(para) for r in mm.group(1).split(",")]
        order = list(dict.fromkeys([r for r in near + cited_all + list(results) if r in results]))
        found = _find_cell(value, decimals, order, results)
        if not found:
            literal.append(token)
            continue
        found.update(decimals=decimals, comma="," in number or (decimals == 0 and abs(value) < 1000), plus=plus)
        name = f"n{len(bindings) + 1}"
        found["name"] = name
        bindings.append(found)
        out.append(text[last:start].replace("{", "{{").replace("}", "}}") + "{" + name + "}")
        last = end
    out.append(text[last:].replace("{", "{{").replace("}", "}}"))
    return "".join(out), bindings, literal


def _find_cell(value: float, decimals: int, refs: list[str], results: dict) -> dict | None:
    for use_abs in (False, True):
        for ref in refs:
            res = results[ref]
            for spot, cell in _cells(res):
                if not isinstance(cell, numbers.Real) or isinstance(cell, (bool, np.bool_)) or pd.isna(cell):
                    continue
                v = abs(cell) if use_abs else cell
                if abs(round(float(v), decimals) - value) < 1e-9:
                    return {"ref": ref, **spot, "abs": use_abs, "orig": float(cell)}
    return None


def _cells(res):
    """(location, value) for every cell; a table row is located by its text columns (stable under re-sorting)."""
    if isinstance(res, pd.DataFrame):
        keys = [c for c in res.columns if not pd.api.types.is_numeric_dtype(res[c])]
        unique = bool(keys) and not res[keys].astype(str).duplicated().any()
        for pos, (_, row) in enumerate(res.iterrows()):
            where = {"key": {k: str(row[k]) for k in keys}} if unique else {"pos": pos}
            for col in res.columns:
                yield {**where, "col": str(col)}, row[col]
    elif isinstance(res, dict):
        for k, v in res.items():
            yield {"field": str(k)}, v
    elif isinstance(res, list):
        for i, v in enumerate(res):
            yield {"pos": i}, v
    else:
        yield {}, res


# ---------------------------------------------------------------- the script

def build_script(state: dict) -> str:
    items = collect_steps(state)
    results, expected, saved, truncated = {}, {}, {}, {}
    for it in items:
        if it["kind"] in ("sql_query", "run_python"):
            results[it["ref"]] = agent_result(state, it["ref"])
            expected[it["ref"]] = fingerprint(results[it["ref"]])
            saved[it["ref"]] = _saved_path(state, it)
            if it["kind"] == "sql_query":
                truncated[it["ref"]] = _truncated(state, it["ref"])
    models = sorted({e.get("model") for e in read_log(state) if e.get("kind") == "model" and e.get("model")})
    from . import connectors
    kinds = {c.kind for c in connectors.enabled().values()}
    lake = "mock" if kinds <= {"sqlite", "mock"} else "odbc"
    title = state.get("title") or state["run_id"]
    cells = [_header(state, title, models, items), _setup(lake, state, items)]
    answers, names, step_no = [], {}, 0
    for it in items:
        if it["kind"] == "task":
            cells.append(f"# %% [markdown]\n# ## Instruction {it['n']}: {_one_line(it['goal'])}")
        elif it["kind"] == "answer":
            cells.append(f"# %% [markdown]\n# **The agent asked:** {_one_line(it['question'])}  \n"
                         f"# **The user answered:** {_one_line(it['answer'])}")
        elif it["kind"] == "attempt":
            body = it["input"].get("code") or it["input"].get("sql") or json.dumps(it["input"])
            cells.append(f"# %% Not replayed: {it['tool']} attempt that did not count ({_one_line(it['why'])})\n"
                         + textwrap.indent(normalize_code(str(body)), "# "))
        elif it["kind"] == "read_file":
            step_no += 1
            cells.append(_read_cell(step_no, it, _name(it["input"].get("path", "data"), names)))
        elif it["kind"] == "sql_query":
            step_no += 1
            cells.append(_sql_cell(step_no, it, saved[it["ref"]], truncated[it["ref"]],
                                   _name(saved[it["ref"]] or it["input"].get("save_as", "extract"), names)))
        elif it["kind"] == "run_python":
            step_no += 1
            cells.append(_python_cell(step_no, it, saved[it["ref"]],
                                      _name(saved[it["ref"]] or it["input"].get("save_as", "analysis"), names)))
        elif it["kind"] == "final":
            template, bindings, literal = bind_numbers(it["text"], results)
            answers.append(_summary_cell(it["task"], template, bindings, literal))
    if not step_no:
        cells.append("# %% Nothing to replay\n# This conversation read no data and ran no analysis code.")
    cells += [_SUPPORT] + answers + [_check_cell(expected)]
    return "\n\n\n".join(cells) + "\n"


def _one_line(text: str) -> str:
    return " ".join(str(text).split())[:300]


def _header(state, title, models, items) -> str:
    goals = [it["goal"] for it in items if it["kind"] == "task"]
    lines = [f'"""Replay of the analysis "{title}" (conversation {state["run_id"]}).', "",
             "Generated by the agent from what actually ran: every data query exactly as it ran and every approved",
             "analysis step exactly as approved, in order. Run it end to end (python <this file>) or cell by cell",
             "(VS Code, Spyder, Jupyter). It re-queries the live data lake, checks each step against what the agent",
             "saw (MATCH / DIFF), and rebuilds each answer with the numbers from this run.", "",
             f"Exported: {datetime.now():%Y-%m-%d %H:%M}    Model(s): {', '.join(models) or 'n/a'}", "Instructions:"]
    lines += [f"  {i}. {_one_line(g)}" for i, g in enumerate(goals, 1)]
    lines += ["", "Data lake access is read-only (queries are SELECTs only; use a read-only account).", '"""']
    return "\n".join(lines).replace("\\", "\\\\")


def _name(path: str, used: dict) -> str:
    """A readable variable name from a saved table or file name (e.g. rwa_by_division), unique in the script."""
    base = re.sub(r"\W+", "_", Path(str(path)).stem).strip("_").lower() or "data"
    base = base if base[0].isalpha() else f"t_{base}"
    if base in ("result", "notes", "tables", "R", "SQL") or base in __import__("keyword").kwlist:
        base += "_table"
    used[base] = used.get(base, 0) + 1
    return base if used[base] == 1 else f"{base}_{used[base]}"


_READ_RANGE = '''

def read_range(path, sheet, cell_range, header_rows=1):
    """One table block of an Excel sheet as a DataFrame (header rows joined into column names)."""
    from openpyxl import load_workbook
    from openpyxl.utils import range_boundaries
    c1, r1, c2, r2 = range_boundaries(cell_range)
    rows = [list(r) for r in load_workbook(str(path).removeprefix("sandbox/"), read_only=True, data_only=True)[sheet]
            .iter_rows(min_row=r1, max_row=r2, min_col=c1, max_col=c2, values_only=True)]
    head, body = rows[:header_rows], [r for r in rows[header_rows:] if any(v is not None for v in r)]
    names = [" ".join(dict.fromkeys(str(h[j]) for h in head if h[j] is not None)) or f"col{j + 1}"
             for j in range(c2 - c1 + 1)]
    return pd.DataFrame(body, columns=names)'''


def _setup(lake: str, state: dict, items: list | None = None) -> str:
    inputs = str(config.inputs_dir().resolve())
    uses_range = any("read_range(" in str(it.get("input", {}).get("code", "")) for it in items or [])
    return f'''# %% Setup: data lake connection and working folder
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

# Where the data comes from: "odbc" = the server over ODBC (connection string without the sign-in in LAKE_ODBC;
# your user ID in ANALYSIS_USER, the password is asked), "mock" = the local mock lake server
# (python scripts/mock_lake_server.py), for testing only.
LAKE = os.getenv("ANALYSIS_LAKE", "{lake}")
INPUTS = Path(os.getenv("ANALYSIS_INPUTS", {inputs!r}))   # the user's input files (attachments)


def run_sql(sql: str) -> pd.DataFrame:
    """One read-only SELECT on a data source."""
    if LAKE == "odbc":
        import pyodbc                                          # the lake's ODBC driver must be installed
        from getpass import getpass
        conn = pyodbc.connect(f"{{os.environ['LAKE_ODBC']}};UID={{os.environ['ANALYSIS_USER']}};PWD={{getpass()}}",
                              readonly=True, autocommit=True)
        try:
            cur = conn.cursor()
            cur.execute(sql)
            return pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()],
                                             columns=[d[0] for d in cur.description])
        finally:
            conn.close()
    url = os.getenv("MOCK_LAKE_URL", "http://127.0.0.1:8765").rstrip("/") + "/query"
    req = urllib.request.Request(url, data=json.dumps({{"sql": sql}}).encode(),
                                 headers={{"Content-Type": "application/json"}})
    if os.getenv("MOCK_LAKE_TOKEN"):
        req.add_header("Authorization", f"Bearer {{os.environ['MOCK_LAKE_TOKEN']}}")
    with urllib.request.urlopen(req, timeout=int(os.getenv("SQL_TIMEOUT_S", "900"))) as resp:
        out = json.loads(resp.read())
    return pd.DataFrame(out["rows"], columns=out["columns"])


# Analysis steps read files by the paths they used in the agent ("inputs/...", "work/<run>/..."), so they run
# in a scratch folder with the inputs linked in and each step's tables saved where later steps expect them.
WORK = Path(tempfile.mkdtemp(prefix="replay_{state["run_id"]}_"))
for name, target in (("inputs", INPUTS), ("sandbox", WORK)):
    try:
        os.symlink(target, WORK / name, target_is_directory=True)
    except OSError:                                            # no symlinks: copy the inputs instead
        if name == "inputs" and INPUTS.is_dir():
            shutil.copytree(INPUTS, WORK / "inputs")
os.chdir(WORK)


def read_table(path):
    suffix = Path(path).suffix.lower()
    path = str(path).removeprefix("sandbox/")
    if suffix in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path)
    if suffix == ".json":
        return pd.read_json(path)
    return pd.read_csv(path)


def keep(path, df):
    """Save a step's table where later steps read it."""
    if path and isinstance(df, pd.DataFrame):
        (WORK / path).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(WORK / path, index=False, float_format="%.10g")


R = {{}}          # result of every step, by the ref the answers cite ([ref: sql_query-3] -> R["sql_query-3"])''' \
        + (_READ_RANGE if uses_range else "")


def _comment(text: str) -> str:
    return "".join(f"# {line}\n" for line in textwrap.wrap(" ".join(str(text).split()), 110)) if text else ""


def _read_cell(n: int, it: dict, var: str) -> str:
    path = it["input"].get("path", "").removeprefix("sandbox/")
    excel = Path(path).suffix.lower() in (".xlsx", ".xlsm", ".xls")
    load = (f"pd.read_excel({path!r}, sheet_name=None, header=None)  # every sheet as read, layout and all"
            if excel else f"read_table({path!r})")
    return (f"# %% Step {n} · read your file {path}\n" + _comment(it.get("explain"))
            + f"{var} = {load}\n{var}")


def _sql_cell(n: int, it: dict, path: str | None, truncated: int | None, var: str = "extract") -> str:
    ref, sql = it["ref"], it["input"].get("sql", "")
    cap = f".head({truncated})  # the agent saw the first {truncated} rows" if truncated else ""
    return (f"# %% Step {n} · data extraction ({ref})\n" + _comment(it.get("explain"))
            + f"SQL = {_literal(format_sql(sql))}\n"
            f"{var} = run_sql(SQL){cap}\n"
            f"keep({path!r}, {var})            # later steps read it from this path\n"
            f'R["{ref}"] = {var}\n'
            f"{var}")


def _python_cell(n: int, it: dict, path: str | None, var: str = "analysis") -> str:
    ref, inp = it["ref"], it["input"]
    code = normalize_code(inp.get("code", ""))
    tables = inp.get("tables") or {}
    load = "{" + ", ".join(f"{k!r}: read_table({v!r})" for k, v in tables.items()) + "}"
    return (f"# %% Step {n} · analysis ({ref}): the approved code, unchanged\n" + _comment(it.get("explain"))
            + f"tables = {load}\n"
            f"result = notes = None\n"
            f"{code}\n"
            f"{var} = result\n"
            f"keep({path!r}, {var})\n"
            f'R["{ref}"] = {var}\n'
            f"{var}")


def _summary_cell(task: int, template: str, bindings: list[dict], literal: list[str]) -> str:
    var = f"ANSWER_{task}"
    lines = [f"# %% Answer to instruction {task}: the agent's wording, numbers recomputed from this run",
             f"{var} = {_literal(template)}",
             f"BINDINGS_{task} = [  # where each number comes from: step ref, row, column, value the agent saw\n"
             + "".join(f"    {b!r},\n" for b in bindings) + "]"]
    if literal:
        lines.append(f"# Numbers kept as written (not traced to a step: dates, counts or the user's own): "
                     f"{', '.join(dict.fromkeys(literal))}")
    lines.append(f"print(fill({var}, BINDINGS_{task}))")
    return "\n".join(lines)


_FILL = '''
def cell(b):
    """The replayed value at the place the agent's number came from (None if it no longer exists)."""
    res = R.get(b["ref"])
    try:
        if isinstance(res, pd.DataFrame):
            if "key" in b:
                rows = res[np.logical_and.reduce([res[k].astype(str) == v for k, v in b["key"].items()])]
                value = rows[b["col"]].iloc[0]
            else:
                value = res[b["col"]].iloc[b["pos"]]
        elif "field" in b:
            value = res[b["field"]]
        elif "pos" in b:
            value = res[b["pos"]]
        else:
            value = res
        return float(value)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def fmt(value, b):
    value = abs(value) if b["abs"] else value
    text = f"{value:,.{b['decimals']}f}" if b["comma"] else f"{value:.{b['decimals']}f}"
    return ("+" + text) if b["plus"] and value >= 0 else text


def fill(template, bindings):
    """The answer with each bound number taken from this run; a changed number is marked with its old value."""
    values = {}
    for b in bindings:
        new = cell(b)
        old = fmt(b["orig"], b)
        if new is None:
            values[b["name"]] = f"{old} [not found in this run]"
        elif fmt(new, b) != old:
            values[b["name"]] = f"{fmt(new, b)} [was {old}]"
        else:
            values[b["name"]] = old
    return template.format(**values)'''


def _check_cell(expected: dict) -> str:
    return (f"# %% Check: each step against what the agent saw\n"
            f"EXPECTED = {{  # shape, column totals and a digest of each step's result when the agent ran it\n"
            + "".join(f"    {k!r}: {v!r},\n" for k, v in expected.items()) + "}\n"
            f"diffs = 0\n"
            f"for ref, exp in EXPECTED.items():\n"
            f"    verdict = compare(exp, R[ref]) if ref in R else 'NOT RUN'\n"
            f"    diffs += verdict != 'MATCH'\n"
            f"    print(f'{{ref:<20}} {{verdict}}')\n"
            f"print('All steps match what the agent saw.' if not diffs else\n"
            f"      f'{{diffs}} step(s) differ: the data has changed since the analysis, or a step is not reproducible.')\n"
            f"if __name__ == '__main__' and 'ipykernel' not in sys.modules:   # exit code for scripts, not notebooks\n"
            f"    sys.exit(1 if diffs else 0)")


def _literal(text: str) -> str:
    """A readable triple-quoted literal when safe, else repr."""
    if '"""' not in text and not text.endswith("\\") and "\\" not in text:
        return '"""' + text + '"""'
    return repr(text)


def export(state: dict) -> Path:
    """Write the script to sandbox/outputs/<run_id>/analysis_<run_id>.py and return its path."""
    out = config.outputs_dir(state["run_id"]) / f"analysis_{state['run_id']}.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_script(state), encoding="utf-8")
    return out


_SUPPORT = ("# %% Support code: rebuilding the answers and checking each step (no need to read)\n"
            + RUNTIME.strip() + "\n\n" + _FILL.rstrip() + "\n\n\nR = {ref: to_result(value) for ref, value in R.items()}")
