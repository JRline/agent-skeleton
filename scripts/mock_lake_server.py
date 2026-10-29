"""Mock data lake server: a stand-in for the remote, read-only data lake cluster, for local testing only.

Run it in its own terminal (standard library + pandas only, nothing to install):
    python scripts/mock_lake_server.py [connector ...]
serves each connector that has local data (default all) at the URL in its connector.py, each on its own port, so
several sources are up at once. Then set KIND = "mock" in a connector.py to reach it over HTTP.

Why a separate process: the agent should meet the lake the way it meets the production lake, as a remote service it can only
query. Its data is outside the agent's sandbox, the connection can fail or be slow, and the server itself refuses
writes, like a read-only account, whatever the client sends.

API (JSON over HTTP, localhost only):
    GET  /datasets                   -> {"datasets": ["<dataset>", ...]}
    POST /query {"sql": "SELECT ..."} -> {"columns": [...], "rows": [[...]], "truncated": false}
    errors                           -> HTTP 400/401 with {"error": "..."}
Optional server settings (environment): MOCK_LAKE_USERS ("user:password,..."; if set, each
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

_READ_ONLY = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


class ReadOnlyLake:
    """All datasets in one in-memory SQLite database, locked so only reads are possible (the read-only account)."""

    def __init__(self, data_dir: Path):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.datasets = sorted(p.stem for p in data_dir.glob("*.csv"))
        for view in self.datasets:
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
            if self.path == "/datasets":
                self._send(200, {"datasets": lake.datasets})
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


def serve(port: int, data_dir: Path, token: str = "", latency_ms: int = 0,
          max_rows: int = 100_000, quiet=False, users: dict[str, str] | None = None) -> ThreadingHTTPServer:
    """Build one server (bound to localhost only). Call serve_forever() on it; tests use port 0."""
    lake = ReadOnlyLake(data_dir)
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(lake, token, latency_ms, max_rows, quiet, users))


def serve_connectors(names: list[str]) -> list[tuple[str, ThreadingHTTPServer]]:
    """One server per connector with local data (all, or those named), each on the port of its own URL in
    connector.py, so the agent reaches several sources at once. The server names no connector itself."""
    import sys
    from urllib.parse import urlsplit
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent import connectors
    conns = connectors.all_connectors()
    unknown = [n for n in names if n not in conns]
    if unknown:
        raise SystemExit(f"Unknown connector(s): {', '.join(unknown)}. Known: {', '.join(conns)}.")
    out = []
    for conn in [conns[n] for n in names] or conns.values():
        data, url = conn.path("data", "data"), conn.setting("url")
        if not url or not data.is_dir():
            continue
        try:
            out.append((conn.name, serve(urlsplit(url).port or 80, data, os.getenv("MOCK_LAKE_TOKEN", ""),
                                         int(os.getenv("MOCK_LAKE_LATENCY_MS", "0")),
                                         int(os.getenv("MOCK_LAKE_MAX_ROWS", "100000")),
                                         users=_users(os.getenv("MOCK_LAKE_USERS", "")))))
        except OSError as e:
            raise SystemExit(f"{conn.name}: cannot listen at {url} ({e.strerror}); is a mock server already running?")
    if not out:
        raise SystemExit("No connector with a URL and local data to serve: run python scripts/setup_local.py.")
    return out


if __name__ == "__main__":
    import sys
    servers = serve_connectors(sys.argv[1:])
    for name, server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    for name, server in servers:
        host, port = server.server_address[:2]
        print(f"Mock data lake (read-only, synthetic) for {name} on http://{host}:{port}.")
    print("Ctrl+C to stop.")
    try:
        servers[0][1].serve_forever()
    except KeyboardInterrupt:
        pass
