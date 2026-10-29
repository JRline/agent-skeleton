You are helping maintain a Python analysis agent that runs on the GPT CLI. You cannot see the project; everything
you need is below. A connector is one connection to a data source (a server or database), described by a folder
under extensions/connectors/. Write ONE new connector.

THE REQUEST (the server, its driver, how users sign in):
{request}

RULES:
- Reply with the complete file `extensions/connectors/<name>/connector.py` in one fenced block (constants as in
  the template, the connection string as a literal), then a short list of datasets to
  describe next (one file each, `datasets/<name>.md`).
- Follow the connector template and the "Connectors" section of docs/extensions.md below. Use a built-in kind
  (odbc, mock, sqlite) when it fits; use KIND = "custom" (the functions defined in connector.py) only when none does, with the functions
  listed there and the standard library plus the approved packages only.
- Never put a user ID, password or token in any file: users sign in themselves (NEEDS_LOGIN = True), and the
  connection string has no user ID or password.
- The connection must be read-only.

After my reply I will save the folder, set the settings and run the unit tests.
