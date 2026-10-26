"""Claude CLI backend against a fake `claude` script: no network, no login needed."""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent.adapter import CallCancelled, ModuleChatModel
from agent.backends import claude_cli

FAKE = Path(__file__).resolve().parent / "fake_claude.py"


class ClaudeCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        wrapper = self.tmp / "claude"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "calls.jsonl"
        self.saved = {k: os.environ.get(k) for k in ("CLAUDE_CLI", "CLAUDE_MODEL", "FAKE_CLAUDE_LOG",
                                                       "FAKE_CLAUDE_MODE", "HOME")}
        os.environ.update(CLAUDE_CLI=str(wrapper), FAKE_CLAUDE_LOG=str(self.log))
        os.environ.pop("CLAUDE_MODEL", None)
        os.environ.pop("FAKE_CLAUDE_MODE", None)

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_call_sends_system_prompt_and_conversation_without_tools(self):
        msgs = [{"role": "system", "content": "PROTOCOL"}, {"role": "user", "content": "GOAL: x"}]
        self.assertEqual(claude_cli.call(msgs, effort="none"), '{"final": "ok"}')
        call = self.calls()[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-5-5")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--effort") + 1], "low")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "PROTOCOL")
        self.assertIn("USER:\nGOAL: x", call["stdin"])
        self.assertNotIn("PROTOCOL", call["stdin"])
        self.assertNotEqual(Path(call["cwd"]).resolve(), Path.cwd().resolve())   # project CLAUDE.md not loaded

    def test_medium_effort_and_model_override(self):
        os.environ["CLAUDE_MODEL"] = "claude-sonnet-5-5"
        claude_cli.call("hi", effort="medium")
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertEqual(claude_cli.model_name("medium"), "claude-sonnet-5-5")

    def test_cli_error_is_raised(self):
        os.environ["FAKE_CLAUDE_MODE"] = "error"
        with self.assertRaisesRegex(RuntimeError, "Not logged in"):
            claude_cli.call("hi")

    def test_cancel_stops_a_running_call_and_is_not_retried(self):
        os.environ["FAKE_CLAUDE_MODE"] = "sleep"
        model = ModuleChatModel(claude_cli, "claude", pass_messages=True)
        result = {}

        def worker():
            t0 = time.perf_counter()
            try:
                model.complete([{"role": "user", "content": "hi"}])
            except Exception as e:
                result["error"] = e
            result["seconds"] = time.perf_counter() - t0

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.5)
        model.cancel()
        t.join(timeout=5)
        self.assertIsInstance(result.get("error"), CallCancelled)
        self.assertLess(result["seconds"], 3)

    def test_finds_newest_cli_in_desktop_app(self):
        os.environ.pop("CLAUDE_CLI")
        os.environ["HOME"] = str(self.tmp)
        base = self.tmp / "Library/Application Support/Claude/claude-code"
        for version in ("2.1.99", "2.1.304"):
            p = base / version / "abc" / "claude.app/Contents/MacOS/claude"
            p.parent.mkdir(parents=True)
            p.write_text("")
        found = claude_cli.find_cli(path_lookup=False)
        self.assertIn("/2.1.304/", found)


if __name__ == "__main__":
    unittest.main()
