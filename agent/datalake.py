"""Read-only data lake access for the SQL tools (agents.md §6).

Production reaches the lake with SQL over ODBC: copy agent/backends/datalake_odbc_template.py, fill it in
inside the closed environment and set DATALAKE=<module name>. Development uses either an in-memory SQLite
copy of the synthetic CSVs in mock_lake/data/ (DATALAKE=sqlite) or the mock lake server, a separate process
reached over HTTP (DATALAKE=agent.backends.datalake_mock), so the same tools and prompts run unchanged.
The lake is remote and read-only: the agent never loads, uploads or writes anything to it.

Two independent safety layers: `check_select` (one SELECT/WITH statement on allowed views only) and a
read-only connection (SQLite: an authorizer that denies anything but reading; production: readonly=True and
a read-only account). Rows are capped with fetchmany so a query can never flood the prompt.
"""
from __future__ import annotations

import importlib
import re
import sqlite3
import time

import pandas as pd

from . import config


class LakeError(Exception):
    pass


_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|exec|"
                        r"execute|call|attach|detach|pragma|vacuum|copy|into|load|set)\b", re.I)


def _strip(sql: str) -> str:
    """Remove comments and string literals so keywords inside them don't count."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def check_select(sql: str, views: list[str]) -> str:
    """Return the cleaned statement, or raise LakeError if it is not one read-only query on allowed views."""
    body = _strip(sql).strip().rstrip(";").strip()
    if ";" in body:
        raise LakeError("Send exactly one SQL statement.")
    if not re.match(r"(select|with)\b", body, re.I):
        raise LakeError("Only SELECT queries (optionally starting with WITH) are allowed.")
    bad = _FORBIDDEN.search(body)
    if bad:
        raise LakeError(f"Keyword not allowed in a read-only query: {bad.group(0).upper()}.")
    ctes = {m.lower() for m in re.findall(r"(?:\bwith|,)\s*([A-Za-z_]\w*)\s+as\s*\(", body, re.I)}
    allowed = {v.lower() for v in views}
    for name in re.findall(r"\b(?:from|join)\s+([A-Za-z_][\w.]*)", body, re.I):
        if name.lower() not in allowed | ctes:
            raise LakeError(f"Unknown or not allowed view: {name}. Allowed: {', '.join(views)}.")
    return sql.strip().rstrip(";").strip()


def limit_sql(sql: str, n: int) -> str:
    """Ask the server for at most n rows, so a view with billions of rows is never scanned in full for a preview.
    Appends LIMIT (keeps ORDER BY); a query with its own LIMIT is wrapped. A lake module may supply its own
    `limit_sql` for its dialect (e.g. FETCH FIRST n ROWS ONLY)."""
    if re.search(r"\blimit\s+\d+\s*$", _strip(sql), re.I):
        return f"SELECT * FROM ({sql}) AS limited_q LIMIT {n}"
    return f"{sql} LIMIT {n}"


def check_required_filters(sql: str, required: dict[str, list[list[str]]]) -> None:
    """Large views marked in the data dictionary must be filtered on their key column (e.g. a date), so a query
    cannot scan them in full. A keyword check like check_select, not a parser: the read-only account and the
    server's own limits remain the real protection."""
    body = _strip(sql)
    used = {m.lower() for m in re.findall(r"\b(?:from|join)\s+([A-Za-z_][\w.]*)", body, re.I)}
    for view, groups in required.items():
        if view.lower() not in used:
            continue
        grouped = re.search(r"\bgroup\s+by\b(.*?)(?:\border\s+by\b|\bhaving\b|\blimit\b|\)|$)", body, re.I | re.S)
        for options in groups:                       # each group: any one of these columns must be filtered
            cols = [c for c in options if c != "GROUP BY"]
            if any(re.search(rf"\b(?:where|and|on)\b[^;]*?\b{re.escape(c)}\b\s*(?:=|<|>|\bin\b|\bbetween\b)",
                             body, re.I | re.S) for c in cols):
                continue
            if "GROUP BY" in options and grouped and any(re.search(rf"\b{re.escape(c)}\b", grouped.group(1), re.I)
                                                         for c in cols):
                continue                             # split by it: the values are never added together
            how = " (or GROUP BY it)" if "GROUP BY" in options else ""
            raise LakeError(f"{view} needs a filter on {' or '.join(cols)}{how} "
                            f"(e.g. WHERE {cols[0]} = ... or IN (...)), and aggregate in SQL.")


