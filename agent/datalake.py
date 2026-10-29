"""Read-only SQL access to the datasets of the enabled connectors, for the SQL tools (docs/design.md §6).

Connectors and datasets are extensions (extensions/connectors/<name>/: connector.py, datasets/*.md; loaded by
agent/connectors). Each query runs on the one connector that holds its datasets, signed in as the user when the
connector needs it. Data sources are remote and read-only: the agent never loads, uploads or writes anything.

Two independent safety layers: `check_select` (one SELECT/WITH statement on documented datasets only, from the
query parsed by sqlglot) and a read-only connection (SQLite: an authorizer that denies anything but reading; servers: a read-only account and
connection). Rows are capped with fetchmany so a query can never flood the prompt.
"""
from __future__ import annotations

import sqlite3
import time

import pandas as pd
import sqlglot
from sqlglot import exp

from . import config, connectors, credentials


class LakeError(Exception):
    pass


# Statements and clauses a read-only query may never contain, found in the parsed query (not by keyword search, so
# a column named "set" or "copy" is fine and a keyword inside a string or comment never counts).
_WRITES = (exp.DML, exp.DDL, exp.Into, exp.Command, exp.Pragma, exp.Set, exp.Copy, exp.Grant, exp.Use,
           exp.Transaction, exp.Commit, exp.Rollback, exp.Attach, exp.Detach, exp.LoadData, exp.Execute)
_FILTERS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between)


def dialect_of(connector) -> str:
    """The connector's SQL dialect for sqlglot (DIALECT in connector.py, e.g. trino, tsql, oracle); local data is
    SQLite; "" is sqlglot's generic SQL."""
    return connector.setting("dialect", "sqlite" if connector.kind in ("sqlite", "mock") else "")


def _parse(sql: str) -> list[exp.Expression]:
    """The statements in `sql`, read in generic SQL, else in the dialect of an enabled connector."""
    dialects = [""] + [d for d in dict.fromkeys(dialect_of(c) for c in connectors.enabled().values()) if d]
    for dialect in dialects:
        try:
            return [e for e in sqlglot.parse(sql, read=dialect or None) if e is not None]
        except sqlglot.errors.SqlglotError:
            continue
    raise LakeError("The query could not be read as SQL. Write one plain SELECT statement.")


def _tables(query: exp.Expression) -> list[str]:
    """The tables a query reads (not its own CTEs), as written: schema.table when qualified."""
    ctes = {c.alias_or_name.lower() for c in query.find_all(exp.CTE)}
    names = (".".join(p for p in (t.catalog, t.db, t.name) if p) for t in query.find_all(exp.Table))
    return [n for n in names if n and n.lower() not in ctes]


def check_select(sql: str, datasets: list[str]) -> str:
    """Return the cleaned statement, or raise LakeError if it is not one read-only query on allowed datasets."""
    statements = _parse(sql)
    if len(statements) != 1:
        raise LakeError("Send exactly one SQL statement.")
    query = statements[0]
    if not isinstance(query, exp.Query):
        raise LakeError("Only SELECT queries (optionally starting with WITH) are allowed.")
    bad = next(query.find_all(*_WRITES), None)
    if bad is not None:
        raise LakeError(f"Not allowed in a read-only query: {bad.key.upper()}.")
    allowed = {v.lower() for v in datasets}
    for name in _tables(query):
        if name.lower() not in allowed:
            raise LakeError(f"Unknown or not allowed dataset: {name}. Allowed: {', '.join(datasets)}.")
    return sql.strip().rstrip(";").strip()


def limit_sql(sql: str, n: int, dialect: str = "") -> str:
    """Ask the server for at most n rows, so a dataset with billions of rows is never scanned in full for a preview.
    Written in the connector's dialect (LIMIT, TOP or FETCH FIRST); a query with its own limit is wrapped, so the
    cap never raises it. A connection kind may still supply its own `limit_sql`."""
    query = _parse(sql)[0]
    if query.args.get("limit") or query.args.get("fetch"):
        query = exp.select("*").from_(query.subquery("limited_q"))
    return query.limit(n).sql(dialect=dialect or None)


