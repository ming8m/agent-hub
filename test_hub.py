import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path

import hub


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-hub-test-")
        self.home = Path(self.temp.name) / "agent-comm-home"
        os.environ["AGENT_COMM_HOME"] = str(self.home)
        self.registry = {"alpha": {"timeout": 30}, "beta": {"timeout": 30}, "gamma": {"timeout": 30}}
        self.instance = hub.AgentHub(home=self.home, agent_loader=lambda: self.registry,
            executor=lambda agents, agent_id, prompt, timeout: f"{agent_id}:{prompt}", max_workers=4)

    def tearDown(self):
        self.instance.close()
        self.temp.cleanup()

    def wait_done(self, run_id):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            run = self.instance.get_run(run_id)
            if run["status"] == "completed":
                return run
            time.sleep(0.01)
        self.fail("run did not complete")

    def test_private_storage_permissions_cover_existing_and_new_records(self):
        root = self.home / "hub"
        if os.name != "nt":
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "config.json").stat().st_mode & 0o777, 0o600)

            legacy = root / "legacy" / "prompt.json"
            legacy.parent.mkdir()
            legacy.write_text("private", encoding="utf-8")
            legacy.chmod(0o644)
            legacy.parent.chmod(0o755)
            hub._secure_hub_storage(root)
            self.assertEqual(legacy.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(legacy.stat().st_mode & 0o777, 0o600)
        else:
            import subprocess
            acl = subprocess.run(["icacls.exe", str(root)], check=True,
                capture_output=True, text=True).stdout
            # icacls resolves the SID to its localized account name for display.
            self.assertIn("(OI)(CI)(F)", acl)
            self.assertNotIn("Everyone", acl)
            self.assertIn("(F)", subprocess.run(["icacls.exe", str(root / ".hub.lock")],
                check=True, capture_output=True, text=True).stdout)

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail("condition did not become true")

    def wait_run_state(self, run_id, area, states):
        return self.wait_until(lambda: self._run_if_state(run_id, area, states))

    def _run_if_state(self, run_id, area, states):
        run = self.instance.get_run(run_id)
        return run if run[area]["status"] in states else None

    @staticmethod
    def plan(*, dependent=False):
        tasks = [{"task_id": "draft", "agent_id": "beta", "title": "Draft", "prompt": "Optimized worker instruction", "depends_on": []}]
        if dependent:
            tasks.append({"task_id": "review", "agent_id": "gamma", "title": "Review", "prompt": "Review the draft", "depends_on": ["draft"]})
        return json.dumps({"summary": "Delegate the task", "tasks": tasks, "questions": []})

    def test_parallel_tasks_persist_independent_full_responses(self):
        results = {"alpha": "A" * 5000, "beta": "beta output"}
        self.instance._executor = lambda agents, agent_id, prompt, timeout: results[agent_id]
        accepted = self.instance.create_run("work", ["alpha", "beta"])
        run = self.wait_done(accepted["run_id"])
        self.assertEqual([task["status"] for task in run["tasks"]], ["succeeded", "succeeded"])
        task = run["tasks"][0]
        self.assertEqual(self.instance.get_response(task["response_id"], run["run_id"], task["task_id"]), results["alpha"])
        reopened = hub.AgentHub(home=self.home, agent_loader=lambda: self.registry)
        self.assertEqual(reopened.get_run(run["run_id"])["tasks"][1]["response_id"], run["tasks"][1]["response_id"])
        reopened.close()

    def test_deadline_reminds_once_and_late_result_is_kept(self):
        started, release = threading.Event(), threading.Event()
        def slow_executor(agents, agent_id, prompt, timeout):
            started.set()
            release.wait(4)
            return "arrived late"
        self.instance._executor = slow_executor
        run_id = self.instance.create_run("slow", ["alpha"], deadline_seconds=1)["run_id"]
        self.assertTrue(started.wait(2))
        self.assertTrue(self.instance.check_deadlines(now=time.time() + 5))
        self.assertEqual(self.instance.check_deadlines(now=time.time() + 10), [])
        self.assertTrue(self.instance.get_run(run_id)["tasks"][0]["overdue"])
        release.set()
        run = self.wait_done(run_id)
        self.assertEqual(run["tasks"][0]["status"], "succeeded")
        self.assertEqual(self.instance.get_response(run["tasks"][0]["response_id"], run_id, run["tasks"][0]["task_id"]), "arrived late")
        self.assertEqual(sum(e["kind"] == "deadline" for e in self.instance.list_events(run_id)), 1)

    def test_executor_failure_isolated_to_its_task(self):
        def executor(agents, agent_id, prompt, timeout):
            if agent_id == "alpha":
                raise RuntimeError("agent failed")
            return "ok"
        self.instance._executor = executor
        run = self.wait_done(self.instance.create_run("work", ["alpha", "beta"])["run_id"])
        self.assertEqual([task["status"] for task in run["tasks"]], ["failed", "succeeded"])
        self.assertEqual(run["status"], "completed")

    def test_idempotency_and_per_run_event_cursor(self):
        first = self.instance.create_run("work", ["alpha"], idempotency_key="retry-1")
        second = self.instance.create_run("work", ["alpha"], idempotency_key="retry-1")
        self.assertEqual(first["run_id"], second["run_id"])
        events = self.instance.list_events(first["run_id"])
        self.assertEqual([e["event_id"] for e in events], sorted(e["event_id"] for e in events))
        self.assertGreater(self.instance.list_events(first["run_id"], after=events[0]["event_id"])[0]["event_id"], events[0]["event_id"])

    def test_event_index_rebuilds_legacy_and_tolerates_bad_lines_and_crash_tail(self):
        run_id = self.instance.create_run("index migration", ["alpha"])["run_id"]
        log_path = self.instance._events_path
        self.instance.close()
        rows = [
            {"event_id": 41, "run_id": run_id, "kind": "status", "relation": "system"},
            {"event_id": 43, "run_id": run_id, "kind": "message", "relation": "worker_worker"},
        ]
        # Legacy logs can contain malformed terminated rows and a valid append
        # that reached disk before its trailing newline/index/meta update.
        log_path.write_bytes((json.dumps(rows[0]) + "\n{bad json}\n" + json.dumps(rows[1])).encode("utf-8"))
        for path in self.instance._event_indexes_dir.glob("*.idx"):
            path.unlink()
        reopened = hub.AgentHub(home=self.home, agent_loader=lambda: self.registry)
        self.instance = reopened
        events = reopened.list_events(run_id)
        self.assertEqual([event["event_id"] for event in events], [41, 43])
        self.assertTrue(log_path.read_bytes().endswith(b"\n"))
        with reopened._locked():
            next_event = reopened._event_locked(run_id, "status")
        self.assertEqual(next_event["event_id"], 44)

    def test_concurrent_event_appends_keep_ids_unique_and_index_complete(self):
        run_id = self.instance.create_run("concurrent events", ["alpha"])["run_id"]

        def append(_):
            with self.instance._locked():
                return self.instance._event_locked(run_id, "status")["event_id"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            event_ids = list(pool.map(append, range(80)))
        self.assertEqual(len(event_ids), len(set(event_ids)))
        indexed = self.instance.list_events(run_id, limit=1000)
        self.assertEqual(len({event["event_id"] for event in indexed}), len(indexed))
        self.assertTrue(set(event_ids).issubset({event["event_id"] for event in indexed}))

    def test_envelope_validation_and_explicit_child_dispatch(self):
        started, release = threading.Event(), threading.Event()
        def holding_executor(agents, agent_id, prompt, timeout):
            if agent_id == "alpha":
                started.set()
                release.wait(3)
            return "done"
        self.instance._executor = holding_executor
        # beta is an explicit participant, so alpha may address it in this run.
        run_id = self.instance.create_run("work", ["alpha", "beta"])["run_id"]
        self.assertTrue(started.wait(2))
        parent = self.instance.get_run(run_id)["tasks"][0]
        sender_context = self.instance.sender_context_for_task(parent["task_id"])
        envelope = {"schema_version": 1, "message_id": hub._id(), "run_id": run_id,
            "task_id": parent["task_id"], "parent_task_id": parent["parent_task_id"], "kind": "message",
            "from_agent_id": "alpha", "to_agent_id": "beta", "relation": "worker_worker",
            "body": "Please inspect this", "execute": True, "created_at": "2026-09-25T00:00:00Z"}
        with self.assertRaises(hub.HubError):
            self.instance.validate_envelope(envelope, run_id=run_id)
        with self.assertRaises(hub.HubError):
            self.instance.validate_envelope({**envelope, "from_agent_id": "gamma"}, sender_context, run_id)
        result = self.instance.validate_envelope(envelope, sender_context, run_id)
        self.assertEqual(result["task"]["parent_task_id"], parent["task_id"])
        self.assertEqual(result["task"]["prompt"], envelope["body"])
        # Registered agents outside the user-selected run participants cannot receive data.
        with self.assertRaises(hub.HubError) as excluded:
            self.instance.validate_envelope({**envelope, "message_id": hub._id(), "to_agent_id": "gamma"},
                                            sender_context, run_id)
        self.assertEqual(excluded.exception.code, "unknown_agent")
        with self.assertRaises(hub.HubError):
            self.instance.validate_envelope({**envelope, "message_id": "../escape"}, sender_context, run_id)
        # Same capability and message ID are safe to retry, including concurrently.
        copies = []
        threads = [threading.Thread(target=lambda: copies.append(
            self.instance.validate_envelope(envelope, sender_context, run_id))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual(len(copies), 2)
        self.assertEqual(copies[0]["task"]["task_id"], copies[1]["task"]["task_id"])
        with self.assertRaises(hub.HubError):
            self.instance.validate_envelope({**envelope, "body": "different"}, sender_context, run_id)
        with self.assertRaises(hub.HubError):
            self.instance.validate_envelope({**envelope, "body": "bad\ud800text", "message_id": hub._id()}, sender_context, run_id)
        with self.assertRaises(hub.HubError):
            self.instance.validate_envelope({**envelope, "schema_version": True, "message_id": hub._id()}, sender_context, run_id)
        release.set()
        with self.assertRaises(hub.HubError):
            self.instance.get_response("../config", run_id, parent["task_id"])

    def test_recovery_resumes_queued_and_never_retries_running(self):
        completed_id = self.instance.create_run("seed", ["alpha", "beta"])["run_id"]
        run = self.wait_done(completed_id)
        with self.instance._locked():
            running = self.instance._read_json(self.instance._record_path(self.instance.tasks_dir, run["tasks"][0]["task_id"]))
            running["status"] = "running"
            running["completed_at"] = None
            self.instance._atomic_json(self.instance._record_path(self.instance.tasks_dir, running["task_id"]), running)
            queued = self.instance._create_task_locked(completed_id, "beta", "safe queued retry", sequence=2)
            record = self.instance._read_json(self.instance._record_path(self.instance.runs_dir, completed_id))
            record["task_ids"].append(queued["task_id"])
            self.instance._atomic_json(self.instance._record_path(self.instance.runs_dir, completed_id), record)
        # Model a process restart only after all old executor callbacks have exited.
        self.instance.close()
        calls = []
        recovered = hub.AgentHub(home=self.home, agent_loader=lambda: self.registry,
            executor=lambda agents, agent_id, prompt, timeout: calls.append((agent_id, prompt)) or "recovered", max_workers=2)
        try:
            self.assertEqual(recovered.get_task(running["task_id"])["status"], "interrupted")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and recovered.get_task(queued["task_id"])["status"] == "queued":
                time.sleep(0.01)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], "beta")
            self.assertTrue(calls[0][1].startswith("safe queued retry\n\nAgent Hub communication is optional."))
        finally:
            recovered.close()

    def test_response_access_requires_matching_run_and_task(self):
        run_id = self.instance.create_run("work", ["alpha"])["run_id"]
        task = self.wait_done(run_id)["tasks"][0]
        with self.assertRaises(hub.HubError):
            self.instance.get_response(task["response_id"])
        with self.assertRaises(hub.HubError):
            self.instance.get_response(task["response_id"], hub._id(), task["task_id"])
        self.assertTrue(self.instance.get_response(task["response_id"], run_id, task["task_id"]).startswith("alpha:work"))

    def test_output_size_and_invalid_unicode_fail_without_writing_response(self):
        self.instance._executor = lambda *args: "x" * (hub.MAX_RESPONSE_BYTES + 1)
        run_id = self.instance.create_run("large", ["alpha"])["run_id"]
        task = self.wait_done(run_id)["tasks"][0]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error"]["category"], "output_too_large")
        self.assertIsNone(task["response_id"])
        self.instance._executor = lambda *args: "bad\ud800text"
        run_id = self.instance.create_run("unicode", ["alpha"])["run_id"]
        task = self.wait_done(run_id)["tasks"][0]
        self.assertEqual(task["error"]["category"], "invalid_output")

    def test_summary_coverage_and_late_result_cas(self):
        self.instance.put_config({"summary_policy": "manual"})
        started, finish_alpha, adapter_started, finish_adapter = (threading.Event() for _ in range(4))
        def executor(agents, agent_id, prompt, timeout):
            if agent_id == "alpha":
                started.set()
                finish_alpha.wait(3)
                return "late alpha"
            raise RuntimeError("beta failed")
        self.instance._executor = executor
        run_id = self.instance.create_run("sum", ["alpha", "beta"])["run_id"]
        self.assertTrue(started.wait(2))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = self.instance.get_run(run_id)
            if any(t["agent_id"] == "beta" and t["status"] == "failed" for t in state["tasks"]):
                break
            time.sleep(0.01)
        with self.assertRaises(hub.HubError):
            self.instance.summarize(run_id, adapter=lambda *a, **k: "summary")
        def adapter(prompt, outputs, partial):
            adapter_started.set()
            finish_adapter.wait(3)
            self.assertEqual({item["agent_id"] for item in outputs}, {"beta"})
            return "partial view"
        result = []
        thread = threading.Thread(target=lambda: result.append(self.instance.summarize(run_id, adapter=adapter, partial=True)))
        thread.start()
        self.assertTrue(adapter_started.wait(2))
        finish_alpha.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if any(t["agent_id"] == "alpha" and t["status"] == "succeeded" for t in self.instance.get_run(run_id)["tasks"]):
                break
            time.sleep(0.01)
        finish_adapter.set()
        thread.join(3)
        self.assertEqual(result[0]["status"], "stale")
        final_run = self.wait_done(run_id)
        full = self.instance.summarize(run_id, adapter=lambda prompt, outputs, partial: ",".join(sorted(i["agent_id"] for i in outputs)))
        self.assertEqual(full["status"], "ready")
        self.assertEqual(set(full["task_ids"]), {t["task_id"] for t in final_run["tasks"]})
        self.assertEqual(full["content"], "alpha,beta")

    def test_orchestrator_auto_plan_validates_and_dispatches_dependencies(self):
        self.instance.put_config({"orchestrator_agent_id": "alpha", "orchestrator_enabled": True,
                                  "summary_policy": "manual"})
        calls = []
        def executor(agents, agent_id, prompt, timeout):
            calls.append((agent_id, prompt))
            if agent_id == "alpha":
                return self.plan(dependent=True)
            return f"result from {agent_id}"
        self.instance._executor = executor
        accepted = self.instance.create_run("user task", mode="orchestrated", dispatch_policy="auto")
        run = self.wait_done(accepted["run_id"])
        self.assertEqual(run["mode"], "orchestrated")
        self.assertEqual(run["orchestrator_agent_id"], "alpha")
        self.assertEqual(run["dispatch_policy"], "auto")
        self.assertEqual(run["plan"]["status"], "completed")
        workers = [task for task in run["tasks"] if task["role"] == "worker"]
        self.assertEqual([task["agent_id"] for task in workers], ["beta", "gamma"])
        self.assertEqual(workers[1]["depends_on_task_ids"], [workers[0]["task_id"]])
        self.assertIn("Optimized worker instruction", workers[0]["prompt"])
        self.assertEqual([agent for agent, _ in calls].count("gamma"), 1)
        events = self.instance.list_events(run["run_id"], relation="orchestrator_worker")
        plan_messages = [event for event in events if event.get("source") == "plan_dispatch"]
        self.assertEqual({event["to_agent_id"] for event in plan_messages}, {"beta", "gamma"})
        self.assertTrue(all(event["from_agent_id"] == "alpha" and event["execute"] for event in plan_messages))
        self.assertTrue(all(event["body"] == event["task_prompt"] for event in plan_messages))

    def test_orchestrator_preview_requires_approval_and_keeps_snapshot(self):
        self.instance.put_config({"orchestrator_agent_id": "alpha", "orchestrator_enabled": True,
                                  "summary_policy": "manual"})
        worker_calls = []
        def executor(agents, agent_id, prompt, timeout):
            if agent_id == "alpha":
                return self.plan()
            worker_calls.append(agent_id)
            return "worker done"
        self.instance._executor = executor
        run_id = self.instance.create_run("preview me", mode="orchestrated", dispatch_policy="preview")["run_id"]
        preview = self.wait_run_state(run_id, "plan", {"awaiting_approval"})
        self.assertEqual(preview["orchestrator_agent_id"], "alpha")
        self.assertEqual([task["role"] for task in preview["tasks"]], ["orchestrator"])
        self.assertEqual(worker_calls, [])
        self.instance.put_config({"orchestrator_agent_id": "gamma"})
        dispatched = self.instance.approve_plan(run_id)
        self.assertEqual(dispatched["orchestrator_agent_id"], "alpha")
        run = self.wait_done(run_id)
        self.assertEqual(worker_calls, ["beta"])
        self.assertEqual(run["plan"]["status"], "completed")

    def test_invalid_orchestrator_plan_fails_without_worker_broadcast(self):
        self.instance.put_config({"orchestrator_agent_id": "alpha", "orchestrator_enabled": True,
                                  "summary_policy": "manual"})
        calls = []
        self.instance._executor = lambda agents, agent_id, prompt, timeout: calls.append(agent_id) or "not JSON"
        accepted = self.instance.create_run("unsafe plan", mode="orchestrated", dispatch_policy="auto")
        run = self.wait_run_state(accepted["run_id"], "plan", {"failed"})
        self.assertEqual(run["status"], "failed")
        self.assertEqual([task["role"] for task in run["tasks"]], ["orchestrator"])
        self.assertEqual(calls, ["alpha"])
        self.assertEqual(run["tasks"][0]["error"]["category"], "invalid_plan")
        self.assertIsNotNone(run["tasks"][0]["response_id"])

    def test_configured_summary_agent_summarizes_success_and_failure_outcomes(self):
        self.instance.put_config({"summary_agent_id": "gamma", "summary_policy": "auto"})
        def executor(agents, agent_id, prompt, timeout):
            if agent_id == "gamma":
                source = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1])
                if source.get("scope") == "task":
                    return json.dumps({"summary": "A concise worker result."})
                self.assertIn("execution_error", prompt)
                return json.dumps({"summary": "One worker succeeded and one failed."})
            if agent_id == "alpha":
                return "alpha output"
            raise RuntimeError("synthetic failure")
        self.instance._executor = executor
        run_id = self.instance.create_run("summarize", ["alpha", "beta"])["run_id"]
        self.wait_done(run_id)
        run = self.wait_run_state(run_id, "summary", {"ready", "failed", "partial", "stale"})
        self.assertEqual(run["summary"]["status"], "ready")
        self.assertEqual(run["summary"]["coverage"], "complete")
        self.assertEqual(len(run["summary"]["task_ids"]), 2)
        self.assertEqual(run["summary"]["content"], "One worker succeeded and one failed.")
        self.wait_until(lambda: all(t["summary"]["status"] != "pending" for t in self.instance.get_run(run_id)["tasks"]))
        for task in self.instance.get_run(run_id)["tasks"]:
            if task["status"] == "succeeded":
                self.assertEqual(task["summary"]["status"], "ready")
                self.assertEqual(task["summary"]["source_agent_id"], "gamma")
                self.assertEqual(task["summary"]["source_response_id"], task["response_id"])
                self.assertIsNotNone(task["summary"]["generated_at"])
            else:
                self.assertEqual(task["summary"]["status"], "unavailable")

    def test_auto_summary_failure_is_visible_and_keeps_task_results(self):
        self.instance.put_config({"summary_agent_id": "gamma", "summary_policy": "auto"})
        def executor(agents, agent_id, prompt, timeout):
            if agent_id == "gamma":
                raise RuntimeError("synthetic summary failure")
            return "worker result"
        self.instance._executor = executor
        run_id = self.instance.create_run("summarize", ["alpha"])["run_id"]
        self.wait_done(run_id)
        run = self.wait_run_state(run_id, "summary", {"failed"})
        self.assertEqual(run["tasks"][0]["status"], "succeeded")
        self.assertEqual(run["summary"]["error"]["category"], "summary_error")
        self.assertEqual(self.instance.get_response(run["tasks"][0]["response_id"], run_id, run["tasks"][0]["task_id"]), "worker result")

    def test_deadline_partial_summary_becomes_stale_and_full_summary_replaces_it(self):
        self.instance.put_config({"summary_agent_id": "gamma", "summary_policy": "auto"})
        alpha_started, release_alpha, partial_started, release_partial = (threading.Event() for _ in range(4))
        summary_calls = []
        def executor(agents, agent_id, prompt, timeout):
            if agent_id == "alpha":
                alpha_started.set()
                release_alpha.wait(4)
                return "late alpha"
            if agent_id == "beta":
                return "early beta"
            source = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1])
            if source.get("scope") == "task":
                return json.dumps({"summary": "Task result summary"})
            summary_calls.append(prompt)
            if len(summary_calls) == 1:
                partial_started.set()
                release_partial.wait(4)
                return json.dumps({"summary": "Partial summary"})
            return json.dumps({"summary": "Complete summary"})
        self.instance._executor = executor
        run_id = self.instance.create_run("deadline summary", ["alpha", "beta"], deadline_seconds=1)["run_id"]
        self.assertTrue(alpha_started.wait(2))
        self.wait_until(lambda: next((t for t in self.instance.get_run(run_id)["tasks"]
            if t["agent_id"] == "beta" and t["status"] == "succeeded"), None))
        self.instance.check_deadlines(now=time.time() + 5)
        self.assertTrue(partial_started.wait(3))
        release_alpha.set()
        self.wait_until(lambda: next((t for t in self.instance.get_run(run_id)["tasks"]
            if t["agent_id"] == "alpha" and t["status"] == "succeeded"), None))
        release_partial.set()
        self.wait_done(run_id)
        run = self.wait_until(lambda: self._complete_summary(run_id))
        self.assertEqual(run["summary"]["content"], "Complete summary")
        events = self.instance.list_events(run_id)
        self.assertTrue(any(e["kind"] == "summary" and e.get("state") == "stale" for e in events))
        self.assertEqual(len(run["summary"]["task_ids"]), 2)

    def _complete_summary(self, run_id):
        run = self.instance.get_run(run_id)
        return run if run["summary"]["status"] == "ready" and run["summary"].get("coverage") == "complete" else None

    def test_no_summary_adapter_is_explicitly_unavailable(self):
        run_id = self.instance.create_run("work", ["alpha"])["run_id"]
        self.wait_done(run_id)
        self.wait_until(lambda: self.instance.get_run(run_id)["tasks"][0]["summary"]["status"] == "unavailable")
        result = self.instance.summarize(run_id)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["content"])
        task = self.instance.get_run(run_id)["tasks"][0]
        self.assertEqual(task["summary"]["status"], "unavailable")
        self.assertEqual(task["summary"]["reason"], "No summary agent configured")

    def test_each_task_summary_is_async_and_scoped_to_its_own_response(self):
        self.instance.put_config({"summary_agent_id": "gamma", "summary_policy": "manual"})
        summaries_started = []
        summaries_release = threading.Event()
        def executor(agents, agent_id, prompt, timeout):
            if agent_id != "gamma":
                return f"full result from {agent_id}"
            source = json.loads(prompt.split("SOURCE_JSON:\n", 1)[1])
            self.assertEqual(source["scope"], "task")
            self.assertEqual(len(source["results"]), 1)
            row = source["results"][0]
            summaries_started.append((row["task_id"], row["output"]))
            summaries_release.wait(3)
            return json.dumps({"summary": f"Brief {row['agent_id']}"})
        self.instance._executor = executor
        run_id = self.instance.create_run("two replies", ["alpha", "beta"])["run_id"]
        self.wait_until(lambda: len(summaries_started) == 2)
        run = self.instance.get_run(run_id)
        self.assertTrue(all(task["status"] == "succeeded" for task in run["tasks"]))
        self.assertTrue(all(task["summary"]["status"] == "pending" for task in run["tasks"]))
        self.assertEqual({output for _, output in summaries_started}, {"full result from alpha", "full result from beta"})
        summaries_release.set()
        self.wait_until(lambda: all(t["summary"]["status"] == "ready" for t in self.instance.get_run(run_id)["tasks"]))
        final = self.instance.get_run(run_id)
        self.assertEqual({task["summary"]["content"] for task in final["tasks"]}, {"Brief alpha", "Brief beta"})
        self.assertEqual({task["summary"]["source_response_id"] for task in final["tasks"]},
                         {task["response_id"] for task in final["tasks"]})

    def test_task_summary_failure_is_visible_and_restart_never_replays_pending(self):
        self.instance.put_config({"summary_agent_id": "gamma", "summary_policy": "manual"})
        calls = []
        def executor(agents, agent_id, prompt, timeout):
            calls.append(agent_id)
            if agent_id == "gamma":
                raise RuntimeError("summary unavailable")
            return "complete worker answer"
        self.instance._executor = executor
        run_id = self.instance.create_run("summary error", ["alpha"])["run_id"]
        self.wait_done(run_id)
        task = self.wait_until(lambda: (lambda t: t if t["summary"]["status"] == "failed" else None)(self.instance.get_run(run_id)["tasks"][0]))
        self.assertEqual(task["summary"]["error"]["category"], "summary_error")
        self.assertEqual(self.instance.get_response(task["response_id"], run_id, task["task_id"]), "complete worker answer")

        task["summary"].update(status="pending", generation_id=hub._id(), source_agent_id="gamma",
                               source_response_id=task["response_id"])
        self.instance._atomic_json(self.instance._record_path(self.instance.tasks_dir, task["task_id"]), task)
        recovered = hub.AgentHub(home=self.home, agent_loader=lambda: self.registry,
            executor=lambda *args: calls.append("unexpected replay") or '{"summary":"bad"}')
        try:
            restarted = recovered.get_task(task["task_id"])
            self.assertEqual(restarted["summary"]["status"], "failed")
            self.assertEqual(restarted["summary"]["error"]["category"], "interrupted")
            self.assertEqual(calls, ["alpha", "gamma"])
        finally:
            recovered.close()

    def test_late_task_summary_cannot_overwrite_a_newer_generation(self):
        self.instance.put_config({"summary_agent_id": "gamma", "summary_policy": "manual"})
        started, release = threading.Event(), threading.Event()
        def executor(agents, agent_id, prompt, timeout):
            if agent_id == "gamma":
                started.set()
                release.wait(3)
                return json.dumps({"summary": "old generation"})
            return "full response"
        self.instance._executor = executor
        run_id = self.instance.create_run("CAS summary", ["alpha"])["run_id"]
        task = self.wait_until(lambda: next((t for t in self.instance.get_run(run_id)["tasks"]
            if t["summary"]["status"] == "pending"), None))
        self.assertTrue(started.wait(2))
        future = self.instance._task_summary_futures[task["task_id"]]
        with self.instance._locked():
            current = self.instance._read_json(self.instance._record_path(self.instance.tasks_dir, task["task_id"]))
            current["summary"].update(status="ready", content="new generation", generation_id=hub._id(), generated_at="2026-01-01T00:00:00+00:00")
            self.instance._atomic_json(self.instance._record_path(self.instance.tasks_dir, task["task_id"]), current)
        release.set()
        self.wait_until(future.done)
        final = self.instance.get_task(task["task_id"])["summary"]
        self.assertEqual(final["status"], "ready")
        self.assertEqual(final["content"], "new generation")

    def test_worker_messages_round_trip_with_depth_limit_and_preserved_answers(self):
        self.instance.put_config({"max_depth": 2, "max_tasks": 8, "max_messages_per_task": 4,
                                  "summary_policy": "manual"})
        def marked(answer, messages):
            return "AGENT_HUB_RESULT\n" + json.dumps({"final_answer": answer, "messages": messages})
        def executor(agents, agent_id, prompt, timeout):
            if agent_id == "alpha" and "A root" in prompt:
                return marked("alpha full result", [{"recipient": "beta", "task": "B child", "body": "from A", "execute": True}])
            if agent_id == "beta" and "A root" in prompt:
                return "beta root participant"
            if agent_id == "beta":
                return marked("beta full result", [{"recipient": "alpha", "task": "A child", "body": "from B", "execute": True}])
            if agent_id == "alpha":
                return marked("alpha child result", [{"recipient": "beta", "task": "too deep", "body": "stop", "execute": True}])
            return "unexpected"
        self.instance._executor = executor
        run_id = self.instance.create_run("A root", ["alpha", "beta"]) ["run_id"]
        run = self.wait_done(run_id)
        tasks = run["tasks"]
        self.assertEqual([(task["agent_id"], task["depth"], task["status"]) for task in tasks],
                         [("alpha", 0, "succeeded"), ("beta", 0, "succeeded"),
                          ("beta", 1, "succeeded"), ("alpha", 2, "succeeded")])
        self.assertEqual(self.instance.get_response(tasks[0]["response_id"], run_id, tasks[0]["task_id"]), "alpha full result")
        self.assertEqual(self.instance.get_response(tasks[2]["response_id"], run_id, tasks[2]["task_id"]), "beta full result")
        self.assertTrue(tasks[3]["protocol_errors"])
        events = self.instance.list_events(run_id)
        messages = [event for event in events if event["kind"] == "message"]
        self.assertEqual(len(messages), 2)
        self.assertEqual([(e["from_agent_id"], e["to_agent_id"], e["relation"]) for e in messages],
                         [("alpha", "beta", "worker_worker"), ("beta", "alpha", "worker_worker")])
        self.assertEqual([(e["body"], e["task_prompt"]) for e in messages],
                         [("from A", "B child"), ("from B", "A child")])
        self.assertTrue(any(e["kind"] == "rejected" and e.get("reason") == "agent_message_protocol" for e in events))

    def test_plain_worker_stdout_remains_full_text_without_message_events(self):
        full_output = "ordinary answer\n" + ("full detail " * 1000)
        self.instance._executor = lambda *args: full_output
        run_id = self.instance.create_run("plain", ["alpha"])["run_id"]
        task = self.wait_done(run_id)["tasks"][0]
        self.assertEqual(self.instance.get_response(task["response_id"], run_id, task["task_id"]), full_output)
        events = self.instance.list_events(run_id)
        self.assertFalse(any(event["kind"] == "message" for event in events))
        self.assertFalse(any(event.get("reason") == "agent_message_rejected" for event in events))

    def test_config_rejects_unknown_agent_and_reports_stale_setting(self):
        with self.assertRaises(hub.HubError) as error:
            self.instance.put_config({"orchestrator_agent_id": "missing", "orchestrator_enabled": True})
        self.assertEqual(error.exception.code, "unknown_agent")
        self.instance.put_config({"orchestrator_agent_id": "alpha"})
        self.instance.put_config({"orchestrator_enabled": True})
        del self.registry["alpha"]
        self.assertEqual(self.instance.get_config()["orchestrator_state"], "invalid")


if __name__ == "__main__":
    unittest.main()
