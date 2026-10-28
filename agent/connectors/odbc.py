"""Connection kind `odbc`: a database or data lake server over ODBC (pyodbc), signed in as the user.

Settings in connector.md: `connection_env`, the name of the environment variable that holds the connection string
without the sign-in (driver, server, port, database; default LAKE_ODBC), and `needs_login: yes`. The user's ID and
password come from their sign-in (agent/credentials.py, memory only) and are added as UID and PWD; never put them in
the connection string, code, the repository, environment files or logs. The datasets the agent may query are the
dataset files in the connector's folder.

Needs pyodbc and the server's ODBC driver in the closed environment (check they are approved before using this).
"""
from __future__ import annotations

import os

from .. import config

EXAMPLE = "DRIVER={Lake ODBC Driver};SERVER=lake.example.internal;PORT=9996;DATABASE=analytics;SSL=1"


def _value(text: str) -> str:
    """An ODBC connection-string value: braced, so a password with ; or = cannot break the string."""
    return "{" + text.replace("}", "}}") + "}"


def connection_string(connector, login) -> str:
    base = os.getenv(connector.setting("connection_env", "LAKE_ODBC"), EXAMPLE)
    if login is None:                         # needs_login: no (the DSN signs in by itself)
        return base
    return f"{base};UID={_value(login.user)};PWD={_value(login.password)}"


def source(connector) -> str:
    return f"odbc:{connector.name}"


def connect(connector, login=None):
    """A read-only DB-API connection, signed in as the user. The query timeout matches config.SQL_TIMEOUT_S
    (15 minutes by default), so the driver does not cut long queries off first. A driver error that echoes the
    connection string has the password masked before anyone sees it (credentials.scrub)."""
    import pyodbc
    conn = pyodbc.connect(connection_string(connector, login), readonly=True, autocommit=True, timeout=30)
    conn.timeout = config.SQL_TIMEOUT_S
    return conn


def list_datasets(connector, login=None) -> list[str]:
    """The documented datasets: an explicit list agreed with the data owner, never the whole catalogue."""
    return list(connector.datasets)


def limit_sql(sql: str, n: int) -> str:
    """How the server's SQL dialect asks for at most n rows. Check which form your server accepts (e.g. `... LIMIT n`
    or `FETCH FIRST n ROWS ONLY`)."""
    return f"{sql} LIMIT {n}"
