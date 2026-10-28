"""Mock data lake server: a stand-in for the remote, read-only data lake cluster, for local testing only.

Run it in its own terminal (standard library + pandas only, nothing to install):
    python mock_lake/server.py                      # http://127.0.0.1:8765, data from mock_lake/data/*.csv
Then start the agent with DATALAKE=agent.backends.datalake_mock.

Why a separate process: the agent should meet the lake the way it meets the production lake, as a remote service it can only
query. Its data is outside the agent's sandbox, the connection can fail or be slow, and the server itself refuses
writes, like a read-only account, whatever the client sends.

API (JSON over HTTP, localhost only):
    GET  /views                      -> {"views": ["usage_limits", ...]}
    POST /query {"sql": "SELECT ..."} -> {"columns": [...], "rows": [[...]], "truncated": false}
    errors                           -> HTTP 400/401 with {"error": "..."}
Optional settings (environment): MOCK_LAKE_PORT (8765), MOCK_LAKE_DATA (mock_lake/data), MOCK_LAKE_USERS ("user:password,..."; if set, each
client signs in with its own user ID and password, HTTP basic auth), MOCK_LAKE_TOKEN (if set,
clients must send "Authorization: Bearer <token>"), MOCK_LAKE_LATENCY_MS (0; simulated network delay),
MOCK_LAKE_MAX_ROWS (100000).
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

DATA = Path(os.getenv("MOCK_LAKE_DATA", Path(__file__).resolve().parent / "data"))
_READ_ONLY = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


class ReadOnlyLake:
    """All views in one in-memory SQLite database, locked so only reads are possible (the read-only account)."""

    def __init__(self, data_dir: Path):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.views = sorted(p.stem for p in data_dir.glob("*.csv"))
        for view in self.views:
            pd.read_csv(data_dir / f"{view}.csv").to_sql(view, self.conn, index=False)
        self.conn.set_authorizer(lambda action, *a: sqlite3.SQLITE_OK if action in _READ_ONLY else sqlite3.SQLITE_DENY)
        self.lock = threading.Lock()

    def query(self, sql: str, max_rows: int) -> dict:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(sql)                     # one statement only: sqlite3 refuses several
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
        return {"columns": columns, "rows": [list(r) for r in rows[:max_rows]], "truncated": len(rows) > max_rows}


def _users(spec: str) -> dict[str, str]:
    """MOCK_LAKE_USERS="analyst:secret,other:pw" -> {user: password} (synthetic test accounts only)."""
    return dict(item.split(":", 1) for item in spec.split(",") if ":" in item)


def make_handler(lake: ReadOnlyLake, token: str = "", latency_ms: int = 0, max_rows: int = 100_000, quiet=False,
                 users: dict[str, str] | None = None):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict):
            data = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self) -> bool:
            if latency_ms:
                time.sleep(latency_ms / 1000)
            auth = self.headers.get("Authorization", "")
            if users:
                try:
                    user, _, password = base64.b64decode(auth.removeprefix("Basic ")).decode().partition(":")
                except (ValueError, UnicodeDecodeError):
                    user, password = "", ""
                if not auth.startswith("Basic ") or users.get(user) != password:
                    self._send(401, {"error": "Sign-in failed: wrong user ID or password."})
                    return False
            elif token and auth != f"Bearer {token}":
                self._send(401, {"error": "Not authorised: send the read-only account's token."})
                return False
            return True

        def do_GET(self):
            if not self._allowed():
                return
            if self.path == "/views":
                self._send(200, {"views": lake.views})
            else:
                self._send(404, {"error": f"Unknown path {self.path}"})

        def do_POST(self):
            if not self._allowed():
                return
            if self.path != "/query":
                return self._send(404, {"error": f"Unknown path {self.path}"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                self._send(200, lake.query(str(body.get("sql", "")), max_rows))
            except (sqlite3.DatabaseError, sqlite3.Warning, ValueError) as e:   # incl. "not authorized" for writes
                self._send(400, {"error": f"{type(e).__name__}: {e}"})

        def log_message(self, fmt, *args):
            if not quiet:
                super().log_message(fmt, *args)

    return Handler


def serve(port: int = 8765, data_dir: Path = DATA, token: str = "", latency_ms: int = 0,
          max_rows: int = 100_000, quiet=False, users: dict[str, str] | None = None) -> ThreadingHTTPServer:
    """Build the server (bound to localhost only). Call serve_forever() on it; tests use port 0."""
    lake = ReadOnlyLake(data_dir)
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(lake, token, latency_ms, max_rows, quiet, users))


if __name__ == "__main__":
    server = serve(int(os.getenv("MOCK_LAKE_PORT", "8765")), DATA, os.getenv("MOCK_LAKE_TOKEN", ""),
                   int(os.getenv("MOCK_LAKE_LATENCY_MS", "0")), int(os.getenv("MOCK_LAKE_MAX_ROWS", "100000")),
                   users=_users(os.getenv("MOCK_LAKE_USERS", "")))
    host, port = server.server_address[:2]
    print(f"Mock data lake (read-only, synthetic) on http://{host}:{port} serving {DATA}. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
