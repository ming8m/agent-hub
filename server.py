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
import html, json, os, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import re

import bus  # 复用总线的全部逻辑（日志、收件箱、run_agent）
import auth  # 本机令牌：只有本机当前用户能拿到
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
            # Token-bearing page is served only to this loopback listener's Host and origin.
            if not self._client_ok():
                return self._send(403, '{"ok":false,"error":"loopback only"}')
            if not self._host_ok():
                return self._send(403, '{"ok":false,"error":"invalid Host"}')
            if not self._origin_ok():
                return self._send(403, '{"ok":false,"error":"cross-origin rejected"}')
            if not isinstance(TOKEN, str) or not re.fullmatch(r"[0-9a-f]{64}", TOKEN):
                return self._send(503, '{"ok":false,"error":"webui token unavailable"}')
            page_path = os.path.join(WEB_DIR, "index.html")
            try:
                with open(page_path, encoding="utf-8") as stream:
                    page = stream.read()
            except OSError:
                return self._send(500, '{"ok":false,"error":"webui unavailable"}')
            if page.count("__BUS_TOKEN__") != 1:
                return self._send(500, '{"ok":false,"error":"webui unavailable"}')
            safe_token = html.escape(TOKEN, quote=True)
            self._send(200, page.replace("__BUS_TOKEN__", safe_token), "text/html; charset=utf-8")
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
            if set(payload) - {"mode", "prompt", "target_agent_ids", "deadline_seconds", "dispatch_policy"}:
                return self._send(400, '{"ok":false,"error":"invalid_request","message":"Unknown run field"}')
            dispatch_policy = payload.get("dispatch_policy", "preview")
            if dispatch_policy not in ("auto", "preview"):
                return self._send(400, '{"ok":false,"error":"invalid_request","message":"dispatch_policy must be auto or preview"}')
            try:
                idem = self.headers.get("Idempotency-Key")
                result = _get_hub().create_run(payload.get("prompt"), payload.get("target_agent_ids"),
                    deadline_seconds=payload.get("deadline_seconds"), idempotency_key=idem,
                    mode=mode, dispatch_policy=dispatch_policy)
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
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Comm Bus</title>
<style>
  :root{
    --bg:#050505; --panel:#0b0b0b; --panel2:#121212; --line:#2a2a2a;
    --text:#f5f5f5; --dim:#8f8f8f; --accent:#ffffff; --accent2:#cfcfcf;
    --user:#1d1d1d; --ok:#f5f5f5; --fail:#f5f5f5; --chipbg:#101010;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{
    font-family:"Segoe UI","Microsoft YaHei UI","PingFang SC",system-ui,sans-serif;
    background:radial-gradient(1200px 700px at 80% -10%, #1a1a1a 0%, var(--bg) 55%);
    color:var(--text);overflow:hidden;font-size:14px;
  }
  #app{display:flex;height:100vh}

  /* ---------- 侧栏 ---------- */
  #side{
    width:248px;min-width:248px;background:linear-gradient(180deg,#0a0a0a 0%,#050505 100%);
    border-right:1px solid var(--line);display:flex;flex-direction:column;
  }
  #logo{padding:18px 18px 14px;display:flex;align-items:center;gap:10px}
  #logo .mark{
    width:34px;height:34px;border-radius:10px;flex:none;
    background:linear-gradient(135deg,#ffffff,#c8c8c8);
    display:flex;align-items:center;justify-content:center;
    font-weight:700;font-size:15px;color:#000;box-shadow:0 4px 14px rgba(255,255,255,.18);
  }
  #logo .t1{font-weight:650;font-size:15px;letter-spacing:.3px}
  #logo .t2{font-size:11px;color:var(--dim);margin-top:1px}
  #agents{flex:1;overflow-y:auto;padding:6px 10px 10px}
  .side-note{font-size:11px;color:var(--dim);padding:8px 10px 4px;letter-spacing:1px}
  .agent{
    display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:10px;
    cursor:pointer;margin-bottom:2px;border:1px solid transparent;transition:.15s;
  }
  .agent:hover{background:var(--panel2)}
  .agent.sel{background:var(--panel2);border-color:#4a4a4a}
  .ava{
    width:32px;height:32px;border-radius:50%;flex:none;color:#fff;font-weight:700;
    display:flex;align-items:center;justify-content:center;font-size:13px;position:relative;
  }
  .agent .meta{flex:1;min-width:0}
  .agent .nm{font-weight:600;font-size:13px;display:flex;gap:6px;align-items:center}
  .agent .tag{font-size:11px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .dot{width:8px;height:8px;border-radius:50%;background:#e8e8e8;flex:none}
  .dot.busy{background:#ffffff;animation:pulse 1.1s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
  #broadcast{
    margin:8px 12px;padding:10px 12px;border-radius:10px;cursor:pointer;
    background:#101010;border:1px dashed #5a5a5a;color:#e8e8e8;font-size:13px;text-align:center;transition:.15s;
  }
  #broadcast:hover{border-color:#ffffff;background:#171717}
  #broadcast.sel{border-style:solid;border-color:#ffffff;background:#1c1c1c}
  #foot{padding:10px 14px;border-top:1px solid var(--line);color:var(--dim);font-size:11px;line-height:1.7}
  #foot b{color:#aab6d4}

  /* ---------- 主区 ---------- */
  #main{flex:1;display:flex;flex-direction:column;min-width:0}
  #top{
    height:52px;flex:none;display:flex;align-items:center;gap:12px;padding:0 18px;
    border-bottom:1px solid var(--line);background:rgba(13,16,23,.6);backdrop-filter:blur(6px);
  }
  #top .title{font-weight:650;font-size:15px}
  #top .sub{color:var(--dim);font-size:12px}
  #conn{margin-left:auto;font-size:12px;color:var(--dim);display:flex;align-items:center;gap:6px}
  #conn .st{width:8px;height:8px;border-radius:50%;background:var(--ok)}
  #clearbtn{
    margin-left:14px;background:none;border:1px solid var(--line);color:var(--dim);
    padding:5px 12px;border-radius:8px;cursor:pointer;font-size:12px;transition:.15s;
  }
  #clearbtn:hover{color:#ffffff;border-color:#ffffff}

  #chat{flex:1;overflow-y:auto;padding:22px 26px 10px;scroll-behavior:smooth}
  #chat::-webkit-scrollbar,#agents::-webkit-scrollbar{width:8px}
  #chat::-webkit-scrollbar-thumb,#agents::-webkit-scrollbar-thumb{background:#3a3a3a;border-radius:4px}

  .row{display:flex;margin-bottom:16px;gap:11px}
  .row.user{justify-content:flex-end}
  .row .bubble{
    max-width:72%;padding:10px 14px;border-radius:14px;line-height:1.65;
    white-space:pre-wrap;word-break:break-word;font-size:13.5px;
  }
  .row.user .bubble{
    background:#1d1d1d;border:1px solid #454545;
    border-bottom-right-radius:4px;
  }
  .row.agent .bubble{
    background:var(--panel2);border:1px solid var(--line);border-bottom-left-radius:4px;
  }
  .row.agent .bubble.fail{border-color:#e8e8e8;border-style:dashed}
  .who{font-size:11px;color:var(--dim);margin-bottom:4px;display:flex;gap:8px;align-items:center}
  .who .tm{opacity:.7}
  .who .fullbtn{
    color:var(--accent);cursor:pointer;font-size:11px;border:none;background:none;padding:0;
  }
  .who .fullbtn:hover{text-decoration:underline}
  .code{background:#0a0a0a;border:1px solid var(--line);border-radius:8px;padding:10px 12px;
        font-family:Consolas,"Cascadia Mono",monospace;font-size:12.5px;margin:6px 0;
        white-space:pre-wrap;color:#d8d8d8}
  .chip{
    text-align:center;margin:14px 0;font-size:11.5px;color:var(--dim);
  }
  .chip span{
    background:var(--chipbg);border:1px solid var(--line);border-radius:999px;
    padding:4px 14px;
  }
  .thinking .bubble{display:flex;gap:5px;align-items:center;color:var(--dim)}
  .tb{width:7px;height:7px;border-radius:50%;background:#ffffff;animation:bob 1s infinite}
  .tb:nth-child(2){animation-delay:.15s}.tb:nth-child(3){animation-delay:.3s}
  @keyframes bob{0%,100%{transform:translateY(0);opacity:.4}50%{transform:translateY(-5px);opacity:1}}

  /* ---------- 输入区 ---------- */
  #inputbar{
    flex:none;padding:14px 22px 18px;border-top:1px solid var(--line);
    background:rgba(13,16,23,.75);backdrop-filter:blur(8px);
  }
  #target{
    display:inline-flex;align-items:center;gap:7px;font-size:12px;color:#cdd6ee;
    background:var(--chipbg);border:1px solid var(--line);border-radius:999px;
    padding:4px 12px;margin-bottom:10px;
  }
  #target .cdot{width:8px;height:8px;border-radius:50%}
  #inputrow{display:flex;gap:12px;align-items:flex-end}
  #box{
    flex:1;background:#111;border:1px solid #3a3a3a;border-radius:14px;
    color:var(--text);padding:12px 15px;font-size:14px;font-family:inherit;
    resize:none;min-height:50px;max-height:170px;line-height:1.6;outline:none;transition:.15s;
  }
  #box:focus{border-color:#ffffff;box-shadow:0 0 0 3px rgba(255,255,255,.12)}
  #send{
    flex:none;height:50px;padding:0 26px;border:none;border-radius:14px;cursor:pointer;
    background:#ffffff;color:#000000;
    font-size:14.5px;font-weight:700;font-family:inherit;transition:.15s;
    box-shadow:0 4px 16px rgba(255,255,255,.15);
  }
  #send:hover{filter:brightness(1.12)}
  #send:disabled{opacity:.45;cursor:not-allowed;box-shadow:none}
  #hint{font-size:11px;color:var(--dim);margin-top:8px;text-align:right}
  #full-dialog{
    width:min(900px,92vw);max-height:86vh;margin:auto;padding:0;overflow:hidden;
    border:1px solid #4a4a4a;border-radius:12px;background:#090909;color:var(--text);
  }
  #full-dialog::backdrop{background:rgba(0,0,0,.78)}
  #full-head{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--line)}
  #full-close{border:1px solid var(--line);border-radius:7px;padding:5px 10px;background:#111;color:var(--text);cursor:pointer}
  #full-text{margin:0;padding:18px;max-height:calc(86vh - 54px);overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.65 Consolas,"Cascadia Mono",monospace}