def check_required_filters(sql: str, required: dict[str, list[list[str]]]) -> None:
    """Large datasets marked in their dataset file must be filtered on their key column (e.g. a date), so a query
    cannot scan them in full: a comparison, IN or BETWEEN on the column in a WHERE or JOIN ... ON, or (where the
    dataset allows it) the column in GROUP BY. The read-only account and the server's own limits remain the real
    protection."""
    query = _parse(sql)[0]
    used = {n.lower() for n in _tables(query)}
    conditions = [c for node in query.find_all(exp.Where, exp.Join)
                  for c in ([node.this] if isinstance(node, exp.Where) else [node.args.get("on")]) if c is not None]
    filtered = {col.name.lower() for cond in conditions for f in cond.find_all(*_FILTERS)
                for col in f.find_all(exp.Column)}
    grouped = {col.name.lower() for g in query.find_all(exp.Group) for col in g.find_all(exp.Column)}
    for dataset, groups in required.items():
        if dataset.lower() not in used:
            continue
        for options in groups:                       # each group: any one of these columns must be filtered
            cols = [c for c in options if c != "GROUP BY"]
            if any(c.lower() in filtered for c in cols):
                continue
            if "GROUP BY" in options and any(c.lower() in grouped for c in cols):
                continue                             # split by it: the values are never added together
            how = " (or GROUP BY it)" if "GROUP BY" in options else ""
            raise LakeError(f"{dataset} needs a filter on {' or '.join(cols)}{how} "
                            f"(e.g. WHERE {cols[0]} = ... or IN (...)), and aggregate in SQL.")


_META: dict[tuple, tuple[float, object]] = {}      # (source, kind, dataset) -> (time, value): one round trip per TTL


def _cached(key: tuple, make):
    hit = _META.get(key)
    if hit and time.monotonic() - hit[0] < config.LAKE_META_TTL_S:
        return hit[1]
    value = make()
    _META[key] = (time.monotonic(), value)
    return value


class Lake:
    """One connector as this conversation's user sees it: its documented datasets, queries, columns, partitions."""

    def __init__(self, connector: "connectors.Connector", login=None):
        self.connector, self.login, self.module = connector, login, connector.module()
        self.name = f"{connector.name} ({connector.kind})"
        source = getattr(self.module, "source", lambda c: c.name)(connector)
        self.source = f"{source}#{login.user if login else ''}"     # each user's own catalogue view
        dialect = dialect_of(connector)
        self._limit = getattr(self.module, "limit_sql", lambda sql, n: limit_sql(sql, n, dialect))

    def _login(self):
        if self.connector.needs_login and self.login is None:
            raise LakeError(f"Not signed in to {self.connector.name}: enter your user ID and password under "
                            "'Data sign-in' (or run the CLI with --sign-in).")
        return self.login

    def connect(self):
        return self.module.connect(self.connector, self._login())

    def list_datasets(self) -> list[str]:
        """The documented datasets the server has: a dataset file is the allowlist."""
        found = _cached((self.source, "datasets"), lambda: self.module.list_datasets(self.connector, self._login()))
        return [d for d in self.connector.datasets if d in found]

    def query(self, sql: str, max_rows: int | None = None) -> tuple[pd.DataFrame, bool]:
        """Run one checked query, limited on the server to max_rows + 1. Returns (rows, truncated)."""
        sql = check_select(sql, self.list_datasets())
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
            raise LakeError(f"Query failed: {credentials.scrub(str(e))}") from None
        finally:
            conn.close()
        return pd.DataFrame([tuple(r) for r in rows[:limit]], columns=cols), len(rows) > limit

    def export(self, sql: str, path, max_rows: int | None = None, chunk: int = 10_000) -> tuple[list[str], int]:
        """Stream one checked query into a CSV file, chunk by chunk, so a large table never sits in memory (or in
        the prompt). Limited on the server to max_rows + 1; over the limit the partial file is removed and the
        query refused. Returns (columns, rows written)."""
        import csv
        sql = check_select(sql, self.list_datasets())
        check_required_filters(sql, required_filters())
        limit = max_rows or config.EXPORT_MAX_ROWS
        conn = self.connect()
        written = 0
        try:
            cur = conn.cursor()
            cur.execute(self._limit(sql, limit + 1))
            cols = [d[0] for d in cur.description]
            with open(path, "w", newline="") as f:
                out = csv.writer(f)
                out.writerow(cols)
                while rows := cur.fetchmany(chunk):
                    if written + len(rows) > limit:
                        raise LakeError(f"The export has more than {limit:,} rows (EXPORT_MAX_ROWS). Filter it "
                                        "further (dates, entity, slice) or split it into several exports.")
                    out.writerows(rows)
                    written += len(rows)
        except Exception as e:
            path.unlink(missing_ok=True)                       # never leave a partial file that looks complete
            if isinstance(e, LakeError):
                raise
            raise LakeError(f"Query failed: {credentials.scrub(str(e))}") from None
        finally:
            conn.close()
        return cols, written

    def describe(self, dataset: str) -> list[str]:
        """Columns of a dataset: catalogue metadata only (a zero-row query; cached), never data rows."""
        if dataset not in self.list_datasets():
            raise LakeError(f"Unknown dataset: {dataset}. Use one of: {', '.join(self.list_datasets())}.")
        return _cached((self.source, "columns", dataset), lambda: self._columns(dataset))

    def partitions(self, dataset: str) -> list[str]:
        """The partitions (e.g. fiscal years) of a partitioned dataset, from catalogue metadata. Empty when the
        dataset is not partitioned or the connection kind cannot list them cheaply."""
        ds = self.connector.datasets.get(dataset)
        fn = getattr(self.module, "partitions", None)
        if not ds or not ds.partition_column or fn is None or ds.partition_column not in self.describe(dataset):
            return []
        return _cached((self.source, "partitions", dataset),
                       lambda: sorted(fn(self.connector, dataset, ds.partition_column, self._login())))

    def _columns(self, dataset: str) -> list[str]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(self._limit(f"SELECT * FROM {dataset}", 0))
            cur.fetchmany(1)
            return [d[0] for d in cur.description]
        except Exception as e:
            raise LakeError(f"Query failed: {credentials.scrub(str(e))}") from None
        finally:
            conn.close()


