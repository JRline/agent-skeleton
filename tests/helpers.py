"""Shared test setup: point runs/ and sandbox/ at a temporary folder."""
from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from agent import config, connectors
from scripts.sample_inputs import write_samples

LAKE = config.connectors_dir() / "sample_lake" / "data"     # the sample connector's synthetic data

connectors.build_samples()                          # data is never committed: build it once from the seed


class TempDirs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._saved = (config.RUNS_DIR, config.SANDBOX_DIR, config.SETTINGS_DIR)
        connectors.OVERRIDES.clear()       # the sample data is read-only: tests that change it copy it (copy_lake)
        config.RUNS_DIR = Path(self._tmp) / "runs"
        config.SETTINGS_DIR = Path(self._tmp) / "settings"
        config.SANDBOX_DIR = Path(self._tmp) / "sandbox"
        config.inputs_dir().mkdir(parents=True)
        from agent import datalake, tablecache
        datalake._META.clear()
        tablecache.clear_memory()
        write_samples(config.inputs_dir())

    def copy_lake(self) -> Path:
        """A private copy of the lake data, for a test that changes it."""
        copy = Path(self._tmp) / "lake"
        shutil.copytree(LAKE, copy)
        connectors.OVERRIDES.setdefault("sample_lake", {})["data"] = str(copy)
        return copy

    def reserve_url(self, name: str = "sample_lake") -> str:
        """A free local port for a connector's mock server, set as its URL (as its connector.py would set it), so a
        replay script exported before the server starts already points at it."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{s.getsockname()[1]}"
        connectors.OVERRIDES.setdefault(name, {})["url"] = url
        return url

    def serve_mock(self, name: str = "sample_lake", data=None, use: bool = False, **kw) -> str:
        """A mock lake server for one connector (its data, or `data`), at its reserved URL or a free port. use=True
        also switches the connector to KIND mock; `kw` go to the server (users, token, max_rows)."""
        from urllib.parse import urlsplit
        from scripts import mock_lake_server as server
        over = connectors.OVERRIDES.setdefault(name, {})
        port = urlsplit(over["url"]).port if "url" in over else 0
        srv = server.serve(port, data or connectors.all_connectors()[name].path("data", "data"), quiet=True, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        over["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
        if use:
            over["kind"] = "mock"
        return over["url"]

    def tearDown(self):
        config.RUNS_DIR, config.SANDBOX_DIR, config.SETTINGS_DIR = self._saved
        connectors.OVERRIDES.clear()
        shutil.rmtree(self._tmp, ignore_errors=True)


class LakeOff(TempDirs):
    """Sessions start without the datalake toolkit, so tests can exercise suggestions and /commands for it."""

    def setUp(self):
        super().setUp()
        self._saved_basic = config.BASIC_TOOLKITS
        config.BASIC_TOOLKITS = ["files", "docs", "analysis"]

    def tearDown(self):
        config.BASIC_TOOLKITS = self._saved_basic
        super().tearDown()


# The filters every query on the synthetic `usage_limits` view needs (see the data dictionary).
STD = ("partition_key = 'FY2025' AND entity = 'DemoBank Group' AND rule = 'JFSA B3' AND currency = 'JPY' "
       "AND version = 'CLOSED' AND method = 'BOOKED'")
LATEST_FINAL_RWA = "2026-03-26"


def j(**kw) -> str:
    """Compact JSON reply for scripted models."""
    return json.dumps(kw)


def tool_call(_tool, step_done=False, **inp) -> str:
    return j(thought=f"call {_tool}", tool=_tool, input=inp, step_done=step_done)


def final(text="Done.") -> str:
    return j(thought="finish", final=text)


PLAN = j(plan=["Read the data", "Compute", "Answer"])
OK = j(ok=True, issues=[])


def first_step(state: dict, model, plan=("Do it",)) -> dict:
    """Plan (a scripted planner reply) and take the first step: the graph always plans before it acts."""
    from agent.loop import run_one_step
    model.replies.setdefault("planner", []).insert(0, j(plan=list(plan)))
    run_one_step(state, model)
    return run_one_step(state, model)
