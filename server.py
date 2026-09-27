#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent Comm Bus — local WebUI

A loopback-only HTTP service with a configuration-driven agent chat interface.
WebUI broadcast starts each configured agent asynchronously.

用法:
  python server.py            # 启动并自动打开浏览器
  python bus.py chat          # 同上（推荐入口）
"""
import json, os, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import re

import bus  # 复用总线的全部逻辑（日志、收件箱、run_agent）
import auth  # DPAPI protects the token at rest; loopback alone is not client identity.
import hub
import hub_providers

BASE = bus.BASE
WEB_DIR = os.path.join(BASE, "web")
STATIC_ASSETS = {
    "/web/style.css": ("style.css", "text/css; charset=utf-8"),
    "/web/status.js": ("status.js", "application/javascript; charset=utf-8"),
    "/web/deadline.js": ("deadline.js", "application/javascript; charset=utf-8"),
    "/web/app.js": ("app.js", "application/javascript; charset=utf-8"),
}
PORT = int(os.environ.get("AGENT_BUS_PORT", "8765"))
TOKEN = None
MAX_BODY = 1024 * 1024  # 1MB 请求体上限
ALLOWED_ORIGIN_PREFIXES = ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")

# agent 元数据：显示名 / 徽标色（头像底色） / 副标题关键词
AGENT_META = {}

# ---------------------------------------------------------------- 后端状态
_busy = {}          # agent -> active legacy worker count
_busy_lock = threading.Lock()
_hub_instance = None
_hub_lock = threading.Lock()
_deadline_monitor_started = False


def _get_hub():
    """Lazily bind the optional Hub store to the current registered argv executor."""
    global _hub_instance, _deadline_monitor_started
    with _hub_lock:
        if _hub_instance is None:
            _hub_instance = hub.AgentHub(
                agent_loader=lambda: bus.load_agents(runtime=True),
                executor=_execute_registered_agent,
                agent_config_loader=lambda: bus.load_agents(runtime=False),
            )
        if not _deadline_monitor_started:
            threading.Thread(target=_monitor_hub_deadlines, args=(_hub_instance,), daemon=True,
                             name="agent-hub-deadlines").start()
            _deadline_monitor_started = True
        return _hub_instance


def _monitor_hub_deadlines(instance):
    while True:
        try:
            instance.check_deadlines()
        except Exception:
            # A later pass retries transient storage failures; request handlers
            # remain available and individual task results are unaffected.
            pass
        threading.Event().wait(1.0)


def _registered_agents():
    return _get_hub()._agents()


def _execute_registered_agent(agents, agent_id, prompt, timeout):
    """Execute a configured argv agent or an explicitly selected provider adapter."""
    spec = agents[agent_id]
    prompt = hub.AgentHub._redact_output(prompt, agents)
    if isinstance(spec, dict) and isinstance(spec.get("provider"), dict):
        return hub_providers.complete(spec, prompt, timeout=timeout)
    result = bus.run_agent(agents, agent_id, prompt, timeout=timeout)
    if isinstance(result, tuple) and len(result) == 2:
        ok, output = result
        if not ok:
            message = str(output)
            if message.startswith("[超时]"):
                raise TimeoutError(message)
            raise RuntimeError(message)
        return str(output)
    return str(result or "")


def _api_error(exc):
    code = exc.code if isinstance(exc, hub.HubError) else "invalid_request"
    status = {"invalid_request": 400, "unknown_agent": 404, "not_found": 404,
              "orchestrator_disabled": 409, "invalid_plan": 422, "task_limit": 422,
              "rate_limited": 429, "conflict": 409, "storage_error": 500,
              "secure_secret_required": 400}.get(code, 400)
    message = str(exc)
    try:
        message = bus.redact_sensitive_text(message)
    except Exception:
        message = "Request failed"
    return status, {"ok": False, "error": code, "message": message[:500]}


def _clear_log_locked():
    """Delegate to the bus path lock shared with appenders and CLI clear."""
    bus.clear_log()


def _agent_list(agents):
    out = []
    for k, v in agents.items():
        m = AGENT_META.get(k, {"name": k, "color": "#6a6a6a", "tag": ""})
        out.append({"id": k, "name": m["name"], "color": m["color"], "tag": m["tag"],
                    "desc": v.get("desc", ""), "kind": "provider" if isinstance(v.get("provider"), dict) else "cli",
                    "model": v.get("model") if isinstance(v.get("provider"), dict) else None})
    return out


def _entries():
    return _read_log()


def _read_log():
    if not os.path.exists(bus.LOG_FILE):
        return []
    out = []
    try:
        raw = open(bus.LOG_FILE, encoding="utf-8").read()
    except OSError:
        return []
    for ln in raw.strip().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _run_async(agents, name, text):
    with _busy_lock:
        _busy[name] = _busy.get(name, 0) + 1
    def work():
        try:
            # 每个 agent 可在自己的配置里设 "timeout": 秒数（默认 600）
            timeout = agents[name].get("timeout", 600)
            _execute_registered_agent(agents, name, text, timeout)
        finally:
            with _busy_lock:
                remaining = _busy.get(name, 1) - 1
                if remaining > 0:
                    _busy[name] = remaining
                else:
                    _busy.pop(name, None)
    threading.Thread(target=work, daemon=True).start()


def handle_chat(payload):
    """payload: {agent: <id|'all'>, text: str}"""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "invalid_request", "message": "JSON body must be an object"}
    agents = _registered_agents()
    text = (payload.get("text") or "").strip()
    target = payload.get("agent", "")
    if not text:
        return {"ok": False, "error": "内容为空"}
    if target == "all":
        for name in agents:
            _run_async(agents, name, text)
        return {"ok": True, "started": sorted(agents.keys())}
    if target not in agents:
        return {"ok": False, "error": f"agent '{target}' 未注册"}
    _run_async(agents, target, text)
    return {"ok": True, "started": [target]}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默访问日志
        pass

    # ---- 安全检查（drive-by / 跨源 / 非本机 全部拒绝）----
    def _client_ok(self):
        """只接受回环连接（绑定 127.0.0.1 之上的纵深防御）。"""
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _host_ok(self):
        """Accept exactly one loopback Host header for this listener and port."""
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            return False
        raw = hosts[0]
        if raw != raw.strip() or any(ch.isspace() or ord(ch) < 0x21 or ord(ch) == 0x7f for ch in raw):
            return False
        try:
            parsed = urlsplit("//" + raw)
            hostname = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError:
            return False
        if (parsed.username is not None or parsed.password is not None or
                parsed.path or parsed.query or parsed.fragment or port is None):
            return False
        listener = getattr(self, "server", None)
        address = getattr(listener, "server_address", ("127.0.0.1", PORT))
        bound_host = str(address[0]).lower()
        listening_port = getattr(listener, "server_port", PORT)
        if bound_host in ("127.0.0.1", "localhost"):
            allowed_hosts = {"127.0.0.1", "localhost"}
        elif bound_host == "::1":
            allowed_hosts = {"::1"}
        else:
            return False
        return hostname in allowed_hosts and port == listening_port

    def _origin_ok(self):
        """拒绝非本源与 cross-site 请求；Sec-Fetch-Site 禁 cross-site。"""
        origin = self.headers.get("Origin")
        if origin:
            if origin == "null":
                return False
            try:
                parsed = urlsplit(origin)
                host = (parsed.hostname or "").lower()
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
            except ValueError:
                return False
            if (parsed.scheme != "http" or host not in ("127.0.0.1", "localhost", "::1")
                    or port != PORT or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment):
                return False
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return False
        return True

    def _authorized(self):
        """API 调用必须带本机令牌头。自定义头使跨源表单必然触发预检（已拒绝）。"""
        return self.headers.get("X-Agent-Bus-Token", "") == TOKEN

    def _guard(self, need_body=False):
        if not self._client_ok():
            self._send(403, '{"ok":false,"error":"loopback only"}')
            return False
        if not self._host_ok():
            self._send(403, '{"ok":false,"error":"invalid Host"}')
            return False
        if not self._origin_ok():
            self._send(403, '{"ok":false,"error":"cross-origin rejected"}')
            return False
        if not self._authorized():
            self._send(401, '{"ok":false,"error":"missing or invalid token"}')
            return False
        if need_body:
            ctype = self.headers.get("Content-Type", "")
            media_type = ctype.split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                self._send(415, '{"ok":false,"error":"Content-Type must be application/json"}')
                return False
            lengths = self.headers.get_all("Content-Length", [])
            transfer = self.headers.get("Transfer-Encoding", "")
            if len(lengths) != 1 or transfer:
                self._send(400, '{"ok":false,"error":"one Content-Length is required"}')
                return False
            raw_length = lengths[0].strip()
            if not raw_length.isascii() or not raw_length.isdigit():
                self._send(400, '{"ok":false,"error":"bad Content-Length"}')
                return False
            length = int(raw_length)
            if length > MAX_BODY:
                self._send(413, '{"ok":false,"error":"body too large"}')
                return False
        return True

    def do_OPTIONS(self):
        # 不响应 CORS 预检：跨源带自定义头的请求在浏览器侧即被拦截
        self._send(403, '{"ok":false}')

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed_url = urlsplit(self.path)
        path = parsed_url.path
        if path in ("/", "/index.html") and not parsed_url.query:
            # The public page never contains the API token.
            if not self._client_ok():
                return self._send(403, '{"ok":false,"error":"loopback only"}')
            if not self._host_ok():
                return self._send(403, '{"ok":false,"error":"invalid Host"}')
            if not self._origin_ok():
                return self._send(403, '{"ok":false,"error":"cross-origin rejected"}')
            page_path = os.path.join(WEB_DIR, "index.html")
            try:
                with open(page_path, "rb") as stream:
                    page = stream.read()
            except OSError:
                return self._send(500, '{"ok":false,"error":"webui unavailable"}')
            self._send(200, page, "text/html; charset=utf-8")
        elif path in STATIC_ASSETS and not parsed_url.query:
            if not self._client_ok():
                return self._send(403, '{"ok":false,"error":"loopback only"}')
            if not self._host_ok():
                return self._send(403, '{"ok":false,"error":"invalid Host"}')
            if not self._origin_ok():
                return self._send(403, '{"ok":false,"error":"cross-origin rejected"}')
            # Resolve only basenames from the fixed allowlist above.
            filename, content_type = STATIC_ASSETS[path]
            asset_path = os.path.join(WEB_DIR, filename)
            try:
                with open(asset_path, "rb") as stream:
                    content = stream.read()
            except OSError:
                return self._send(500, '{"ok":false,"error":"web asset unavailable"}')
            self._send(200, content, content_type)
        elif path == "/api/state":
            if not self._guard():
                return
            try:
                params = parse_qs(parsed_url.query, keep_blank_values=True, strict_parsing=True) if parsed_url.query else {}
            except ValueError:
                return self._send(400, '{"ok":false,"error":"invalid_request"}')
            compact = False
            if params:
                if set(params) != {"compact"} or params["compact"] != ["1"]:
                    return self._send(400, '{"ok":false,"error":"invalid_request","message":"Only compact=1 is supported"}')
                compact = True
            agents = _registered_agents()
            with _busy_lock:
                busy = {key: value > 0 for key, value in _busy.items()}
            try:
                config = _get_hub().get_config()
            except (hub.HubError, OSError):
                config = None
            state = {"agents": _agent_list(agents), "busy": busy, "hub": config}
            if not compact:
                state["entries"] = _entries()
            self._send(200, json.dumps(state, ensure_ascii=False))
        elif path == "/api/full":
            if not self._guard():
                return
            try:
                params = parse_qs(parsed_url.query, keep_blank_values=True, strict_parsing=True) if parsed_url.query else {}
            except ValueError:
                return self._send(400, '{"ok":false,"error":"invalid_request"}')
            rid_values = params.get("id", [])
            rid = rid_values[0] if len(rid_values) == 1 and set(params) == {"id"} else ""
            if not re.fullmatch(r"[0-9a-fA-F]{8,32}", rid):
                return self._send(400, '{"ok":false,"error":"invalid_request"}')
            safe = rid
            p = os.path.join(bus.RESP_DIR, safe + ".txt")
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                self._send(200, json.dumps({"text": text}, ensure_ascii=False))
            except OSError:
                self._send(404, '{"error":"not found"}')
        elif path == "/api/config/hub" and not parsed_url.query:
            if not self._guard():
                return
            try:
                self._send(200, json.dumps({"ok": True, "config": _get_hub().get_config()}, ensure_ascii=False))
            except hub.HubError as exc:
                status, body = _api_error(exc)
                self._send(status, json.dumps(body, ensure_ascii=False))
        elif path == "/api/config/agents" and not parsed_url.query:
            if not self._guard():
                return
            try:
                self._send(200, json.dumps({"ok": True, **_get_hub().get_agent_registry()}, ensure_ascii=False))
            except hub.HubError as exc:
                status, body = _api_error(exc)
                self._send(status, json.dumps(body, ensure_ascii=False))
        elif re.fullmatch(r"/api/runs/[0-9a-f]{32}", path) and not parsed_url.query:
            if not self._guard():
                return
            run_id = path.rsplit("/", 1)[-1]
            try:
                self._send(200, json.dumps({"ok": True, "run": _get_hub().get_run(run_id)}, ensure_ascii=False))
            except hub.HubError as exc:
                status, body = _api_error(exc)
                self._send(status, json.dumps(body, ensure_ascii=False))
        elif re.fullmatch(r"/api/runs/[0-9a-f]{32}/disputes", path) and not parsed_url.query:
            if not self._guard():
                return
            try:
                disputes = _get_hub().list_disputes(path.split("/")[3])
                self._send(200, json.dumps({"ok": True, "disputes": disputes}, ensure_ascii=False))
            except hub.HubError as exc:
                status, body = _api_error(exc)
                self._send(status, json.dumps(body, ensure_ascii=False))
        elif re.fullmatch(r"/api/runs/[0-9a-f]{32}/events", path):
            if not self._guard():
                return
            run_id = path.split("/")[3]
            try:
                params = parse_qs(parsed_url.query, keep_blank_values=True, strict_parsing=True) if parsed_url.query else {}
                if set(params) - {"after", "relation", "limit"} or any(len(v) != 1 for v in params.values()):
                    raise hub.HubError("invalid_request", "Invalid event query")
                after = int(params.get("after", ["0"])[0])
                relation = params.get("relation", ["all"])[0]
                limit = int(params.get("limit", ["500"])[0])
                events = _get_hub().list_events(run_id, after=after, relation=relation, limit=limit)
                self._send(200, json.dumps({"ok": True, "events": events}, ensure_ascii=False))
            except (hub.HubError, ValueError) as exc:
                status, body = _api_error(exc if isinstance(exc, hub.HubError) else hub.HubError("invalid_request", "Invalid event query"))
                self._send(status, json.dumps(body, ensure_ascii=False))
        elif re.fullmatch(r"/api/tasks/[0-9a-f]{32}", path) and not parsed_url.query:
            if not self._guard():
                return
            task_id = path.rsplit("/", 1)[-1]
            try:
                self._send(200, json.dumps({"ok": True, "task": _get_hub().get_task(task_id)}, ensure_ascii=False))
            except hub.HubError as exc:
                status, body = _api_error(exc)
                self._send(status, json.dumps(body, ensure_ascii=False))
        elif re.fullmatch(r"/api/responses/[0-9a-f]{32}", path) and not parsed_url.query:
            # Response IDs alone are never sufficient authority; the Hub checks
            # that both caller-supplied owners match persisted response metadata.
            if not self._guard():
                return
            return self._send(400, '{"ok":false,"error":"invalid_request","message":"run_id and task_id are required"}')
        elif re.fullmatch(r"/api/responses/[0-9a-f]{32}", path):
            if not self._guard():
                return
            response_id = path.rsplit("/", 1)[-1]
            try:
                params = parse_qs(parsed_url.query, keep_blank_values=True, strict_parsing=True)
                if set(params) != {"run_id", "task_id"} or any(len(values) != 1 for values in params.values()):
                    raise hub.HubError("invalid_request", "run_id and task_id are required")
                run_id = params["run_id"][0]
                task_id = params["task_id"][0]
                if not re.fullmatch(r"[0-9a-f]{32}", run_id) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
                    raise hub.HubError("invalid_request", "Invalid run or task ID")
                text = _get_hub().get_response(response_id, run_id, task_id)
                self._send(200, json.dumps({"ok": True, "response_id": response_id, "text": text}, ensure_ascii=False))
            except (hub.HubError, ValueError) as caught:
                exc = caught if isinstance(caught, hub.HubError) else hub.HubError("invalid_request", "Invalid response query")
                status, body = _api_error(exc)
                self._send(status, json.dumps(body, ensure_ascii=False))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        path = urlsplit(self.path).path
        if self.path == "/api/chat":
            if not self._guard(need_body=True):
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return self._send(400, '{"ok":false,"error":"bad request"}')
            result = handle_chat(payload)
            status = 200 if result.get("ok") else 400
            self._send(status, json.dumps(result, ensure_ascii=False))
        elif self.path == "/api/clear":
            if not self._guard(need_body=True):
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                return self._send(400, '{"ok":false,"error":"bad request"}')
            if not isinstance(payload, dict):
                return self._send(400, '{"ok":false,"error":"invalid_request","message":"JSON body must be an object"}')
            if payload.get("confirm") is not True:
                return self._send(400, '{"ok":false,"error":"confirm:true required"}')
            try:
                _clear_log_locked()
            except OSError:
                return self._send(500, '{"ok":false,"error":"storage_error"}')
            self._send(200, '{"ok":true}')
        elif path == "/api/runs" and not urlsplit(self.path).query:
            if not self._guard(need_body=True):
                return
            payload = self._read_object_body()
            if payload is None:
                return
            try:
                mode = payload.get("mode", _get_hub().get_config()["mode_default"])
            except hub.HubError as exc:
                status, body = _api_error(exc)
                return self._send(status, json.dumps(body, ensure_ascii=False))
            if not isinstance(mode, str) or mode not in ("direct", "orchestrated"):
                return self._send(400, '{"ok":false,"error":"invalid_request","message":"mode must be direct or orchestrated"}')
            if set(payload) - {"mode", "prompt", "target_agent_ids", "deadline_seconds", "dispatch_policy", "collaboration"}:
                return self._send(400, '{"ok":false,"error":"invalid_request","message":"Unknown run field"}')
            dispatch_policy = payload.get("dispatch_policy", "preview")
            if dispatch_policy not in ("auto", "preview"):
                return self._send(400, '{"ok":false,"error":"invalid_request","message":"dispatch_policy must be auto or preview"}')
            try:
                idem = self.headers.get("Idempotency-Key")
                result = _get_hub().create_run(payload.get("prompt"), payload.get("target_agent_ids"),
                    deadline_seconds=payload.get("deadline_seconds"), idempotency_key=idem,
                    mode=mode, dispatch_policy=dispatch_policy, collaboration=payload.get("collaboration"))
                self._send(202, json.dumps({"ok": True, **result}, ensure_ascii=False))
            except hub.HubError as exc:
                status, body = _api_error(exc)
                self._send(status, json.dumps(body, ensure_ascii=False))
        elif re.fullmatch(r"/api/runs/[0-9a-f]{32}/approve", path) and not urlsplit(self.path).query:
            if not self._guard(need_body=True):
                return
            payload = self._read_object_body()
            if payload is None:
                return
            if payload:
                return self._send(400, '{"ok":false,"error":"invalid_request","message":"Approval body must be empty"}')
            try:
                run = _get_hub().approve_plan(path.split("/")[3])
                self._send(200, json.dumps({"ok": True, "run": run}, ensure_ascii=False))
            except hub.HubError as exc:
                status, body = _api_error(exc)
                self._send(status, json.dumps(body, ensure_ascii=False))
        elif re.fullmatch(r"/api/runs/[0-9a-f]{32}/summary", path) and not urlsplit(self.path).query:
            if not self._guard(need_body=True):
                return
            payload = self._read_object_body()
            if payload is None:
                return
            try:
                if set(payload) - {"partial"}:
                    return self._send(400, '{"ok":false,"error":"invalid_request","message":"Unknown summary field"}')
                summary = _get_hub().summarize(path.split("/")[3], partial=payload.get("partial", False))
                self._send(200, json.dumps({"ok": True, "summary": summary}, ensure_ascii=False))
            except hub.HubError as exc:
                status, body = _api_error(exc)
                self._send(status, json.dumps(body, ensure_ascii=False))
        elif re.fullmatch(r"/api/runs/[0-9a-f]{32}/disputes", path) and not urlsplit(self.path).query:
            if not self._guard(need_body=True):
                return
            payload = self._read_object_body()
            if payload is None:
                return
            try:
                dispute = _get_hub().create_dispute(path.split("/")[3], payload)
                self._send(201, json.dumps({"ok": True, "dispute": dispute}, ensure_ascii=False))
            except hub.HubError as exc:
                status, body = _api_error(exc)
                self._send(status, json.dumps(body, ensure_ascii=False))
        elif re.fullmatch(r"/api/runs/[0-9a-f]{32}/disputes/[0-9a-f]{32}/(review|evidence|human-decision)", path) and not urlsplit(self.path).query:
            if not self._guard(need_body=True):
                return
            payload = self._read_object_body()
            if payload is None:
                return
            parts = path.split("/")
            try:
                if parts[-1] == "review":
                    if payload:
                        raise hub.HubError("invalid_request", "Review body must be empty")
                    dispute = _get_hub().review_dispute(parts[3], parts[5])
                    code = 202
                elif parts[-1] == "evidence":
                    dispute = _get_hub().add_dispute_evidence(parts[3], parts[5], payload)
                    code = 200
                else:
                    dispute = _get_hub().decide_dispute(parts[3], parts[5], payload)
                    code = 200
                self._send(code, json.dumps({"ok": True, "dispute": dispute}, ensure_ascii=False))
            except hub.HubError as exc:
                status, body = _api_error(exc)
                self._send(status, json.dumps(body, ensure_ascii=False))
        elif re.fullmatch(r"/api/runs/[0-9a-f]{32}/tasks/[0-9a-f]{32}/messages", path) and not urlsplit(self.path).query:
            if not self._guard(need_body=True):
                return
            payload = self._read_object_body()
            if payload is None:
                return
            # No executor-originated identity channel exists yet. Accepting a
            # caller-supplied from_agent_id would let a token holder forge agents.
            self._send(501, '{"ok":false,"error":"message_adapter_unavailable","message":"Agent-authenticated message adapter is not implemented"}')
        else:
            self._send(404, '{"error":"not found"}')

    def _read_object_body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self._send(400, '{"ok":false,"error":"invalid_request","message":"Malformed JSON"}')
            return None
        if not isinstance(payload, dict):
            self._send(400, '{"ok":false,"error":"invalid_request","message":"JSON body must be an object"}')
            return None
        return payload

    def do_PUT(self):
        if urlsplit(self.path).path not in {"/api/config/hub", "/api/config/agents"} or urlsplit(self.path).query:
            return self._send(404, '{"error":"not found"}')
        if not self._guard(need_body=True):
            return
        payload = self._read_object_body()
        if payload is None:
            return
        try:
            if urlsplit(self.path).path == "/api/config/agents":
                config = _get_hub().put_agent_registry(payload)
                self._send(200, json.dumps({"ok": True, **config}, ensure_ascii=False))
            else:
                config = _get_hub().put_config(payload)
                self._send(200, json.dumps({"ok": True, "config": config}, ensure_ascii=False))
        except hub.HubError as exc:
            status, body = _api_error(exc)
            self._send(status, json.dumps(body, ensure_ascii=False))


# ---------------------------------------------------------------- 前端页面
def main():
    global TOKEN
    try:
        TOKEN = auth.get_or_create_token()
        bus.register_redaction_secrets([TOKEN])
        bus.ensure_dirs()
        agents = bus.load_agents(runtime=True)
    except RuntimeError as exc:
        raise SystemExit(f"WebUI unavailable: {exc}")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/#token={TOKEN}"
    print(f"Agent Comm Bus 聊天窗口已启动；本地访问链接（含令牌，请勿转发）: {url}")
    print("访问仅限本机回环地址；API 需要 X-Agent-Bus-Token。")
    print(f"已接入 agent: {', '.join(agents.keys())}  (Ctrl+C 退出)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
