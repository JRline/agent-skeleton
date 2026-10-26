"""Ollama backend without Ollama: a local stand-in server that answers slowly."""
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent.adapter import CallCancelled, ModuleChatModel
from agent.backends import ollama_api


class SlowChat(BaseHTTPRequestHandler):
    delay = 5.0
    reject_think = False          # behave like a model without thinking support
    bodies: list = []

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SlowChat.bodies.append(req)
        if self.reject_think and "think" in req:
            body = b'{"error": "\\"m:1b\\" does not support thinking"}'
            self.send_response(400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        time.sleep(self.delay)
        body = json.dumps({"message": {"content": "<think>hmm</think>{\"final\": \"ok\"}"}}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass                                  # client went away (cancelled)

    def log_message(self, *args):
        pass


class OllamaBackend(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SlowChat)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.saved = os.environ.get("OLLAMA_URL")
        os.environ["OLLAMA_URL"] = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if self.saved is None:
            os.environ.pop("OLLAMA_URL", None)
        else:
            os.environ["OLLAMA_URL"] = self.saved
        SlowChat.delay, SlowChat.reject_think, SlowChat.bodies = 5.0, False, []
        os.environ.pop("OLLAMA_THINK", None)
        ollama_api._no_think.clear()

    def test_thinking_follows_effort_by_default(self):
        SlowChat.delay = 0
        os.environ.pop("OLLAMA_THINK", None)
        ollama_api.call("hi", effort="none")
        ollama_api.call("hi", effort="medium")
        self.assertEqual([b.get("think") for b in SlowChat.bodies], [False, True])

    def test_think_control_can_be_turned_off(self):
        SlowChat.delay = 0
        os.environ["OLLAMA_THINK"] = "0"
        ollama_api.call("hi")
        self.assertNotIn("think", SlowChat.bodies[-1])

    def test_think_never_turns_thinking_off_for_every_effort(self):
        SlowChat.delay = 0
        os.environ["OLLAMA_THINK"] = "never"
        ollama_api.call("hi", effort="none")
        ollama_api.call("hi", effort="medium")
        self.assertEqual([b.get("think") for b in SlowChat.bodies], [False, False])

    def test_model_without_thinking_support_is_retried_without_it(self):
        SlowChat.delay, SlowChat.reject_think = 0, True
        self.assertEqual(ollama_api.call("hi", effort="medium"), '{"final": "ok"}')
        self.assertEqual(ollama_api.call("hi"), '{"final": "ok"}')
        self.assertEqual(["think" in b for b in SlowChat.bodies], [True, False, False])   # remembered

    def test_call_returns_answer_without_thinking(self):
        SlowChat.delay = 0
        self.assertEqual(ollama_api.call("hi"), '{"final": "ok"}')

    def test_cancel_stops_a_slow_call_quickly_and_is_not_retried(self):
        model = ModuleChatModel(ollama_api, "ollama", pass_messages=True)
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
        time.sleep(0.3)
        model.cancel()
        t.join(timeout=4)
        self.assertIsInstance(result.get("error"), CallCancelled)
        self.assertLess(result["seconds"], 2)

    def test_lone_closing_think_tag_is_stripped(self):
        self.assertEqual(ollama_api.answer_only("Okay, the user asked...\n</think>\n\nHello!"), "Hello!")
        self.assertEqual(ollama_api.answer_only('{"final": "x"}'), '{"final": "x"}')


if __name__ == "__main__":
    unittest.main()