_META: dict[tuple, tuple[float, object]] = {}      # (lake, kind, view) -> (time, value): one round trip per TTL


def _cached(key: tuple, make):
    hit = _META.get(key)
    if hit and time.monotonic() - hit[0] < config.LAKE_META_TTL_S:
        return hit[1]
    value = make()
    _META[key] = (time.monotonic(), value)
    return value


class Lake:
    """One read-only SQL source. `connect()` returns a DB-API connection; `views()` the queryable views.
    Optional: `limit_sql(sql, n)` for the server's dialect."""

    def __init__(self, name: str, connect, views, limit=None, source: str = "", partitions=None):
        self.name, self.connect, self._views, self._limit = name, connect, views, limit or limit_sql
        self.source = source or name          # identifies the server/data for the metadata cache
        self._partitions = partitions         # partitions(view) -> list of partition values, from the catalogue

    def list_views(self) -> list[str]:
        views = _cached((self.source, "views"), self._views)
        return [v for v in views if v in config.DATALAKE_VIEWS] if config.DATALAKE_VIEWS else list(views)

    def query(self, sql: str, max_rows: int | None = None) -> tuple[pd.DataFrame, bool]:
        """Run one checked query, limited on the server to max_rows + 1. Returns (rows, truncated)."""
        sql = check_select(sql, self.list_views())
        check_required_filters(sql, required_filters())
        limit = max_rows or config.MAX_SQL_ROWS
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(self._limit(sql, limit + 1))
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(limit + 1)
        except LakeError:
            raise
        except Exception as e:
            raise LakeError(f"Query failed: {e}") from e
        finally:
            conn.close()
        return pd.DataFrame([tuple(r) for r in rows[:limit]], columns=cols), len(rows) > limit

    def describe(self, view: str) -> list[str]:
        """Columns of a view: catalogue metadata only (a zero-row query; cached), never data rows."""
        if view not in self.list_views():
            raise LakeError(f"Unknown view: {view}. Use one of: {', '.join(self.list_views())}.")
        return _cached((self.source, "columns", view), lambda: self._columns(view))

    def partitions(self, view: str) -> list[str]:
        """The partitions (values of config.LAKE_PARTITION_COLUMN, e.g. fiscal years) a partitioned view has, from catalogue metadata.
        Empty when the view is not partitioned or the lake module cannot list them cheaply."""
        if self._partitions is None or config.LAKE_PARTITION_COLUMN not in self.describe(view):
            return []
        return _cached((self.source, "partitions", view), lambda: sorted(self._partitions(view)))

    def _columns(self, view: str) -> list[str]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(self._limit(f"SELECT * FROM {view}", 0))
            cur.fetchmany(1)
            return [d[0] for d in cur.description]
        except Exception as e:
            raise LakeError(f"Query failed: {e}") from e
        finally:
            conn.close()


# ---------------------------------------------------------------- local SQLite stand-in

_READ_ONLY = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


def _sqlite_views() -> list[str]:
    return sorted(p.stem for p in config.datalake_dir().glob("*.csv"))


_TEMPLATE: dict = {}       # the CSVs loaded once per version, copied (not re-parsed) for each connection


def _sqlite_connect() -> sqlite3.Connection:
    """Fresh in-memory copy of the CSVs, then locked: the authorizer denies anything but reading."""
    files = sorted(config.datalake_dir().glob("*.csv"))
    version = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if _TEMPLATE.get("version") != version:
        template = sqlite3.connect(":memory:", check_same_thread=False)
        for p in files:
            pd.read_csv(p).to_sql(p.stem, template, index=False)
        _TEMPLATE.update(version=version, conn=template)
    conn = sqlite3.connect(":memory:")
    _TEMPLATE["conn"].backup(conn)
    conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_OK if action in _READ_ONLY else sqlite3.SQLITE_DENY)
    return conn


