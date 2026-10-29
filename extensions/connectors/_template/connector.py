"""One connector: copy this folder to extensions/connectors/<name>/ (folders starting with "_" are not loaded).

KIND is odbc (pyodbc, with CONNECTION_STRING below), mock (the local mock lake server, URL), sqlite (a folder of
CSVs, DATA = "data") or custom (define connect, list_datasets and, optionally, partitions and source in this file; see
agent/connectors/__init__.py). The connection is set here only, never in an environment variable: each connector
has its own, so the agent can use several sources at once.
Describe each dataset the agent may query in datasets/<name>.md (copy datasets/_template.md): only described datasets
can be queried.
"""

NAME = "my_lake"
KIND = "odbc"
DESCRIPTION = "One line: what this data source is (e.g. the production data lake, read-only)."
# True: each user signs in with their own user ID and password (sidebar, or CLI --sign-in); they are held in memory
# only and added to the connection as UID and PWD. Never put them in CONNECTION_STRING.
NEEDS_LOGIN = True
# Driver, server, port and database only: no user ID or password. Upper case, as written here: the agent reads
# only upper-case constants. A data source already defined on the machine can be named instead: DSN = "<name>".
CONNECTION_STRING = "DRIVER={Lake ODBC Driver};SERVER=lake.example.internal;PORT=9996;DATABASE=analytics;SSL=1"
DIALECT = "trino"                 # the server's SQL dialect as sqlglot names it (trino, tsql, oracle, postgres, ...)

ABOUT = """
Notes every dataset of this connector shares (calendars, units, how dates work).
"""
