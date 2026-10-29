"""Connection kind `sqlite`: a folder of CSV files (one per dataset) as an in-memory, read-only SQLite database.

For local runs and tests (the synthetic sample lake). Setting: DATA, the folder of CSVs, relative to the
connector's folder. Each connection is a fresh copy, locked by an authorizer that denies anything but reading.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

_READ_ONLY = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
_TEMPLATE: dict = {}       # folder -> the CSVs loaded once per version, copied (not re-parsed) for each connection


def _files(connector):
    return sorted(connector.path("data", "data").glob("*.csv"))


def source(connector) -> str:
    return f"sqlite:{connector.path('data', 'data')}"


def list_datasets(connector, login=None) -> list[str]:
    return [p.stem for p in _files(connector)]


def connect(connector, login=None) -> sqlite3.Connection:
    files = _files(connector)
    version = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    key = source(connector)
    if _TEMPLATE.get(key, {}).get("version") != version:
        template = sqlite3.connect(":memory:", check_same_thread=False)
        for p in files:
            pd.read_csv(p).to_sql(p.stem, template, index=False)
        _TEMPLATE[key] = {"version": version, "conn": template}
    conn = sqlite3.connect(":memory:")
    _TEMPLATE[key]["conn"].backup(conn)
    conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_OK if action in _READ_ONLY else sqlite3.SQLITE_DENY)
    return conn


def partitions(connector, dataset: str, column: str, login=None) -> list[str]:
    """Locally the 'catalogue' is the data itself; a real server lists partitions from its metadata."""
    conn = connect(connector)
    try:
        return [r[0] for r in conn.execute(f"SELECT DISTINCT {column} FROM {dataset}")]
    finally:
        conn.close()
