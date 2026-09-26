"""Persistent orchestration primitives for Agent Comm's optional Hub layer.

This module deliberately has no HTTP or UI dependency.  The server can wrap
the stable :class:`AgentHub` methods while retaining its existing auth guard.
Agent configuration is loaded at dispatch time and is never copied to Hub
storage.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
import hub_protocol
import hub_agent_io

SCHEMA_VERSION = 1
RELATIONS = {"orchestrator_worker", "worker_worker", "user_agent", "system"}
EVENT_KINDS = {"status", "message", "dispatch", "deadline", "response", "summary", "rejected"}
ID_RE = re.compile(r"^[0-9a-f]{32}$")
AGENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_CAPABILITY_SEAL = object()
_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCAL_LOCKS = {}


def _secure_hub_storage(root: Path):
    """Restrict the Hub runtime tree to its owner before reading or writing it.

    ``root`` is always the dedicated ``hub`` child, never the configured home.
    Existing custom homes are handled the same way so legacy plaintext prompts
    and results do not remain readable by other local accounts.
    """
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        root_info = None
    if root_info is not None and (root.is_symlink() or
            (os.name == "nt" and getattr(root_info, "st_file_attributes", 0) & 0x400)):
        raise OSError("Hub storage root cannot be a link or reparse point")
    root.mkdir(parents=True, exist_ok=True)
    # Do not let recursive platform tools traverse a link/reparse point out of
    # the Hub tree. Failing closed is safer than changing an unrelated target.
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            child = Path(current) / name
            if child.is_symlink() or (os.name == "nt" and getattr(child.lstat(), "st_file_attributes", 0) & 0x400):
                raise OSError(f"Hub storage contains a link or reparse point: {child}")

    if os.name == "nt":
        # Use the current account SID (not a localized account name) and reset
        # only this subtree before removing inherited access and granting that
        # SID full control. /T applies the ACL to pre-existing Hub records too.
        try:
            identity = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"],
                check=True, capture_output=True).stdout
            # The account name uses the active Windows code page. The SID is
            # ASCII in every code page and can be read without decoding it.
            rows = [line for line in identity.splitlines() if line.strip()]
            match = re.fullmatch(rb'.*,\s*"?(S-\d+(?:-\d+)+)"?\s*', rows[0]) if len(rows) == 1 else None
            if match is None:
                raise OSError("could not determine current Windows user SID")
            sid = match.group(1).decode("ascii")
            subprocess.run(["icacls.exe", str(root), "/reset", "/T", "/C"],
                check=True, capture_output=True)
            subprocess.run(["icacls.exe", str(root), "/inheritance:r", "/T", "/C"],
                check=True, capture_output=True)
            # The recursive grant must apply F directly to files and folders;
            # OI/CI-only ACEs on files would leave them with no usable access.
            subprocess.run(["icacls.exe", str(root), "/grant:r", f"*{sid}:F", "/T", "/C"],
                check=True, capture_output=True)
            # Add inheritance for objects created after initialization.
            subprocess.run(["icacls.exe", str(root), "/grant", f"*{sid}:(OI)(CI)F"],
                check=True, capture_output=True)
        except (OSError, subprocess.SubprocessError, IndexError) as exc:
            raise OSError("could not restrict Windows permissions on Hub storage") from exc
    else:
        # Restrict only Hub's own files and directories, without following links.
        try:
            for current, dirs, files in os.walk(root, followlinks=False):
                os.chmod(current, 0o700)
                for name in files:
                    os.chmod(Path(current) / name, 0o600, follow_symlinks=False)
        except OSError as exc:
            raise OSError("could not restrict permissions on Hub storage") from exc


class SenderContext:
    """In-process sender identity minted only for a persisted running task."""
    __slots__ = ("run_id", "task_id", "agent_id", "_seal")

    def __init__(self, run_id, task_id, agent_id, seal):
        if seal is not _CAPABILITY_SEAL:
            raise TypeError("SenderContext cannot be constructed directly")
        self.run_id, self.task_id, self.agent_id, self._seal = run_id, task_id, agent_id, seal


class HubError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class OutputLimitError(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or _now(), timezone.utc).isoformat()


def _id() -> str:
    return uuid.uuid4().hex


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


class _FileLock:
    """Cross-process exclusive lock using the platform's standard library."""
    def __init__(self, path: Path):
        self.path = path
        self.file = None
        self.local_lock = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.path.resolve())
        with _LOCAL_LOCKS_GUARD:
            self.local_lock = _LOCAL_LOCKS.setdefault(key, threading.RLock())
        self.local_lock.acquire()
        try:
            self.file = open(self.path, "a+b")
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                if self.file.read(1) == b"":
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)
            return self
        except BaseException:
            if self.file is not None:
                self.file.close()
                self.file = None
            self.local_lock.release()
            raise

    def __exit__(self, *_):
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.local_lock.release()


