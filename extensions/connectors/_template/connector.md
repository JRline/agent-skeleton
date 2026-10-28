---
name: my_lake
kind: odbc
description: One line: what this data source is (e.g. the production data lake, read-only).
needs_login: yes
connection_env: LAKE_ODBC
---
Notes every dataset of this connector shares (calendars, units, how dates work). Folders starting with "_" are not
loaded: copy this folder to extensions/connectors/<name>/, then:

- kind: odbc (pyodbc; the connection string without the sign-in is in the environment variable named by
  connection_env), mock (the local mock lake server), sqlite (a folder of CSVs: `data: data`), or custom (write
  connector.py next to this file; see agent/connectors/__init__.py for the functions it provides).
- needs_login: yes means each user signs in with their own user ID and password (sidebar, or CLI --sign-in);
  they are held in memory only and added to the connection as UID and PWD.
- Describe each dataset the agent may query in datasets/<name>.md (copy datasets/_template.md). Only described
  datasets can be queried.
