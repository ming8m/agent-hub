import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import hub


class CollaborationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-collaboration-")
        self.home = Path(self.temp.name)
        self.agents = {name: {"timeout": 10} for name in ("alpha", "beta", "gamma")}
        self.calls = []
        self.plan = {"summary": "Split review", "tasks": [
            {"task_id": "left", "agent_id": "beta", "title": "Left", "prompt": "Check left", "depends_on": [], "group_id": "team"},
            {"task_id": "right", "agent_id": "gamma", "title": "Right", "prompt": "Check right", "depends_on": [], "group_id": "team"}],
            "groups": [{"group_id": "team", "leader_agent_id": "beta", "member_agent_ids": ["beta", "gamma"]}]}
        self.hub = hub.AgentHub(home=self.home, agent_loader=lambda: self.agents,
                                executor=self.execute, max_workers=4)
        self.hub.put_config({"orchestrator_agent_id": "alpha", "orchestrator_enabled": True,
                             "summary_policy": "manual"})

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def execute(self, agents, agent_id, prompt, timeout):
        self.calls.append((agent_id, prompt))
        if "Analyze only SOURCE_JSON" in prompt:
            return json.dumps({"summary": "The source has one assignment", "evidence": [
                {"path": "src/main.py", "line_start": 1, "line_end": 1, "note": "assignment"}],
                "verified": False, "unknowns": []})
        if "Agent Hub planning component" in prompt:
            return json.dumps(self.plan)
        if "DISPUTE_JSON:\n" in prompt:
            data = json.loads(prompt.split("DISPUTE_JSON:\n", 1)[1])
            return json.dumps({"verdict": "resolved", "rationale": "Left has stronger evidence",
                "evidence_refs": [data["left_response_id"]],
                "selected_response_id": data["left_response_id"]})
        if "SOURCE_JSON:\n" in prompt:
            return json.dumps({"summary": "task result"})
        return "worker result from " + agent_id

    def collaboration(self, max_calls=12):
        return {"source": {"version": "v1", "files": [
            {"path": "src/main.py", "text": "x = 1\n"}]},
            "representative_agent_ids": ["alpha", "beta"], "arbiter_agent_id": "alpha",
            "limits": {"max_concurrent_tasks": 2, "max_context_bytes": 65536, "max_calls": max_calls}}

    def wait_for(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.01)
        self.fail("condition did not become true")

    def completed_run(self):
        run_id = self.hub.create_run("Review code", mode="orchestrated", dispatch_policy="auto",
                                     collaboration=self.collaboration())["run_id"]
        def terminal():
            run = self.hub.get_run(run_id)
            return run if run["status"] in {"completed", "failed"} else None
        return self.wait_for(terminal)

    def test_representatives_precede_plan_and_worker_receives_explanations(self):
        run = self.completed_run()
        self.assertEqual(run["status"], "completed")
        self.assertEqual([task["role"] for task in run["tasks"]],
                         ["representative", "representative", "orchestrator", "worker", "worker"])
        self.assertEqual([row["status"] for row in run["collaboration"]["representatives"]],
                         ["succeeded", "succeeded"])
        self.assertEqual(run["groups"][0]["leader_agent_id"], "beta")
        self.assertNotIn("_collaboration_source", run)
        self.assertNotIn("x = 1", json.dumps(run["collaboration"], ensure_ascii=False))
        self.assertTrue(all("REPRESENTATIVE_BRIEFS_JSON" in task["prompt"]
                            for task in run["tasks"] if task["role"] == "worker"))
        self.assertEqual(sum("Analyze only SOURCE_JSON" in prompt for _, prompt in self.calls), 2)

    def test_dispute_pins_responses_rejects_replay_and_resolves(self):
        run = self.completed_run()
        workers = [task for task in run["tasks"] if task["role"] == "worker"]
        payload = {"left_task_id": workers[0]["task_id"], "right_task_id": workers[1]["task_id"],
                   "arbiter_agent_id": "alpha", "claim": "They disagree", "evidence": [
                       {"path": "src/main.py", "line_start": 1, "line_end": 1, "note": "source line"}]}
        dispute = self.hub.create_dispute(run["run_id"], payload)
        self.assertEqual(self.hub.get_run(run["run_id"])["review_state"], "unresolved")
        with self.assertRaises(hub.HubError):
            self.hub.create_dispute(run["run_id"], payload)
        self.hub.review_dispute(run["run_id"], dispute["dispute_id"])
        def reviewed():
            row = self.hub.list_disputes(run["run_id"])[0]
            return row if row["status"] != "reviewing" else None
        resolved = self.wait_for(reviewed)
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["selected_response_id"], workers[0]["response_id"])
        self.assertEqual(resolved["rounds"][0]["verdict"]["evidence_refs"], [workers[0]["response_id"]])
        self.assertEqual(self.hub.get_run(run["run_id"])["review_state"], "resolved")
        with self.assertRaises(hub.HubError):
            self.hub.review_dispute(run["run_id"], dispute["dispute_id"])

    def test_invalid_representative_blocks_planner_without_fallback(self):
        self.hub._executor = lambda agents, agent_id, prompt, timeout: self.calls.append((agent_id, prompt)) or "bad response"
        run_id = self.hub.create_run("Review code", mode="orchestrated", dispatch_policy="auto",
                                     collaboration=self.collaboration())["run_id"]
        def failed():
            row = self.hub.get_run(run_id)
            return row if row["status"] == "failed" else None
        run = self.wait_for(failed)
        self.assertEqual(run["plan"]["status"], "failed")
        self.assertEqual([task["role"] for task in run["tasks"]],
                         ["representative", "representative", "orchestrator"])
        self.assertEqual(len(self.calls), 2)

    def test_two_review_rounds_require_new_evidence_then_human(self):
        run = self.completed_run()
        workers = [task for task in run["tasks"] if task["role"] == "worker"]
        dispute = self.hub.create_dispute(run["run_id"], {
            "left_task_id": workers[0]["task_id"], "right_task_id": workers[1]["task_id"],
            "arbiter_agent_id": "alpha", "claim": "Different answers", "evidence": [
                {"path": "src/main.py", "line_start": 1, "line_end": 1, "note": "initial"}]})
        original = self.hub._executor
        def request_evidence(agents, agent_id, prompt, timeout):
            if "DISPUTE_JSON:\n" in prompt:
                data = json.loads(prompt.split("DISPUTE_JSON:\n", 1)[1])
                return json.dumps({"verdict": "needs_evidence", "rationale": "Need more detail",
                    "evidence_refs": [data["left_response_id"], data["right_response_id"]]})
            return original(agents, agent_id, prompt, timeout)
        self.hub._executor = request_evidence
        self.hub.review_dispute(run["run_id"], dispute["dispute_id"])
        self.wait_for(lambda: self.hub.list_disputes(run["run_id"])[0]["status"] == "needs_evidence")
        with self.assertRaises(hub.HubError):
            self.hub.add_dispute_evidence(run["run_id"], dispute["dispute_id"], {"evidence": []})
        with self.assertRaises(hub.HubError):
            self.hub.add_dispute_evidence(run["run_id"], dispute["dispute_id"], {"evidence": [
                {"path": "src/main.py", "line_start": 1, "line_end": 1, "note": "initial"}]})
        self.hub.add_dispute_evidence(run["run_id"], dispute["dispute_id"], {"evidence": [
            {"path": "src/main.py", "line_start": 1, "line_end": 1, "note": "new verification"}]})
        self.hub.review_dispute(run["run_id"], dispute["dispute_id"])
        self.wait_for(lambda: self.hub.list_disputes(run["run_id"])[0]["status"] == "needs_human")
        current = self.hub.list_disputes(run["run_id"])[0]
        self.assertEqual(len(current["rounds"]), 2)
        with self.assertRaises(hub.HubError):
            self.hub.review_dispute(run["run_id"], dispute["dispute_id"])
        with self.assertRaises(hub.HubError):
            self.hub.decide_dispute(run["run_id"], dispute["dispute_id"],
                                    {"selected_response_id": [], "rationale": "bad"})
        decided = self.hub.decide_dispute(run["run_id"], dispute["dispute_id"],
                                          {"selected_response_id": workers[1]["response_id"],
                                           "rationale": "Human checked the source"})
        self.assertEqual(decided["resolution"]["source"], "human")
        self.assertEqual(decided["selected_response_id"], workers[1]["response_id"])

    def test_restart_marks_inflight_review_for_human_without_replay(self):
        run = self.completed_run()
        workers = [task for task in run["tasks"] if task["role"] == "worker"]
        dispute = self.hub.create_dispute(run["run_id"], {
            "left_task_id": workers[0]["task_id"], "right_task_id": workers[1]["task_id"],
            "arbiter_agent_id": "alpha", "claim": "Different answers", "evidence": [
                {"path": "src/main.py", "line_start": 1, "line_end": 1, "note": "initial"}]})
        with self.hub._locked():
            record = self.hub._read_json(self.hub._record_path(self.hub.runs_dir, run["run_id"]))
            record["disputes"][0]["status"] = "reviewing"
            self.hub._atomic_json(self.hub._record_path(self.hub.runs_dir, run["run_id"]), record)
        self.hub.close()
        before = len(self.calls)
        self.hub = hub.AgentHub(home=self.home, agent_loader=lambda: self.agents,
                                executor=self.execute, max_workers=4)
        recovered = self.hub.list_disputes(run["run_id"])[0]
        self.assertEqual(recovered["status"], "needs_human")
        self.assertEqual(self.hub.get_run(run["run_id"])["review_state"], "needs_human")
        self.assertEqual(len(self.calls), before)

    def test_dispute_blocks_inflight_summary_and_resolved_summary_excludes_loser(self):
        run = self.completed_run()
        workers = [task for task in run["tasks"] if task["role"] == "worker"]
        entered, release = threading.Event(), threading.Event()
        summaries = []
        def slow_adapter(prompt, results, partial):
            entered.set()
            release.wait(5)
            return "old summary"
        thread = threading.Thread(target=lambda: summaries.append(
            self.hub.summarize(run["run_id"], adapter=slow_adapter)))
        thread.start()
        self.assertTrue(entered.wait(5))
        dispute = self.hub.create_dispute(run["run_id"], {
            "left_task_id": workers[0]["task_id"], "right_task_id": workers[1]["task_id"],
            "arbiter_agent_id": "alpha", "claim": "Different answers", "evidence": [
                {"path": "src/main.py", "line_start": 1, "line_end": 1, "note": "initial"}]})
        release.set()
        thread.join(5)
        self.assertEqual(summaries[0]["status"], "blocked_review")
        self.assertEqual(self.hub.get_run(run["run_id"])["summary"]["status"], "blocked_review")
        seen = []
        self.hub.decide_dispute(run["run_id"], dispute["dispute_id"],
                                {"selected_response_id": workers[0]["response_id"],
                                 "rationale": "Human verified left"})
        summary = self.hub.summarize(run["run_id"], adapter=lambda prompt, results, partial:
            seen.extend(results) or "reviewed summary")
        self.assertEqual(summary["status"], "ready")
        self.assertEqual([row["task_id"] for row in seen], [workers[0]["task_id"]])

    def test_call_budget_and_actual_concurrency_limit(self):
        active = peak = 0
        lock = threading.Lock()
        base = self.execute
        def metered(agents, agent_id, prompt, timeout):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.03)
                return base(agents, agent_id, prompt, timeout)
            finally:
                with lock:
                    active -= 1
        self.hub._executor = metered
        collaboration = {"arbiter_agent_id": "alpha", "limits": {
            "max_concurrent_tasks": 1, "max_context_bytes": 65536, "max_calls": 10}}
        run_id = self.hub.create_run("Bound calls", mode="orchestrated", dispatch_policy="auto",
                                     collaboration=collaboration)["run_id"]
        self.wait_for(lambda: self.hub.get_run(run_id)["status"] == "completed")
        self.wait_for(lambda: all(task.get("summary", {}).get("status") != "pending"
            for task in self.hub.get_run(run_id)["tasks"]))
        self.assertEqual(peak, 1)
        self.assertLessEqual(self.hub.get_run(run_id)["collaboration"]["calls_used"], 10)

        before = len(self.calls)
        collaboration["limits"]["max_calls"] = 2
        limited_id = self.hub.create_run("Bound calls again", mode="orchestrated", dispatch_policy="auto",
                                         collaboration=collaboration)["run_id"]
        self.wait_for(lambda: self.hub.get_run(limited_id)["status"] == "completed")
        limited = self.hub.get_run(limited_id)
        self.assertEqual(limited["collaboration"]["calls_used"], 2)
        self.assertEqual(len(self.calls) - before, 2)
        self.assertTrue(any(task["status"] == "failed" for task in limited["tasks"]
                            if task["role"] == "worker"))


if __name__ == "__main__":
    unittest.main()