class AgentHub:
    """Persistent run/task/event/response store and direct parallel dispatcher.

    ``agent_loader`` returns the currently registered runtime agent mapping.
    ``executor(agents, agent_id, prompt, timeout)`` must return the complete
    output string or raise; by default this delegates to ``bus.run_agent``.
    """
    def __init__(self, home=None, agent_loader=None, executor=None, max_workers=8, agent_config_loader=None):
        if home is None:
            home = os.environ.get("AGENT_COMM_HOME")
            if not home:
                base = os.environ.get("APPDATA") or os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
                home = str(Path(base) / "AgentCommBus")
        self.home = Path(home).expanduser().resolve()
        self.root = self.home / "hub"
        _secure_hub_storage(self.root)
        self.runs_dir = self.root / "runs"
        self.tasks_dir = self.root / "tasks"
        self.responses_dir = self.root / "responses"
        for directory in (self.runs_dir, self.tasks_dir, self.responses_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / ".hub.lock"
        self._thread_lock = threading.RLock()
        self._lock_depth = threading.local()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent-hub")
        self._agent_loader = agent_loader or self._load_bus_agents
        self._public_agent_loader = agent_config_loader or self._load_bus_agent_config
        self._executor = executor or self._run_bus_agent
        self._futures = {}
        self._summary_futures = {}
        self._task_summary_futures = {}
        self._config_path = self.root / "config.json"
        self._registry_path = self.root / "registry.json"
        self._meta_path = self.root / "meta.json"
        self._events_path = self.root / "events.jsonl"
        self._event_indexes_dir = self.root / "event-index"
        self._event_indexes_dir.mkdir(parents=True, exist_ok=True)
        with self._locked():
            config_defaults = {"schema_version": SCHEMA_VERSION, "mode_default": "direct",
                "orchestrator_agent_id": None, "orchestrator_enabled": False, "deadline_seconds": None,
                "summary_agent_id": None, "summary_policy": "auto", "max_tasks": 16,
                "max_depth": 4, "max_messages_per_task": 20, "max_agents": 16, "updated_at": _iso()}
            if not self._config_path.exists():
                self._atomic_json(self._config_path, config_defaults)
            else:
                existing = self._read_json(self._config_path)
                migrated = {**config_defaults, **existing}
                # Earlier releases accepted 5..16, but CLI communication has
                # always enforced a maximum depth of four.
                if type(migrated.get("max_depth")) is int and migrated["max_depth"] > 4:
                    migrated["max_depth"] = 4
                if migrated != existing:
                    self._atomic_json(self._config_path, migrated)
            if not self._registry_path.exists():
                self._write_registry_locked({"schema_version": 1, "providers": [],
                    "provider_agents": [], "templates": [], "approved_template_ids": []})
            if not self._meta_path.exists():
                self._atomic_json(self._meta_path, {"event_id": 0})
            self._events_path.touch(exist_ok=True)
            self._reconcile_event_index_locked()
        self._recover()

    def _recover(self):
        """At-most-once restart policy: resume only queued work; never replay running work."""
        queued = []
        summary_idle = []
        healthy_run_ids = set()
        with self._locked():
            runs = {}
            for run_path in sorted(self.runs_dir.glob("*.json")):
                try:
                    run = self._read_json(run_path)
                    if (not isinstance(run, dict) or run.get("run_id") != run_path.stem or
                            not isinstance(run.get("task_ids"), list) or
                            run.get("mode") not in ("direct", "orchestrated") or
                            "completed_at" not in run or
                            (run.get("mode") == "orchestrated" and not isinstance(run.get("plan"), dict)) or
                            not isinstance(run.get("summary", {}), dict) or
                            not isinstance(run.get("status"), str) or
                            any(not isinstance(tid, str) or not ID_RE.fullmatch(tid)
                                for tid in run["task_ids"])):
                        continue
                except HubError:
                    continue
                runs[run["run_id"]] = run
            for path in sorted(self.tasks_dir.glob("*.json")):
                try:
                    task = self._read_json(path)
                    if (not isinstance(task, dict) or task.get("task_id") != path.stem or
                            not isinstance(task.get("run_id"), str) or
                            not ID_RE.fullmatch(task["run_id"]) or
                            task.get("status") not in ("queued", "running", "succeeded", "failed",
                                "execution_timeout", "cancelled", "interrupted") or
                            not isinstance(task.get("summary", {}), dict)):
                        continue
                except HubError:
                    continue
                run_id = task["run_id"]
                healthy_run = run_id in runs and task["task_id"] in runs[run_id]["task_ids"]
                if task.get("summary", {}).get("status") == "pending":
                    task["summary"].update(status="failed", content=None, generated_at=_iso(),
                        error={"category": "interrupted", "message": "Hub restarted during task summary generation"})
                    self._atomic_json(path, task)
                    if healthy_run:
                        self._event_locked(run_id, "summary", task_id=task["task_id"], scope="task",
                            state="failed", error_category="interrupted")
                elif (task.get("status") == "succeeded" and task.get("response_id") and
                      task.get("summary", {}).get("status") in (None, "idle") and healthy_run):
                    summary_idle.append(task["task_id"])
                if task.get("status") == "running" or not healthy_run:
                    task["status"] = "interrupted"
                    task["completed_at"] = _iso()
                    category = "orphaned_record" if not healthy_run else "interrupted"
                    message = ("Task record is not in its own valid run and was not dispatched" if category == "orphaned_record"
                               else "Hub restarted after dispatch; task was not retried")
                    task["error"] = {"category": category, "message": message}
                    self._atomic_json(path, task)
                    if healthy_run:
                        self._event_locked(run_id, "status", task_id=task["task_id"], state="interrupted",
                                           error_category=category)
                elif task.get("status") == "queued" and healthy_run:
                    queued.append(task["task_id"])
            for run_id, run in runs.items():
                path = self._record_path(self.runs_dir, run_id)
                try:
                    tasks = [self._read_json(self._record_path(self.tasks_dir, tid)) for tid in run["task_ids"]]
                    if any(not isinstance(task, dict) or task.get("task_id") != tid or
                           task.get("run_id") != run_id or task.get("status") not in (
                               "queued", "running", "succeeded", "failed", "execution_timeout", "cancelled", "interrupted") or
                           (task.get("status") == "queued" and (not isinstance(task.get("agent_id"), str) or
                               not isinstance(task.get("prompt"), str) or
                               not isinstance(task.get("depends_on_task_ids", []), list)))
                           for task, tid in zip(tasks, run["task_ids"])):
                        continue
                except HubError:
                    continue
                healthy_run_ids.add(run_id)
                if run.get("summary", {}).get("status") == "pending":
                    run["summary"].update(status="failed", content=None,
                        error={"category": "interrupted", "message": "Hub restarted during summary generation"})
                    self._atomic_json(path, run)
                    self._event_locked(run["run_id"], "summary", state="failed", error_category="interrupted")
                if any(task["status"] == "interrupted" for task in tasks):
                    self._refresh_run_locked(run_id)
            ready = []
            for task_id in queued:
                path = self._record_path(self.tasks_dir, task_id)
                task = self._read_json(path)
                if task["run_id"] in healthy_run_ids:
                    ready.append(task_id)
                    continue
                # A run may parse correctly yet have a missing or damaged
                # sibling. Do not leave its surviving queued tasks stranded.
                task["status"] = "interrupted"
                task["completed_at"] = _iso()
                task["error"] = {"category": "invalid_run",
                                 "message": "Hub restarted with an incomplete run; task was not dispatched"}
                self._atomic_json(path, task)
            queued = ready
        for task_id in queued:
            self._submit(task_id)
        for task_id in summary_idle:
            if self.get_task(task_id)["run_id"] in healthy_run_ids:
                self._request_task_summary(task_id)

    @staticmethod
    def _load_bus_agents():
        import bus
        return bus.load_agents(runtime=True)

    @staticmethod
    def _load_bus_agent_config():
        import bus
        return bus.load_agents(runtime=False)

    @staticmethod
    def _run_bus_agent(agents, agent_id, prompt, timeout):
        import bus
        result = bus.run_agent(agents, agent_id, prompt, timeout=timeout)
        if isinstance(result, tuple) and len(result) == 2:
            ok, output = result
            if not ok:
                message = str(output)
                if message.startswith("[超时]"):
                    raise TimeoutError(message)
                raise RuntimeError(message)
            return str(output)
        if isinstance(result, Mapping):
            if not result.get("ok", True):
                raise RuntimeError(str(result.get("error", "agent execution failed")))
            return str(result.get("output", result.get("text", "")))
        return "" if result is None else str(result)

    @contextlib.contextmanager
    def _locked(self):
        with self._thread_lock:
            depth = getattr(self._lock_depth, "value", 0)
            if depth:
                self._lock_depth.value = depth + 1
                try:
                    yield
                finally:
                    self._lock_depth.value = depth
            else:
                with _FileLock(self._lock_path):
                    self._lock_depth.value = 1
                    try:
                        yield
                    finally:
                        self._lock_depth.value = 0

    @staticmethod
    def _atomic_json(path: Path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".hub-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _read_json(self, path):
        with self._locked():
            try:
                with open(path, encoding="utf-8") as stream:
                    return json.load(stream)
            except FileNotFoundError:
                raise HubError("not_found", "Hub record not found")
            except (OSError, ValueError) as exc:
                raise HubError("storage_error", "Hub record could not be read") from exc

    def _record_path(self, directory, record_id):
        if not isinstance(record_id, str) or not ID_RE.fullmatch(record_id):
            raise HubError("invalid_request", "Invalid record ID")
        path = directory / (record_id + ".json")
        if path.parent.resolve() != directory.resolve():
            raise HubError("invalid_request", "Invalid record ID")
        return path

    def _agents(self):
        agents = self._agent_loader()
        if not isinstance(agents, Mapping):
            raise HubError("storage_error", "Agent registry is unavailable")
        result = dict(agents)
        registry = self._read_registry(runtime=True)
        providers = {row["id"]: row for row in registry["providers"]}
        for row in registry["provider_agents"]:
            provider = providers.get(row["provider_id"])
            if provider:
                result[row["id"]] = {"desc": row.get("desc", ""), "provider_id": row["provider_id"],
                    "provider": provider, "model": row["model"], "timeout": row.get("timeout", 600),
                    "max_tokens": row.get("max_tokens", 4096)}
        return result

    def _read_registry(self, runtime=False):
        try:
            with open(self._registry_path, encoding="utf-8") as stream:
                stored = json.load(stream)
        except FileNotFoundError:
            stored = {"schema_version": 1, "providers": [], "provider_agents": [],
                      "templates": [], "approved_template_ids": []}
        except (OSError, ValueError, RecursionError) as exc:
            raise HubError("storage_error", "Agent provider configuration could not be read") from exc
        if runtime:
            try:
                import bus
                stored = bus.unprotect_config_tree(stored, resolve_env=True)
                bus.register_redaction_secrets(row.get("api_key") for row in stored.get("providers", [])
                    if isinstance(row, dict))
            except Exception as exc:
                raise HubError("storage_error", "Agent provider credentials could not be resolved") from exc
        return stored

    def _write_registry_locked(self, value):
        try:
            import bus
            protected = bus.protect_config_tree(value)
            self._atomic_json(self._registry_path, protected)
        except Exception as exc:
            if "Raw secret values cannot be persisted" in str(exc):
                raise HubError("secure_secret_required",
                    "This platform only stores ${ENV:VARIABLE_NAME} references; set the environment variable before starting the server") from exc
            raise HubError("storage_error", "Agent provider configuration could not be protected") from exc

    def get_agent_registry(self):
        registry = self._read_registry(runtime=False)
        providers = []
        for row in registry.get("providers", []):
            public = {"id": row.get("id"), "protocol": row.get("protocol"),
                "base_url": row.get("base_url"), "allow_insecure_loopback": row.get("allow_insecure_loopback", False),
                "default_model": row.get("default_model", ""), "has_api_key": bool(row.get("api_key"))}
            if row.get("protocol") == "openai-chat-completions":
                public["output_limit_field"] = row.get("output_limit_field", "max_tokens")
            providers.append(public)
        provider_agents = [{key: row[key] for key in ("id", "desc", "provider_id", "model", "timeout", "max_tokens") if key in row}
                           for row in registry.get("provider_agents", [])]
        templates = [{key: row[key] for key in ("id", "desc", "provider_id", "model", "enabled", "max_tokens") if key in row}
                     for row in registry.get("templates", [])]
        agents = []
        provider_ids = {row["id"] for row in provider_agents}
        for agent_id, spec in self._public_agent_loader().items():
            if agent_id not in provider_ids:
                agents.append({"id": agent_id, "desc": str(spec.get("desc", spec.get("description", "")))[:500], "kind": "cli"})
        agents.extend({"id": row["id"], "desc": row.get("desc", ""), "kind": "provider",
                       "provider_id": row["provider_id"], "model": row["model"]} for row in provider_agents)
        return {"agents": agents, "providers": providers, "provider_agents": provider_agents,
                "templates": templates, "approved_template_ids": list(registry.get("approved_template_ids", [])),
                "secret_storage": "dpapi-or-env" if os.name == "nt" else "env-reference-only"}

    def put_agent_registry(self, updates):
        if not isinstance(updates, Mapping) or set(updates) != {"providers", "provider_agents", "templates", "approved_template_ids"}:
            raise HubError("invalid_request", "Expected providers, provider_agents, templates, and approved_template_ids")
        old = self._read_registry(runtime=False)
        incoming = _json_copy(dict(updates))
        for key in ("providers", "provider_agents", "templates", "approved_template_ids"):
            if not isinstance(incoming[key], list):
                raise HubError("invalid_request", f"{key} must be an array")
        previous_providers = {row["id"]: row for row in old.get("providers", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
        if len(incoming["providers"]) > 100 or len(incoming["provider_agents"]) > 100 or len(incoming["templates"]) > 100:
            raise HubError("invalid_request", "Provider registry exceeds its size limit")
        provider_rows, provider_ids = [], set()
        import hub_providers
        for row in incoming["providers"]:
            if not isinstance(row, dict) or set(row) - {"id", "protocol", "base_url", "api_key", "allow_insecure_loopback", "output_limit_field", "default_model"}:
                raise HubError("invalid_request", "Provider contains unsupported fields")
            ident, protocol, url = row.get("id"), row.get("protocol"), row.get("base_url")
            if not isinstance(ident, str) or not AGENT_RE.fullmatch(ident) or ident in provider_ids:
                raise HubError("invalid_request", "Provider IDs must be unique and use letters, digits, _ or -")
            allow_http = row.get("allow_insecure_loopback", False)
            if type(allow_http) is not bool:
                raise HubError("invalid_request", "allow_insecure_loopback must be a boolean")
            try:
                hub_providers.validate_base_url(protocol, url, allow_http)
            except hub_providers.ProviderError as exc:
                raise HubError("invalid_request", str(exc)) from exc
            output_limit_field = row.get("output_limit_field", "max_tokens")
            if output_limit_field not in ("max_tokens", "max_completion_tokens") or (
                    "output_limit_field" in row and protocol != "openai-chat-completions"):
                raise HubError("invalid_request", "output_limit_field is invalid for this protocol")
            default_model = row.get("default_model", "")
            if (not isinstance(default_model, str) or len(default_model) > 500 or
                    (default_model and not default_model.strip())):
                raise HubError("invalid_request", "Provider default_model must be empty or a non-blank identifier up to 500 characters")
            previous = previous_providers.get(ident, {})
            def origin(protocol_value, url_value):
                try:
                    # Compare the URL the adapter will actually call. A root
                    # URL and its protocol prefix can be aliases, while a
                    # different tenant/path is a different credential target.
                    parsed_value = urllib.parse.urlsplit(hub_providers._endpoint(protocol_value, url_value))
                    effective_port = parsed_value.port or (443 if parsed_value.scheme == "https" else 80)
                    return (protocol_value, parsed_value.scheme.lower(), parsed_value.hostname.lower(),
                            effective_port, parsed_value.path.rstrip("/") or "/")
                except (AttributeError, ValueError):
                    return None
            same_destination = (previous.get("protocol") == protocol and
                origin(previous.get("protocol"), previous.get("base_url")) == origin(protocol, url))
            if previous.get("api_key") and not same_destination and "api_key" not in row:
                raise HubError("invalid_request", "Re-enter or clear the credential when a provider destination changes")
            secret = row.get("api_key", previous.get("api_key"))
            if secret is None:
                secret = None
            elif not isinstance(secret, str) or len(secret) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in secret):
                raise HubError("invalid_request", "api_key must be a bounded credential without control characters")
            clean = {"id": ident, "protocol": protocol, "base_url": url.rstrip("/"),
                "allow_insecure_loopback": allow_http, "default_model": default_model}
            if protocol == "openai-chat-completions":
                clean["output_limit_field"] = output_limit_field
            if secret is not None:
                clean["api_key"] = secret
            provider_rows.append(clean)
            provider_ids.add(ident)
        provider_agent_rows, static_ids = [], set()
        previous_static_ids = {row["id"] for row in old.get("provider_agents", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
        existing_agent_ids = set(self._public_agent_loader()) - previous_static_ids
        for row in incoming["provider_agents"]:
            if not isinstance(row, dict) or set(row) - {"id", "desc", "provider_id", "model", "timeout", "max_tokens"}:
                raise HubError("invalid_request", "Provider agent contains unsupported fields")
            ident, provider_id, model = row.get("id"), row.get("provider_id"), row.get("model")
            if (not isinstance(ident, str) or not AGENT_RE.fullmatch(ident) or ident in static_ids or ident in existing_agent_ids
                    or not isinstance(provider_id, str) or provider_id not in provider_ids):
                raise HubError("invalid_request", "Provider agent ID or provider reference is invalid")
            if not isinstance(model, str) or not model.strip() or len(model) > 500:
                raise HubError("invalid_request", "Provider agent model must be a non-empty identifier")
            timeout = row.get("timeout", 600)
            max_tokens = row.get("max_tokens", 4096)
            if type(timeout) not in (int, float) or not 1 <= timeout <= 86400:
                raise HubError("invalid_request", "Provider agent timeout is out of range")
            if type(max_tokens) is not int or not 1 <= max_tokens <= 100000:
                raise HubError("invalid_request", "Provider agent max_tokens is out of range")
            desc = row.get("desc", "")
            if not isinstance(desc, str) or len(desc) > 500:
                raise HubError("invalid_request", "Provider agent desc is too long")
            provider_agent_rows.append({"id": ident, "desc": desc, "provider_id": provider_id,
                "model": model, "timeout": timeout, "max_tokens": max_tokens})
            static_ids.add(ident)
        template_rows, template_ids = [], set()
        for row in incoming["templates"]:
            if not isinstance(row, dict) or set(row) - {"id", "desc", "provider_id", "model", "enabled", "max_tokens"}:
                raise HubError("invalid_request", "Agent template contains unsupported fields")
            ident, provider_id, model = row.get("id"), row.get("provider_id"), row.get("model")
            if (not isinstance(ident, str) or not AGENT_RE.fullmatch(ident) or ident in template_ids
                    or not isinstance(provider_id, str) or provider_id not in provider_ids):
                raise HubError("invalid_request", "Template ID or provider reference is invalid")
            if not isinstance(model, str) or not model.strip() or len(model) > 500:
                raise HubError("invalid_request", "Template model must be a non-empty identifier")
            desc, enabled = row.get("desc", ""), row.get("enabled", True)
            max_tokens = row.get("max_tokens", 4096)
            if not isinstance(desc, str) or len(desc) > 500 or type(enabled) is not bool:
                raise HubError("invalid_request", "Template description or enabled flag is invalid")
            if type(max_tokens) is not int or not 1 <= max_tokens <= 100000:
                raise HubError("invalid_request", "Template max_tokens is out of range")
            template_rows.append({"id": ident, "desc": desc, "provider_id": provider_id,
                "model": model, "enabled": enabled, "max_tokens": max_tokens})
            template_ids.add(ident)
        approved = incoming["approved_template_ids"]
        if (len(approved) > 100 or any(not isinstance(item, str) for item in approved) or
                len(set(approved)) != len(approved) or any(item not in template_ids for item in approved) or
                any(not next(row for row in template_rows if row["id"] == item)["enabled"] for item in approved)):
            raise HubError("invalid_request", "approved_template_ids must reference enabled templates")
        # Do not remove an identity while existing defaults still refer to it.
        hub_config = self._read_json(self._config_path)
        if hub_config.get("orchestrator_agent_id") and any(row.get("id") == hub_config["orchestrator_agent_id"] for row in old.get("provider_agents", [])) and hub_config["orchestrator_agent_id"] not in static_ids:
            raise HubError("invalid_request", "Unselect the configured orchestrator before removing its agent")
        candidate = {"schema_version": 1, "providers": provider_rows, "provider_agents": provider_agent_rows,
                     "templates": template_rows, "approved_template_ids": approved}
        with self._locked():
            self._write_registry_locked(candidate)
        return self.get_agent_registry()

    def get_config(self):
        config = self._read_json(self._config_path)
        agents = self._agents()
        agent_id = config.get("orchestrator_agent_id")
        valid = bool(agent_id and agent_id in agents)
        summary_id = config.get("summary_agent_id")
        return {**config, "orchestrator_state": ("disabled" if not config.get("orchestrator_enabled")
                else "enabled" if valid else "invalid"), "orchestrator_valid": valid,
                "summary_agent_state": "unavailable" if not summary_id else "enabled" if summary_id in agents else "invalid"}

    def put_config(self, updates):
        if not isinstance(updates, Mapping):
            raise HubError("invalid_request", "Configuration must be an object")
        allowed = {"mode_default", "orchestrator_agent_id", "orchestrator_enabled", "deadline_seconds",
                   "max_tasks", "max_depth", "max_messages_per_task", "max_agents", "summary_agent_id", "summary_policy"}
        if set(updates) - allowed:
            raise HubError("invalid_request", "Unknown configuration field")
        config = self._read_json(self._config_path)
        candidate = {**config, **_json_copy(dict(updates))}
        if candidate["mode_default"] not in ("direct", "orchestrated"):
            raise HubError("invalid_request", "mode_default must be direct or orchestrated")
        agent_id = candidate.get("orchestrator_agent_id")
        agents = self._agents()
        if agent_id is not None and (not isinstance(agent_id, str) or not AGENT_RE.fullmatch(agent_id) or agent_id not in agents):
            raise HubError("unknown_agent", "Orchestrator agent is not registered")
        summary_agent_id = candidate.get("summary_agent_id")
        if summary_agent_id is not None and (not isinstance(summary_agent_id, str) or not AGENT_RE.fullmatch(summary_agent_id) or summary_agent_id not in agents):
            raise HubError("unknown_agent", "Summary agent is not registered")
        if type(candidate.get("orchestrator_enabled")) is not bool:
            raise HubError("invalid_request", "orchestrator_enabled must be a boolean")
        if candidate.get("summary_policy") not in ("auto", "manual"):
            raise HubError("invalid_request", "summary_policy must be auto or manual")
        for key, minimum, maximum in (("max_tasks", 1, 100), ("max_depth", 1, 4),
                                      ("max_messages_per_task", 1, 1000), ("max_agents", 0, 100)):
            value = candidate.get(key)
            if type(value) is not int or not minimum <= value <= maximum:
                raise HubError("invalid_request", f"{key} is out of range")
        deadline = candidate.get("deadline_seconds")
        if deadline is not None and (type(deadline) not in (int, float) or not 1 <= deadline <= 86400):
            raise HubError("invalid_request", "deadline_seconds is out of range")
        if candidate.get("orchestrator_enabled") and not agent_id:
            raise HubError("invalid_request", "Select an orchestrator before enabling it")
        candidate["updated_at"] = _iso()
        with self._locked():
            self._atomic_json(self._config_path, candidate)
        return self.get_config()

    def _agents_for_run(self, run):
        registered = self._agents()
        allowed = set(run.get("allowed_agent_ids", registered))
        agents = {key: value for key, value in registered.items() if key in allowed}
        children = run.get("child_agents", {})
        if not children:
            return agents
        registry = self._read_registry(runtime=True)
        providers = {row["id"]: row for row in registry.get("providers", [])}
        templates = run.get("approved_templates", {})
        for child_id, template_id in children.items():
            template = templates.get(template_id)
            provider = providers.get(template.get("provider_id")) if template else None
            if template_id not in run.get("approved_template_ids", []) or not template or not provider:
                continue
            agents[child_id] = {"desc": template.get("desc", ""), "provider_id": provider["id"],
                "provider": provider, "model": template["model"], "timeout": 600,
                "max_tokens": template.get("max_tokens", 4096), "template_id": template_id}
        return agents

    @staticmethod
    def _redact_output(text, agents):
        secrets = []
        for spec in agents.values():
            if isinstance(spec, Mapping) and isinstance(spec.get("provider"), Mapping):
                key = spec["provider"].get("api_key")
                if isinstance(key, str) and key:
                    secrets.append(key)
        try:
            import bus
            return bus.redact_sensitive_text(text, extra_secrets=secrets)
        except Exception:
            # Never persist or return unredacted text if the shared redactor fails.
            return "[output withheld because secure redaction failed]"

    @staticmethod
    def _redact_data(value, agents):
        secrets = []
        for spec in agents.values():
            if isinstance(spec, Mapping) and isinstance(spec.get("provider"), Mapping):
                key = spec["provider"].get("api_key")
                if isinstance(key, str) and key:
                    secrets.append(key)
        try:
            import bus
            return bus.redact_sensitive_data(value, extra_secrets=secrets)
        except Exception:
            # Keep the outer JSON shape safe without exposing any source values.
            return "[output withheld because secure redaction failed]"

    def _event_locked(self, run_id, kind, relation="system", task_id=None, **fields):
        if kind not in EVENT_KINDS or relation not in RELATIONS:
            raise HubError("invalid_request", "Invalid event kind or relation")
        meta = self._read_json(self._meta_path)
        event_id = int(meta.get("event_id", 0)) + 1
        event = {"event_id": event_id, "created_at": _iso(), "run_id": run_id,
                 "task_id": task_id, "kind": kind, "relation": relation, **fields}
        try:
            import bus
            event = bus.redact_sensitive_data(event)
        except Exception:
            raise HubError("storage_error", "Event could not be safely redacted")
        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8", "strict")
        offset = self._events_path.stat().st_size
        with open(self._events_path, "ab") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        indexable = isinstance(run_id, str) and ID_RE.fullmatch(run_id)
        if indexable:
            with open(self._event_index_path(run_id), "ab") as stream:
                stream.write(struct.pack(">QQ", event_id, offset))
                stream.flush()
                os.fsync(stream.fileno())
        meta.update(event_id=event_id, indexed_bytes=offset + len(line), index_state="ready",
                    index_records=int(meta.get("index_records", 0)) + (1 if indexable else 0))
        self._atomic_json(self._meta_path, meta)
        return event

    def _event_index_path(self, run_id):
        if not isinstance(run_id, str) or not ID_RE.fullmatch(run_id):
            raise HubError("invalid_request", "Invalid run ID for event index")
        return self._event_indexes_dir / (run_id + ".idx")

    def _reconcile_event_index_locked(self):
        """Recover/migrate side indexes once at startup, not once per event."""
        meta = self._read_json(self._meta_path)
        log_size = self._events_path.stat().st_size
        index_files = list(self._event_indexes_dir.glob("*.idx"))
        index_records = 0
        shape_ok = True
        for path in index_files:
            size = path.stat().st_size
            shape_ok &= size % 16 == 0
            index_records += size // 16
        if (meta.get("index_state") == "ready" and meta.get("indexed_bytes") == log_size
                and meta.get("index_records") == index_records and shape_ok):
            return

        meta["index_state"] = "building"
        self._atomic_json(self._meta_path, meta)
        per_run = {}
        maximum_id = int(meta.get("event_id", 0))
        truncate_at = None
        with open(self._events_path, "rb") as stream:
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line:
                    break
                terminated = line.endswith(b"\n")
                try:
                    event = json.loads(line.decode("utf-8", "strict"))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    if not terminated:
                        truncate_at = offset
                        break
                    continue
                if not isinstance(event, dict):
                    continue
                event_id = event.get("event_id")
                if type(event_id) is int and event_id > 0:
                    maximum_id = max(maximum_id, event_id)
                run_id = event.get("run_id")
                if (type(event_id) is int and event_id > 0 and isinstance(run_id, str)
                        and ID_RE.fullmatch(run_id)):
                    per_run.setdefault(run_id, []).append((event_id, offset))
                if not terminated:
                    # Salvage a valid final JSON row and establish the next append boundary.
                    with open(self._events_path, "ab") as append_stream:
                        append_stream.write(b"\n")
                    break
        if truncate_at is not None:
            with open(self._events_path, "r+b") as stream:
                stream.truncate(truncate_at)
        log_size = self._events_path.stat().st_size
        valid_records = 0
        for run_id, entries in per_run.items():
            entries.sort(key=lambda item: (item[0], item[1]))
            temp_path = self._event_index_path(run_id).with_suffix(".idx.tmp")
            with open(temp_path, "wb") as stream:
                for event_id, offset in entries:
                    stream.write(struct.pack(">QQ", event_id, offset))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self._event_index_path(run_id))
            valid_records += len(entries)
        live = {run_id + ".idx" for run_id in per_run}
        for path in self._event_indexes_dir.glob("*.idx"):
            if path.name not in live:
                path.unlink(missing_ok=True)
        meta.update(event_id=maximum_id, indexed_bytes=log_size, index_records=valid_records, index_state="ready")
        self._atomic_json(self._meta_path, meta)

    def _create_task_locked(self, run_id, agent_id, prompt, role="worker", parent_task_id=None, sequence=1,
                            execution_timeout=None):
        task_id = _id()
        task = {"task_id": task_id, "run_id": run_id, "parent_task_id": parent_task_id,
                "sequence": sequence, "role": role, "agent_id": agent_id, "prompt": prompt,
                "status": "queued", "created_at": _iso(), "started_at": None, "completed_at": None,
                "execution_timeout": execution_timeout, "overdue": False, "overdue_at": None,
                "return_code": None, "response_id": None, "error": None, "retry_count": 0,
                "depth": 0, "depends_on_task_ids": [], "parent_event_id": None,
                "protocol_errors": [], "summary": {"status": "idle", "content": None,
                    "source_agent_id": None, "generated_at": None, "source_response_id": None}}
        self._atomic_json(self._record_path(self.tasks_dir, task_id), task)
        self._event_locked(run_id, "status", task_id=task_id, state="queued", to_agent_id=agent_id)
        return task

    def create_run(self, prompt, target_agent_ids=None, deadline_seconds=None, initiated_by="user", idempotency_key=None,
                   mode="direct", dispatch_policy="preview"):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
            raise HubError("invalid_request", "prompt must contain 1 to 100000 characters")
        if mode not in {"direct", "orchestrated"}:
            raise HubError("invalid_request", "mode must be direct or orchestrated")
        if mode == "direct":
            if not isinstance(target_agent_ids, list) or not target_agent_ids or len(target_agent_ids) > 100:
                raise HubError("invalid_request", "Select one or more target agents")
            if any(not isinstance(a, str) or not AGENT_RE.fullmatch(a) for a in target_agent_ids):
                raise HubError("invalid_request", "Invalid agent ID")
            if len(set(target_agent_ids)) != len(target_agent_ids):
                raise HubError("invalid_request", "Duplicate agent IDs are not allowed")
        elif target_agent_ids not in (None, []):
            raise HubError("invalid_request", "orchestrated runs choose targets from the validated plan")
        if mode == "orchestrated" and dispatch_policy not in {"auto", "preview"}:
            raise HubError("invalid_request", "dispatch_policy must be auto or preview")
        agents = self._agents()
        unknown = [a for a in target_agent_ids or [] if a not in agents]
        allowed_agent_ids = list(agents) if mode == "orchestrated" else [a for a in target_agent_ids if a in agents]
        safe_prompt = self._redact_output(prompt.strip(), agents)
        config = self._read_json(self._config_path)
        if mode == "direct" and len(target_agent_ids) > config["max_tasks"]:
            raise HubError("task_limit", "Target count exceeds the configured task limit")
        orchestrator_id = None
        if mode == "orchestrated":
            orchestrator_id = config.get("orchestrator_agent_id")
            if not config.get("orchestrator_enabled"):
                raise HubError("orchestrator_disabled", "主 Agent 未启用")
            if not orchestrator_id or orchestrator_id not in agents:
                raise HubError("unknown_agent", "已设置的主 Agent 不可用")
            registry = self._read_registry(runtime=False)
            approved_template_ids = list(registry.get("approved_template_ids", []))
            templates = {row["id"]: {"provider_id": row["provider_id"], "model": row["model"],
                "desc": row.get("desc", ""), "max_tokens": row.get("max_tokens", 4096)}
                for row in registry.get("templates", []) if row.get("id") in approved_template_ids}
            try:
                plan_agents = {agent_id: agents[agent_id] for agent_id in allowed_agent_ids}
                plan_prompt = hub_protocol.build_plan_prompt(safe_prompt, plan_agents, max_tasks=config["max_tasks"],
                    approved_templates=[{"id": key, **value} for key, value in templates.items()],
                    max_agents=config.get("max_agents", 16))
            except hub_protocol.ProtocolError as exc:
                raise HubError("invalid_request", str(exc)) from exc
        else:
            plan_prompt = None
            approved_template_ids = []
            templates = {}
        if deadline_seconds is None:
            deadline_seconds = config.get("deadline_seconds")
        if deadline_seconds is not None and (type(deadline_seconds) not in (int, float) or not 1 <= deadline_seconds <= 86400):
            raise HubError("invalid_request", "deadline_seconds is out of range")
        now = _now()
        run_id = _id()
        deadline_at = now + deadline_seconds if deadline_seconds is not None else None
        summary_agent_id = orchestrator_id if mode == "orchestrated" else config.get("summary_agent_id")
        run = {"run_id": run_id, "created_at": _iso(now), "prompt": safe_prompt,
               "initiated_by": str(initiated_by)[:128], "mode": mode, "orchestrator_agent_id": orchestrator_id,
               "deadline_at": deadline_at, "deadline_state": "pending" if deadline_at else "none",
               "dispatch_policy": dispatch_policy if mode == "orchestrated" else "parallel",
               "summary_agent_id": summary_agent_id, "summary_policy": config.get("summary_policy", "auto"),
               "max_tasks": config["max_tasks"], "max_depth": config["max_depth"],
               "max_messages_per_task": config["max_messages_per_task"],
               "max_agents": config.get("max_agents", 16), "approved_template_ids": approved_template_ids,
               "approved_templates": templates, "allowed_agent_ids": allowed_agent_ids, "child_agents": {},
               "status": "planning" if mode == "orchestrated" else "running", "task_ids": [],
               "plan": {"status": "pending", "content": None, "error": None, "create_agents": []} if mode == "orchestrated" else None,
               "summary": {"status": "idle" if summary_agent_id else "unavailable", "content": None,
                   "reason": None if summary_agent_id else "No summary agent configured"},
               "error": None, "completed_at": None, "idempotency_key": None}
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
                raise HubError("invalid_request", "Invalid idempotency key")
            run["idempotency_key"] = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        accepted = []
        with self._locked():
            if run["idempotency_key"]:
                for path in self.runs_dir.glob("*.json"):
                    old = self._read_json(path)
                    if old.get("idempotency_key") == run["idempotency_key"]:
                        return self.get_run(old["run_id"])
            self._atomic_json(self._record_path(self.runs_dir, run_id), run)
            self._event_locked(run_id, "status", state=run["status"], mode=mode,
                               orchestrator_agent_id=orchestrator_id, dispatch_policy=run["dispatch_policy"])
            if mode == "orchestrated":
                task = self._create_task_locked(run_id, orchestrator_id, plan_prompt, role="orchestrator", sequence=0,
                    execution_timeout=agents[orchestrator_id].get("timeout", 600))
                run["plan_task_id"] = task["task_id"]
                run["task_ids"].append(task["task_id"])
                accepted.append({"task_id": task["task_id"], "agent_id": orchestrator_id, "role": "orchestrator", "status": "queued"})
            else:
                for index, agent_id in enumerate(target_agent_ids, 1):
                    if agent_id in unknown:
                        task = self._create_task_locked(run_id, agent_id, safe_prompt, sequence=index)
                        task.update(status="failed", completed_at=_iso(), error={"category": "unknown_agent",
                            "message": "Agent is no longer registered"})
                        self._atomic_json(self._record_path(self.tasks_dir, task["task_id"]), task)
                        self._event_locked(run_id, "status", task_id=task["task_id"], state="failed", error_category="unknown_agent")
                    else:
                        timeout = agents[agent_id].get("timeout", 600)
                        task = self._create_task_locked(run_id, agent_id, safe_prompt, sequence=index,
                                                        execution_timeout=timeout)
                    run["task_ids"].append(task["task_id"])
                    accepted.append({"task_id": task["task_id"], "agent_id": agent_id, "status": task["status"]})
            self._atomic_json(self._record_path(self.runs_dir, run_id), run)
            if mode == "direct":
                self._refresh_run_locked(run_id)
        for item in accepted:
            if item["status"] == "queued":
                self._submit(item["task_id"])
        if mode == "direct":
            self._after_task_change(run_id)
        return {"run_id": run_id, "status": "accepted", "accepted_tasks": accepted}

    def _materialize_plan_locked(self, run):
        agents = self._agents_for_run(run)
        stored = run.get("plan", {})
        try:
            validated = hub_protocol.validate_plan({"summary": stored["summary"], "tasks": stored["tasks"],
                "questions": stored.get("questions", []), "create_agents": stored.get("create_agents", [])}, agents,
                max_tasks=run["max_tasks"], dispatch_policy=run["dispatch_policy"],
                approved_template_ids=run.get("approved_template_ids", []), max_agents=run.get("max_agents", 0))
        except (hub_protocol.ProtocolError, KeyError) as exc:
            stored.update(status="failed", error={"category": "invalid_plan", "message": str(exc)[:1000]})
            run["status"] = "failed"
            run["error"] = stored["error"]
            run["completed_at"] = _iso()
            self._atomic_json(self._record_path(self.runs_dir, run["run_id"]), run)
            self._event_locked(run["run_id"], "rejected", reason="invalid_plan", message=str(exc)[:500])
            return []
        run["child_agents"] = {row["agent_id"]: row["template_id"] for row in validated["create_agents"]}
        self._atomic_json(self._record_path(self.runs_dir, run["run_id"]), run)
        agents = self._agents_for_run(run)
        id_map = {}
        task_records = []
        for index, planned in enumerate(validated["tasks"], 1):
            timeout = agents[planned["agent_id"]].get("timeout", 600)
            task = self._create_task_locked(run["run_id"], planned["agent_id"], planned["prompt"],
                parent_task_id=run.get("plan_task_id"), sequence=index, execution_timeout=timeout)
            task["plan_task_key"] = planned["task_id"]
            task["title"] = planned["title"]
            task["depth"] = 1
            if run.get("deadline_state") == "reached":
                task["overdue"] = True
                task["overdue_at"] = run.get("deadline_reached_at") or _iso()
            self._atomic_json(self._record_path(self.tasks_dir, task["task_id"]), task)
            id_map[planned["task_id"]] = task["task_id"]
            task_records.append((task, planned))
        for task, planned in task_records:
            task["depends_on_task_ids"] = [id_map[dep] for dep in planned["depends_on"]]
            self._atomic_json(self._record_path(self.tasks_dir, task["task_id"]), task)
            run["task_ids"].append(task["task_id"])
        run["plan"]["dispatch"] = id_map
        run["plan"]["approved_at"] = _iso() if run["dispatch_policy"] == "preview" else None
        run["plan"]["status"] = "dispatched"
        run["status"] = "running"
        run["completed_at"] = None
        self._atomic_json(self._record_path(self.runs_dir, run["run_id"]), run)
        self._event_locked(run["run_id"], "dispatch", task_id=run.get("plan_task_id"),
            policy=run["dispatch_policy"], task_ids=[task["task_id"] for task, _ in task_records])
        response_event_id = run.get("plan", {}).get("response_event_id")
        for task, planned in task_records:
            event = self._event_locked(run["run_id"], "message", relation="orchestrator_worker",
                task_id=run.get("plan_task_id"), message_id=_id(), from_agent_id=run["orchestrator_agent_id"],
                to_agent_id=task["agent_id"], body=task["prompt"], task_prompt=task["prompt"],
                parent_event_id=response_event_id,
                execute=True, derived_task_id=task["task_id"], source="plan_dispatch")
            task["parent_event_id"] = event["event_id"]
            self._atomic_json(self._record_path(self.tasks_dir, task["task_id"]), task)
        return [task["task_id"] for task, _ in task_records]

    def approve_plan(self, run_id):
        with self._locked():
            path = self._record_path(self.runs_dir, run_id)
            run = self._read_json(path)
            if run.get("mode") != "orchestrated":
                raise HubError("invalid_request", "Run has no orchestrated plan")
            if run.get("plan", {}).get("status") in {"dispatching", "dispatched", "completed"}:
                task_ids = [tid for tid in run["task_ids"] if tid != run.get("plan_task_id")]
            elif run.get("plan", {}).get("status") == "awaiting_approval":
                task_ids = self._materialize_plan_locked(run)
            else:
                raise HubError("conflict", "Plan is not waiting for approval")
        self._schedule_ready(run_id)
        return self.get_run(run_id)

    def _after_task_change(self, run_id):
        try:
            self._schedule_ready(run_id)
            run = self.get_run(run_id)
            if (run.get("plan") or {}).get("status") == "failed":
                return
            for task in run.get("tasks", []):
                if task.get("status") in hub_protocol.TERMINAL_STATES:
                    self._request_task_summary(task["task_id"])
            if run.get("status") == "completed":
                self._request_auto_summary(run_id, partial=False)
            elif run.get("deadline_state") == "reached":
                self._request_auto_summary(run_id, partial=True)
        except HubError:
            return

    def _request_task_summary(self, task_id):
        """Schedule one result summary without coupling it to run summary revisions."""
        with self._locked():
            task_path = self._record_path(self.tasks_dir, task_id)
            task = self._read_json(task_path)
            if task.get("status") != "succeeded" or not task.get("response_id"):
                if task.get("status") in hub_protocol.TERMINAL_STATES:
                    task.setdefault("summary", {}).update(status="unavailable", content=None,
                        reason="No successful response to summarize", generated_at=None)
                    self._atomic_json(task_path, task)
                return None
            run = self._read_json(self._record_path(self.runs_dir, task["run_id"]))
            agent_id = run.get("summary_agent_id")
            summary = task.setdefault("summary", {"status": "idle", "content": None})
            if not agent_id:
                summary.update(status="unavailable", content=None, reason="No summary agent configured",
                    source_agent_id=None, source_response_id=task["response_id"], generated_at=None)
                self._atomic_json(task_path, task)
                return None
            if summary.get("source_response_id") == task["response_id"] and summary.get("status") in {
                    "pending", "ready", "failed", "stale", "unavailable"}:
                if summary.get("status") == "pending":
                    return self._task_summary_futures.get(task_id)
                return None
            generation_id = _id()
            summary.update(status="pending", content=None, reason=None, error=None,
                source_agent_id=agent_id, source_response_id=task["response_id"],
                generation_id=generation_id, generated_at=None, started_at=_iso())
            self._atomic_json(task_path, task)
            self._event_locked(task["run_id"], "summary", task_id=task_id, scope="task", state="pending",
                generation_id=generation_id, source_agent_id=agent_id, source_response_id=task["response_id"])
        try:
            future = self._pool.submit(self._run_task_summary, task_id, generation_id)
        except RuntimeError as exc:
            with self._locked():
                current = self._read_json(task_path)
                if current.get("summary", {}).get("generation_id") == generation_id:
                    current["summary"].update(status="failed", generated_at=_iso(),
                        error={"category": "dispatch_error", "message": str(exc)[:500]})
                    self._atomic_json(task_path, current)
                    self._event_locked(task["run_id"], "summary", task_id=task_id, scope="task", state="failed",
                        error_category="dispatch_error")
            return None
        self._task_summary_futures[task_id] = future
        future.add_done_callback(lambda _future, key=task_id: self._task_summary_futures.pop(key, None))
        return future

    def _run_task_summary(self, task_id, generation_id):
        with self._locked():
            task = self._read_json(self._record_path(self.tasks_dir, task_id))
            summary = task.get("summary", {})
            if summary.get("generation_id") != generation_id or summary.get("status") != "pending":
                return summary
            response_id = summary.get("source_response_id")
            agent_id = summary.get("source_agent_id")
            task_prompt = task["prompt"]
            run_id = task["run_id"]
            try:
                agents = self._agents()
            except Exception as exc:
                return self._finish_task_summary(task_id, generation_id, None,
                    {"category": "summary_error", "message": str(exc)[:500]})
            try:
                response = self.get_response(response_id, run_id, task_id)
                request = hub_protocol.build_task_summary_prompt(task_id, task["agent_id"], task_prompt, response)
            except Exception as exc:
                return self._finish_task_summary(task_id, generation_id, None,
                    {"category": "summary_input_error", "message": str(exc)[:500]})
        try:
            if agent_id not in agents:
                raise HubError("unknown_agent", "Configured summary agent is no longer registered")
            raw = self._executor(agents, agent_id, request["prompt"], agents[agent_id].get("timeout", 600))
            if not isinstance(raw, str):
                raise HubError("invalid_summary", "Summary agent must return text")
            parsed = hub_protocol.parse_task_summary_response(raw, task_id=task_id)
            return self._finish_task_summary(task_id, generation_id, parsed["content"], None)
        except Exception as exc:
            return self._finish_task_summary(task_id, generation_id, None,
                {"category": "summary_error", "message": str(exc)[:500]})

    def _finish_task_summary(self, task_id, generation_id, content, error):
        with self._locked():
            task_path = self._record_path(self.tasks_dir, task_id)
            task = self._read_json(task_path)
            summary = task.get("summary", {})
            if summary.get("generation_id") != generation_id or summary.get("status") != "pending":
                return summary
            if task.get("response_id") != summary.get("source_response_id"):
                summary.update(status="stale", content=None, generated_at=_iso())
            elif error:
                summary.update(status="failed", content=None, generated_at=_iso(), error=error)
            else:
                summary.update(status="ready", content=content, generated_at=_iso(), error=None)
            self._atomic_json(task_path, task)
            self._event_locked(task["run_id"], "summary", task_id=task_id, scope="task",
                state=summary["status"], generation_id=generation_id,
                source_agent_id=summary.get("source_agent_id"), source_response_id=summary.get("source_response_id"))
            return summary

    def _request_auto_summary(self, run_id, partial):
        with self._locked():
            path = self._record_path(self.runs_dir, run_id)
            run = self._read_json(path)
            if run.get("summary_policy", "manual") != "auto":
                return None
            agent_id = run.get("summary_agent_id")
            if not agent_id:
                if run.get("summary", {}).get("status") == "pending":
                    self._event_locked(run_id, "summary", state="stale",
                        task_ids=run["summary"].get("task_ids", []))
                run["summary"] = {"status": "unavailable", "content": None,
                    "reason": "No summary agent configured"}
                self._atomic_json(path, run)
                return None
            tasks = [self._read_json(self._record_path(self.tasks_dir, tid)) for tid in run.get("task_ids", [])]
            terminal = {"succeeded", "failed", "execution_timeout", "cancelled", "interrupted"}
            workers = [task for task in tasks if task.get("role") == "worker"]
            finished = [task for task in workers if task["status"] in terminal]
            if not partial and (not workers or len(finished) != len(workers)):
                return None
            if not finished:
                run["summary"] = {"status": "unavailable", "content": None,
                    "reason": "No completed worker results to summarize"}
                self._atomic_json(path, run)
                return None
            revision = self._run_revision_locked(run_id)
            old = run.get("summary", {})
            if old.get("status") == "pending" and old.get("source_run_revision") == revision:
                return self._summary_futures.get(run_id)
            if old.get("status") == "pending" and old.get("source_run_revision") != revision:
                self._event_locked(run_id, "summary", state="stale", task_ids=old.get("task_ids", []))
            if old.get("status") in {"ready", "partial"} and old.get("source_run_revision") == revision:
                return None
            generation_id = _id()
            run["summary"] = {"status": "pending", "content": None, "partial": bool(partial),
                "source_run_revision": revision, "generation_id": generation_id,
                "task_ids": [task["task_id"] for task in finished], "source": "model_adapter"}
            self._atomic_json(path, run)
            self._event_locked(run_id, "summary", state="pending", task_ids=run["summary"]["task_ids"], partial=bool(partial))
        try:
            future = self._pool.submit(self._run_auto_summary, run_id, bool(partial), revision, generation_id)
        except RuntimeError as exc:
            with self._locked():
                current = self._read_json(self._record_path(self.runs_dir, run_id))
                if current.get("summary", {}).get("generation_id") == generation_id:
                    current["summary"].update(status="failed", error={"category": "dispatch_error", "message": str(exc)[:500]})
                    self._atomic_json(self._record_path(self.runs_dir, run_id), current)
                    self._event_locked(run_id, "summary", state="failed", error_category="dispatch_error")
            return None
        self._summary_futures[run_id] = future
        future.add_done_callback(lambda _future, key=run_id: self._summary_futures.pop(key, None))
        return future

    def _run_auto_summary(self, run_id, partial, revision, generation_id):
        self.summarize(run_id, partial=partial, expected_revision=revision, generation_id=generation_id)

    def _schedule_ready(self, run_id):
        """Dispatch queued dependency leaves; fail descendants of failed tasks."""
        for _ in range(101):
            run = self.get_run(run_id)
            queued = [task["task_id"] for task in run["tasks"] if task["status"] == "queued"]
            if not queued:
                return
            progressed = False
            for task_id in queued:
                before = self.get_task(task_id)["status"]
                started = self._submit(task_id)
                after = self.get_task(task_id)["status"]
                if started or after != before:
                    progressed = True
            if not progressed:
                return

    def _plan_finished(self, task, output, agents):
        run = self.get_run(task["run_id"])
        try:
            return hub_protocol.validate_plan(output, agents, max_tasks=run["max_tasks"],
                dispatch_policy=run["dispatch_policy"], approved_template_ids=run.get("approved_template_ids", []),
                max_agents=run.get("max_agents", 0)), None
        except hub_protocol.ProtocolError as exc:
            return None, str(exc)[:1000]

    def _submit(self, task_id):
        with self._locked():
            path = self._record_path(self.tasks_dir, task_id)
            task = self._read_json(path)
            if task["status"] != "queued":
                return False
            dependencies = [self._read_json(self._record_path(self.tasks_dir, dep)) for dep in task.get("depends_on_task_ids", [])]
            terminal = {"succeeded", "failed", "execution_timeout", "cancelled", "interrupted"}
            if any(dep["status"] not in terminal for dep in dependencies):
                return False
            failed_dependencies = [dep for dep in dependencies if dep["status"] != "succeeded"]
            if failed_dependencies:
                task.update(status="failed", completed_at=_iso(), error={"category": "dependency_failed",
                    "message": "A required task did not succeed"})
                self._atomic_json(path, task)
                self._event_locked(task["run_id"], "status", task_id=task_id, state="failed", error_category="dependency_failed")
                self._refresh_run_locked(task["run_id"])
                return False
            run = self._read_json(self._record_path(self.runs_dir, task["run_id"]))
            agents = self._agents_for_run(run)
            if task["agent_id"] not in agents:
                task.update(status="failed", completed_at=_iso(), error={"category": "unknown_agent", "message": "Agent is no longer registered"})
                self._atomic_json(path, task)
                self._event_locked(task["run_id"], "status", task_id=task_id, state="failed", error_category="unknown_agent")
                self._refresh_run_locked(task["run_id"])
                return False
            task["status"] = "running"
            task["started_at"] = _iso()
            self._atomic_json(path, task)
            self._event_locked(task["run_id"], "status", task_id=task_id, state="running", from_agent_id="system", to_agent_id=task["agent_id"])
            # Persist running before dispatch (at-most-once). If submit fails or
            # the process dies in this small window, recovery records interrupted
            # and never risks dispatching the command twice.
            try:
                future = self._pool.submit(self._execute, task_id)
                self._futures[task_id] = future
            except RuntimeError as exc:
                task["status"] = "interrupted"
                task["completed_at"] = _iso()
                task["error"] = {"category": "dispatch_interrupted", "message": str(exc)[:1000]}
                self._atomic_json(path, task)
                self._event_locked(task["run_id"], "status", task_id=task_id, state="interrupted",
                                   error_category="dispatch_interrupted")
                self._refresh_run_locked(task["run_id"])
                return False
        return True

    def _execute(self, task_id):
        task = None
        agents = {}
        try:
            task = self.get_task(task_id)
            run_snapshot = self._read_json(self._record_path(self.runs_dir, task["run_id"]))
            agents = self._agents_for_run(run_snapshot)
            role = task.get("role", "worker")
            is_message_worker = role in {"worker", "orchestrator_reply"}
            executor_prompt = hub_agent_io.build_cli_prompt(task["prompt"]) if is_message_worker else task["prompt"]
            output = self._executor(agents, task["agent_id"], executor_prompt, task["execution_timeout"])
            if not isinstance(output, str):
                raise TypeError("Agent output must be text")
            output = self._redact_output(output, agents)
            try:
                raw = output.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ValueError("Agent output contains invalid Unicode") from exc
            if len(raw) > MAX_RESPONSE_BYTES:
                raise OutputLimitError("Agent output exceeds the configured Hub limit")
            response_text = output
            protocol_errors = []
            if is_message_worker:
                message_limit = min(run_snapshot.get("max_messages_per_task", hub_agent_io.MAX_MESSAGES),
                                    hub_agent_io.MAX_MESSAGES)
                depth_limit = min(run_snapshot.get("max_depth", hub_agent_io.MAX_DEPTH), hub_agent_io.MAX_DEPTH)
                parsed_messages = {"messages": []}
                try:
                    parsed_messages = hub_agent_io.parse_cli_output(output, agents,
                        max_messages=message_limit, max_depth=depth_limit, depth=task["depth"])
                    parsed_messages = self._redact_data(parsed_messages, agents)
                    response_text = parsed_messages["final_answer"]
                    context = self.sender_context_for_task(task_id)
                    sender_role = "orchestrator" if role == "orchestrator_reply" else "worker"
                    envelopes = hub_agent_io.build_envelopes(parsed_messages, run_id=context.run_id,
                        task_id=context.task_id, parent_task_id=task["parent_task_id"],
                        sender_agent_id=context.agent_id, sender_role=sender_role, depth=task["depth"],
                        orchestrator_agent_id=run_snapshot.get("orchestrator_agent_id"),
                        registered_agent_ids=agents, max_messages=message_limit, max_depth=depth_limit)
                except (hub_agent_io.AgentIOError, HubError) as exc:
                    protocol_errors.append(str(exc)[:500])
                    envelopes = []
                    with self._locked():
                        self._event_locked(task["run_id"], "rejected", task_id=task_id,
                            reason="agent_message_protocol", message=str(exc)[:300])
                for envelope, normalized in zip(envelopes, parsed_messages["messages"]):
                    # The wire adapter has one body field; retain its child prompt
                    # there for dispatch and carry the authored message separately.
                    envelope["message_body"] = normalized["body"]
                    try:
                        self.validate_envelope(envelope, context, task["run_id"])
                    except HubError as exc:
                        protocol_errors.append(str(exc)[:500])
                        with self._locked():
                            self._event_locked(task["run_id"], "rejected", relation=envelope.get("relation"),
                                task_id=task_id, message_id=envelope.get("message_id"),
                                reason="agent_message_rejected", message=str(exc)[:300])
            try:
                response_raw = response_text.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                response_text = output
                response_raw = raw
                protocol_errors.append("final_answer contains invalid Unicode")
            if len(response_raw) > MAX_RESPONSE_BYTES:
                response_text, response_raw = output, raw
                protocol_errors.append("final_answer exceeds the Hub response limit")
            parsed_plan = None
            plan_error = None
            if task.get("role") == "orchestrator":
                parsed_plan, plan_error = self._plan_finished(task, output, agents)
                if parsed_plan is not None:
                    parsed_plan = self._redact_data(parsed_plan, agents)
            response_id = _id()
            response_path = self.responses_dir / (response_id + ".txt")
            with self._locked():
                with open(response_path, "xb") as stream:
                    stream.write(response_raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                response = {"response_id": response_id, "run_id": task["run_id"], "task_id": task_id,
                    "size_bytes": len(response_raw), "encoding": "utf-8-replacement", "sha256": hashlib.sha256(response_raw).hexdigest(),
                    "created_at": _iso(), "path_name": response_id + ".txt"}
                self._atomic_json(self.responses_dir / (response_id + ".json"), response)
                task["status"] = "failed" if plan_error else "succeeded"
                task["response_id"] = response_id
                task["completed_at"] = _iso()
                if protocol_errors:
                    task["protocol_errors"] = protocol_errors[:hub_agent_io.MAX_MESSAGES + 1]
                if plan_error:
                    task["error"] = {"category": "invalid_plan", "message": plan_error}
                self._atomic_json(self._record_path(self.tasks_dir, task_id), task)
                response_event = self._event_locked(task["run_id"], "response", task_id=task_id, response_id=response_id,
                                   state=task["status"], to_agent_id=task["agent_id"])
                run_record = self._read_json(self._record_path(self.runs_dir, task["run_id"]))
                if task.get("role") == "orchestrator":
                    if plan_error:
                        run_record["plan"].update(status="failed", error={"category": "invalid_plan", "message": plan_error},
                                                   response_id=response_id)
                        run_record["status"] = "failed"
                        run_record["error"] = run_record["plan"]["error"]
                        run_record["completed_at"] = _iso()
                        self._atomic_json(self._record_path(self.runs_dir, task["run_id"]), run_record)
                        self._event_locked(task["run_id"], "rejected", task_id=task_id, reason="invalid_plan", message=plan_error[:500])
                    else:
                        run_record["plan"].update(status="ready", error=None, response_id=response_id,
                            summary=parsed_plan["summary"], questions=parsed_plan["questions"],
                            tasks=parsed_plan["tasks"], create_agents=parsed_plan.get("create_agents", []),
                            response_event_id=response_event["event_id"])
                        self._atomic_json(self._record_path(self.runs_dir, task["run_id"]), run_record)
                        if run_record["dispatch_policy"] == "auto":
                            run_record = self._read_json(self._record_path(self.runs_dir, task["run_id"]))
                            self._materialize_plan_locked(run_record)
                        else:
                            run_record["plan"]["status"] = "awaiting_approval"
                            run_record["status"] = "awaiting_approval"
                            self._atomic_json(self._record_path(self.runs_dir, task["run_id"]), run_record)
                            self._event_locked(task["run_id"], "status", task_id=task_id, state="awaiting_approval")
                if run_record.get("summary", {}).get("status") in {"ready", "partial"}:
                    run_record["summary"]["status"] = "stale"
                    self._event_locked(task["run_id"], "summary", state="stale", task_ids=[task_id])
                    self._atomic_json(self._record_path(self.runs_dir, task["run_id"]), run_record)
                self._refresh_run_locked(task["run_id"])
        except BaseException as exc:
            # Persist the task's terminal state before touching its run or
            # event log. Either may be absent or damaged while the worker runs.
            with self._locked():
                try:
                    task = self._read_json(self._record_path(self.tasks_dir, task_id))
                except HubError:
                    task = None
                if isinstance(task, dict) and task.get("status") in ("running", "overdue"):
                    category = ("execution_timeout" if isinstance(exc, TimeoutError) else
                                "output_too_large" if isinstance(exc, OutputLimitError) else
                                "invalid_output" if isinstance(exc, (TypeError, UnicodeError, ValueError)) else "execution_error")
                    try:
                        message = self._redact_output(str(exc), agents)[:1000]
                    except Exception:
                        message = "Worker failed; error detail unavailable"
                    task["status"] = "execution_timeout" if isinstance(exc, TimeoutError) else "failed"
                    task["completed_at"] = _iso()
                    task["error"] = {"category": category, "message": message}
                    self._atomic_json(self._record_path(self.tasks_dir, task_id), task)
                    try:
                        if task.get("role") == "orchestrator":
                            run = self._read_json(self._record_path(self.runs_dir, task["run_id"]))
                            run["plan"].update(status="failed", error=task["error"])
                            run["status"] = "failed"
                            run["error"] = task["error"]
                            run["completed_at"] = _iso()
                            self._atomic_json(self._record_path(self.runs_dir, run["run_id"]), run)
                        self._event_locked(task["run_id"], "status", task_id=task_id, state=task["status"], error_category=category)
                        self._refresh_run_locked(task["run_id"])
                    except (HubError, KeyError, TypeError):
                        # A damaged run remains on disk as evidence; the task
                        # itself is terminal and will never be replayed.
                        pass
                elif isinstance(task, dict) and task.get("status") in hub_protocol.TERMINAL_STATES:
                    # The response may have been committed before a transient
                    # run refresh error. Retry that derived status once.
                    try:
                        self._refresh_run_locked(task["run_id"])
                    except (HubError, KeyError, TypeError):
                        pass
        finally:
            self._futures.pop(task_id, None)
            if isinstance(task, dict) and isinstance(task.get("run_id"), str):
                try:
                    self._after_task_change(task["run_id"])
                except (HubError, KeyError, TypeError):
                    pass

    def _refresh_run_locked(self, run_id):
        run = self._read_json(self._record_path(self.runs_dir, run_id))
        tasks = [self._read_json(self._record_path(self.tasks_dir, tid)) for tid in run["task_ids"]]
        terminal = {"succeeded", "failed", "execution_timeout", "cancelled", "interrupted"}
        if run.get("mode") == "orchestrated" and run.get("plan", {}).get("status") == "failed":
            run["status"] = "failed"
            run["completed_at"] = run["completed_at"] or _iso()
        elif run.get("mode") == "orchestrated" and run.get("plan", {}).get("status") == "awaiting_approval":
            run["status"] = "awaiting_approval"
        elif tasks and all(t["status"] in terminal for t in tasks):
            run["status"] = "completed"
            if run.get("mode") == "orchestrated" and (run.get("plan") or {}).get("status") == "dispatched":
                run["plan"]["status"] = "completed"
            run["completed_at"] = run["completed_at"] or _iso()
        elif run.get("deadline_state") == "reached":
            run["status"] = "deadline_reached"
        else:
            run["status"] = "running"
        self._atomic_json(self._record_path(self.runs_dir, run_id), run)

    def check_deadlines(self, now=None):
        """Mark due tasks overdue and emit one reminder; execution keeps running."""
        now = _now() if now is None else now
        changed = []
        summaries = []
        with self._locked():
            for path in self.runs_dir.glob("*.json"):
                run = self._read_json(path)
                if run.get("deadline_at") is None or run.get("deadline_state") == "reached" or run["deadline_at"] > now:
                    continue
                tasks = [self._read_json(self._record_path(self.tasks_dir, tid)) for tid in run["task_ids"]]
                pending = [t for t in tasks if t["status"] not in {"succeeded", "failed", "execution_timeout", "cancelled", "interrupted"}]
                run["deadline_state"] = "reached"
                run["deadline_reached_at"] = _iso(now)
                self._event_locked(run["run_id"], "deadline", state="reached", pending_task_ids=[t["task_id"] for t in pending])
                for task in pending:
                    if not task["overdue"]:
                        task["overdue"] = True
                        task["overdue_at"] = _iso(now)
                        self._atomic_json(self._record_path(self.tasks_dir, task["task_id"]), task)
                        changed.append(task["task_id"])
                run["status"] = "deadline_reached" if pending else "completed"
                self._atomic_json(path, run)
                summaries.append((run["run_id"], bool(pending)))
        for run_id, partial in summaries:
            self._request_auto_summary(run_id, partial=partial)
        return changed

    def get_run(self, run_id):
        run = self._read_json(self._record_path(self.runs_dir, run_id))
        tasks = [self.get_task(tid) for tid in run["task_ids"]]
        return {**run, "tasks": tasks, "counts": {state: sum(t["status"] == state for t in tasks)
                for state in ("queued", "running", "succeeded", "failed", "execution_timeout", "cancelled", "interrupted")}}

    def get_task(self, task_id):
        return self._read_json(self._record_path(self.tasks_dir, task_id))

    def get_response(self, response_id, run_id=None, task_id=None):
        if not isinstance(response_id, str) or not ID_RE.fullmatch(response_id):
            raise HubError("invalid_request", "Invalid response ID")
        if not isinstance(run_id, str) or not ID_RE.fullmatch(run_id) or not isinstance(task_id, str) or not ID_RE.fullmatch(task_id):
            raise HubError("invalid_request", "Response access requires valid run and task IDs")
        meta_path = self.responses_dir / (response_id + ".json")
        response = self._read_json(meta_path)
        if response.get("run_id") != run_id or response.get("task_id") != task_id:
            raise HubError("not_found", "Response not found")
        task = self.get_task(task_id)
        if task.get("run_id") != run_id or task.get("response_id") != response_id:
            raise HubError("not_found", "Response not found")
        # Resolve a fixed UUID-derived basename beneath the response directory.
        path = (self.responses_dir / (response_id + ".txt")).resolve()
        if path.parent != self.responses_dir.resolve():
            raise HubError("not_found", "Response not found")
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HubError("storage_error", "Response content is unavailable") from exc

    def list_events(self, run_id, after=0, relation="all", limit=500):
        self.get_run(run_id)
        if relation not in {"all", "orchestrator_worker", "worker_worker"}:
            raise HubError("invalid_request", "Invalid relation filter")
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise HubError("invalid_request", "Invalid event cursor or limit")
        with self._locked():
            return self._indexed_events_locked(run_id, after=after, relation=relation, limit=limit)

    def _indexed_events_locked(self, run_id, after=0, relation="all", limit=None):
        """Read this run's indexed rows. Caller holds the hub lock."""
        result = []
        index_path = self._event_index_path(run_id)
        if not index_path.exists():
            return result
        with open(index_path, "rb") as index:
            count = index_path.stat().st_size // 16
            low, high = 0, count
            while low < high:
                middle = (low + high) // 2
                index.seek(middle * 16)
                record = index.read(16)
                if len(record) != 16:
                    high = middle
                    continue
                event_id, _ = struct.unpack(">QQ", record)
                if event_id <= after:
                    low = middle + 1
                else:
                    high = middle
            index.seek(low * 16)
            with open(self._events_path, "rb") as events:
                while limit is None or len(result) < limit:
                    record = index.read(16)
                    if len(record) != 16:
                        break
                    expected_id, offset = struct.unpack(">QQ", record)
                    events.seek(offset)
                    line = events.readline()
                    try:
                        event = json.loads(line.decode("utf-8", "strict"))
                    except (UnicodeDecodeError, ValueError, RecursionError):
                        continue
                    if (not isinstance(event, dict) or event.get("run_id") != run_id
                            or event.get("event_id") != expected_id):
                        continue
                    if relation != "all" and event.get("relation") != relation:
                        continue
                    result.append(event)
        return result

    def sender_context_for_task(self, task_id):
        """Mint an in-process capability for an adapter currently executing task_id."""
        task = self.get_task(task_id)
        if task.get("status") != "running":
            raise HubError("conflict", "Sender capability requires a running task")
        return SenderContext(task["run_id"], task["task_id"], task["agent_id"], _CAPABILITY_SEAL)

    def validate_envelope(self, envelope, sender_context=None, run_id=None):
        """Validate a message against trusted caller context; envelope identity is never authority."""
        required = {"schema_version", "message_id", "run_id", "task_id", "parent_task_id", "kind",
                    "from_agent_id", "to_agent_id", "relation", "body", "execute", "created_at"}
        if not isinstance(envelope, Mapping) or set(envelope) not in (required, required | {"message_body"}):
            raise HubError("invalid_request", "Message envelope fields do not match schema")
        if not isinstance(sender_context, SenderContext) or sender_context._seal is not _CAPABILITY_SEAL:
            raise HubError("unauthorized_sender", "A trusted sender capability is required")
        try:
            e = _json_copy(dict(envelope))
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise HubError("invalid_request", "Message envelope is not valid JSON data") from exc
        if type(e["schema_version"]) is not int or e["schema_version"] != 1 or not isinstance(e["message_id"], str) or not ID_RE.fullmatch(e["message_id"]):
            raise HubError("invalid_request", "Invalid message schema version or ID")
        if not isinstance(e["run_id"], str) or not ID_RE.fullmatch(e["run_id"]):
            raise HubError("invalid_request", "Invalid run ID")
        if not isinstance(e["task_id"], str) or not ID_RE.fullmatch(e["task_id"]):
            raise HubError("invalid_request", "Invalid task ID")
        if e["parent_task_id"] is not None and (not isinstance(e["parent_task_id"], str) or not ID_RE.fullmatch(e["parent_task_id"])):
            raise HubError("invalid_request", "Invalid parent task ID")
        for key in ("from_agent_id", "to_agent_id"):
            if not isinstance(e[key], str) or not AGENT_RE.fullmatch(e[key]):
                raise HubError("invalid_request", "Invalid message participant")
        if not isinstance(e["kind"], str) or not isinstance(e["relation"], str):
            raise HubError("invalid_request", "Invalid message kind or relation")
        if not isinstance(e["body"], str) or not e["body"].strip():
            raise HubError("invalid_request", "Message body must be non-empty text")
        try:
            body_size = len(e["body"].encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise HubError("invalid_request", "Message body contains invalid Unicode") from exc
        if body_size > 64 * 1024:
            raise HubError("invalid_request", "Message body exceeds 65536 UTF-8 bytes")
        if type(e["execute"]) is not bool:
            raise HubError("invalid_request", "execute must be a boolean")
        if "message_body" in e:
            if not isinstance(e["message_body"], str) or not e["message_body"].strip():
                raise HubError("invalid_request", "message_body must be non-empty text")
            try:
                message_body_size = len(e["message_body"].encode("utf-8", errors="strict"))
            except UnicodeEncodeError as exc:
                raise HubError("invalid_request", "message_body contains invalid Unicode") from exc
            if message_body_size > 64 * 1024:
                raise HubError("invalid_request", "message_body exceeds 65536 UTF-8 bytes")
        if run_id is not None and e["run_id"] != run_id:
            raise HubError("invalid_request", "Run ID mismatch")
        if sender_context.run_id != e["run_id"] or sender_context.task_id != e["task_id"] or sender_context.agent_id != e["from_agent_id"]:
            raise HubError("unauthorized_sender", "Envelope sender does not match the executing task")
        if not isinstance(e["created_at"], str) or len(e["created_at"]) > 64:
            raise HubError("invalid_request", "Invalid message timestamp")
        try:
            datetime.fromisoformat(e["created_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise HubError("invalid_request", "Invalid message timestamp") from exc
        run = self.get_run(e["run_id"])
        parent = self.get_task(e["task_id"])
        if parent["run_id"] != run["run_id"] or e["parent_task_id"] != parent.get("parent_task_id"):
            raise HubError("invalid_request", "Task relationship mismatch")
        agents = self._agents_for_run(run)
        if e["from_agent_id"] not in agents or e["to_agent_id"] not in agents:
            raise HubError("unknown_agent", "Message participant is not registered")
        if e["kind"] != "message" or e["relation"] not in RELATIONS - {"system", "user_agent"}:
            raise HubError("invalid_request", "Invalid agent message kind or relation")
        if parent["agent_id"] != e["from_agent_id"]:
            raise HubError("invalid_request", "Sender does not own the parent task")
        if e["relation"] == "worker_worker":
            if parent["role"] != "worker" or e["to_agent_id"] == e["from_agent_id"]:
                raise HubError("invalid_request", "worker_worker must connect distinct worker agents")
        else:
            orchestrator_id = run.get("orchestrator_agent_id")
            if (orchestrator_id is None or
                    (parent["role"] in {"orchestrator", "orchestrator_reply"} and e["from_agent_id"] != orchestrator_id) or
                    (parent["role"] == "worker" and e["to_agent_id"] != orchestrator_id) or
                    parent["role"] not in {"orchestrator", "orchestrator_reply", "worker"}):
                raise HubError("invalid_request", "orchestrator_worker relationship does not match this run")
        if e["execute"] and e["to_agent_id"] == e["from_agent_id"]:
            raise HubError("invalid_request", "A derived task must target another agent")
        try:
            envelope_hash = hashlib.sha256(json.dumps(e, sort_keys=True, ensure_ascii=False,
                separators=(",", ":")).encode("utf-8", errors="strict")).hexdigest()
        except (TypeError, ValueError, UnicodeError) as exc:
            raise HubError("invalid_request", "Message envelope contains invalid values") from exc
        with self._locked():
            current_parent = self._read_json(self._record_path(self.tasks_dir, parent["task_id"]))
            if current_parent["status"] != "running":
                raise HubError("conflict", "Parent task is no longer running")
            prior = None
            count = 0
            for candidate in self._indexed_events_locked(run["run_id"]):
                if candidate.get("message_id") == e["message_id"]:
                    prior = candidate
                    break
                if candidate.get("kind") == "message" and candidate.get("task_id") == parent["task_id"]:
                    count += 1
            if prior:
                if prior.get("envelope_sha256") != envelope_hash:
                    raise HubError("conflict", "message_id was already used for a different envelope")
                child_id = prior.get("derived_task_id")
                return {"event": prior, "task": self.get_task(child_id) if child_id else None}
            current = self._read_json(self._record_path(self.runs_dir, run["run_id"]))
            config = self._read_json(self._config_path)
            message_limit = current.get("max_messages_per_task", config["max_messages_per_task"])
            if count >= message_limit:
                self._event_locked(run["run_id"], "rejected", relation=e["relation"], task_id=parent["task_id"], reason="message_limit")
                raise HubError("rate_limited", "Maximum messages per task exceeded")
            if e["execute"]:
                if parent["depth"] + 1 > current.get("max_depth", config["max_depth"]):
                    self._event_locked(run["run_id"], "rejected", relation=e["relation"], task_id=parent["task_id"], reason="max_depth")
                    raise HubError("task_limit", "Maximum communication depth exceeded")
                if len(current["task_ids"]) >= current.get("max_tasks", config["max_tasks"]):
                    self._event_locked(run["run_id"], "rejected", relation=e["relation"], task_id=parent["task_id"], reason="max_tasks")
                    raise HubError("task_limit", "Maximum task count exceeded")
                child_role = "orchestrator_reply" if e["to_agent_id"] == current.get("orchestrator_agent_id") else "worker"
                child = self._create_task_locked(run["run_id"], e["to_agent_id"], e["body"], role=child_role, parent_task_id=parent["task_id"],
                    sequence=len(current["task_ids"])+1, execution_timeout=agents[e["to_agent_id"]].get("timeout", 600))
                child["depth"] = parent["depth"] + 1
                self._atomic_json(self._record_path(self.tasks_dir, child["task_id"]), child)
                current["task_ids"].append(child["task_id"])
                current["status"] = "running"
                current["completed_at"] = None
                self._atomic_json(self._record_path(self.runs_dir, current["run_id"]), current)
            else:
                child = None
            event = self._event_locked(run["run_id"], "message", relation=e["relation"], task_id=parent["task_id"],
                message_id=e["message_id"], from_agent_id=e["from_agent_id"], to_agent_id=e["to_agent_id"],
                body=e.get("message_body", e["body"]), task_prompt=e["body"] if e["execute"] else None,
                parent_event_id=None, execute=e["execute"], source_created_at=e["created_at"],
                envelope_sha256=envelope_hash, derived_task_id=child["task_id"] if child else None)
            if child is not None:
                child["parent_event_id"] = event["event_id"]
                self._atomic_json(self._record_path(self.tasks_dir, child["task_id"]), child)
        if child is not None:
            self._submit(child["task_id"])
        return {"event": event, "task": self.get_task(child["task_id"]) if child else None}

    def _run_revision_locked(self, run_id):
        revision = 0
        for event in self._indexed_events_locked(run_id):
            if event.get("kind") != "summary":
                event_id = event.get("event_id", 0)
                if type(event_id) is int:
                    revision = max(revision, event_id)
        return revision

    def summarize(self, run_id, adapter=None, partial=False, expected_revision=None, generation_id=None):
        """Generate a bounded protocol summary from terminal worker results."""
        if type(partial) is not bool:
            raise HubError("invalid_request", "partial must be a boolean")
        with self._locked():
            run = self._read_json(self._record_path(self.runs_dir, run_id))
            tasks = [self._read_json(self._record_path(self.tasks_dir, tid)) for tid in run["task_ids"]]
            workers = [task for task in tasks if task.get("role") == "worker"]
            terminal = set(hub_protocol.TERMINAL_STATES)
            included = [task for task in workers if task["status"] in terminal]
            if not run.get("summary_agent_id") and adapter is None:
                return {"status": "unavailable", "content": None, "reason": "No summary agent configured"}
            if not workers or not included:
                return {"status": "unavailable", "content": None, "reason": "No completed worker results to summarize"}
            if not partial and len(included) != len(workers):
                raise HubError("conflict", "A complete summary requires every worker task to finish")
            revision = self._run_revision_locked(run_id)
            if expected_revision is not None and expected_revision != revision:
                current = run.get("summary", {})
                if current.get("generation_id") == generation_id:
                    current.update(status="stale", content=None)
                    self._atomic_json(self._record_path(self.runs_dir, run_id), run)
                return current or {"status": "stale", "content": None}
            generation_id = generation_id or _id()
            old_summary = run.get("summary", {})
            if expected_revision is not None and old_summary.get("generation_id") != generation_id:
                return old_summary
            if expected_revision is None:
                source_ids = [task["task_id"] for task in included]
                run["summary"] = {"status": "pending", "content": None, "partial": partial,
                    "source_run_revision": revision, "generation_id": generation_id,
                    "task_ids": source_ids, "source": "model_adapter"}
                self._atomic_json(self._record_path(self.runs_dir, run_id), run)
                self._event_locked(run_id, "summary", state="pending", task_ids=source_ids, partial=partial)
            prompt = run["prompt"]
            source_ids = [task["task_id"] for task in included]
            results = []
            for task in included:
                row = {"task_id": task["task_id"], "agent_id": task["agent_id"], "status": task["status"]}
                if task["status"] == "succeeded" and task.get("response_id"):
                    row["output"] = self.get_response(task["response_id"], run_id, task["task_id"])
                elif task.get("error"):
                    row["error"] = task["error"].get("category", "execution_error")
                results.append(row)
            messages = [event for event in self.list_events(run_id) if event.get("kind") == "message"]
            summary_agent_id = run.get("summary_agent_id")
            agents = self._agents() if adapter is None else None
        try:
            if adapter is not None:
                content = adapter(prompt, results, partial=partial)
            else:
                if summary_agent_id not in agents:
                    raise HubError("unknown_agent", "Configured summary agent is no longer registered")
                request = hub_protocol.build_summary_prompt(prompt, results, messages, partial=partial)
                raw = self._executor(agents, summary_agent_id, request["prompt"], agents[summary_agent_id].get("timeout", 600))
                parsed = hub_protocol.parse_summary_response(raw, task_ids=source_ids, partial=partial)
                content = parsed["content"]
            if not isinstance(content, str):
                raise HubError("invalid_summary", "Summary adapter must return text")
            if len(content.encode("utf-8", errors="strict")) > hub_protocol.MAX_SUMMARY_BYTES:
                raise HubError("invalid_summary", "Summary adapter returned invalid content")
        except Exception as exc:
            with self._locked():
                current = self._read_json(self._record_path(self.runs_dir, run_id))
                if current.get("summary", {}).get("generation_id") != generation_id:
                    return current.get("summary", {"status": "stale", "content": None})
                changed = self._run_revision_locked(run_id) != revision
                status = "stale" if changed else "failed"
                current["summary"].update(status=status, content=None, generated_at=_iso(),
                    error={"category": "summary_error", "message": str(exc)[:500]})
                self._atomic_json(self._record_path(self.runs_dir, run_id), current)
                self._event_locked(run_id, "summary", state=status, task_ids=source_ids)
                return current["summary"]
        with self._locked():
            current = self._read_json(self._record_path(self.runs_dir, run_id))
            if current.get("summary", {}).get("generation_id") != generation_id:
                return current.get("summary", {"status": "stale", "content": None})
            changed = self._run_revision_locked(run_id) != revision
            status = "stale" if changed else "partial" if partial else "ready"
            current["summary"].update(status=status, content=content, source_run_revision=revision,
                task_ids=source_ids, generated_at=_iso(), source="model_adapter",
                coverage="partial" if partial else "complete")
            self._atomic_json(self._record_path(self.runs_dir, run_id), current)
            self._event_locked(run_id, "summary", state=status, task_ids=source_ids)
            return current["summary"]

    def close(self, wait=True):
        self._pool.shutdown(wait=wait, cancel_futures=False)

