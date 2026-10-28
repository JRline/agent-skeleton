"""Session memory mode: a backend that keeps one memory per channel gets fixed parts once, then only what is new."""
import os

from agent import config
from agent.adapter import ModuleChatModel
from agent.backends import session_sim
from agent.loop import run_one_step
from agent.memory import add_turn, build_messages, new_state, read_log
from agent.session import Session

from tests import sim_inner_fake
from tests.helpers import PLAN, TempDirs, final, j, tool_call


class Backend:
    """A production-like module: call(text, effort, channel) answers from a queue per channel."""

    def __init__(self, replies):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls, self.sessions = [], 0

    def call(self, text, effort="none", channel="review"):
        self.calls.append({"channel": channel, "effort": effort, "text": text})
        return self.replies[channel].pop(0)

    def new_session(self):
        self.sessions += 1


class StatelessBackend:
    def __init__(self):
        self.kwargs = []

    def call(self, text, effort="none"):
        self.kwargs.append({"effort": effort})
        return PLAN


class SessionMode(TempDirs):
    def setUp(self):
        super().setUp()
        saved = (config.MODEL_MEMORY, config.MEMORY_REFRESH_TOKENS)
        self.addCleanup(lambda: (setattr(config, "MODEL_MEMORY", saved[0]),
                                 setattr(config, "MEMORY_REFRESH_TOKENS", saved[1])))
        config.MODEL_MEMORY = "session"

    def run_task(self):
        backend = Backend({"review": [PLAN, j(ok=True, issues=[])],
                           "executor": [tool_call("list_datasets", step_done=True), final("Done.")]})
        model = ModuleChatModel(backend, "prod")
        state = new_state("What views are there?")
        for _ in range(6):
            run_one_step(state, model)
            if state["status"] == "finished":
                break
        return backend, model, state

    def test_executor_gets_fixed_parts_once_then_only_what_is_new(self):
        backend, model, state = self.run_task()
        self.assertEqual(state["status"], "finished")
        first, second = [c for c in backend.calls if c["channel"] == "executor"]
        self.assertIn("## Reply protocol", first["text"])
        self.assertIn("NEW CONVERSATION", first["text"])
        self.assertNotIn("## Reply protocol", second["text"])          # sent once
        for part in ("OBSERVATION", "REMINDER: reply with exactly one JSON object", "list_datasets", "STATE"):
            self.assertIn(part, second["text"])
        self.assertLess(len(second["text"]), len(first["text"]) / 2)

    def test_each_channel_has_its_fixed_effort_and_no_summariser_runs(self):
        backend, model, state = self.run_task()
        self.assertEqual({(c["channel"], c["effort"]) for c in backend.calls},
                         {("executor", "none"), ("review", "medium")})
        self.assertTrue(model.remembers)
        logged = [e for e in read_log(state) if e["kind"] == "model"]
        self.assertNotIn("summariser", {e["purpose"] for e in logged})
        self.assertEqual([e["send"] for e in logged if e["channel"] == "executor"], ["full", "delta"])
        self.assertTrue(all(e["sent_tokens"] > 0 for e in logged))

    def test_a_changed_block_is_sent_alone(self):
        session, state = Session(), new_state("g", toolkits=["files"])
        session.prepare(build_messages(state), "executor", state["run_id"])
        add_turn(state, "assistant", '{"tool": "read_file"}')
        add_turn(state, "user", "OBSERVATION: {}")
        state["toolkits"].append("analysis")                     # the user enabled a toolkit
        text, sent = session.prepare(build_messages(state), "executor", state["run_id"])
        self.assertEqual(sent["mode"], "delta")
        self.assertIn("UPDATE (## Tools)", text)
        self.assertIn("run_python", text)
        self.assertNotIn("UPDATE (## Reply protocol)", text)
        self.assertNotIn('{"tool": "read_file"}', text)           # its own reply is already in its memory

    def test_full_send_on_new_conversation_resync_retry_and_refresh(self):
        session, a, b = Session(), new_state("first"), new_state("second")
        self.assertEqual(session.prepare(build_messages(a), "executor", a["run_id"])[1]["mode"], "full")
        add_turn(a, "user", "OBSERVATION: {}")
        self.assertEqual(session.prepare(build_messages(a), "executor", a["run_id"])[1]["mode"], "delta")
        self.assertEqual(session.prepare(build_messages(b), "executor", b["run_id"])[1]["mode"], "full")
        session.resync("executor")
        self.assertEqual(session.prepare(build_messages(b), "executor", b["run_id"])[1]["mode"], "full")
        add_turn(b, "user", "x")
        session.prepare(build_messages(b), "executor", b["run_id"])
        b["turns"].pop()                                          # a retried step: fewer turns than were sent
        self.assertEqual(session.prepare(build_messages(b), "executor", b["run_id"])[1]["mode"], "full")
        config.MEMORY_REFRESH_TOKENS = 1                          # compressed memory: refresh soon
        add_turn(b, "user", "y")
        session.prepare(build_messages(b), "executor", b["run_id"])
        add_turn(b, "user", "z")
        self.assertEqual(session.prepare(build_messages(b), "executor", b["run_id"])[1]["mode"], "full")

    def test_a_repair_sends_only_the_correction(self):
        session, state = Session(), new_state("g")
        msgs = build_messages(state)
        session.prepare(msgs, "executor", state["run_id"])
        repair = msgs + [{"role": "assistant", "content": "oops"},
                         {"role": "user", "content": "Invalid output: no JSON. Reply with one JSON object."}]
        text, sent = session.prepare(repair, "executor", state["run_id"])
        self.assertEqual((sent["mode"], text), ("repair", repair[-1]["content"]))

    def test_new_session_starts_fresh_instances(self):
        backend = Backend({"executor": []})
        model = ModuleChatModel(backend, "prod")
        state = new_state("g")
        model.session.prepare(build_messages(state), "executor", state["run_id"])
        model.new_session()
        self.assertEqual(backend.sessions, 1)
        self.assertEqual(model.session.prepare(build_messages(state), "executor", state["run_id"])[1]["mode"], "full")

    def test_off_by_default_and_needs_a_channel_aware_backend(self):
        stateless = StatelessBackend()
        model = ModuleChatModel(stateless, "prod")               # session requested, backend has no channel
        self.assertFalse(model.remembers)
        model.complete([{"role": "user", "content": "hi"}], purpose="planner")
        self.assertEqual(stateless.kwargs, [{"effort": "none"}])
        config.MODEL_MEMORY = "off"
        self.assertFalse(ModuleChatModel(Backend({}), "prod").remembers)


