"""Export a conversation as one Python script that replays its analysis (traceability for reviewers).

The script is assembled by the harness from what actually ran, never written by the model: every successful
`sql_query` exactly as it ran, every approved `run_python` code exactly as approved, in order, followed by a
check of each step against what the agent saw (MATCH / DIFF) and each answer as a template whose numbers are
filled from the replayed results. Failed or rejected attempts appear as comments, so the audit trail is complete.

The file is plain Python with `# %%` cells: `python analysis_<run>.py` runs it end to end, and VS Code, Spyder or
Jupyter open it as cells. It needs pandas plus a connection to each data source the queries used (one per
connector, each query sent to its own): a server over ODBC in production, or the mock lake server when testing.
It re-queries the live sources; it does not ship the data. A step that stops with an error is recorded and the
script carries on; all checks sit in one Checks cell at the end, which the user may skip.
"""
from __future__ import annotations

import json
import numbers
import re
import textwrap
import tokenize
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
def note_numbers(text):
    """The numbers in a step's notes, in order: totals the code printed there (e.g. "USED 237790 245463")."""
    import re
    return [float(t.replace(",", "")) for t in re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", str(text or ""))]


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


def as_text(v):
    """A cell as the text that identifies its row; a missing value is "" however it was stored (NaN, None, NA)."""
    return "" if v is None or (not isinstance(v, str) and pd.isna(v)) else str(plain(v))


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
fingerprint, to_result, as_text = _runtime["fingerprint"], _runtime["to_result"], _runtime["as_text"]
note_numbers = _runtime["note_numbers"]

_NOT_DATA = re.compile(r"\[(?:ref|doc):[^\]]*\]|\b\d{4}-\d{2}-\d{2}\b|\b\d{4}Q[1-4]\b|(?m:^\s*\d+[.)]\s)|\b\d{8}(?:-\d{6}(?:-\w+)?)?\b")
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
            if tool not in ("sql_query", "export_query", "run_python"):
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
    return any(it["kind"] in ("read_file", "sql_query", "export_query", "run_python") for it in collect_steps(state))


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


def agent_notes(state: dict, ref: str) -> str:
    """The notes run_python returned with its result (the code's own totals and remarks), as the agent saw them."""
    if not ref.startswith("run_python"):
        return ""
    return str(json.loads((run_dir(state) / "outputs" / f"{ref}.txt").read_text()).get("notes") or "")


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
    if item["kind"] in ("sql_query", "export_query"):
        m = re.search(r"saved as (\S+?\.csv)", text.partition("\n")[0])
        return m.group(1) if m else None
    return json.loads(text).get("saved_as")


# ---------------------------------------------------------------- answer → template

def bind_numbers(text: str, results: dict, notes: dict | None = None) -> tuple[str, list[dict], list[str]]:
    """Replace each traceable number in an answer with {nK}, bound to the result cell it came from, or else to its
    place in the step's notes (a total the code printed there). Cells of refs cited in the same paragraph are tried
    first, then the rest of the answer's refs, then any step.
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
        found = _find_cell(value, decimals, order, results) or _find_note(value, decimals, order, notes or {})
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


def _find_note(value: float, decimals: int, refs: list[str], notes: dict) -> dict | None:
    for use_abs in (False, True):
        for ref in refs:
            for i, n in enumerate(note_numbers(notes.get(ref, ""))):
                v = abs(n) if use_abs else n
                if abs(round(v, decimals) - value) < 1e-9:
                    return {"ref": ref, "note": i, "abs": use_abs, "orig": n}
    return None


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
        unique = bool(keys) and not res[keys].map(as_text).duplicated().any()
        for pos, (_, row) in enumerate(res.iterrows()):
            where = {"key": {k: as_text(row[k]) for k in keys}} if unique else {"pos": pos}
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
    results, expected, saved, truncated, notes = {}, {}, {}, {}, {}
    for it in items:
        if it["kind"] == "export_query":            # the agent saw only a preview: the check uses the file itself
            saved[it["ref"]] = _saved_path(state, it)
            file = config.SANDBOX_DIR / (saved[it["ref"]] or "-")
            if file.is_file():
                results[it["ref"]] = pd.read_csv(file)
                expected[it["ref"]] = fingerprint(results[it["ref"]])
        if it["kind"] == "run_python":
            notes[it["ref"]] = agent_notes(state, it["ref"])
        if it["kind"] in ("sql_query", "run_python"):
            results[it["ref"]] = agent_result(state, it["ref"])
            expected[it["ref"]] = fingerprint(results[it["ref"]])
            saved[it["ref"]] = _saved_path(state, it)
            if it["kind"] == "sql_query":
                truncated[it["ref"]] = _truncated(state, it["ref"])
    models = sorted({e.get("model") for e in read_log(state) if e.get("kind") == "model" and e.get("model")})
    title = state.get("title") or state["run_id"]
    cells = [_header(state, title, models, items), _setup(_sources(items), state, items)]
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
        elif it["kind"] == "export_query":
            step_no += 1
            cells.append(_export_cell(step_no, it, saved[it["ref"]],
                                      _name(saved[it["ref"]] or it["input"].get("name", "export"), names)))
        elif it["kind"] == "run_python":
            step_no += 1
            cells.append(_python_cell(step_no, it, saved[it["ref"]],
                                      _name(saved[it["ref"]] or it["input"].get("save_as", "analysis"), names)))
        elif it["kind"] == "final":
            template, bindings, literal = bind_numbers(it["text"], results, notes)
            answers.append(_summary_cell(it["task"], template, bindings, literal))
    if not step_no:
        cells.append("# %% Nothing to replay\n# This conversation read no data and ran no analysis code.")
    cells += [_SUPPORT] + answers + [_check_cell(expected)]
    return "\n\n\n".join(cells) + "\n"


def _sources(items: list[dict]) -> dict[str, dict]:
    """The data source of each query (the connector holding its datasets, as when it ran; stored on the item) and the
    script's settings for each source used. A connector no longer configured falls back to the first enabled one."""
    from . import connectors
    from .datalake import LakeError, connector_for
    enabled = connectors.enabled()
    out = {}
    for it in items:
        if it["kind"] not in ("sql_query", "export_query"):
            continue
        try:
            name = connector_for(it["input"].get("sql", ""))
        except LakeError:
            name = next(iter(enabled), "lake")
        it["source"] = name
        conn = enabled.get(name)
        kind = conn.kind if conn else "odbc"
        out[name] = {"kind": "mock" if kind in ("mock", "sqlite") else "odbc",   # local CSVs are served by the mock server
                     "url": conn.setting("url") if conn and kind == "mock" else "",
                     "odbc_env": conn.setting("connection_env", "LAKE_ODBC") if conn else "LAKE_ODBC",
                     "login": bool(conn and conn.needs_login)}
    return out


def _indent(code: str) -> str:
    """Code one level in, for its `with step(...)` block; lines inside a multi-line string keep their text."""
    inside: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(StringIO(code).readline):
            if tok.type == tokenize.STRING and tok.start[0] != tok.end[0]:
                inside.update(range(tok.start[0] + 1, tok.end[0] + 1))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    return "\n".join("    " + line if line.strip() and i not in inside else line
                     for i, line in enumerate(code.split("\n"), 1))


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


def _setup(sources: dict, state: dict, items: list | None = None) -> str:
    inputs = str(config.inputs_dir().resolve())
    uses_range = any("read_range(" in str(it.get("input", {}).get("code", "")) for it in items or [])
    table = "".join(f"    {name!r}: {spec!r},\n" for name, spec in sources.items())
    return (_SETUP.replace("__SOURCES__", table).replace("__INPUTS__", repr(inputs))
            .replace("__RUN__", state["run_id"]) + (_READ_RANGE if uses_range else ""))


_SETUP = '''# %% Setup: data sources and working folder
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import traceback
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

# The data sources the queries ran on (one per connector); each query names its own: run_sql(SQL, "<source>").
#   kind "odbc": the server over ODBC; odbc_env names the environment variable holding the connection string
#                without the sign-in (driver, server, database).
#   kind "mock": the local mock lake server (python scripts/mock_lake_server.py) at url, for testing only;
#                an empty url means MOCK_LAKE_URL or http://127.0.0.1:8765.
#   login:       sign in as yourself: your user ID in ANALYSIS_USER; the password is asked once per source and
#                never stored.
# Change a setting here, or with an environment variable ANALYSIS_<SOURCE>_<SETTING> (e.g. ANALYSIS_SAMPLE_LAKE_URL);
# ANALYSIS_LAKE=mock sends every query to the mock server.
SOURCES = {
__SOURCES__}
INPUTS = Path(os.getenv("ANALYSIS_INPUTS", __INPUTS__)).resolve()   # the user's input files (attachments)


def setting(source, key):
    value = os.getenv(f"ANALYSIS_{source.upper()}_{key.upper()}")
    if value is None and key == "kind":
        value = os.getenv("ANALYSIS_LAKE")
    if value is None:
        return SOURCES[source][key]
    return value.lower() in ("yes", "true", "1") if key == "login" else value


def run_sql(sql: str, source: str) -> pd.DataFrame:
    """One read-only SELECT on one data source."""
    if setting(source, "kind") == "odbc":
        import pyodbc                                          # the server's ODBC driver must be installed
        dsn = os.environ[setting(source, "odbc_env")]
        if setting(source, "login"):                         # braced values: a ; or = in them cannot break the string
            user, pwd = (v.replace("}", "}}") for v in (_user(), _password(source)))
            dsn += f";UID={{{user}}};PWD={{{pwd}}}"
        conn = pyodbc.connect(dsn, readonly=True, autocommit=True)
        try:
            cur = conn.cursor()
            cur.execute(sql)
            return pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()],
                                             columns=[d[0] for d in cur.description])
        finally:
            conn.close()
    url = (setting(source, "url") or os.getenv("MOCK_LAKE_URL", "http://127.0.0.1:8765")).rstrip("/") + "/query"
    req = urllib.request.Request(url, data=json.dumps({"sql": sql}).encode(),
                                 headers={"Content-Type": "application/json"})
    if setting(source, "login") or os.getenv("ANALYSIS_USER"):
        from base64 import b64encode
        token = b64encode(f"{_user()}:{_password(source)}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    elif os.getenv("MOCK_LAKE_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['MOCK_LAKE_TOKEN']}")
    with urllib.request.urlopen(req, timeout=int(os.getenv("SQL_TIMEOUT_S", "900"))) as resp:
        out = json.loads(resp.read())
    if out.get("truncated"):
        raise RuntimeError(f"The server returned only its first {len(out['rows']):,} rows: the result is incomplete.")
    return pd.DataFrame(out["rows"], columns=out["columns"])


def _user():
    if not os.getenv("ANALYSIS_USER"):
        raise RuntimeError("Set ANALYSIS_USER to your user ID to sign in to the data source.")
    return os.environ["ANALYSIS_USER"]


def _password(source, cache={}):
    """Asked once per source and run of the script, never stored."""
    from getpass import getpass
    if source not in cache:
        cache[source] = getpass(f"Password for {_user()} on {source}: ")
    return cache[source]


# Analysis steps read files by the paths they used in the agent ("inputs/...", "work/<run>/..."), so they run
# in a scratch folder with the inputs linked in and each step's tables saved where later steps expect them.
WORK = Path(tempfile.mkdtemp(prefix="replay___RUN___"))
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


R = {}          # result of every step, by the ref the answers cite ([ref: sql_query-3] -> R["sql_query-3"])
NOTES = {}      # the notes each analysis step returned (totals it printed there)
FAILED = {}     # steps that stopped with an error, by ref: reported in the Checks cell at the end


class step:
    """Runs one step. The steps hold no checks: if one stops with an error (e.g. the approved code meets data it
    was not written for, such as a new limit breach), the error is printed and recorded, and the script carries on
    with the next step. Steps that need its table then fail too. The Checks cell at the end reports it."""
    def __init__(self, ref):
        self.ref = ref

    def __enter__(self):
        return self

    def __exit__(self, kind, error, trace):
        if not isinstance(error, Exception):
            return False
        FAILED[self.ref] = f"{kind.__name__}: {error}"
        print(f"--- {self.ref} stopped with an error; continuing with the next step:", file=sys.stderr)
        traceback.print_exception(kind, error, trace, file=sys.stderr)
        return True'''


def _comment(text: str) -> str:
    return "".join(f"# {line}\n" for line in textwrap.wrap(" ".join(str(text).split()), 110)) if text else ""


def _read_cell(n: int, it: dict, var: str) -> str:
    path = it["input"].get("path", "").removeprefix("sandbox/")
    excel = Path(path).suffix.lower() in (".xlsx", ".xlsm", ".xls")
    load = (f"pd.read_excel({path!r}, sheet_name=None, header=None)  # every sheet as read, layout and all"
            if excel else f"read_table({path!r})")
    return (f"# %% Step {n} · read your file {path}\n" + _comment(it.get("explain"))
            + f'{var} = None\nwith step("{it["ref"]}"):\n    {var} = {load}\n{var}')


def _sql_cell(n: int, it: dict, path: str | None, truncated: int | None, var: str = "extract") -> str:
    ref, sql, source = it["ref"], it["input"].get("sql", ""), it.get("source", "lake")
    cap = f".head({truncated})  # the agent saw the first {truncated} rows" if truncated else ""
    return (f"# %% Step {n} · data extraction from {source} ({ref})\n" + _comment(it.get("explain"))
            + f"SQL = {_literal(format_sql(sql))}\n"
            f"{var} = None\n"
            f'with step("{ref}"):\n'
            f"    {var} = run_sql(SQL, {source!r}){cap}\n"
            f"    keep({path!r}, {var})            # later steps read it from this path\n"
            f'    R["{ref}"] = {var}\n'
            f"{var}")


def _export_cell(n: int, it: dict, path: str | None, var: str = "export") -> str:
    ref, sql, source = it["ref"], it["input"].get("sql", ""), it.get("source", "lake")
    return (f"# %% Step {n} · data export from {source} to a file for the user ({ref})\n"
            + _comment(it.get("explain"))
            + f"SQL = {_literal(format_sql(sql))}\n"
            f"{var} = None\n"
            f'with step("{ref}"):\n'
            f"    {var} = run_sql(SQL, {source!r})\n"
            f"    keep({path!r}, {var})            # the exported file, where the agent wrote it\n"
            f'    R["{ref}"] = {var}\n'
            f"{var}")


def _python_cell(n: int, it: dict, path: str | None, var: str = "analysis") -> str:
    ref, inp = it["ref"], it["input"]
    code = normalize_code(inp.get("code", ""))
    tables = inp.get("tables") or {}
    load = "{" + ", ".join(f"{k!r}: read_table({v!r})" for k, v in tables.items()) + "}"
    return (f"# %% Step {n} · analysis ({ref}): the approved code, unchanged\n" + _comment(it.get("explain"))
            + f"{var} = result = notes = None\n"
            f'with step("{ref}"):\n'
            f"    tables = {load}\n"
            f"{_indent(code)}\n"
            f"    {var} = result\n"
            f"    keep({path!r}, {var})\n"
            f'    R["{ref}"] = {var}\n'
            f'    NOTES["{ref}"] = notes\n'
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
        if "note" in b:
            return note_numbers(NOTES.get(b["ref"]))[b["note"]]
        if isinstance(res, pd.DataFrame):
            if "key" in b:
                rows = res[np.logical_and.reduce([res[k].map(as_text) == v for k, v in b["key"].items()])]
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
        if b["ref"] in FAILED:
            values[b["name"]] = f"{old} [step {b['ref']} failed]"
        elif new is None:
            values[b["name"]] = f"{old} [not found in this run]"
        elif fmt(new, b) != old:
            values[b["name"]] = f"{fmt(new, b)} [was {old}]"
        else:
            values[b["name"]] = old
    return template.format(**values)'''


def _check_cell(expected: dict) -> str:
    return ("# %% Checks: each step against what the agent saw\n"
            "# Every check in this script is here; the steps above only run and record. This cell changes nothing,\n"
            "# so you can skip it and use the results as they are. MATCH: the step gives what the agent saw.\n"
            "# DIFF: the data has changed since the analysis, or the step is not reproducible. FAILED: the step\n"
            "# stopped with an error (its traceback is printed where it ran), e.g. the approved code met data it\n"
            "# was not written for; later steps that need its table fail too.\n"
            "EXPECTED = {  # shape, column totals and a digest of each step's result when the agent ran it\n"
            + "".join(f"    {k!r}: {v!r},\n" for k, v in expected.items()) + "}\n"
            "VERDICTS = {ref: f'FAILED: {FAILED[ref]}' if ref in FAILED else compare(exp, R[ref]) if ref in R\n"
            "            else 'NOT RUN' for ref, exp in EXPECTED.items()}\n"
            "VERDICTS.update({ref: f'FAILED: {why}' for ref, why in FAILED.items() if ref not in VERDICTS})\n"
            "for ref, verdict in VERDICTS.items():\n"
            "    print(f'{ref:<20} {verdict}')\n"
            "diffs = sum(v != 'MATCH' for v in VERDICTS.values())\n"
            "print('All steps match what the agent saw.' if not diffs else\n"
            "      f'{diffs} step(s) differ or failed: the data has changed since the analysis, or a step is not '\n"
            "      'reproducible.')\n"
            "if __name__ == '__main__' and 'ipykernel' not in sys.modules:   # exit code for scripts, not notebooks\n"
            "    sys.exit(1 if diffs else 0)")


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
