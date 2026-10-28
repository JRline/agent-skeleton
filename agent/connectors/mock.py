"""Connection kind `mock`: the mock lake server (scripts/mock_lake_server.py), a separate read-only process over HTTP.

Closer to a real server than `sqlite`: network, per-user sign-in, latency. Settings: `url` (default MOCK_LAKE_URL or
http://127.0.0.1:8765). With `needs_login: yes` each user's ID and password go as HTTP basic auth (the server is
started with MOCK_LAKE_USERS); otherwise an optional MOCK_LAKE_TOKEN. Standard library only.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

from .. import config


def _url(connector=None) -> str:
    default = os.getenv("MOCK_LAKE_URL", "http://127.0.0.1:8765")
    return (connector.setting("url", default) if connector is not None else default).rstrip("/")


def source(connector) -> str:
    """Which server this is, for the agent's metadata cache."""
    return _url(connector)


def _call(connector, path: str, body: dict | None = None, login=None) -> dict:
    """One request. Errors become plain messages the agent can show: unreachable, unauthorised, bad SQL."""
    req = urllib.request.Request(_url(connector) + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    if login is not None:                    # the user's own sign-in (HTTP basic auth; use HTTPS for a real server)
        token = base64.b64encode(f"{login.user}:{login.password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    elif os.getenv("MOCK_LAKE_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['MOCK_LAKE_TOKEN']}")
    try:
        with urllib.request.urlopen(req, timeout=config.SQL_TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get("error", str(e))
        except ValueError:
            message = str(e)
        finally:
            e.close()
        raise RuntimeError(f"Data lake refused the request: {message}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ConnectionError(f"Data lake server not reachable at {_url(connector)} ({getattr(e, 'reason', e)}). "
                              "Start it with: python scripts/mock_lake_server.py") from None


class _Cursor:
    def __init__(self, connector, login=None):
        self.description, self._rows, self._connector, self._login = None, [], connector, login

    def execute(self, sql: str):
        out = _call(self._connector, "/query", {"sql": sql}, self._login)
        self.description = [(c, None, None, None, None, None, None) for c in out["columns"]]
        self._rows = [tuple(r) for r in out["rows"]]

    def fetchmany(self, size: int):
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows


class _Connection:
    def __init__(self, connector, login=None):
        self._connector, self._login = connector, login

    def cursor(self):
        return _Cursor(self._connector, self._login)

    def close(self):
        pass


def connect(connector, login=None) -> _Connection:
    return _Connection(connector, login)


def partitions(connector, dataset: str, column: str, login=None) -> list[str]:
    """Partition values (the mock server answers from its data; a real server from its metadata)."""
    return [r[0] for r in _call(connector, "/query", {"sql": f"SELECT DISTINCT {column} FROM {dataset}"}, login)["rows"]]


def list_datasets(connector, login=None) -> list[str]:
    return _call(connector, "/datasets", login=login)["datasets"]
