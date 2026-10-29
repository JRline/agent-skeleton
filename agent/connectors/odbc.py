"""Connection kind `odbc`: a database or data lake server over ODBC (pyodbc), signed in as the user.

Settings in connector.py: CONNECTION_STRING, the connection string without the sign-in (driver, server, port,
database; or DSN = "<name>" for a data source defined on the machine), NEEDS_LOGIN = True, and DIALECT, the server's SQL dialect as sqlglot names it (e.g. trino, tsql, oracle,
postgres; default generic SQL): queries are read and row limits written in it. The user's ID and password come from
their sign-in (agent/credentials.py, memory only) and are added as UID and PWD; never put them in
the connection string, code, the repository, environment files or logs. The datasets the agent may query are the
dataset files in the connector's folder.

Needs pyodbc and the server's ODBC driver in the closed environment (check they are approved before using this).
"""
from __future__ import annotations

import re



def _value(text: str) -> str:
    """An ODBC connection-string value: braced, so a password with ; or = cannot break the string."""
    return "{" + text.replace("}", "}}") + "}"


def connection_string(connector, login) -> str:
    if missing_connection(connector):
        raise RuntimeError(missing_connection(connector))
    base = _base(connector)
    if login is None:                         # needs_login: no (the DSN signs in by itself)
        return base
    return f"{base};UID={_value(login.user)};PWD={_value(login.password)}"


_OLD_ENV = ("LAKE_ODBC", "LAKE_DSN", "DATALAKE")      # where earlier versions read the connection from


def _base(connector) -> str:
    text = connector.setting("connection_string").strip().rstrip(";")
    dsn = connector.setting("dsn").strip()
    return text or (f"DSN={dsn}" if dsn else "")


def missing_connection(connector) -> str:
    """Empty when the connector has a connection; else why not, in words: what connector.py does set, a
    near-miss spelling, and any old environment variable that is set but no longer read."""
    if _base(connector):
        return ""
    import os
    keys = sorted(k.upper() for k in connector.settings)
    near = [k for k in keys if any(w in k for w in ("CONN", "DSN", "ODBC", "STRING", "SERVER")) and k != "DSN"]
    code = getattr(connector, "code", None)
    lower = [k for k in vars(code) if k.lower() in ("connection_string", "dsn") and not k.isupper()] if code else []
    env = [k for k in os.environ if k in _OLD_ENV or (k.startswith("CONNECTOR_") and "CONN" in k)]
    msg = [f'Connector {connector.name}: KIND is "odbc" but its connector.py sets no CONNECTION_STRING (or DSN).',
           f"It sets: {', '.join(keys) or 'nothing'}."]
    if near or lower:
        msg.append(f"Rename {', '.join(near + lower)} to CONNECTION_STRING (upper case).")
    if env:
        msg.append(f"{', '.join(sorted(env))} is set in the environment but not read: connections are set only in "
                   "connector.py.")
    msg.append('Write it as a literal, e.g. CONNECTION_STRING = "DRIVER={<driver>};SERVER=<host>;PORT=<port>;'
               'DATABASE=<db>" (no UID or PWD).')
    return " ".join(msg)


def driver_help(connector, installed: list[str]) -> str:
    """Why ODBC found no data source (IM002), in words: the driver the connection string names is not installed
    under that exact name, or the string names neither a DRIVER nor a DSN. Uses only CONNECTION_STRING (no sign-in)."""
    text = _base(connector)
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
    """A DB-API connection, signed in as the user, opened with the connection string and nothing else: no
    readonly, autocommit or timeout attributes, which some drivers reject or misread (each is a connection
    attribute the driver must support). Reading only is enforced by datalake.check_select and the read-only account;
    driver options such as a query timeout go in CONNECTION_STRING in the driver's own keywords. A driver error that
    echoes the connection string has the password masked before anyone sees it (credentials.scrub)."""
    import pyodbc
    try:
        conn = pyodbc.connect(connection_string(connector, login))
    except pyodbc.Error as e:
        if "IM002" in str(e):                 # "Data source name not found and no default driver specified"
            raise RuntimeError(driver_help(connector, pyodbc.drivers())) from None
        raise
    return conn


def list_datasets(connector, login=None) -> list[str]:
    """The documented datasets: an explicit list agreed with the data owner, never the whole catalogue."""
    return list(connector.datasets)