def _sqlite_partitions(view: str) -> list[str]:
    """Locally the 'catalogue' is the data itself; in production the lake module lists partitions from metadata."""
    conn = _sqlite_connect()
    try:
        return [r[0] for r in conn.execute(f"SELECT DISTINCT {config.LAKE_PARTITION_COLUMN} FROM {view}")]
    finally:
        conn.close()


def required_filters() -> dict[str, list[list[str]]]:
    """Views the data dictionary marks with `Required filter: `a` or `b`, `c` (or GROUP BY)`: every comma-separated
    group must be filtered on any one of its columns (e.g. a date range by as_of_date or fiscal_year); a group marked
    "(or GROUP BY)" may instead be split by in GROUP BY (e.g. one row per scenario: never added together)."""
    out = {}
    for view, body in view_notes().items():
        m = re.search(r"(?im)^\s*-?\s*required filters?:\s*(.+)$", body)
        if m:
            groups = [(re.findall(r"`([^`]+)`", g) or [g.strip(" .")]) + (["GROUP BY"] if re.search(r"group by", g, re.I)
                                                                          else []) for g in m.group(1).split(",")]
            out[view] = [g for g in groups if g and g[0]]
    return out


def default_filters() -> dict[str, str]:
    """Views the data dictionary gives a `Default filters: <WHERE condition>` line: the recommended values."""
    out = {}
    for view, body in view_notes().items():
        m = re.search(r"(?im)^\s*-?\s*default filters?:\s*(.+?)\.?\s*$", body)
        if m:
            out[view] = m.group(1).strip()
    return out


def view_rules() -> str:
    """Each view's required filters and default values, for the agent's standing guidance: the agent knows them
    before writing any SQL, instead of learning them from a refused query."""
    required, defaults = required_filters(), default_filters()
    lines = []
    for view in sorted(set(required) | set(defaults)):
        groups = [" or ".join(c for c in g if c != "GROUP BY") + (" (or GROUP BY it)" if "GROUP BY" in g else "")
                  for g in required.get(view, [])]
        line = f"View {view}: every query must filter on " + "; ".join(groups) + "." if groups else f"View {view}:"
        if view in defaults:
            line += f" Start from WHERE {defaults[view]} and change a value only when the user asks."
        lines.append(line)
    return " ".join(lines)


def static_problem(sql: str) -> str | None:
    """Why a query would be refused (one SELECT on allowed views, required filters), checked without the server:
    a request with a problem goes back to the agent before the user is asked, never as a failed query."""
    try:
        sql = check_select(sql, get_lake().list_views())
        check_required_filters(sql, required_filters())
    except LakeError as e:
        return str(e)
    except Exception:
        return None                            # the catalogue is unreachable: the query itself will say so
    return None


def view_notes() -> dict[str, str]:
    """Meaning of each view (and general notes) from the data dictionary, keyed by its '## ' heading.
    It is a reference document: sandbox/references/datalake_dictionary.md, here and in production."""
    path = config.datalake_dictionary()
    if not path.exists():
        return {}
    intro, *sections = re.split(r"(?m)^## +", path.read_text())
    notes = {s.split("\n", 1)[0].strip(): s.split("\n", 1)[1].strip() if "\n" in s else "" for s in sections}
    about = "\n".join(ln for ln in intro.splitlines() if ln.strip() and not ln.startswith("# "))
    return ({"About the data": about} if about else {}) | notes


def get_lake() -> Lake:
    if config.DATALAKE == "sqlite":
        return Lake("sqlite (in-process synthetic stand-in)", _sqlite_connect, _sqlite_views,
                    source=f"sqlite:{config.datalake_dir()}", partitions=_sqlite_partitions)
    module = importlib.import_module(config.DATALAKE)
    return Lake(getattr(module, "NAME", config.DATALAKE), module.connect, module.list_views,
                getattr(module, "limit_sql", None), getattr(module, "source", lambda: "")(),
                getattr(module, "partitions", None))