</style>
</head>
<body>
<div id="app">
  <div id="side">
    <div id="logo">
      <div class="mark">AC</div>
      <div><div class="t1">Agent Comm Bus</div><div class="t2">多 Agent 通信总线</div></div>
    </div>
    <div class="side-note">AGENTS</div>
    <div id="agents"></div>
    <div id="broadcast" title="同一条消息发给全部 agent 并行执行">⚡ 广播模式 · 与全体对话</div>
    <div id="foot">
      <b>直连对话</b>：选择一个 agent 后在下方输入<br>
      <b>广播模式</b>：一条消息，所有 agent 同时回答<br>
      <span id="stat"></span>
    </div>
  </div>
  <div id="main">
    <div id="top">
      <div class="title" id="chatTitle">全部对话</div>
      <div class="sub" id="chatSub"></div>
      <div id="conn"><div class="st"></div><span>已连接</span></div>
      <button id="clearbtn">清空日志</button>
    </div>
    <div id="chat"></div>
    <div id="inputbar">
      <div id="target"><span class="cdot" id="cdot"></span><span id="tname">全部 agent（广播）</span></div>
      <div id="inputrow">
        <textarea id="box" placeholder="输入消息…  Enter 发送 · Shift+Enter 换行" rows="1"></textarea>
        <button id="send">发送 ➤</button>
      </div>
      <div id="hint">回复由各 CLI 真实生成，可能需要几十秒；日志同步落盘 log/chat.log</div>
    </div>
  </div>
