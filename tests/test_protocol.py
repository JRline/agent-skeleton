import unittest

from agent import config
from agent.chat_model import ReplyError, TextChatModel, parse_reply, to_message
from agent.protocol import ProtocolError, extract_json


class ExtractJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_code_fence_and_leading_prose(self):
        self.assertEqual(extract_json('Sure:\n```json\n{"a": "x}"}\n```'), {"a": "x}"})

    def test_braces_inside_strings(self):
        self.assertEqual(extract_json('{"a": "{not an object}"}')["a"], "{not an object}")

    def test_no_json(self):
        with self.assertRaises(ProtocolError):
            extract_json("I will read the file now.")

    def test_two_objects_rejected(self):
        with self.assertRaises(ProtocolError):
            extract_json('{"a": 1} {"b": 2}')

    def test_invalid_json(self):
        with self.assertRaises(ProtocolError):
            extract_json('{"a": 1,}')

    def test_reasoning_before_answer_is_ignored(self):
        reply = ('Okay, the protocol shape is {"thought": "...", "tool": "<name>"} and earlier I sent '
                 '{"tool": "read_file", "input": {"path": "x"}}. So now:\n{"final": "done"}')
        self.assertEqual(extract_json(reply), {"final": "done"})

    def test_placeholder_braces_in_prose_are_skipped(self):
        self.assertEqual(extract_json('Inputs go in {<arguments>}. {"a": 1}'), {"a": 1})

    def test_cut_off_answer_after_quoted_json_is_rejected(self):
        with self.assertRaises(ProtocolError):
            extract_json('Before I sent {"tool": "read_file"}. Now: {"final": "unfinish')


class ReplyToMessage(unittest.TestCase):
    """The text-only model's reply becomes a LangChain tool call or answer (agent/chat_model.py)."""

    TOOLS = [{"type": "function", "function": {"name": n, "parameters": {}}} for n in ("read_file", "search_docs")]

    def test_tool_call_and_final(self):
        msg = to_message(parse_reply('{"thought": "t", "tool": "search_docs", "input": {"query": "tier 1"}}'),
                         self.TOOLS, False)
        self.assertEqual((msg.tool_calls[0]["name"], msg.tool_calls[0]["args"]), ("search_docs", {"query": "tier 1"}))
        self.assertEqual(msg.additional_kwargs["thought"], "t")
        msg = to_message({"final": "answer", "tables": ["t"]}, self.TOOLS, False)
        self.assertEqual((msg.content, msg.additional_kwargs["tables"]), ("answer", ["t"]))

    def test_unknown_tool_and_neither(self):
        with self.assertRaisesRegex(ReplyError, "Unknown tool"):
            to_message({"tool": "delete_everything", "input": {}}, self.TOOLS, False)
        with self.assertRaisesRegex(ReplyError, "exactly one"):
            to_message({"thought": "hmm"}, self.TOOLS, False)

    def test_a_disabled_toolkits_tool_is_marked_for_the_user(self):
        msg = to_message({"tool": "sql_query", "input": {}}, self.TOOLS, False, {"sql_query": "datalake"})
        self.assertEqual(msg.additional_kwargs["needs_toolkit"], "datalake")


class Repairs(unittest.TestCase):
    def test_repairs_at_none_then_escalates_to_medium(self):
        from agent.backends.fake import FakeModel
        m = FakeModel({"reviewer": ["not json"] * config.MAX_REPAIRS + ["also not", '{"ok": true, "issues": []}']})
        from agent.checks import _Verdict
        out = TextChatModel(chat=m).for_role("reviewer", "none").with_structured_output(_Verdict).invoke("check")
        self.assertTrue(out.ok)
        self.assertEqual(m.efforts(), ["none"] * (config.MAX_REPAIRS + 1) + ["medium"])

    def test_gives_up_with_a_clear_error(self):
        from agent.backends.fake import FakeModel
        from agent.checks import _Verdict
        m = FakeModel({"reviewer": ["x"] * 4})
        with self.assertRaisesRegex(ReplyError, "outside the protocol"):
            TextChatModel(chat=m).for_role("reviewer").with_structured_output(_Verdict).invoke("check")

    def test_repair_prompt_stays_small(self):
        from agent.backends.fake import FakeModel
        from agent.checks import _Verdict
        m = FakeModel({"reviewer": ["Let me think. " * 300] * 2 + ['{"ok": true}']})
        TextChatModel(chat=m).for_role("reviewer").with_structured_output(_Verdict).invoke("check")
        third = m.calls[2]["messages"]
        self.assertEqual(len(third), 3)                    # original + latest bad reply + error only
        self.assertLessEqual(len(third[1]["content"]), 401)
        self.assertTrue(third[2]["content"].startswith("Invalid output"))


if __name__ == "__main__":
    unittest.main()


class FencesInsideAnswers(unittest.TestCase):
    def test_code_fences_in_the_answer_text_are_kept(self):
        from agent.protocol import extract_json
        reply = '```json\n{"thought": "t", "final": "Query:\\n```sql\\nSELECT 1\\n```"}\n```'
        self.assertEqual(extract_json(reply)["final"], "Query:\n```sql\nSELECT 1\n```")
        self.assertEqual(extract_json('{"final": "```sql\\nSELECT 1\\n```"}')["final"], "```sql\nSELECT 1\n```")
