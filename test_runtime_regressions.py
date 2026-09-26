"""Failure and restart regressions for the persistent Hub runtime."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import hub


class RuntimeRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-runtime-regression-")
        self.home = Path(self.temp.name)
        self.agents = {"alpha": {"timeout": 5}}
        self.instance = hub.AgentHub(home=self.home, agent_loader=lambda: self.agents,
            executor=lambda *_: "ok", max_workers=1)

    def tearDown(self):
        self.instance.close()
        self.temp.cleanup()

    def test_registry_failure_leaves_terminal_task(self):
        entered, release = threading.Event(), threading.Event()
        blocker = self.instance._pool.submit(lambda: (entered.set(), release.wait(2)))
        self.assertTrue(entered.wait(2))
        accepted = self.instance.create_run("work", ["alpha"])
        original = self.instance._agents_for_run
        def fail_once(run):
            raise hub.HubError("storage_error", "registry unavailable")
        self.instance._agents_for_run = fail_once
        release.set()
        blocker.result(2)
        task_id = accepted["accepted_tasks"][0]["task_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.instance.get_task(task_id)["status"] == "running":
            time.sleep(.01)
        self.assertEqual(self.instance.get_task(task_id)["status"], "failed")
        self.instance._agents_for_run = original

    def test_transient_run_refresh_failure_recovers_completed_status(self):
        entered, release = threading.Event(), threading.Event()
        blocker = self.instance._pool.submit(lambda: (entered.set(), release.wait(2)))
        self.assertTrue(entered.wait(2))
        accepted = self.instance.create_run("work", ["alpha"])
        original = self.instance._refresh_run_locked
        failed = False
        def fail_once(run_id):
            nonlocal failed
            if not failed:
                failed = True
                raise hub.HubError("storage_error", "temporary refresh failure")
            return original(run_id)
        self.instance._refresh_run_locked = fail_once
        release.set()
        blocker.result(2)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.instance.get_run(accepted["run_id"])["status"] != "completed":
            time.sleep(.01)
        self.assertTrue(failed)
        self.assertEqual(self.instance.get_run(accepted["run_id"])["status"], "completed")

    def test_missing_run_during_execution_leaves_terminal_task(self):
        entered, release = threading.Event(), threading.Event()
        blocker = self.instance._pool.submit(lambda: (entered.set(), release.wait(2)))
        self.assertTrue(entered.wait(2))
        accepted = self.instance.create_run("work", ["alpha"])
        task_id = accepted["accepted_tasks"][0]["task_id"]
        (self.instance.runs_dir / (accepted["run_id"] + ".json")).unlink()
        release.set()
        blocker.result(2)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.instance.get_task(task_id)["status"] == "running":
            time.sleep(.01)
        self.assertIn(self.instance.get_task(task_id)["status"], {"failed", "interrupted"})

    def test_restart_isolates_run_with_missing_task(self):
        good = self.instance.create_run("good", ["alpha"])
        bad = self.instance.create_run("bad", ["alpha"])
        self.instance.close()
        missing = bad["accepted_tasks"][0]["task_id"]
        (self.instance.tasks_dir / (missing + ".json")).unlink()
        reopened = hub.AgentHub(home=self.home, agent_loader=lambda: self.agents)
        try:
            self.assertEqual(reopened.get_run(good["run_id"])["run_id"], good["run_id"])
            self.assertTrue((reopened.runs_dir / (bad["run_id"] + ".json")).exists())
        finally:
            reopened.close()

    def test_restart_isolates_invalid_run_schema(self):
        good = self.instance.create_run("good", ["alpha"])
        bad = self.instance.create_run("bad", ["alpha"])
        self.instance.close()
        path = self.instance.runs_dir / (bad["run_id"] + ".json")
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["summary"] = "invalid"
        path.write_text(json.dumps(stored), encoding="utf-8")
        reopened = hub.AgentHub(home=self.home, agent_loader=lambda: self.agents)
        try:
            self.assertEqual(reopened.get_run(good["run_id"])["run_id"], good["run_id"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["summary"], "invalid")
        finally:
            reopened.close()

    def test_message_validation_scans_events_once(self):
        self.agents["beta"] = {"timeout": 5}
        entered, release = threading.Event(), threading.Event()
        def executor(_, agent_id, *args):
            if agent_id == "alpha":
                entered.set()
                release.wait(3)
            return "ok"
        self.instance._executor = executor
        accepted = self.instance.create_run("work", ["alpha", "beta"])
        self.assertTrue(entered.wait(2))
        task = self.instance.get_task(accepted["accepted_tasks"][0]["task_id"])
        context = self.instance.sender_context_for_task(task["task_id"])
        envelope = {"schema_version": 1, "message_id": hub._id(), "run_id": accepted["run_id"],
            "task_id": task["task_id"], "parent_task_id": None, "kind": "message",
            "from_agent_id": "alpha", "to_agent_id": "beta", "relation": "worker_worker",
            "body": "hello", "execute": False, "created_at": "2026-09-25T00:00:00Z"}
        try:
            with patch.object(self.instance, "_indexed_events_locked", wraps=self.instance._indexed_events_locked) as scan:
                self.instance.validate_envelope(envelope, context, accepted["run_id"])
                self.assertEqual(scan.call_count, 1)
        finally:
            release.set()

    def test_windows_sid_parser_uses_ascii_bytes_and_rejects_ambiguous_output(self):
        root = self.home / "hub"
        sid = b"S-1-5-21-100-200-300-400"
        calls = []
        def command(argv, **kwargs):
            self.assertTrue(kwargs["check"])
            self.assertTrue(kwargs["capture_output"])
            self.assertNotIn("text", kwargs)
            calls.append(argv)
            if argv[0] == "whoami.exe":
                return Mock(stdout=b'"\xc3\xff\xd6\xd0\xce\xc4","' + sid + b'"\r\n')
            return Mock(stdout=b"done")
        with patch.object(hub.subprocess, "run", side_effect=command):
            hub._secure_hub_storage(root)
        self.assertEqual(len(calls), 5)
        self.assertIn("*" + sid.decode("ascii") + ":F", calls[3])
        with patch.object(hub.subprocess, "run", return_value=Mock(stdout=b'"user","invalid"\r\n')):
            with self.assertRaises(OSError):
                hub._secure_hub_storage(root)

    def test_provider_output_limit_field_is_bounded(self):
        base = {"providers": [{"id": "p", "protocol": "openai-chat-completions",
            "base_url": "https://example.test/v1", "output_limit_field": "max_completion_tokens"},
            {"id": "local", "protocol": "ollama-chat", "base_url": "http://127.0.0.1:11434",
                "allow_insecure_loopback": True}],
            "provider_agents": [], "templates": [], "approved_template_ids": []}
        result = self.instance.put_agent_registry(base)
        self.assertEqual(result["providers"][0]["output_limit_field"], "max_completion_tokens")
        self.assertNotIn("output_limit_field", result["providers"][1])
        public_rows = [{key: value for key, value in row.items() if key != "has_api_key"}
            for row in result["providers"]]
        self.assertEqual(len(self.instance.put_agent_registry({**base, "providers": public_rows})["providers"]), 2)
        for bad in ("arbitrary", "__dict__", None):
            base["providers"][0]["output_limit_field"] = bad
            with self.assertRaises(hub.HubError):
                self.instance.put_agent_registry(base)


if __name__ == "__main__":
    unittest.main()
