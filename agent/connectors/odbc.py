"""Connection kind `odbc`: a database or data lake server over ODBC (pyodbc), signed in as the user.

Settings in connector.py: CONNECTION_STRING, the connection string without the sign-in (driver, server, port,
database), NEEDS_LOGIN = True, and DIALECT, the server's SQL dialect as sqlglot names it (e.g. trino, tsql, oracle,
postgres; default generic SQL): queries are read and row limits written in it. The user's ID and password come from
their sign-in (agent/credentials.py, memory only) and are added as UID and PWD; never put them in
the connection string, code, the repository, environment files or logs. The datasets the agent may query are the
dataset files in the connector's folder.

Needs pyodbc and the server's ODBC driver in the closed environment (check they are approved before using this).
"""
from __future__ import annotations

from .. import config


def _value(text: str) -> str:
    """An ODBC connection-string value: braced, so a password with ; or = cannot break the string."""
    return "{" + text.replace("}", "}}") + "}"


def connection_string(connector, login) -> str:
    base = connector.setting("connection_string").rstrip(";")
    if not base:
        raise RuntimeError(f"Connector {connector.name} has no CONNECTION_STRING in its connector.py.")
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
