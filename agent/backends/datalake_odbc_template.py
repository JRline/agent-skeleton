"""Shape of the production data lake module: the production lake over ODBC (copy into the closed environment and fill in).

Set DATALAKE=<this module's name>. agent/datalake.py adds the SQL guard, row cap and allowlist
(DATALAKE_VIEWS) on top, so this module only has to connect and list views.

Each user signs in with their own lake user ID and password (NEEDS_LOGIN = True): the UI asks for them, keeps them
in memory only (agent/credentials.py) and passes them here as `login` (login.user, login.password). Never put
credentials in code, the repository, environment files or logs.

Needs pyodbc and the lake's ODBC driver in the closed environment (check they are approved before using this).
Fill in LAKE_ODBC (driver, server, port, database: no user or password) for your lake.
"""
import os

from agent import config

NAME = "production lake (odbc)"
NEEDS_LOGIN = True          # each user signs in with their own user ID and password
# Everything but the sign-in. Example values: replace with your lake's driver name, host, port and database.
LAKE_ODBC = os.getenv("LAKE_ODBC", "DRIVER={Lake ODBC Driver};SERVER=lake.example.internal;PORT=9996;"
                                   "DATABASE=analytics;SSL=1")


def _value(text: str) -> str:
    """An ODBC connection-string value: braced, so a password with ; or = cannot break the string."""
    return "{" + text.replace("}", "}}") + "}"


def connection_string(login) -> str:
    return f"{LAKE_ODBC};UID={_value(login.user)};PWD={_value(login.password)}"


def connect(login=None):
    """A read-only DB-API connection, signed in as the user. The query timeout matches config.SQL_TIMEOUT_S
    (15 minutes by default), so the driver does not cut long queries off first. A driver error that echoes the
    connection string has the password masked before anyone sees it (credentials.scrub)."""
    import pyodbc
    conn = pyodbc.connect(connection_string(login), readonly=True, autocommit=True, timeout=30)
    conn.timeout = config.SQL_TIMEOUT_S
    return conn


def limit_sql(sql: str, n: int) -> str:
    """Optional: how the lake's SQL dialect asks for at most n rows. Check which form your lake version accepts
    (e.g. `... LIMIT n`); without this function the agent appends LIMIT n."""
    return f"{sql} LIMIT {n}"


def partitions(view: str, login=None) -> list[str]:
    """Optional: the partitions (values of config.LAKE_PARTITION_COLUMN) of a partitioned view, from catalogue metadata, never a table scan.
    Without this function describe_dataset lists no partitions."""
    raise NotImplementedError("List partitions from the lake catalogue")


def list_views(login=None) -> list[str]:
    """Views the agent may query. Prefer an explicit list agreed with the data owner."""
    # Example only:
    # import os
    # return [v.strip() for v in os.environ["LAKE_VIEWS"].split(",") if v.strip()]
    raise NotImplementedError("List the allowed lake views")
