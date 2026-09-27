import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import hub


class DiscussionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-discussion-")
        self.home = Path(self.temp.name)
        self.agents = {name: {"timeout": 10} for name in ("chair", "a", "b", "c")}
        self.calls = []
        self.hub = hub.AgentHub(home=self.home, agent_loader=lambda: self.agents,
                                executor=self.execute, max_workers=4)
        self.hub.put_config({"orchestrator_agent_id": "chair", "orchestrator_enabled": True,
                             "max_tasks": 8, "summary_policy": "auto"})

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def execute(self, agents, agent_id, prompt, timeout):
        self.calls.append((agent_id, prompt))
        data = json.loads(prompt.split("UNTRUSTED_RECORDS_JSON:\n", 1)[1])
        if "Independently analyze" in prompt:
            if agent_id == getattr(self, "fail_agent", None):
                raise RuntimeError("synthetic analyst failure")
            return json.dumps({"summary": agent_id, "proposal": "proposal " + agent_id,
                               "evidence": ["visible"], "risks": []})
        if "Compare the independent" in prompt:
            ids = [row["response_id"] for row in data["analyses"] if row["status"] == "succeeded"]
            return json.dumps({"summary": "comparison", "conflicts": [] if not getattr(self, "conflict", False)
                               else [{"conflict_id": "c1", "description": "different proposals", "response_ids": ids[:2]}]})
        if "Give one visible peer review" in prompt:
            return json.dumps({"summary": "review", "reviews": [
                {"conflict_id": cid, "position": "retain evidence", "evidence": ["visible"]}
                for cid in data["assigned_conflict_ids"]]})
        if "As discussion chair" in prompt:
            partial = any(row["status"] != "succeeded" for row in data["analyses"])
            return json.dumps({"summary": "final conclusion", "decisions": [
                {"conflict_id": row["conflict_id"], "status": "resolved", "resolution": "adopt a",
                 "adopted_response_ids": [row["response_ids"][0]],
                 "evidence_response_ids": row["response_ids"]}
                for row in data["conflicts"]], "unresolved": ["Analyst unavailable"] if partial else []})
        raise AssertionError("Unexpected prompt")

    def wait(self, run_id):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            run = self.hub.get_run(run_id)
            if run["discussion"]["status"] in {"ready", "needs_attention", "failed", "interrupted"}:
                return run
            time.sleep(0.02)
        self.fail("discussion did not terminate")

    def test_three_people_no_conflict_skips_review_and_summary_call(self):
        run_id = self.hub.create_run("Discuss", ["a", "b"], mode="discussion")["run_id"]
        run = self.wait(run_id)
        self.assertEqual(run["discussion"]["status"], "ready")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["summary"]["content"], "final conclusion")
        self.assertEqual(run["discussion"]["review_task_ids"], [])
        self.assertEqual(run["collaboration"]["calls_used"], 4)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.hub.summarize(run_id), run["summary"])

    def test_four_people_conflict_reviews_once_each(self):
        self.conflict = True
        run_id = self.hub.create_run("Discuss", ["a", "b", "c"], mode="discussion")["run_id"]
        run = self.wait(run_id)
        self.assertEqual(run["discussion"]["status"], "ready")
        self.assertEqual(len(run["discussion"]["review_task_ids"]), 2)
        self.assertEqual(run["collaboration"]["calls_used"], 7)
        self.assertEqual(len(self.calls), 7)
        self.assertEqual(run["discussion"]["final"]["decisions"][0]["status"], "resolved")

    def test_failed_analyst_keeps_partial_unknown_and_no_retry(self):
        self.fail_agent = "b"
        run_id = self.hub.create_run("Discuss", ["a", "b"], mode="discussion")["run_id"]
        run = self.wait(run_id)
        self.assertEqual(run["discussion"]["status"], "needs_attention")
        self.assertEqual(run["summary"]["status"], "partial")
        self.assertIn("Analyst unavailable", run["discussion"]["unresolved"])
        self.assertEqual(run["collaboration"]["calls_used"], 4)
        self.assertEqual(len(self.calls), 4)

    def test_uploaded_source_is_masked_and_invalid_roster_writes_nothing(self):
        with self.assertRaises(hub.HubError):
            self.hub.create_run("Discuss", ["a", "a"], mode="discussion")
        self.assertEqual(list(self.hub.runs_dir.glob("*.json")), [])
        source = {"source": {"version": "v1", "files": [{"path": "src/a.py", "text": "x = 42\n"}]}}
        run_id = self.hub.create_run("Discuss", ["a", "b"], mode="discussion",
                                     collaboration=source)["run_id"]
        run = self.wait(run_id)
        self.assertNotIn("x = 42", json.dumps(run, ensure_ascii=False))
        self.assertEqual(run["discussion"]["status"], "ready")

    def test_terminal_analysis_handoff_is_not_completed_and_restart_resumes_once(self):
        with patch.object(self.hub, "_after_task_change", return_value=None):
            run_id = self.hub.create_run("Discuss", ["a", "b"], mode="discussion")["run_id"]
            end = time.monotonic() + 5
            while time.monotonic() < end:
                run = self.hub.get_run(run_id)
                if all(task["status"] == "succeeded" for task in run["tasks"]):
                    break
                time.sleep(0.01)
            else:
                self.fail("analyses did not complete")
            self.assertEqual("running", run["status"])
            self.assertIsNone(run["discussion"]["compare_task_id"])
        self.hub.close()
        self.hub = hub.AgentHub(home=self.home, agent_loader=lambda: self.agents,
                                executor=self.execute, max_workers=4)
        recovered = self.wait(run_id)
        self.assertEqual("ready", recovered["discussion"]["status"])
        self.assertEqual(4, len(self.calls))
        self.assertEqual(4, recovered["collaboration"]["calls_used"])

    def test_restart_interrupts_running_analysis_without_replaying_it(self):
        with patch.object(self.hub, "_after_task_change", return_value=None):
            run_id = self.hub.create_run("Discuss", ["a", "b"], mode="discussion")["run_id"]
            end = time.monotonic() + 5
            while time.monotonic() < end:
                run = self.hub.get_run(run_id)
                if all(task["status"] == "succeeded" for task in run["tasks"]):
                    break
                time.sleep(0.01)
            else:
                self.fail("analyses did not complete")
            task = self.hub.get_task(run["discussion"]["analysis_task_ids"][0])
            task["status"] = "running"
            self.hub._atomic_json(self.hub._record_path(self.hub.tasks_dir, task["task_id"]), task)
        self.hub.close()
        calls_before = len(self.calls)
        self.hub = hub.AgentHub(home=self.home, agent_loader=lambda: self.agents,
                                executor=self.execute, max_workers=4)
        recovered = self.wait(run_id)
        self.assertEqual("interrupted", recovered["discussion"]["status"])
        self.assertEqual(calls_before, len(self.calls))


if __name__ == "__main__":
    unittest.main()