class ProductionStandIn(TempDirs):
    """agent/backends/session_sim.py behaves like the production stack: fixed instances with memory."""

    def setUp(self):
        super().setUp()
        saved = os.environ.get("SIM_INNER"), os.environ.get("SIM_MEMORY_TOKENS")
        os.environ["SIM_INNER"] = "tests.sim_inner_fake"
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in zip(("SIM_INNER", "SIM_MEMORY_TOKENS"), saved)])
        session_sim.new_session()
        sim_inner_fake.reset()

    def test_each_instance_remembers_its_own_conversation_with_a_fixed_effort(self):
        session_sim.call("first", effort="medium", channel="executor")
        session_sim.call("second", effort="medium", channel="executor")
        session_sim.call("check this", channel="check")
        sent = sim_inner_fake.calls[1]["messages"]
        self.assertEqual([m["content"] for m in sent[1:]], ["first", "ok", "second"])   # memory kept
        self.assertEqual(sim_inner_fake.calls[1]["effort"], "none")                      # executor is fixed at none
        self.assertEqual([m["content"] for m in sim_inner_fake.calls[2]["messages"][1:]], ["check this"])
        self.assertEqual(sim_inner_fake.calls[2]["effort"], "medium")

    def test_memory_is_compressed_past_its_limit(self):
        os.environ["SIM_MEMORY_TOKENS"] = "10"
        for i in range(3):
            session_sim.call("x" * 40, channel="executor")
        self.assertEqual(session_sim.compressions.get("executor"), 1)
        self.assertTrue(session_sim._memory["executor"][0]["content"].startswith("[Earlier conversation, compressed]"))

    def test_a_failed_call_leaves_no_half_exchange_and_new_session_forgets(self):
        sim_inner_fake.reset(["FAIL"])
        with self.assertRaises(RuntimeError):
            session_sim.call("lost", channel="executor")
        self.assertEqual(session_sim._memory["executor"], [])
        session_sim.call("kept", channel="executor")
        session_sim.new_session()
        self.assertEqual(session_sim._memory, {})
