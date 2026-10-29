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

import re

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


def driver_help(connector, installed: list[str]) -> str:
    """Why ODBC found no data source (IM002), in words: the driver the connection string names is not installed
    under that exact name, or the string names neither a DRIVER nor a DSN. Uses only CONNECTION_STRING (no sign-in)."""
    text = connector.setting("connection_string")
    m = re.search(r"(?i)\bDRIVER\s*=\s*(\{[^}]*\}|[^;]*)", text)
    have = ", ".join(installed) or "none"
    if m:
        name = m.group(1).strip("{} ")
        return (f"Connector {connector.name}: ODBC driver '{name}' (DRIVER= in its connector.py) is not installed "
                f"under that name. Installed drivers: {have}. Copy the exact name into CONNECTION_STRING.")
    if not re.search(r"(?i)\bDSN\s*=", text):
        return (f"Connector {connector.name}: its CONNECTION_STRING names neither DRIVER={{...}} nor DSN=..., so ODBC "
                f"has nothing to connect with. Installed drivers: {have}.")
    return (f"Connector {connector.name}: the DSN in its CONNECTION_STRING is not defined on this machine (odbc.ini / "
            f"ODBC Data Sources). Installed drivers: {have}.")


def source(connector) -> str:
    return f"odbc:{connector.name}"


def connect(connector, login=None):
    """A read-only DB-API connection, signed in as the user. The query timeout matches config.SQL_TIMEOUT_S
    (15 minutes by default), so the driver does not cut long queries off first. A driver error that echoes the
    connection string has the password masked before anyone sees it (credentials.scrub)."""
    import pyodbc
    try:
        conn = pyodbc.connect(connection_string(connector, login), readonly=True, autocommit=True, timeout=30)
    except pyodbc.Error as e:
        if "IM002" in str(e):                 # "Data source name not found and no default driver specified"
            raise RuntimeError(driver_help(connector, pyodbc.drivers())) from None
        raise
    conn.timeout = config.SQL_TIMEOUT_S
    return conn


def list_datasets(connector, login=None) -> list[str]:
    """The documented datasets: an explicit list agreed with the data owner, never the whole catalogue."""
    return list(connector.datasets)