</div>
<dialog id="full-dialog" aria-labelledby="full-title">
  <div id="full-head"><strong id="full-title">回复全文</strong><button id="full-close" type="button">关闭</button></div>
  <pre id="full-text"></pre>
</dialog>
<script>
const TOKEN='__BUS_TOKEN__';
let AGENTS=[], META={}, CUR='all', LASTN=0, THINK=new Set();

const $=s=>document.querySelector(s);
const chat=$('#chat'), box=$('#box');
const fullDialog=$('#full-dialog'), fullText=$('#full-text');
const H={'X-Agent-Bus-Token':TOKEN};

function meta(id){return META[id]||{name:id,color:'#6a6a6a'}}
function avatar(id,size){
  const m=meta(id);
  return `<div class="ava" style="width:${size}px;height:${size}px;background:${m.color}">${esc((m.name||id)[0].toUpperCase())}</div>`;
}
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

/* 简易 markdown：```代码块``` 与 `行内码`，其余按纯文本（已 esc） */
function fmt(t){
  t=esc(t);
  const parts=[];let i=0;
  t=t.replace(/```([\s\S]*?)```/g,(_,c)=>{parts.push(c);return `\x00${parts.length-1}\x00`});
  t=t.replace(/`([^`\n]+)`/g,'<code style="background:#0a0a0a;padding:1px 5px;border-radius:4px;font-family:Consolas,monospace">$1</code>');
  t=t.replace(/\x00(\d+)\x00/g,(_,n)=>`<div class="code">${parts[n].replace(/^\w*\n/,'')}</div>`);
  return t;
}

function bubbleUser(e){
  return `<div class="row user"><div style="max-width:72%">
    <div class="who" style="justify-content:flex-end"><span class="tm">${esc(e.time)}</span><span>我</span></div>
    <div class="bubble">${fmt(e.task||'')}</div></div></div>`;
}
function bubbleAgent(e){
  const m=meta(e.from), fail=e.ok===false;
  let extra='';
  if(e.full) extra=`<button class="fullbtn" data-id="${esc(e.id||'')}" onclick="showFull(this.dataset.id)">展开全文</button>`;
  return `<div class="row agent">${avatar(e.from,32)}
    <div style="max-width:72%">
      <div class="who"><span style="color:${m.color};font-weight:600">${esc(m.name)}</span>
        <span class="oktag" style="color:${fail?'var(--fail)':'var(--ok)'}">${fail?'✕ 失败':'✓'}</span>
        <span class="tm">${esc(e.time)}</span>${extra}</div>
      <div class="bubble ${fail?'fail':''}">${fmt(e.content||'')}</div>
    </div></div>`;
}
function chipMsg(e){
  return `<div class="chip"><span>💬 ${esc(e.from)} → ${esc(e.to)} · ${esc(e.time)}</span></div>`;
}
function thinking(id){
  const m=meta(id);
  return `<div class="row agent thinking" data-think="${esc(id)}">${avatar(id,32)}
    <div><div class="who"><span style="color:${m.color};font-weight:600">${esc(m.name)}</span><span class="tm">正在思考…</span></div>
    <div class="bubble"><div class="tb"></div><div class="tb"></div><div class="tb"></div></div></div></div>`;
}

function render(){
  /* 过滤当前 agent */
  const ents=ENTRIES.filter(e=> CUR==='all' || e.from===CUR || e.to===CUR);
  let html='';
  for(const e of ents){
    if(e.type==='task') html+=bubbleUser(e);
    else if(e.type==='response') html+=bubbleAgent(e);
    else html+=chipMsg(e);
  }
  for(const id of THINK) if(CUR==='all'||id===CUR) html+=thinking(id);
  if(!ents.length && !THINK.size) html='<div class="chip"><span>暂无对话，下方发第一条消息吧</span></div>';
  const stick = chat.scrollHeight-chat.scrollTop-chat.clientHeight < 120;
  chat.innerHTML=html;
  if(stick) chat.scrollTop=chat.scrollHeight;
  $('#stat').textContent=`共 ${ENTRIES.length} 条记录`;
}

let ENTRIES=[];
function renderAgents(){
  let h='';
  for(const a of AGENTS){
    const busy=THINK.has(a.id);
    h+=`<div class="agent ${CUR===a.id?'sel':''}" onclick="selAgent('${a.id}')">
      <div class="ava" style="background:${a.color}">${esc(a.name[0])}</div>
      <div class="meta"><div class="nm">${esc(a.name)}<div class="dot ${busy?'busy':''}"></div></div>
      <div class="tag">${esc(a.tag||a.desc||'')}</div></div></div>`;
  }
  $('#agents').innerHTML=h;
  $('#broadcast').className=CUR==='all'?'sel':'';
}
function selAgent(id){
  CUR=id;
  const a=AGENTS.find(x=>x.id===id);
  $('#tname').textContent=a?a.name+' · 单聊':'';
  $('#cdot').style.background=a?a.color:'#ffffff';
  $('#chatTitle').textContent=a?a.name+' 对话':'全部对话';
  $('#chatSub').textContent=a?(a.tag||''):'时间线视图';
  renderAgents();render();
}
$('#broadcast').onclick=()=>{selAgent('all');$('#tname').textContent='全部 agent（广播）'};

window.showFull=function(rid){
  fetch('/api/full?id='+encodeURIComponent(rid),{headers:H})
    .then(async r=>{
      const d=await r.json();
      if(!r.ok || typeof d.text!=='string') throw new Error('全文不存在');
      fullText.textContent=d.text;
      fullDialog.showModal();
    }).catch(e=>alert(e.message||'读取全文失败'));
};
$('#full-close').onclick=()=>fullDialog.close();

function poll(){
  fetch('/api/state',{headers:H}).then(r=>r.json()).then(s=>{
    AGENTS=s.agents; META={}; AGENTS.forEach(a=>META[a.id]=a);
    THINK=new Set(Object.keys(s.busy).filter(k=>s.busy[k]));
    ENTRIES=s.entries;
    renderAgents();render();
  }).catch(()=>{$('#conn span').textContent='连接断开';$('#conn .st').style.background='var(--fail)'});
}

function send(){
  const t=box.value.trim(); if(!t) return;
  box.value='';box.style.height='auto';
  fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Bus-Token':TOKEN},
    body:JSON.stringify({agent:CUR,text:t})}).then(r=>r.json()).then(()=>{
      poll();
    });
}
$('#send').onclick=send;
box.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}
});
box.addEventListener('input',()=>{box.style.height='auto';box.style.height=Math.min(box.scrollHeight,170)+'px'});

$('#clearbtn').onclick=()=>{
  if(!confirm('确定清空 log/chat.log？inbox 与回复全文不会被删除')) return;
  fetch('/api/clear',{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Bus-Token':TOKEN},
    body:JSON.stringify({confirm:true})}).then(poll);
};

selAgent('all');poll();setInterval(poll,1500);
</script>
</body>
</html>"""


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
    url = f"http://127.0.0.1:{PORT}"
    print(f"Agent Comm Bus 聊天窗口已启动: {url}")
    print("访问仅限本机回环地址；API 需要 X-Agent-Bus-Token。")
    print(f"已接入 agent: {', '.join(agents.keys())}  (Ctrl+C 退出)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