# ---------------------------------------------------------------- datasets and their rules

def required_filters() -> dict[str, list[list[str]]]:
    """Datasets whose file sets `required_filters`: every comma-separated group must be filtered on any one of its
    columns (e.g. a date range by as_of_date or fiscal_year); a group marked "(or GROUP BY)" may instead be split
    by in GROUP BY (e.g. one row per scenario: never added together)."""
    return {name: ds.required for name, ds in connectors.datasets().items() if ds.required}


def default_filters() -> dict[str, str]:
    """Datasets whose file sets `default_filters` (a WHERE condition): the recommended values."""
    return {name: ds.defaults for name, ds in connectors.datasets().items() if ds.defaults}


def dataset_rules() -> str:
    """Each dataset's required filters and default values, for the agent's standing guidance: the agent knows them
    before writing any SQL, instead of learning them from a refused query."""
    required, defaults = required_filters(), default_filters()
    lines = []
    for name in sorted(set(required) | set(defaults)):
        groups = [" or ".join(c for c in g if c != "GROUP BY") + (" (or GROUP BY it)" if "GROUP BY" in g else "")
                  for g in required.get(name, [])]
        line = f"Dataset {name}: every query must filter on " + "; ".join(groups) + "." if groups else f"Dataset {name}:"
        if name in defaults:
            line += f" Start from WHERE {defaults[name]} and change a value only when the user asks."
        lines.append(line)
    return " ".join(lines)


def static_problem(sql: str, run_id: str | None = None) -> str | None:
    """Why a query would be refused (one SELECT on documented datasets of one connector, required filters), checked
    without the server: a request with a problem goes back to the agent before the user is asked."""
    try:
        sql = check_select(sql, list(connectors.datasets()))
        connector_for(sql)
        check_required_filters(sql, required_filters())
    except LakeError as e:
        return str(e)
    return None


# ---------------------------------------------------------------- connectors and sign-in

def needs_login() -> list[str]:
    """The enabled connectors that need each user's own sign-in."""
    return [name for name, c in connectors.enabled().items() if c.needs_login]


def connector_for(sql: str) -> str:
    """The one connector that holds the datasets a query reads. Datasets of two connectors cannot be joined in
    one query: query each, then combine the saved extracts with run_python."""
    owners = connectors.datasets()
    used = {owners[n].connector for n in _tables(_parse(sql)[0]) if n in owners}
    if len(used) > 1:
        raise LakeError(f"This query reads datasets of several connectors ({', '.join(sorted(used))}). Query each "
                        "one separately, then combine the saved extracts with run_python.")
    if not used:
        raise LakeError(f"The query reads no known dataset. Use one of: {', '.join(owners)}.")
    return used.pop()


def get_lake(run_id: str | None = None, connector: str | None = None) -> Lake:
    """A connector (by name; default: the first enabled) as this conversation's user sees it."""
    enabled = connectors.enabled()
    if not enabled:
        raise LakeError("No data connector is enabled (extensions/connectors/, CONNECTORS).")
    conn = enabled.get(connector) if connector else next(iter(enabled.values()))
    if conn is None:
        raise LakeError(f"Unknown connector: {connector}. Enabled: {', '.join(enabled)}.")
    login = credentials.login_for(run_id, conn.name) if conn.needs_login else None
    return Lake(conn, login)


def lake_for(sql: str, run_id: str | None = None) -> Lake:
    """The connector a query runs on (the statement is checked first, so a write is refused as such)."""
    check_select(sql, list(connectors.datasets()))
    return get_lake(run_id, connector_for(sql))


def dataset_notes() -> dict[str, str]:
    """Each dataset's one-line description (list_datasets)."""
    return {name: ds.description for name, ds in connectors.datasets().items()}
