"""Client for the mock lake server (mock_lake/server.py): set DATALAKE=agent.backends.datalake_mock.

Shaped like the production lake module (datalake_odbc_template.py): `connect()` returns a minimal DB-API
connection and `list_views()` lists the views, so agent/datalake.py adds the same SQL guard, row cap and
allowlist on top. Standard library only. Settings: MOCK_LAKE_URL (http://127.0.0.1:8765), MOCK_LAKE_TOKEN.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .. import config

TIMEOUT_S = 30


def _url() -> str:
    return os.getenv("MOCK_LAKE_URL", "http://127.0.0.1:8765").rstrip("/")


NAME = f"mock lake server ({_url()})"


def source() -> str:
    """Which server this is, for the agent's metadata cache."""
    return _url()


def _call(path: str, body: dict | None = None) -> dict:
    """One request. Errors become plain messages the agent can show: unreachable, unauthorised, bad SQL."""
    req = urllib.request.Request(_url() + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    if os.getenv("MOCK_LAKE_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['MOCK_LAKE_TOKEN']}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
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
        raise ConnectionError(f"Data lake server not reachable at {_url()} ({getattr(e, 'reason', e)}). "
                              "Start it with: python mock_lake/server.py") from None


class _Cursor:
    def __init__(self):
        self.description, self._rows = None, []

    def execute(self, sql: str):
        out = _call("/query", {"sql": sql})
        self.description = [(c, None, None, None, None, None, None) for c in out["columns"]]
        self._rows = [tuple(r) for r in out["rows"]]

    def fetchmany(self, size: int):
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows


class _Connection:
    def cursor(self):
        return _Cursor()

    def close(self):
        pass


def connect() -> _Connection:
    return _Connection()


def partitions(view: str) -> list[str]:
    """Partition values of a partitioned view (the mock server answers from its data; a real lake from metadata)."""
    return [r[0] for r in _call("/query", {"sql": f"SELECT DISTINCT {config.LAKE_PARTITION_COLUMN} FROM {view}"})["rows"]]


def list_views() -> list[str]:
    return _call("/views")["views"]
