"""HTTP integration checks using a synthetic registry and isolated Hub home."""
import http.client
import json
import tempfile
import threading
import time
import unittest
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import hub
import server


class _FakeProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.server.requests.append((self.path, dict(self.headers), payload))
        prompt = payload["messages"][0]["content"]
        if "UNTRUSTED_INPUT_JSON" in prompt:
            content = json.dumps({"summary": "Use configured child", "questions": [],
                "create_agents": [{"agent_id": "child_one", "template_id": "approved_child"}],
                "tasks": [{"task_id": "work", "agent_id": "child_one", "title": "Do work",
                    "prompt": "child task", "depends_on": []}]})
        elif "child task" in prompt:
            content = "AGENT_HUB_RESULT\n" + json.dumps({"final_answer": "child finished", "messages": [
                {"recipient": "peer", "task": "reply to child", "body": "Please reply", "execute": True}]})
        elif "reply to child" in prompt:
            content = "peer acknowledged child"
        else:
            content = "synthetic summary"
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class HubApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-http-test-")
        self.registry = {"alpha": {"timeout": 5}, "beta": {"timeout": 5}, "gamma": {"timeout": 5}}
        self.store = hub.AgentHub(home=Path(self.temp.name) / "home",
            agent_loader=lambda: self.registry,
            agent_config_loader=lambda: self.registry,
            executor=lambda agents, agent_id, prompt, timeout: f"synthetic:{agent_id}:{prompt}")
        self.token = "synthetic-http-token"
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.patches = [patch.object(server, "TOKEN", self.token), patch.object(server, "_get_hub", return_value=self.store)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=3)
        headers = {"Host": f"127.0.0.1:{self.httpd.server_port}", "X-Agent-Bus-Token": self.token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra or {})
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read().decode("utf-8"))
        conn.close()
        return result

    def request_raw(self, path, host=None, token=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=3)
        headers = {"Host": host or f"127.0.0.1:{self.httpd.server_port}"}
        if token is not None:
            headers["X-Agent-Bus-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        result = (response.status, response.getheader("Content-Type"),
                  response.getheader("Cache-Control"), response.read())
        conn.close()
        return result

    def wait_for_run(self, run_id, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.store.get_run(run_id)
            if predicate(run):
                return run
            time.sleep(0.01)
        self.fail(f"run condition did not become true: {self.store.get_run(run_id)}")

    @staticmethod
    def plan_text(agent_id="beta"):
        return json.dumps({"summary": "Delegate synthetic task", "tasks": [{
            "task_id": "worker-1", "agent_id": agent_id, "title": "Synthetic work",
            "prompt": "Optimized synthetic prompt", "depends_on": []}], "questions": []})

    def test_static_frontend_allowlist_mime_token_and_host_guards(self):
        webroot = Path(self.temp.name) / "web"
        webroot.mkdir()
        (webroot / "index.html").write_text(
            '<link href="/web/style.css">'
            '<script src="/web/status.js"></script><script src="/web/deadline.js"></script>'
            '<script src="/web/app.js"></script>',
            encoding="utf-8")
        (webroot / "style.css").write_text("body { color: black; }", encoding="utf-8")
        (webroot / "status.js").write_text("window.AgentHubTaskStatus = {};", encoding="utf-8")
        (webroot / "deadline.js").write_text("window.AgentHubDeadline = {};", encoding="utf-8")
        (webroot / "app.js").write_text("const ui = true;", encoding="utf-8")
        secret = "a" * 64
        with patch.object(server, "WEB_DIR", str(webroot)), patch.object(server, "TOKEN", secret):
            status, mime, cache_control, body = self.request_raw("/")
            self.assertEqual((200, "text/html; charset=utf-8"), (status, mime))
            self.assertEqual("no-store", cache_control)
            self.assertNotIn(secret.encode(), body)
            status, mime, _, body = self.request_raw("/index.html")
            self.assertEqual((200, "text/html; charset=utf-8"), (status, mime))
            self.assertIn(b"/web/status.js", body)
            self.assertIn(b"/web/deadline.js", body)
            self.assertNotIn(secret.encode(), body)
            status, _, _, body = self.request_raw("/api/state")
            self.assertEqual(401, status)
            self.assertNotIn(secret.encode(), body)
            for path, expected in (
                    ("/web/style.css", ("text/css; charset=utf-8", b"color: black")),
                    ("/web/status.js", ("application/javascript; charset=utf-8", b"AgentHubTaskStatus")),
                    ("/web/deadline.js", ("application/javascript; charset=utf-8", b"AgentHubDeadline")),
                    ("/web/app.js", ("application/javascript; charset=utf-8", b"const ui"))):
                status, mime, _, body = self.request_raw(path)
                self.assertEqual((200, expected[0]), (status, mime), path)
                self.assertIn(expected[1], body, path)
                self.assertNotIn(secret.encode(), body, path)
            for path in ("/web/secret.txt", "/web/../index.html"):
                status, _, _, body = self.request_raw(path)
                self.assertEqual(404, status)
                self.assertNotIn(secret.encode(), body)
            status, _, _, body = self.request_raw("/", host=f"attacker.invalid:{self.httpd.server_port}")
            self.assertEqual(403, status)
            self.assertNotIn(secret.encode(), body)
            with patch.object(server, "PORT", self.httpd.server_port):
                status, _, _, body = self.request_raw("/", host=f"127.0.0.1:{self.httpd.server_port}",
                    origin=f"http://attacker.invalid:{self.httpd.server_port}")
                self.assertEqual(403, status)
                self.assertNotIn(secret.encode(), body)
        with patch.object(server, "WEB_DIR", str(webroot)), patch.object(server, "TOKEN", '" onfocus="x'):
            status, _, _, body = self.request_raw("/")
            self.assertEqual(200, status)
            self.assertNotIn(b'onfocus="x', body)

    def test_mixed_provider_registry_preserves_output_limits_on_save(self):
        payload = {"providers": [
            {"id": "openai", "protocol": "openai-chat-completions", "base_url": "https://example.invalid/v1", "output_limit_field": "max_completion_tokens", "default_model": "vendor/default:2026"},
            {"id": "anthropic", "protocol": "anthropic-messages", "base_url": "https://example.invalid/v1"},
            {"id": "ollama", "protocol": "ollama-chat", "base_url": "https://example.invalid"}],
            "provider_agents": [{"id": "api_agent", "desc": "Synthetic", "provider_id": "openai", "model": "synthetic-model", "timeout": 30, "max_tokens": 8192}],
            "templates": [{"id": "child", "desc": "Synthetic", "provider_id": "anthropic", "model": "synthetic-model", "enabled": True, "max_tokens": 2048}],
            "approved_template_ids": ["child"]}
        status, _ = self.request("PUT", "/api/config/agents", payload)
        self.assertEqual(200, status)
        status, loaded = self.request("GET", "/api/config/agents")
        self.assertEqual(200, status)
        self.assertEqual(8192, loaded["provider_agents"][0]["max_tokens"])
        self.assertEqual(2048, loaded["templates"][0]["max_tokens"])
        self.assertEqual("max_completion_tokens", loaded["providers"][0]["output_limit_field"])
        self.assertEqual("vendor/default:2026", loaded["providers"][0]["default_model"])
        self.assertEqual("", loaded["providers"][1]["default_model"])
        self.assertEqual("synthetic-model", loaded["provider_agents"][0]["model"])
        self.assertEqual("synthetic-model", loaded["templates"][0]["model"])
        self.assertNotIn("output_limit_field", loaded["providers"][1])
        self.assertNotIn("output_limit_field", loaded["providers"][2])
        status, _ = self.request("PUT", "/api/config/agents", payload)
        self.assertEqual(200, status)

        reopened = hub.AgentHub(home=self.store.home, agent_loader=lambda: self.registry,
            agent_config_loader=lambda: self.registry)
        try:
            self.assertEqual("vendor/default:2026", reopened.get_agent_registry()["providers"][0]["default_model"])
        finally:
            reopened.close()

    def test_provider_default_model_validation_and_independent_agent_models(self):
        payload = {"providers": [{"id": "custom", "protocol": "openai-chat-completions",
            "base_url": "https://example.invalid/prefix/v1", "default_model": "vendor/default:v1"}],
            "provider_agents": [{"id": "worker", "provider_id": "custom", "model": "vendor/worker:v2"}],
            "templates": [{"id": "child", "provider_id": "custom", "model": "vendor/child:v3"}],
            "approved_template_ids": []}
        status, saved = self.request("PUT", "/api/config/agents", payload)
        self.assertEqual(200, status)
        self.assertEqual("vendor/default:v1", saved["providers"][0]["default_model"])
        self.assertEqual("vendor/worker:v2", saved["provider_agents"][0]["model"])
        self.assertEqual("vendor/child:v3", saved["templates"][0]["model"])
        payload["providers"][0]["default_model"] = "vendor/changed:v4"
        status, saved = self.request("PUT", "/api/config/agents", payload)
        self.assertEqual(200, status)
        self.assertEqual("vendor/worker:v2", saved["provider_agents"][0]["model"])
        self.assertEqual("vendor/child:v3", saved["templates"][0]["model"])
        for invalid in (None, 42, "   ", "x" * 501):
            with self.subTest(default_model=invalid):
                payload["providers"][0]["default_model"] = invalid
                status, result = self.request("PUT", "/api/config/agents", payload)
                self.assertEqual(400, status)
                self.assertEqual("invalid_request", result["error"])
        payload["providers"][0]["default_model"] = ""
        status, saved = self.request("PUT", "/api/config/agents", payload)
        self.assertEqual(200, status)
        self.assertEqual("", saved["providers"][0]["default_model"])
        payload["providers"][0].pop("default_model")
        status, saved = self.request("PUT", "/api/config/agents", payload)
        self.assertEqual(200, status)
        self.assertEqual("", saved["providers"][0]["default_model"])
        payload["provider_agents"][0].pop("model")
        status, _ = self.request("PUT", "/api/config/agents", payload)
        self.assertEqual(400, status)

    def test_compact_state_skips_large_or_damaged_legacy_log(self):
        log = Path(self.temp.name) / "chat.log"
        log.write_bytes((b"{" + b"x" * (2 * 1024 * 1024)) + b"\n{broken\n")
        synthetic_agents = {"alpha": {"desc": "synthetic"}}
        with patch.object(server.bus, "LOG_FILE", str(log)), \
             patch.object(self.store, "_agent_loader", return_value=synthetic_agents), \
             patch.object(server, "_entries", side_effect=AssertionError("compact state read legacy log")):
            status, state = self.request("GET", "/api/state?compact=1")
        self.assertEqual(200, status)
        self.assertEqual({"agents", "busy", "hub"}, set(state))
        self.assertEqual(["alpha"], [agent["id"] for agent in state["agents"]])

    def test_compact_state_query_is_strict_and_legacy_state_keeps_entries(self):
        for query in ("compact=true", "compact=1&x=1", "compact=1&compact=1", "after=0"):
            status, error = self.request("GET", f"/api/state?{query}")
            self.assertEqual(400, status)
            self.assertEqual("invalid_request", error["error"])
        with patch.object(server.bus, "load_agents", return_value={}), \
             patch.object(server, "_entries", return_value=[{"type": "legacy"}]) as entries:
            status, state = self.request("GET", "/api/state")
        self.assertEqual(200, status)
        self.assertEqual([{"type": "legacy"}], state["entries"])
        entries.assert_called_once()

    def test_direct_run_lifecycle_and_unavailable_summary(self):
        status, accepted = self.request("POST", "/api/runs", {"mode": "direct", "prompt": "synthetic task",
            "target_agent_ids": ["alpha", "beta"], "deadline_seconds": 60}, {"Idempotency-Key": "same-request"})
        self.assertEqual(202, status)
        self.assertEqual(2, len(accepted["accepted_tasks"]))
        run_id = accepted["run_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.store.get_run(run_id)["status"] != "completed":
            time.sleep(0.01)
        status, run = self.request("GET", f"/api/runs/{run_id}")
        self.assertEqual(200, status)
        self.assertEqual("completed", run["run"]["status"])
        self.assertTrue(all(task["status"] == "succeeded" for task in run["run"]["tasks"]))
        first = run["run"]["tasks"][0]
        status, response = self.request("GET", f"/api/responses/{first['response_id']}?run_id={run_id}&task_id={first['task_id']}")
        self.assertEqual(200, status)
        self.assertTrue(response["text"].startswith("synthetic:alpha:synthetic task"))
        status, events = self.request("GET", f"/api/runs/{run_id}/events?relation=all")
        self.assertEqual(200, status)
        self.assertTrue(events["events"])
        status, summary = self.request("POST", f"/api/runs/{run_id}/summary", {})
        self.assertEqual(200, status)
        self.assertEqual("unavailable", summary["summary"]["status"])

    def test_response_requires_both_owners_and_rejects_cross_run_or_task_access(self):
        run_a = self.store.create_run("run a", ["alpha", "beta"])["run_id"]
        run_b = self.store.create_run("run b", ["beta"])["run_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            a, b = self.store.get_run(run_a), self.store.get_run(run_b)
            if a["status"] == "completed" and b["status"] == "completed":
                break
            time.sleep(0.01)
        task_a, task_a2 = a["tasks"]
        task_b = b["tasks"][0]
        path = f"/api/responses/{task_a['response_id']}"
        status, error = self.request("GET", path)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", error["error"])
        status, content = self.request("GET", f"{path}?run_id={run_a}&task_id={task_a['task_id']}")
        self.assertEqual(200, status)
        self.assertTrue(content["text"].startswith("synthetic:alpha:run a"))
        for query in (f"run_id={run_b}&task_id={task_a['task_id']}",
                      f"run_id={run_a}&task_id={task_a2['task_id']}",
                      f"run_id={run_a}&task_id={task_b['task_id']}"):
            status, error = self.request("GET", f"{path}?{query}")
            self.assertEqual(404, status)
            self.assertEqual("not_found", error["error"])
            self.assertNotIn("text", error)

    def test_config_run_validation_and_object_boundary(self):
        status, config = self.request("GET", "/api/config/hub")
        self.assertEqual(200, status)
        self.assertEqual("disabled", config["config"]["orchestrator_state"])
        status, error = self.request("PUT", "/api/config/hub", {"orchestrator_agent_id": "missing"})
        self.assertEqual(404, status)
        self.assertEqual("unknown_agent", error["error"])
        status, error = self.request("POST", "/api/runs", [], {})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", error["error"])
        status, error = self.request("POST", "/api/runs", {"mode": "orchestrated", "prompt": "x"})
        self.assertEqual(409, status)
        self.assertEqual("orchestrator_disabled", error["error"])
        status, result = self.request("PUT", "/api/config/hub", {
            "orchestrator_agent_id": "alpha", "orchestrator_enabled": True,
            "summary_agent_id": "gamma", "summary_policy": "manual", "mode_default": "orchestrated"})
        self.assertEqual(200, status)
        self.assertEqual("enabled", result["config"]["orchestrator_state"])
        self.assertEqual("gamma", result["config"]["summary_agent_id"])
        status, current = self.request("GET", "/api/config/hub")
        self.assertEqual(200, status)
        self.assertEqual("manual", current["config"]["summary_policy"])

    def test_provider_registry_secret_redaction_and_run_scoped_child_message_e2e(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProviderHandler)
        upstream.daemon_threads = True
        upstream.requests = []
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        secret = "synthetic-provider-secret-2026"
        env_name = "AGENT_HUB_PROVIDER_TEST_KEY"
        try:
            with patch.dict(os.environ, {env_name: secret}, clear=False):
                status, saved = self.request("PUT", "/api/config/agents", {
                    "providers": [{"id": "custom_endpoint", "protocol": "openai-chat-completions",
                        "base_url": f"http://127.0.0.1:{upstream.server_port}/tenant/v1/chat/completions",
                        "allow_insecure_loopback": True, "default_model": "vendor/default:unused",
                        "api_key": "${ENV:" + env_name + "}"}],
                    "provider_agents": [
                        {"id": "coordinator", "desc": "Synthetic coordinator", "provider_id": "custom_endpoint",
                            "model": "vendor/custom-planner:rc1", "timeout": 20},
                        {"id": "peer", "desc": "Synthetic peer", "provider_id": "custom_endpoint",
                            "model": "vendor/custom-peer:2", "timeout": 20}],
                    "templates": [{"id": "approved_child", "desc": "Synthetic child", "provider_id": "custom_endpoint",
                        "model": "vendor/custom-child:7", "enabled": True}],
                    "approved_template_ids": ["approved_child"]})
                self.assertEqual(200, status)
                serialized = json.dumps(saved)
                self.assertNotIn(secret, serialized)
                self.assertNotIn('"api_key"', serialized)
                self.assertTrue(saved["providers"][0]["has_api_key"])
                self.assertEqual("vendor/default:unused", saved["providers"][0]["default_model"])
                stored = json.loads(self.store._registry_path.read_text(encoding="utf-8"))
                self.assertEqual("${ENV:" + env_name + "}", stored["providers"][0]["api_key"])
                self.assertNotIn(secret, self.store._registry_path.read_text(encoding="utf-8"))
                status, current = self.request("GET", "/api/config/agents")
                self.assertEqual(200, status)
                self.assertTrue(current["providers"][0]["has_api_key"])
                self.assertNotIn(secret, json.dumps(current))

                status, _ = self.request("PUT", "/api/config/hub", {
                    "mode_default": "orchestrated", "orchestrator_agent_id": "coordinator",
                    "orchestrator_enabled": True, "summary_agent_id": "coordinator",
                    "max_agents": 1, "max_tasks": 4, "max_depth": 3})
                self.assertEqual(200, status)
                # Exercise the same registered-agent executor used by server.py;
                # this fixture's default executor returns synthetic plain text.
                self.store._executor = server._execute_registered_agent
                # `mode` is intentionally omitted to exercise the user-configured default.
                status, accepted = self.request("POST", "/api/runs", {
                    "prompt": "Plan a child task and ask its peer for a reply.", "dispatch_policy": "auto"})
                self.assertEqual(202, status)
                run = self.wait_for_run(accepted["run_id"], lambda value: value["status"] == "completed", timeout=7)
                self.assertEqual("orchestrated", run["mode"])
                self.assertEqual({"child_one": "approved_child"}, run["child_agents"])
                worker = next(task for task in run["tasks"] if task["agent_id"] == "child_one")
                reply = next(task for task in run["tasks"] if task["agent_id"] == "peer")
                self.assertEqual("succeeded", worker["status"])
                self.assertEqual("succeeded", reply["status"])
                events = self.store.list_events(run["run_id"], relation="worker_worker")
                self.assertTrue(any(event.get("from_agent_id") == "child_one" and event.get("to_agent_id") == "peer"
                    for event in events))
                calls = upstream.requests
                self.assertTrue(calls)
                self.assertTrue(all(request[0] == "/tenant/v1/chat/completions" for request in calls))
                self.assertEqual({"vendor/custom-planner:rc1", "vendor/custom-child:7", "vendor/custom-peer:2"},
                    {request[2]["model"] for request in calls if request[2]["model"] != "vendor/custom-planner:rc1" or "UNTRUSTED_INPUT_JSON" in request[2]["messages"][0]["content"]})
                self.assertTrue(all(request[1].get("Authorization") == "Bearer " + secret for request in calls))
        finally:
            upstream.shutdown()
            upstream.server_close()

    def test_provider_key_requires_reentry_when_effective_endpoint_changes(self):
        def registry(base_url, api_key_marker=...):
            provider = {"id": "provider", "protocol": "openai-chat-completions", "base_url": base_url}
            if api_key_marker is not ...:
                provider["api_key"] = api_key_marker
            return {"providers": [provider], "provider_agents": [], "templates": [], "approved_template_ids": []}

        self.store.put_agent_registry(registry("https://example.invalid", "synthetic-key"))
        # Root and /v1 map to the same adapter endpoint, so the saved key can
        # be retained when only this equivalent spelling changes.
        self.store.put_agent_registry(registry("https://example.invalid/v1"))
        self.assertTrue(self.store.get_agent_registry()["providers"][0]["has_api_key"])
        self.assertNotIn("api_key", self.store.get_agent_registry()["providers"][0])
        with self.assertRaisesRegex(hub.HubError, "Re-enter or clear the credential"):
            self.store.put_agent_registry(registry("https://example.invalid/tenant-b/v1"))
        self.store.put_agent_registry(registry("https://example.invalid/tenant-b/v1", None))
        self.assertFalse(self.store.get_agent_registry()["providers"][0]["has_api_key"])

    def test_orchestrated_auto_preview_approval_and_invalid_plan_isolation(self):
        status, result = self.request("PUT", "/api/config/hub", {
            "orchestrator_agent_id": "alpha", "orchestrator_enabled": True,
            "summary_policy": "manual"})
        self.assertEqual(200, status)
        calls = []
        def executor(agents, agent_id, prompt, timeout):
            calls.append(agent_id)
            return self.plan_text() if agent_id == "alpha" else f"worker output from {agent_id}"
        self.store._executor = executor

        status, accepted = self.request("POST", "/api/runs", {
            "mode": "orchestrated", "dispatch_policy": "auto", "prompt": "auto plan", "deadline_seconds": 60})
        self.assertEqual(202, status)
        auto = self.wait_for_run(accepted["run_id"], lambda run: run["status"] == "completed")
        self.assertEqual("completed", auto["plan"]["status"])
        self.assertEqual("auto", auto["dispatch_policy"])
        self.assertEqual({"orchestrator", "worker"}, {task["role"] for task in auto["tasks"]})

        status, preview_accepted = self.request("POST", "/api/runs", {
            "mode": "orchestrated", "dispatch_policy": "preview", "prompt": "preview plan"})
        self.assertEqual(202, status)
        preview = self.wait_for_run(preview_accepted["run_id"],
            lambda run: run["plan"]["status"] == "awaiting_approval")
        status, detail = self.request("GET", f"/api/runs/{preview['run_id']}")
        self.assertEqual(200, status)
        self.assertEqual("awaiting_approval", detail["run"]["status"])
        self.assertEqual(["orchestrator"], [task["role"] for task in detail["run"]["tasks"]])
        status, approved = self.request("POST", f"/api/runs/{preview['run_id']}/approve", {})
        self.assertEqual(200, status)
        self.assertIn(approved["run"]["plan"]["status"], {"dispatched", "completed"})
        final_preview = self.wait_for_run(preview["run_id"], lambda run: run["status"] == "completed")
        self.assertEqual({"orchestrator", "worker"}, {task["role"] for task in final_preview["tasks"]})

        calls.clear()
        self.store._executor = lambda agents, agent_id, prompt, timeout: calls.append(agent_id) or "not valid plan json"
        status, rejected = self.request("POST", "/api/runs", {
            "mode": "orchestrated", "dispatch_policy": "auto", "prompt": "bad plan"})
        self.assertEqual(202, status)
        failed = self.wait_for_run(rejected["run_id"], lambda run: run["plan"]["status"] == "failed")
        self.assertEqual("failed", failed["status"])
        self.assertEqual(["orchestrator"], [task["role"] for task in failed["tasks"]])
        self.assertEqual(["alpha", "alpha"], calls)

    def test_summary_endpoint_runs_configured_registered_summary_agent(self):
        status, config = self.request("PUT", "/api/config/hub", {
            "summary_agent_id": "gamma", "summary_policy": "manual"})
        self.assertEqual(200, status)
        calls = []
        def executor(agents, agent_id, prompt, timeout):
            calls.append(agent_id)
            if agent_id == "gamma":
                return json.dumps({"summary": "Synthetic model summary"})
            return "synthetic worker result"
        self.store._executor = executor
        status, accepted = self.request("POST", "/api/runs", {
            "mode": "direct", "prompt": "summarize this", "target_agent_ids": ["alpha"]})
        self.assertEqual(202, status)
        run = self.wait_for_run(accepted["run_id"], lambda item: item["status"] == "completed")
        status, response = self.request("POST", f"/api/runs/{run['run_id']}/summary", {})
        self.assertEqual(200, status)
        self.assertEqual("ready", response["summary"]["status"])
        self.assertEqual("Synthetic model summary", response["summary"]["content"])
        self.assertIn("gamma", calls)

    def test_exact_routes_and_relation_filter_validation(self):
        run_id = self.store.create_run("synthetic", ["alpha"])["run_id"]
        self.assertEqual(404, self.request("GET", f"/api/runs/{run_id}/extra")[0])
        status, error = self.request("GET", f"/api/runs/{run_id}/events?relation=unknown")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", error["error"])

    def test_legacy_chat_rejects_non_object_and_busy_tracks_overlapping_runs(self):
        self.assertEqual("invalid_request", server.handle_chat([])["error"])
        started = {"first": threading.Event(), "second": threading.Event()}
        release = {"first": threading.Event(), "second": threading.Event()}
        finished = {"first": threading.Event(), "second": threading.Event()}
        def fake_run(agents, name, prompt, timeout=600):
            started[prompt].set()
            release[prompt].wait(3)
            finished[prompt].set()
            return True, "synthetic"
        with patch.object(server.bus, "run_agent", side_effect=fake_run):
            server._run_async({"alpha": {"timeout": 5}}, "alpha", "first")
            server._run_async({"alpha": {"timeout": 5}}, "alpha", "second")
            self.assertTrue(started["first"].wait(2))
            self.assertTrue(started["second"].wait(2))
            release["first"].set()
            self.assertTrue(finished["first"].wait(2))
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with server._busy_lock:
                    if server._busy.get("alpha", 0) == 1:
                        break
                time.sleep(0.01)
            with server._busy_lock:
                self.assertEqual(1, server._busy.get("alpha"))
            release["second"].set()
            self.assertTrue(finished["second"].wait(2))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with server._busy_lock:
                    if "alpha" not in server._busy:
                        break
                time.sleep(0.01)
            with server._busy_lock:
                self.assertNotIn("alpha", server._busy)

    def test_clear_and_append_share_bus_path_lock(self):
        log = Path(self.temp.name) / "log" / "chat.log"
        errors = []
        barrier = threading.Barrier(3)
        with patch.object(server.bus, "LOG_FILE", str(log)), \
             patch.object(server.bus, "LOG_DIR", str(log.parent)), \
             patch.object(server.bus, "RESP_DIR", str(log.parent / "responses")), \
             patch.object(server.bus, "INBOX_DIR", str(Path(self.temp.name) / "inbox")):
            def append_many():
                try:
                    barrier.wait()
                    for i in range(80):
                        server.bus.append_log({"type": "synthetic", "i": i})
                except Exception as exc:
                    errors.append(exc)
            def clear_many():
                try:
                    barrier.wait()
                    for _ in range(80):
                        server._clear_log_locked()
                except Exception as exc:
                    errors.append(exc)
            writers = [threading.Thread(target=append_many), threading.Thread(target=clear_many)]
            for worker in writers:
                worker.start()
            barrier.wait()
            for worker in writers:
                worker.join(5)
            self.assertFalse(any(worker.is_alive() for worker in writers))
            self.assertEqual([], errors)
            for line in log.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                self.assertEqual("synthetic", record["type"])


if __name__ == "__main__":
    unittest.main()
