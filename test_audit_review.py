"""Independent review regressions for recovery and configuration limits."""
import json
import tempfile
import unittest
from pathlib import Path

import hub


class AuditReviewTests(unittest.TestCase):
    def test_foreign_run_reference_cannot_keep_orphan_queued(self):
        with tempfile.TemporaryDirectory(prefix="hub-review-") as temp:
            home = Path(temp)
            agents = {"alpha": {"timeout": 5}}
            instance = hub.AgentHub(home=home, agent_loader=lambda: agents,
                executor=lambda *_: "ok")
            orphan = instance.create_run("orphan", ["alpha"])
            foreign = instance.create_run("foreign", ["alpha"])
            instance.close()
            task_id = orphan["accepted_tasks"][0]["task_id"]
            task_path = instance.tasks_dir / f"{task_id}.json"
            task = json.loads(task_path.read_text(encoding="utf-8"))
            task["status"] = "queued"
            task["completed_at"] = None
            task_path.write_text(json.dumps(task), encoding="utf-8")
            (instance.runs_dir / f"{orphan['run_id']}.json").unlink()
            foreign_path = instance.runs_dir / f"{foreign['run_id']}.json"
            foreign_run = json.loads(foreign_path.read_text(encoding="utf-8"))
            foreign_run["task_ids"].append(task_id)
            foreign_path.write_text(json.dumps(foreign_run), encoding="utf-8")
            reopened = hub.AgentHub(home=home, agent_loader=lambda: agents,
                executor=lambda *_: "ok")
            try:
                self.assertEqual("interrupted", reopened.get_task(task_id)["status"])
            finally:
                reopened.close()

    def test_missing_sibling_does_not_leave_queued_task_stranded(self):
        with tempfile.TemporaryDirectory(prefix="hub-review-") as temp:
            home = Path(temp)
            agents = {"alpha": {"timeout": 5}}
            instance = hub.AgentHub(home=home, agent_loader=lambda: agents,
                executor=lambda *_: "ok")
            accepted = instance.create_run("work", ["alpha"])
            instance.close()
            run_id = accepted["run_id"]
            task_id = accepted["accepted_tasks"][0]["task_id"]
            task_path = instance.tasks_dir / f"{task_id}.json"
            run_path = instance.runs_dir / f"{run_id}.json"
            task = json.loads(task_path.read_text(encoding="utf-8"))
            run = json.loads(run_path.read_text(encoding="utf-8"))
            task["status"] = "queued"
            task["completed_at"] = None
            run["status"] = "running"
            run["task_ids"].append(hub._id())  # A missing sibling makes the run unhealthy.
            task_path.write_text(json.dumps(task), encoding="utf-8")
            run_path.write_text(json.dumps(run), encoding="utf-8")
            reopened = hub.AgentHub(home=home, agent_loader=lambda: agents,
                executor=lambda *_: "ok")
            try:
                self.assertEqual("interrupted", reopened.get_task(task_id)["status"])
            finally:
                reopened.close()

    def test_depth_above_adapter_limit_rejected_and_legacy_config_migrated(self):
        with tempfile.TemporaryDirectory(prefix="hub-review-") as temp:
            home = Path(temp)
            instance = hub.AgentHub(home=home, agent_loader=lambda: {"alpha": {"timeout": 5}})
            try:
                with self.assertRaises(hub.HubError):
                    instance.put_config({"max_depth": 5})
                config_path = instance.root / "config.json"
                legacy = json.loads(config_path.read_text(encoding="utf-8"))
                legacy["max_depth"] = 16
                config_path.write_text(json.dumps(legacy), encoding="utf-8")
            finally:
                instance.close()
            reopened = hub.AgentHub(home=home, agent_loader=lambda: {"alpha": {"timeout": 5}})
            try:
                self.assertEqual(4, reopened.get_config()["max_depth"])
            finally:
                reopened.close()
