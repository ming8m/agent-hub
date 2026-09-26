import json
import unittest
from datetime import datetime, timezone

import hub_agent_io as io


RUN = "1" * 32
TASK = "2" * 32
PARENT = "3" * 32


class AgentIOTests(unittest.TestCase):
    def marked(self, value):
        return io.RESULT_MARKER + "\n" + json.dumps(value, ensure_ascii=False)

    def test_prompt_explains_optional_structured_contract_without_command_metadata(self):
        prompt = io.build_cli_prompt("Review the implementation")
        self.assertIn(io.RESULT_MARKER, prompt)
        self.assertIn('"final_answer":"string"', prompt)
        self.assertIn("ordinary plain text", prompt)
        self.assertIn("sender/run/task identity", prompt)

    def test_unmarked_stdout_is_an_ordinary_final_reply(self):
        self.assertEqual(io.parse_cli_output("plain answer\n", ["alpha"]), {
            "final_answer": "plain answer\n", "messages": [], "structured": False,
        })

    def test_parses_windows_crlf_marker_line(self):
        value = {"final_answer": "Done", "messages": [
            {"recipient": "beta", "task": "Review", "body": "Please review", "execute": True},
        ]}
        raw = io.RESULT_MARKER + "\r\n" + json.dumps(value, ensure_ascii=False) + "\r\n"
        parsed = io.parse_cli_output(raw, ["alpha", "beta"])
        self.assertEqual("Done", parsed["final_answer"])
        self.assertTrue(parsed["structured"])
        self.assertEqual("beta", parsed["messages"][0]["recipient"])

    def test_appended_block_after_plain_answer_is_parsed(self):
        value = {"final_answer": "Done", "messages": [
            {"recipient": "beta", "body": "Review finding", "execute": False},
        ]}
        raw = "Here is the result.\n\n" + self.marked(value)
        parsed = io.parse_cli_output(raw, ["alpha", "beta"])
        self.assertTrue(parsed["structured"])
        self.assertEqual("Done", parsed["final_answer"])
        self.assertEqual("beta", parsed["messages"][0]["recipient"])

    def test_appended_malformed_or_multiple_blocks_are_errors(self):
        block = self.marked({"final_answer": "Done", "messages": []})
        for raw in ("Answer\n\nAGENT_HUB_RESULT\n{bad}", block + "\n\n" + block,
                    block + "\ntrailing prose"):
            with self.subTest(raw=raw), self.assertRaises(io.AgentIOError):
                io.parse_cli_output(raw, ["alpha"])

    def test_fenced_and_discussed_marker_stays_plain_text(self):
        block = self.marked({"final_answer": "Done", "messages": [
            {"recipient": "beta", "body": "do not send", "execute": False},
        ]})
        for raw in ("Example:\n```text\n" + block + "\n```",
                    "I saw AGENT_HUB_RESULT in a log.",
                    "The protocol uses `AGENT_HUB_RESULT` as a marker."):
            with self.subTest(raw=raw):
                self.assertEqual([], io.parse_cli_output(raw, ["alpha", "beta"])["messages"])

    def test_parses_structured_reply_and_builds_identity_from_context(self):
        parsed = io.parse_cli_output(self.marked({
            "final_answer": "Done",
            "messages": [
                {"recipient": "beta", "task": "Review this result", "body": "Please review", "execute": True},
                {"recipient": "gamma", "body": "Recorded for the run", "execute": False},
            ],
        }), ["alpha", "beta", "gamma", "delta"])
        envelopes = io.build_envelopes(
            parsed, run_id=RUN, task_id=TASK, parent_task_id=PARENT,
            sender_agent_id="alpha", sender_role="worker", depth=0,
            orchestrator_agent_id="delta", registered_agent_ids=["alpha", "beta", "gamma", "delta"],
            now=datetime(2026, 9, 25, tzinfo=timezone.utc),
        )
        self.assertEqual(envelopes[0]["body"], "Review this result")
        self.assertEqual(envelopes[0]["relation"], "worker_worker")
        self.assertEqual(envelopes[1]["relation"], "worker_worker")
        self.assertEqual(envelopes[0]["from_agent_id"], "alpha")
        self.assertEqual(envelopes[0]["run_id"], RUN)
        self.assertEqual(envelopes[0]["task_id"], TASK)
        self.assertEqual(envelopes[0]["parent_task_id"], PARENT)
        self.assertEqual(set(envelopes[0]), {
            "schema_version", "message_id", "run_id", "task_id", "parent_task_id", "kind",
            "from_agent_id", "to_agent_id", "relation", "body", "execute", "created_at",
        })

    def test_worker_to_orchestrator_gets_orchestrator_worker_relation(self):
        parsed = io.parse_cli_output(self.marked({"final_answer": "", "messages": [
            {"recipient": "lead", "body": "Need a decision", "execute": False},
        ]}), ["lead", "worker"])
        envelope = io.build_envelopes(
            parsed, run_id=RUN, task_id=TASK, parent_task_id=None,
            sender_agent_id="worker", sender_role="worker", depth=0,
            orchestrator_agent_id="lead", registered_agent_ids=["lead", "worker"],
        )[0]
        self.assertEqual(envelope["relation"], "orchestrator_worker")

    def test_rejects_unknown_recipient_and_forged_identity_command_or_path_fields(self):
        valid = {"final_answer": "ok", "messages": [
            {"recipient": "beta", "body": "hello", "execute": False},
        ]}
        for field, value in (("from_agent_id", "forged"), ("run_id", RUN),
                             ("command", "whoami"), ("path", "C:\\secret"),
                             ("shell", "whoami")):
            with self.subTest(field=field):
                attack = json.loads(json.dumps(valid))
                attack["messages"][0][field] = value
                with self.assertRaises(io.AgentIOError):
                    io.parse_cli_output(self.marked(attack), ["alpha", "beta"])
        attack = json.loads(json.dumps(valid))
        attack["messages"][0]["recipient"] = "not_registered"
        with self.assertRaisesRegex(io.AgentIOError, "registered"):
            io.parse_cli_output(self.marked(attack), ["alpha", "beta"])

    def test_rejects_bad_marker_schema_types_and_limits(self):
        for raw in (
            io.RESULT_MARKER + "\n{" ,
            self.marked({"final_answer": "ok", "messages": [], "run_id": RUN}),
            self.marked({"final_answer": 42, "messages": []}),
            self.marked({"final_answer": "ok", "messages": [
                {"recipient": "beta", "body": "x", "execute": "true"},
            ]}),
        ):
            with self.subTest(raw=raw), self.assertRaises(io.AgentIOError):
                io.parse_cli_output(raw, ["alpha", "beta"])
        oversized = self.marked({"final_answer": "ok", "messages": [
            {"recipient": "beta", "body": "x", "execute": False},
        ]})
        with self.assertRaises(io.AgentIOError):
            io.parse_cli_output(oversized, ["alpha", "beta"], max_messages=0)
        huge = self.marked({"final_answer": "ok", "messages": [
            {"recipient": "beta", "body": "x" * (io.MAX_BODY_BYTES + 1), "execute": False},
        ]})
        with self.assertRaises(io.AgentIOError):
            io.parse_cli_output(huge, ["alpha", "beta"])

    def test_rejects_missing_child_prompt_self_dispatch_and_excess_depth(self):
        for message in (
            {"recipient": "beta", "body": "hello", "execute": True},
            {"recipient": "alpha", "task": "again", "body": "hello", "execute": True},
        ):
            with self.subTest(message=message), self.assertRaises(io.AgentIOError):
                parsed = io.parse_cli_output(self.marked({"final_answer": "", "messages": [message]}), ["alpha", "beta"])
                io.build_envelopes(parsed, run_id=RUN, task_id=TASK, parent_task_id=None,
                                   sender_agent_id="alpha", sender_role="orchestrator", depth=0,
                                   orchestrator_agent_id="alpha", registered_agent_ids=["alpha", "beta"])
        with self.assertRaisesRegex(io.AgentIOError, "depth"):
            io.parse_cli_output(self.marked({"final_answer": "", "messages": [
                {"recipient": "beta", "task": "child", "body": "do it", "execute": True},
            ]}), ["alpha", "beta"], depth=io.MAX_DEPTH)


if __name__ == "__main__":
    unittest.main()
