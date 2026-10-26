"""Shared test setup: point runs/ and sandbox/ at a temporary folder."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent import config
from mock_lake import generate_view
from mock_lake.samples import write_samples

LAKE = Path(__file__).resolve().parent.parent / "mock_lake" / "data"
DICTIONARY = Path(__file__).resolve().parent.parent / "sandbox" / "references" / "datalake_dictionary.md"


if not (LAKE / "usage_limits.csv").exists():      # data is never committed: build it once from the seed
    generate_view.write(LAKE / "usage_limits.csv")


class TempDirs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._saved = (config.RUNS_DIR, config.SANDBOX_DIR, config.SETTINGS_DIR, config.LAKE_DATA_DIR)
        config.LAKE_DATA_DIR = LAKE        # read-only: tests that change lake data copy it (copy_lake)
        config.RUNS_DIR = Path(self._tmp) / "runs"
        config.SETTINGS_DIR = Path(self._tmp) / "settings"
        config.SANDBOX_DIR = Path(self._tmp) / "sandbox"
        config.inputs_dir().mkdir(parents=True)
        from agent import datalake, tablecache
        datalake._META.clear()
        tablecache.clear_memory()
        write_samples(config.inputs_dir())
        config.datalake_dictionary().parent.mkdir(parents=True)
        shutil.copy(DICTIONARY, config.datalake_dictionary())

    def copy_lake(self) -> Path:
        """A private copy of the lake data, for a test that changes it."""
        config.LAKE_DATA_DIR = Path(self._tmp) / "lake"
        shutil.copytree(LAKE, config.LAKE_DATA_DIR)
        return config.LAKE_DATA_DIR

    def tearDown(self):
        config.RUNS_DIR, config.SANDBOX_DIR, config.SETTINGS_DIR, config.LAKE_DATA_DIR = self._saved
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
