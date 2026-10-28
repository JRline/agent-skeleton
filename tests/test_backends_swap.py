"""The model can be swapped for a real one: an OpenAI-compatible API, or any LangChain chat model used natively."""
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agent import config
from agent.adapter import LangChainModel, get_model
from agent.loop import run
from agent.memory import read_log

from tests.helpers import OK, PLAN, TempDirs, final, tool_call

READ = tool_call("read_file", path="work/sample_capital.csv")


class Api(TempDirs):
    """A stand-in OpenAI-compatible server: answers each request from a queue and records it."""

    def setUp(self):
        super().setUp()
        (config.SANDBOX_DIR / "work").mkdir(parents=True, exist_ok=True)
        shutil.copy(config.inputs_dir() / "sample_capital.csv", config.SANDBOX_DIR / "work" / "sample_capital.csv")
        self.requests, replies = [], [PLAN, READ, final("CET1 capital is 12850 [ref: read_file-1]"), OK]
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                test.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                      "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
                data = json.dumps({"choices": [{"message": {"content": replies.pop(0)}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        key = config.RUNS_DIR.parent / "key.txt"
        key.write_text("test-key\n")
        env = mock.patch.dict("os.environ", {"API_URL": f"http://127.0.0.1:{srv.server_address[1]}/v1",
                                             "API_MODEL": "small", "API_MODEL_MEDIUM": "large",
                                             "API_KEY_FILE": str(key), "API_EFFORT_PARAM": "reasoning_effort"})
        env.start()
        self.addCleanup(env.stop)

    def test_a_real_api_is_one_setting_away(self):
        state = run("What is the 2026Q3 CET1 capital?", get_model("api"))
        self.assertEqual(state["status"], "finished")
        first = self.requests[0]
        self.assertEqual((first["path"], first["auth"]), ("/v1/chat/completions", "Bearer test-key"))
        self.assertEqual((first["body"]["model"], first["body"]["reasoning_effort"]), ("large", "medium"))  # planner
        self.assertEqual(self.requests[1]["body"]["model"], "small")                                      # executor
        self.assertEqual(self.requests[1]["body"]["messages"][0]["role"], "system")
        self.assertEqual({e["model"] for e in read_log(state) if e["kind"] == "model"}, {"small", "large"})


class NativeFake(BaseChatModel):
    """A LangChain chat model with native tool calling and structured output (as a provider package gives)."""
    executor: list = []
    seen: list = []

    @property
    def _llm_type(self):
        return "native-fake"

    def bind_tools(self, tools, *, tool_choice=None, **kw):
        from langchain_core.utils.function_calling import convert_to_openai_tool
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], tool_choice=tool_choice, **kw)

    def _generate(self, messages, stop=None, run_manager=None, tools=None, tool_choice=None, **kw):
        self.seen.append({"messages": messages, "tools": [t["function"]["name"] for t in tools or []]})
        names = [t["function"]["name"] for t in tools or []]
        structured = {"_Plan": {"plan": ["Read the file"]}, "_Verdict": {"ok": True, "issues": []},
                      "_ActionVerdict": {"ok": True, "issues": [], "explain": "x"}}
        if tool_choice and names and names[0] in structured:     # with_structured_output: one forced tool
            msg = AIMessage("", tool_calls=[{"name": names[0], "args": structured[names[0]], "id": "s1"}])
        else:
            msg = self.executor.pop(0)
        return ChatResult(generations=[ChatGeneration(message=msg)])


class LangChainNative(TempDirs):
    def test_a_langchain_chat_model_is_used_natively(self):
        (config.SANDBOX_DIR / "work").mkdir(parents=True, exist_ok=True)
        shutil.copy(config.inputs_dir() / "sample_capital.csv", config.SANDBOX_DIR / "work" / "sample_capital.csv")
        lc = NativeFake(executor=[
            AIMessage("", tool_calls=[{"name": "read_file", "args": {"path": "work/sample_capital.csv"}, "id": "c1"}]),
            AIMessage("CET1 capital is 12850 [ref: read_file-1]")], seen=[])
        state = run("What is the 2026Q3 CET1 capital?", LangChainModel(lc))
        self.assertEqual(state["status"], "finished")
        self.assertIn("12850", state["final"])
        executor = [c for c in lc.seen if "read_file" in c["tools"]][0]
        self.assertIn("## Toolkit rules", executor["messages"][0].content)       # our rules, its own tool calling
        self.assertNotIn("Reply protocol", executor["messages"][0].content)      # no JSON protocol needed
        purposes = [e["purpose"] for e in read_log(state) if e["kind"] == "model"]
        self.assertEqual(purposes[0], "planner")
        self.assertIn("reviewer", purposes)
