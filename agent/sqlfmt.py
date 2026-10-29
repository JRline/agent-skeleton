"""Readable SQL for people: one clause per line, one selected column per line, subqueries indented (sqlglot's
pretty printer, in the same dialect it was read in).

Display only (approval cards, Sources, the replay script); the SQL that runs is never changed. A query sqlglot
cannot read is shown as written.
"""
from __future__ import annotations

import sqlglot


def format_sql(sql: str, dialect: str = "") -> str:
    text = sql.strip().rstrip(";")
    try:
        return sqlglot.transpile(text, read=dialect or None, write=dialect or None, pretty=True)[0]
    except (sqlglot.errors.SqlglotError, IndexError):
        return text
