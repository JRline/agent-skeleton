"""Stand-in for the `claude` CLI in tests: no network. Behaviour is set by FAKE_CLAUDE_MODE:
echo (default) answers with a fixed JSON action, error reports is_error, sleep waits 30 s.
Every call is recorded to FAKE_CLAUDE_LOG (argv, cwd, stdin) so tests can assert on them."""
import json
import os
import sys
import time

stdin = sys.stdin.read()
if os.getenv("FAKE_CLAUDE_LOG"):
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin}) + "\n")
mode = os.getenv("FAKE_CLAUDE_MODE", "echo")
if mode == "sleep":
    time.sleep(30)
if mode == "error":
    print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in"}))
    sys.exit(1)
print(json.dumps({"type": "result", "is_error": False, "result": '{"final": "ok"}'}))
