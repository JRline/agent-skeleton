import unittest

from agent import config
from agent.protocol import ProtocolError, extract_json, parse_action, request_json
from agent.tools import TOOLS


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


class ParseAction(unittest.TestCase):
    def test_valid_tool_call_is_validated_and_coerced(self):
        a = parse_action('{"tool": "search_docs", "input": {"query": "tier 1", "k": "3"}}', TOOLS)
        self.assertEqual(a.tool, "search_docs")
        self.assertEqual(a.input["k"], 3)
        a = parse_action('{"tool": "run_python", "input": {"code": "result = 1"}}', TOOLS)
        self.assertEqual(a.input["save_as"], "analysis")   # default filled in

    def test_final(self):
        self.assertEqual(parse_action('{"final": "answer"}', TOOLS).final, "answer")

    def test_unknown_tool(self):
        with self.assertRaisesRegex(ProtocolError, "Unknown tool"):
            parse_action('{"tool": "delete_everything", "input": {}}', TOOLS)

    def test_both_tool_and_final(self):
        with self.assertRaisesRegex(ProtocolError, "exactly one"):
            parse_action('{"tool": "calc", "input": {"expression": "1"}, "final": "x"}', TOOLS)

    def test_neither(self):
        with self.assertRaisesRegex(ProtocolError, "exactly one"):
            parse_action('{"thought": "hmm"}', TOOLS)

    def test_bad_arguments(self):
        with self.assertRaisesRegex(ProtocolError, "Bad input for read_file"):
            parse_action('{"tool": "read_file", "input": {}}', TOOLS)


class RequestJson(unittest.TestCase):
    def _run(self, replies, base="none"):
        efforts = []

        def call(msgs, effort):
            efforts.append(effort)
            return replies.pop(0)
        result = request_json(call, [{"role": "user", "content": "go"}],
                              lambda t: parse_action(t, TOOLS), base)
        return result, efforts

    def test_first_try(self):
        (action, _, attempts), efforts = self._run(['{"final": "ok"}'], base="medium")
        self.assertEqual((action.final, attempts, efforts), ("ok", 1, ["medium"]))

    def test_repairs_at_none_then_escalates_to_medium(self):
        bad = "not json"
        (action, _, attempts), efforts = self._run([bad, bad, bad, '{"final": "ok"}'])
        self.assertEqual(attempts, 4)
        self.assertEqual(efforts, ["none"] + ["none"] * config.MAX_REPAIRS + ["medium"])

    def test_gives_up(self):
        with self.assertRaises(ProtocolError):
            self._run(["x"] * 4)

    def test_repair_message_explains_error(self):
        seen = []

        def call(msgs, effort):
            seen.append(msgs)
            return "bad" if len(seen) == 1 else '{"final": "ok"}'
        request_json(call, [{"role": "user", "content": "go"}], lambda t: parse_action(t, TOOLS), "none")
        self.assertIn("Invalid output", seen[1][-1]["content"])
        self.assertEqual(seen[1][-2], {"role": "assistant", "content": "bad"})

    def test_repair_prompt_stays_small(self):
        seen = []

        def call(msgs, effort):
            seen.append(msgs)
            return "Let me think. " * 300 if len(seen) < 3 else '{"final": "ok"}'
        request_json(call, [{"role": "user", "content": "go"}], lambda t: parse_action(t, TOOLS), "none")
        self.assertEqual(len(seen[2]), 3)                   # original + latest bad reply + error only
        self.assertLessEqual(len(seen[2][1]["content"]), 401)


if __name__ == "__main__":
    unittest.main()


class FencesInsideAnswers(unittest.TestCase):
    def test_code_fences_in_the_answer_text_are_kept(self):
        from agent.protocol import extract_json
        reply = '```json\n{"thought": "t", "final": "Query:\\n```sql\\nSELECT 1\\n```"}\n```'
        self.assertEqual(extract_json(reply)["final"], "Query:\n```sql\nSELECT 1\n```")
        self.assertEqual(extract_json('{"final": "```sql\\nSELECT 1\\n```"}')["final"], "```sql\nSELECT 1\n```")
