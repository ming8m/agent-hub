#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多 Agent 通信总线 (Agent Comm Bus)

让命令行 agent 通过共享消息总线互相通信。
所有通信内容落盘到 log/ 目录，用户可随时查看；agents.json 可随时增删 agent。

用法:
  python bus.py list                          # 列出所有已注册 agent
  python bus.py register <name> <argv-json>   # 注册 agent（argv 必须是 JSON 字符串数组）
  python bus.py run <name> "<task>"           # 让某个 agent 执行任务
  python bus.py send <from> <to> "<msg>"      # 从某 agent 发消息给另一个
  python bus.py broadcast <from> "<msg>"      # 广播给所有 agent（不含发送者自己）
  python bus.py relay <from> <to> "<task>"    # from 执行 task，结果自动转交 to 处理
  python bus.py read <agent> [n]              # 查看 agent 收件箱最近 n 条（默认 20）
  python bus.py log [n]                       # 查看最近 n 条通信记录（默认 20）
  python bus.py view                          # 打开图形对话查看窗口（viewer.py）
  python bus.py clear                         # 清空日志
  python bus.py token                         # 打印本机 WebUI 访问令牌
  python bus.py gate <case-directory>         # configured external completion gate

说明:
  - command 配成列表（如 ["agent-cli","--prompt"]）走 stdin 模式，中文/特殊字符最稳。
  - 字符串 command 只有能安全解析为 argv 时才执行；shell 回退已禁用。
  - 子进程输出自动按 UTF-8 → GBK → latin-1 顺序解码，兼容 node 系 CLI 和老工具。
  - agent 条目可选 "env": {"VAR": "val"}，调用时注入环境变量。
"""
import contextlib
import threading
from collections.abc import Mapping
import json, math, os, re, shlex, shutil, sys, subprocess, tempfile, time, uuid
from urllib.parse import quote, quote_plus

BASE = os.path.dirname(os.path.abspath(__file__))
# Runtime data is isolated from the source tree. Override with AGENT_COMM_HOME.
_DEFAULT_HOME = (os.environ.get("APPDATA") or os.environ.get("XDG_DATA_HOME") or
                os.path.join(os.path.expanduser("~"), ".local", "share"))
DATA_DIR = os.path.abspath(os.environ.get(
    "AGENT_COMM_HOME", os.path.join(_DEFAULT_HOME, "AgentCommBus")))
AGENTS_FILE = os.path.join(DATA_DIR, "agents.json")
LOG_DIR = os.path.join(DATA_DIR, "log")
RESP_DIR = os.path.join(LOG_DIR, "responses")
INBOX_DIR = os.path.join(DATA_DIR, "inbox")
LOG_FILE = os.path.join(LOG_DIR, "chat.log")

# Windows sensitive-storage operations use the bundled DPAPI adapter; off Windows they fail closed.
_sec = None
_SECRET_FIELD_SEPARATORS = re.compile(r"[^a-z0-9]+")
_SECRET_WORDS = {"secret", "password", "passwd", "token", "tokens",
                 "credential", "credentials", "key", "authorization",
                 "passphrase", "private"}
_ENV_REFERENCE_RE = re.compile(r"^\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ARGUMENT_RE = re.compile(
    r"(?i)^--?(?:api[-_]?key|access[-_]?token|client[-_]?secret|password|passwd|token|credential)(?:=|$)"
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:sk-proj-|sk-)[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(?P<label>\b[A-Z0-9_]*(?:API[_-]?KEY|ACCESS[_-]?TOKEN|CLIENT[_-]?SECRET|PASSWORD|PASSWD|AUTHORIZATION)\b\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^\s\"'&,;]+)"),
)
_SAFE_INHERITED_ENV = {
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "TERM", "COMSPEC", "PATHEXT",
}
_REDACTION_SECRET_LOCK = threading.Lock()
_ADDITIONAL_REDACTION_SECRETS = set()

def _is_secret_field(name):
    words = [word for word in _SECRET_FIELD_SEPARATORS.split(str(name).lower()) if word]
    compact = "".join(words)
    return bool(_SECRET_WORDS.intersection(words) or compact in
                {"apikey", "accesstoken", "refreshtoken", "clientsecret",
                 "privatekey", "authtoken"})

def _require_secrets():
    global _sec
    if _sec is None:
        if os.name != "nt":
            raise RuntimeError("Sensitive configuration storage requires Windows DPAPI")
        import win_dpapi as _sec
    required = ("protect_text", "unprotect_text")
    if not all(callable(getattr(_sec, name, None)) for name in required):
        raise RuntimeError("Bundled Windows DPAPI adapter has an invalid interface")
    return _sec

def _is_env_reference(value):
    return isinstance(value, str) and _ENV_REFERENCE_RE.fullmatch(value) is not None

def _valid_dpapi_shape(value):
    if not isinstance(value, str) or not value.startswith("dpapi:"):
        return False
    try:
        import base64
        raw = value[len("dpapi:"):].encode("ascii")
        return bool(raw) and bool(base64.b64decode(raw, validate=True))
    except (UnicodeError, ValueError):
        return False

def protect_config_tree(value):
    """Copy config and protect named secret fields without a plaintext fallback.

    `${ENV:NAME}` references are portable and stored as references. Raw secret
    values are persisted only through current-user DPAPI on Windows.
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if _is_secret_field(key) and isinstance(item, str):
                if _is_env_reference(item):
                    result[key] = item
                elif os.name != "nt":
                    raise RuntimeError(
                        "Raw secret values cannot be persisted on this platform; use an environment reference"
                    )
                else:
                    result[key] = _require_secrets().protect_text(item)
            else:
                result[key] = protect_config_tree(item)
        return result
    if isinstance(value, list):
        return [protect_config_tree(item) for item in value]
    return value

def unprotect_config_tree(value, *, resolve_env=False):
    """Copy config, decrypt DPAPI fields when available, and optionally expand env refs."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if _is_secret_field(key) and isinstance(item, str):
                if _is_env_reference(item):
                    if resolve_env:
                        name = _ENV_REFERENCE_RE.fullmatch(item).group(1)
                        resolved = os.environ.get(name)
                        if resolved is None:
                            raise RuntimeError("A required secret environment variable is not set")
                        result[key] = resolved
                    else:
                        result[key] = item
                elif not item.startswith("dpapi:"):
                    raise RuntimeError("Stored secret configuration is not protected")
                elif os.name != "nt":
                    raise RuntimeError("DPAPI-protected configuration requires Windows")
                else:
                    result[key] = _require_secrets().unprotect_text(item)
            else:
                result[key] = unprotect_config_tree(item, resolve_env=resolve_env)
        return result
    if isinstance(value, list):
        return [unprotect_config_tree(item, resolve_env=resolve_env) for item in value]
    return value

def _secret_values(value):
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_secret_field(key) and isinstance(item, str):
                if not _is_env_reference(item) and not item.startswith("dpapi:") and item:
                    found.append(item)
            else:
                found.extend(_secret_values(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_secret_values(item))
    return found

def _configured_secret_values():
    try:
        config = load_agents(runtime=False)
    except Exception:
        return []
    found = []
    def visit(value):
        if isinstance(value, Mapping):
            for key, item in value.items():
                if _is_secret_field(key) and isinstance(item, str):
                    if _is_env_reference(item):
                        name = _ENV_REFERENCE_RE.fullmatch(item).group(1)
                        env_value = os.environ.get(name)
                        if env_value:
                            found.append(env_value)
                    elif item.startswith("dpapi:") and os.name == "nt":
                        try:
                            found.append(_require_secrets().unprotect_text(item))
                        except Exception:
                            pass
                else:
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
    visit(config)
    return found

def register_redaction_secrets(values):
    """Keep provider secrets in process memory for shared log/output redaction."""
    with _REDACTION_SECRET_LOCK:
        for value in values or ():
            if isinstance(value, str) and value:
                _ADDITIONAL_REDACTION_SECRETS.add(value)

def _known_redaction_secrets(extra_secrets=()):
    values = set(v for v in extra_secrets or () if isinstance(v, str) and v)
    with _REDACTION_SECRET_LOCK:
        values.update(_ADDITIONAL_REDACTION_SECRETS)
    values.update(_configured_secret_values())
    return values

def redact_sensitive_text(value, extra_secrets=()):
    """Redact supplied secret values and common credential forms from text."""
    text = str(value)
    for secret in sorted(_known_redaction_secrets(extra_secrets), key=len, reverse=True):
        variants = {
            secret,
            quote(secret, safe=""),
            quote_plus(secret, safe=""),
            json.dumps(secret, ensure_ascii=True)[1:-1],
            json.dumps(secret, ensure_ascii=False)[1:-1],
        }
        for variant in sorted(variants, key=len, reverse=True):
            if not variant:
                continue
            if len(variant) < 4:
                text = re.sub(r"(?<!\w)" + re.escape(variant) + r"(?!\w)",
                              "[REDACTED]", text)
            else:
                text = text.replace(variant, "[REDACTED]")
    for pattern in _SECRET_VALUE_PATTERNS:
        if "label" in pattern.groupindex:
            text = pattern.sub(lambda m: f"{m.group('label')}{m.group('quote')}[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text

def redact_sensitive_data(value, extra_secrets=()):
    """Redact string values recursively before JSON encoding."""
    if isinstance(value, str):
        return redact_sensitive_text(value, extra_secrets)
    if isinstance(value, dict):
        return {key: redact_sensitive_data(item, extra_secrets)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_sensitive_data(item, extra_secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item, extra_secrets) for item in value)
    return value

def _redact_json(value, extra_secrets=()):
    return json.dumps(redact_sensitive_data(value, extra_secrets), ensure_ascii=False)

def _validate_stored_secret_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_secret_field(key) and isinstance(item, str):
                if _is_env_reference(item):
                    continue
                if not _valid_dpapi_shape(item):
                    raise RuntimeError("Stored secret configuration is not protected")
                if os.name == "nt":
                    _require_secrets().unprotect_text(item)
            else:
                _validate_stored_secret_fields(item)
    elif isinstance(value, list):
        for item in value:
            _validate_stored_secret_fields(item)

def _has_recognized_secret_text(value):
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)

def _validate_no_inline_credentials(value):
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_secret_field(key):
                _validate_no_inline_credentials(item)
                continue
            if str(key).lower() == "command" and isinstance(item, list):
                for index, arg in enumerate(item):
                    if isinstance(arg, str) and _SECRET_ARGUMENT_RE.match(arg):
                        raise ValueError("Store credentials in a protected env field, not command arguments")
                    if (index > 0 and isinstance(arg, str)
                            and _SECRET_ARGUMENT_RE.match(item[index - 1])):
                        raise ValueError("Store credentials in a protected env field, not command arguments")
            elif isinstance(item, str) and _has_recognized_secret_text(item):
                raise ValueError("Store credentials in a protected secret field, not ordinary config text")
            _validate_no_inline_credentials(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and _has_recognized_secret_text(item):
                raise ValueError("Store credentials in a protected secret field, not ordinary config text")
            _validate_no_inline_credentials(item)

def _protect_existing_config_tree(value):
    """Preserve verified ciphertext from a disk-backed protected view."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if _is_secret_field(key) and isinstance(item, str):
                if _is_env_reference(item):
                    result[key] = item
                elif _valid_dpapi_shape(item):
                    if os.name == "nt":
                        _require_secrets().unprotect_text(item)
                    result[key] = item
                else:
                    result[key] = protect_config_tree({key: item})[key]
            else:
                result[key] = _protect_existing_config_tree(item)
        return result
    if isinstance(value, list):
        return [_protect_existing_config_tree(item) for item in value]
    return value

# Agent 名称白名单：收件箱/日志文件名由它拼出，必须防路径注入
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS = {}


def check_name(name):
    if not NAME_RE.match(name or ""):
        raise ValueError(
            f"非法 agent 名 '{name}'：仅允许字母数字_-（1-32 字符）"
        )

# 常见 CLI 安装位置（npm 垫片 / 本地 bin），用于解析可执行文件
NPM_BIN = os.path.join(os.environ.get("APPDATA", ""), "npm")
LOCAL_BIN = os.path.join(os.path.expanduser("~"), ".local", "bin")

# 默认注册表（内置几个常用 agent）
DEFAULT_AGENTS = {}


def ensure_dirs():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(RESP_DIR, exist_ok=True)
    os.makedirs(INBOX_DIR, exist_ok=True)


class RuntimeAgents(dict):
    """解密后的运行时 agents 视图（仅内存使用）。

    save_agents() 拒绝保存该类型：防止注册/编辑等操作把运行时解密出的
    明文凭据写回磁盘（R01）。"""
    runtime = True

class ProtectedAgents(dict):
    """Disk-backed view whose existing secret values were checked as opaque data."""
    protected_view = True


def load_agents(runtime=False):
    """读取 agents.json。

    runtime=False（默认）：返回磁盘原始结构（dpapi: 密文保持密文），
        供注册/编辑等需要回存的操作使用。
    runtime=True：返回解密后的 RuntimeAgents 视图（用于执行 agent、
        注入 env）；该视图禁止交给 save_agents()。
    """
    if not os.path.exists(AGENTS_FILE):
        raw = ProtectedAgents(json.loads(json.dumps(DEFAULT_AGENTS)))
        return _to_runtime(raw) if runtime else raw
    with open(AGENTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    _validate_stored_secret_fields(data)
    _validate_no_inline_credentials(data)
    raw = ProtectedAgents(data)
    return _to_runtime(raw) if runtime else raw


def _to_runtime(data):
    view = RuntimeAgents()
    view.update(unprotect_config_tree(data, resolve_env=True))
    return view


@contextlib.contextmanager
def _agents_write_lock():
    """agents.json 专属写锁（agents.json.lock 第 0 字节），覆盖读-改-写全程。"""
    lock_path = AGENTS_FILE + ".lock"
    lf = open(lock_path, "a", encoding="utf-8")
    pos = _lock_fd(lf)
    try:
        yield
    finally:
        _unlock_fd(lf, pos)
        lf.close()


def _dump_atomic(path, data):
    """临时文件 + fsync + os.replace 原子替换（调用方须已持锁）。"""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                               suffix=".agents.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_agents(agents):
    """保存 agents.json：密文保持密文，疑似明文密钥字段统一加密后落盘。

    - 拒绝保存 RuntimeAgents / 带 runtime 标记的对象（明文回写防护）
    - Windows 敏感字段使用 DPAPI 保护；非 Windows 拒绝明文落盘
    - 无敏感字段的配置可在无后端平台保存
    - 配置写锁 + 原子替换；写入失败保留原文件
    """
    if getattr(agents, "runtime", False):
        raise ValueError(
            "拒绝保存运行时(已解密)的 agents 对象；"
            "编辑请基于 load_agents() 默认返回的密文视图"
        )
    _validate_no_inline_credentials(agents)
    if isinstance(agents, ProtectedAgents):
        protected = _protect_existing_config_tree(agents)
    else:
        protected = protect_config_tree(agents)
    with _agents_write_lock():
        _dump_atomic(AGENTS_FILE, protected)


def _lock_fd(f):
    """Windows: msvcrt 锁第 0 字节；Unix: flock 整个文件。带重试。
    固定锁文件头而非末尾：锁末尾字节时文件一旦增长，后到进程会锁到
    不同位置，互斥失效；锁第 0 字节保证所有写入方竞争同一把锁。"""
    try:
        import msvcrt
        f.seek(0)
        for _ in range(100):
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)
        raise TimeoutError("file lock timeout")
    except ImportError:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return None


def _unlock_fd(f, pos=None):
    try:
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except ImportError:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _path_lock(path):
    """Serialize operations on a path across threads and processes.

    The lock lives beside the data file so replacing or deleting the data file
    cannot detach a writer from the lock used by clear/read-modify operations.
    """
    absolute = os.path.abspath(path)
    lock_path = absolute + ".lock"
    with _PATH_LOCKS_GUARD:
        thread_lock = _PATH_LOCKS.setdefault(absolute, threading.Lock())
    with thread_lock:
        os.makedirs(os.path.dirname(absolute), exist_ok=True)
        with open(lock_path, "a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            pos = _lock_fd(lock_file)
            try:
                yield
            finally:
                _unlock_fd(lock_file, pos)


def append_line(path, line):
    """带旁路文件锁的追加写，避免并发交错及清空竞态。"""
    ensure_dirs()
    with _path_lock(path):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()


def append_log(entry, extra_secrets=()):
    append_line(LOG_FILE, json.dumps(
        redact_sensitive_data(entry, extra_secrets), ensure_ascii=False))


def append_inbox(agent, entry, extra_secrets=()):
    append_line(os.path.join(INBOX_DIR, agent + ".jsonl"),
                json.dumps(redact_sensitive_data(entry, extra_secrets), ensure_ascii=False))


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _decode_output(b):
    """按 UTF-8 → GBK → latin-1 链解码子进程输出。
    多数命令行工具输出 UTF-8；cmd 内建/老工具
    输出系统 GBK 代码页。UTF-8 严格解码失败才尝试后续编码。"""
    if not b:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1", errors="replace")


def _string_to_argv(cmd_spec, task, env_path):
    """把字符串命令模板解析成 argv 直调列表（中文/引号原样保真）。
    模板含 shell 元字符、引号不闭合或解析不到可执行文件时返回 None。"""
    if any(ch in cmd_spec for ch in '|&<>^%()'):
        return None
    try:
        argv = shlex.split(cmd_spec, posix=False)
    except ValueError:
        return None
    # posix=False 会保留成对引号，去掉外层引号（保留反斜杠，兼容 Windows 路径）
    argv = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'" else a
            for a in argv]
    if not argv or not shutil.which(argv[0], path=env_path):
        return None
    return [a.replace("{prompt}", task) if "{prompt}" in a else a for a in argv]

def _build_child_environment(spec_env):
    if not isinstance(spec_env, Mapping):
        raise ValueError("Agent environment configuration is invalid")
    child_env = {
        key: value for key, value in os.environ.items()
        if key.upper() in _SAFE_INHERITED_ENV and not _is_secret_field(key)
    }
    for key, value in spec_env.items():
        if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
            raise ValueError("Agent environment variable name is invalid")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("Agent environment value is invalid")
        if _is_env_reference(value):
            if not _is_secret_field(key):
                raise ValueError("Environment references are allowed only for secret fields")
            env_name = _ENV_REFERENCE_RE.fullmatch(value).group(1)
            value = os.environ.get(env_name)
            if value is None:
                raise ValueError("A required secret environment variable is not set")
        child_env[key] = value
    return child_env


def _windows_job_object():
    """Create a kill-on-close Job Object, or return None when unavailable."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = None
    try:
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = ExtendedLimit()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. No breakaway flag is enabled.
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel.CloseHandle(job)
            return None
    except Exception:
        if job:
            kernel.CloseHandle(job)
        return None
    return kernel, job


def _resume_suspended_process(pid):
    """Resume the single initial thread of a CREATE_SUSPENDED child by PID."""
    if os.name != "nt" or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    import ctypes
    from ctypes import wintypes

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
    kernel.Thread32First.restype = wintypes.BOOL
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
    kernel.Thread32Next.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == wintypes.HANDLE(-1).value:
        return False
    thread_ids = []
    entry = ThreadEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                thread_ids.append(entry.th32ThreadID)
            ok = kernel.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    if len(thread_ids) != 1:
        return False
    thread = kernel.OpenThread(0x0002, False, thread_ids[0])  # THREAD_SUSPEND_RESUME
    if not thread:
        return False
    try:
        return kernel.ResumeThread(thread) != 0xFFFFFFFF
    finally:
        kernel.CloseHandle(thread)


def _windows_process_matches(proc):
    """Check that proc's still-live Windows PID refers to its original process."""
    if proc.poll() is not None or isinstance(proc.pid, bool) or not isinstance(proc.pid, int) or proc.pid <= 0:
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    access = 0x1000 | 0x00100000  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    target = kernel.OpenProcess(access, False, proc.pid)
    if not target:
        return False
    try:
        original_creation, target_creation = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        original_exit, target_exit = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        original_kernel, target_kernel = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        original_user, target_user = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        if not kernel.GetProcessTimes(wintypes.HANDLE(proc._handle), ctypes.byref(original_creation),
                ctypes.byref(original_exit), ctypes.byref(original_kernel), ctypes.byref(original_user)):
            return False
        if not kernel.GetProcessTimes(target, ctypes.byref(target_creation), ctypes.byref(target_exit),
                ctypes.byref(target_kernel), ctypes.byref(target_user)):
            return False
        code = wintypes.DWORD()
        return (kernel.GetExitCodeProcess(target, ctypes.byref(code)) and code.value == 259
                and original_creation.value == target_creation.value)
    finally:
        kernel.CloseHandle(target)


def _taskkill_process_tree(proc):
    """Fallback tree kill with strict live-process and creation-time validation."""
    if os.name != "nt" or not _windows_process_matches(proc):
        return False
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    taskkill = os.path.join(system_root, "System32", "taskkill.exe")
    if not os.path.isfile(taskkill) or not _windows_process_matches(proc):
        return False
    try:
        result = subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_process_tree(argv, *, input_bytes, timeout, env):
    """Run a process with descendant cleanup on timeout (Windows Job / POSIX group)."""
    if os.name == "nt":
        import ctypes
        job_info = _windows_job_object()
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input_bytes is not None else None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, creationflags=flags)
        except BaseException:
            if job_info is not None:
                job_info[0].CloseHandle(job_info[1])
            raise
        job = None
        if job_info is not None:
            kernel, job = job_info
            try:
                kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                kernel.AssignProcessToJobObject.restype = ctypes.c_int
                assigned = bool(kernel.AssignProcessToJobObject(job, ctypes.c_void_p(int(proc._handle))))
            except (OSError, AttributeError, TypeError, ValueError):
                assigned = False
            if not assigned:
                kernel.CloseHandle(job)
                job = None
        try:
            resumed = _resume_suspended_process(proc.pid)
        except Exception:
            resumed = False
        if not resumed:
            terminated = False
            if job is not None:
                try:
                    kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                    kernel.TerminateJobObject.restype = ctypes.c_int
                    terminated = bool(kernel.TerminateJobObject(job, 1))
                except (OSError, AttributeError, TypeError, ValueError):
                    terminated = False
            if not terminated:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            if job is not None:
                kernel.CloseHandle(job)
            raise OSError("Unable to resume suspended agent process")
        try:
            try:
                stdout, stderr = proc.communicate(input=input_bytes, timeout=timeout)
            except subprocess.TimeoutExpired:
                cleaned = False
                if job is not None:
                    try:
                        kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                        kernel.TerminateJobObject.restype = ctypes.c_int
                        cleaned = bool(kernel.TerminateJobObject(job, 1))
                    except (OSError, AttributeError, TypeError, ValueError):
                        cleaned = False
                if not cleaned:
                    try:
                        cleaned = _taskkill_process_tree(proc)
                    except Exception:
                        cleaned = False
                if not cleaned and proc.poll() is None:
                    proc.kill()
                try:
                    proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                raise
            return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
        finally:
            if job is not None:
                # Also terminates any descendants which outlived a successful CLI.
                kernel.CloseHandle(job)

    import signal
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input_bytes is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(input=input_bytes, timeout=timeout)
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise


def run_agent(agents, name, task, timeout=None):
    """调用 agent CLI 执行 task，返回 (ok, output)。
    command 为列表时：subprocess 直调 + stdin 传 prompt（避免中文编码/元字符问题）。
    command 为字符串时：仅在可安全解析为 argv 时直调，否则拒绝。
    agent 条目可带 "env" 注入环境变量；timeout=None 时读取该 agent 的
    timeout 配置，未配置时默认 600 秒。"""
    known_secrets = _secret_values(agents) + list(_known_redaction_secrets())
    if name not in agents:
        return False, redact_sensitive_text(
            f"[错误] agent '{name}' 未注册。可用: {', '.join(agents.keys())}", known_secrets)
    try:
        check_name(name)
    except ValueError as e:
        return False, redact_sensitive_text(f"[错误] {e}", known_secrets)
    spec = agents[name]
    timeout = spec.get("timeout", 600) if timeout is None else timeout
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        return False, redact_sensitive_text(
            f"[错误] agent '{name}' 的 timeout 必须是正数秒，当前为 {timeout!r}", known_secrets)
    cmd_spec = spec["command"]
    safe_task = redact_sensitive_text(task, known_secrets)
    entry = {
        "id": uuid.uuid4().hex[:8],
        "time": now(),
        "type": "task",
        "from": "user",
        "to": name,
        "task": safe_task,
    }
    append_log(entry, known_secrets)
    append_inbox(name, entry, known_secrets)
    try:
        child_env = _build_child_environment(spec.get("env", {}))
    except ValueError:
        return False, "[拒绝] Agent 环境配置无效或所需密钥环境变量不可用。"
    # 扩展 PATH，使 npm .CMD 垫片和 ~/.local/bin 可被解析
    child_env["PATH"] = os.pathsep.join(
        [p for p in (NPM_BIN, LOCAL_BIN) if p] + [child_env.get("PATH", "")])
    argv = None
    use_stdin = True
    if isinstance(cmd_spec, list):
        exe = shutil.which(cmd_spec[0], path=child_env["PATH"])
        # 解析不到也原样直调，让 FileNotFoundError 带出可执行文件名
        argv = ([exe] + list(cmd_spec[1:])) if exe else list(cmd_spec)
        if any("{prompt}" in a for a in argv):
            # 列表命令也支持 {prompt} 占位符：替换进 argv，不走 stdin
            argv = [a.replace("{prompt}", safe_task) for a in argv]
            use_stdin = False
    else:
        argv = _string_to_argv(cmd_spec, safe_task, child_env["PATH"])
        if argv is None:
            # 安全策略：不再回退 shell=True。含 shell 元字符或无法解析的
            # 字符串命令一律拒绝，必须改写成 agents.json 的列表（argv）形式。
            return False, (
                f"[拒绝] agent '{name}' 的字符串命令无法安全解析为 argv，"
                "shell 回退已禁用。请把 agents.json 中该 command 改成列表形式，如 "
                '["agent-cli","--prompt"]，并用 {prompt} 占位或走 stdin。'
            )
    try:
        r = _run_process_tree(argv,
            input_bytes=safe_task.encode("utf-8") if use_stdin else None,
            timeout=timeout, env=child_env)
        out = redact_sensitive_text(_decode_output(r.stdout).strip(), known_secrets)
        if r.returncode != 0:
            stderr = redact_sensitive_text(_decode_output(r.stderr).strip(), known_secrets)
            if stderr:
                out = f"{out}\n\n[stderr]\n{stderr}" if out else stderr
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        ok, out = False, f"[超时] {name} 执行超过 {timeout}s"
    except FileNotFoundError as e:
        ok, out = False, "[错误] 配置的 CLI 无法启动。请检查 command 与 PATH。"
    except Exception as e:
        ok, out = False, f"[异常] {type(e).__name__}: 执行失败；错误详情已省略。"
    out = redact_sensitive_text(out, known_secrets)
    # 完整输出落盘 responses/<id>.txt，log 里存摘要 + 路径
    resp_id = uuid.uuid4().hex[:8]
    full_path = None
    try:
        ensure_dirs()
        with open(os.path.join(RESP_DIR, resp_id + ".txt"), "w", encoding="utf-8") as f:
            f.write(out)
        full_path = os.path.join("log", "responses", resp_id + ".txt")
    except Exception:
        pass
    resp = {
        "id": resp_id,
        "time": now(),
        "type": "response",
        "from": name,
        "to": "user",
        "content": out[:4000],
        "full": full_path,
        "ok": ok,
    }
    append_log(resp, known_secrets)
    append_inbox(name, resp, known_secrets)
    return ok, out


def cmd_list(agents):
    print("已注册 agent：")
    known_secrets = _secret_values(agents)
    for k, v in agents.items():
        details = {"desc": v.get("desc", ""), "command": v.get("command", [])}
        print(f"  - {redact_sensitive_text(k, known_secrets)}: "
              f"{redact_sensitive_text(json.dumps(details, ensure_ascii=False), known_secrets)}")


def cmd_register(name, command):
    """注册 agent：基于密文配置做读-改-写，全程持锁。

    修改的是磁盘原始（密文）结构，其他 agent 的 dpapi: 密文原样保留，
    不会出现"新增一个 agent、其余凭据变明文"的泄露路径（R01）。
    """
    check_name(name)
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("command must be a non-empty argv list of strings")
    _validate_no_inline_credentials({"command": command})
    with _agents_write_lock():
        raw = load_agents()  # 密文视图
        raw[name] = {"command": command, "desc": ""}
        protected = _protect_existing_config_tree(raw)
        _dump_atomic(AGENTS_FILE, protected)
    print(redact_sensitive_text(f"已注册 agent: {name}  ->  {command}",
                               _known_redaction_secrets()))


def cmd_send(agents, frm, to, msg):
    try:
        check_name(to)
    except ValueError as e:
        print(f"[错误] {e}")
        return
    if to not in agents:
        print(f"[错误] 目标 agent '{to}' 未注册，消息未投递。可用: {', '.join(agents.keys())}")
        return
    entry = {
        "id": uuid.uuid4().hex[:8],
        "time": now(),
        "type": "message",
        "from": frm,
        "to": to,
        "content": msg,
    }
    append_log(entry)
    append_inbox(to, entry)
    print(f"[{now()}] {frm} -> {to}: {msg}")


def cmd_broadcast(agents, frm, msg):
    sent = []
    for to in agents:
        if to == frm:
            continue  # 不发回发送者自己
        cmd_send(agents, frm, to, msg)
        sent.append(to)
    if not sent:
        print(f"[{now()}] 没有其他 agent 可接收广播")


def cmd_relay(agents, frm, to, task):
    print(f"=== [{now()}] {frm} 执行任务 ===")
    ok, out = run_agent(agents, frm, task)
    print(out[:2000])
    if ok:
        print(f"\n=== [{now()}] 结果转交 {to} 审阅验证 ===")
        relay_task = (f"另一位 agent（{frm}）刚完成了任务：「{task}」\n"
                      f"它的回答是：{out}\n\n"
                      f"请你作为审阅者，验证这个回答是否正确。"
                      f"正确就回复「正确」，错误就给出正确答案。")
        cmd_send(agents, frm, to, f"[来自 {frm} 的结果] {out[:500]}")
        ok2, out2 = run_agent(agents, to, relay_task)
        print(out2[:2000])
        return ok2
    else:
        print(f"\n[{frm}] 执行失败，跳过审阅")
        return False


def cmd_read(agents, name, n=20):
    if name not in agents:
        print(f"[错误] agent '{name}' 未注册。可用: {', '.join(agents.keys())}")
        return
    try:
        check_name(name)
    except ValueError as e:
        print(f"[错误] {e}")
        return
    p = os.path.join(INBOX_DIR, name + ".jsonl")
    if not os.path.exists(p):
        print(f"（{name} 的收件箱为空）")
        return
    entries = _read_jsonl(p)
    for e in entries[-n:]:
        t = e.get("time", "?")
        if e.get("type") == "task":
            print(f"[{t}] 任务 | {str(e.get('task', ''))[:150]}")
        elif e.get("type") == "response":
            mark = "OK" if e.get("ok") else "FAIL"
            print(f"[{t}] 我方回复 {mark} | {str(e.get('content', ''))[:200]}")
        else:
            print(f"[{t}] {e.get('from', '?')} -> {e.get('to', '?')} | {str(e.get('content', ''))[:150]}")


def _read_jsonl(path):
    """Read valid object records, skipping a truncated/corrupt JSONL row."""
    entries = []
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(entry, dict) and entry.get("type") in {"task", "response", "message"}:
                    entries.append(entry)
    except OSError:
        return []
    return entries


def cmd_log(agents, n):
    if not os.path.exists(LOG_FILE):
        print("（暂无日志）")
        return
    entries = _read_jsonl(LOG_FILE)
    for e in entries[-n:]:
        t = e.get("time", "?")
        if e.get("type") == "task":
            print(f"[{t}] USER -> {e.get('to', '?')} | 任务: {str(e.get('task', ''))[:120]}")
        elif e.get("type") == "response":
            mark = "OK" if e.get("ok") else "FAIL"
            extra = f" (全文: {e.get('full')})" if e.get("full") else ""
            print(f"[{t}] {e.get('from', '?')} -> USER {mark} | {str(e.get('content', ''))[:200]}{extra}")
        else:
            print(f"[{t}] {e.get('from', '?')} -> {e.get('to', '?')} | {str(e.get('content', ''))[:120]}")


def clear_log():
    """Remove the main log under the same lock used by append_log()."""
    with _path_lock(LOG_FILE):
        if os.path.exists(LOG_FILE):
            os.remove(LOG_FILE)


def cmd_clear():
    clear_log()
    print("日志已清空")


def cmd_view():
    """启动图形对话查看窗口（viewer.py）。Windows 下用 DETACHED_PROCESS
    脱离当前控制台，关闭终端不影响窗口。"""
    viewer = os.path.join(BASE, "viewer.py")
    if not os.path.exists(viewer):
        print("[错误] 缺少 viewer.py")
        return
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS |
                                   subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, viewer], **kwargs)
    print("已打开对话查看窗口（独立进程，关闭终端不影响）")


def cmd_chat():
    """启动 WebUI 聊天窗口（server.py），可直连或广播已配置 agent。"""
    server = os.path.join(BASE, "server.py")
    if not os.path.exists(server):
        print("[错误] 缺少 server.py")
        return
    subprocess.call([sys.executable, server])


def cmd_token():
    """打印 Windows WebUI 令牌；elsewhere the token is process-session only."""
    import auth
    if os.name != "nt":
        print("非 Windows WebUI 使用进程内会话令牌；请从本地 WebUI 页面使用，CLI 不读取或保存该令牌。")
        return 1
    token = auth.get_or_create_token()
    print(f"访问令牌: {token}")
    print("用法: curl -H \"X-Agent-Bus-Token: <令牌>\" http://127.0.0.1:8765/api/state")
    return 0


def cmd_gate(case_path):
    """调用显式配置的外部完成门禁 provider；provider 缺失时拒绝通过。

    用法: python bus.py gate <案件目录或 blackboard.json 路径>
    退出码 0 = 全部通过；1 = 存在未通过的门禁（禁止宣告完成）。
    """
    for stream in (sys.stdout, sys.stderr):
        # 被其他进程直接 import 调用时，控制台可能仍是 GBK，重配置避免
        # 门禁符号（✓/⚠）触发 UnicodeEncodeError
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    p = os.path.abspath(case_path)
    if os.path.isdir(p):
        p = os.path.join(p, "blackboard.json")
    if not os.path.exists(p):
        print(f"[错误] 找不到黑板: {p}")
        print("提示: 确认案件目录及已配置的门禁 provider")
        return 1
    try:
        module_name = os.environ.get("AGENT_COMM_GATE_MODULE", "")
        if not module_name:
            print("[错误] 未配置完成门禁 provider；无法验证案件状态，拒绝通过。")
            return 1
        import importlib
        provider = importlib.import_module(module_name)
        full_health_check = provider.full_health_check
    except (ImportError, AttributeError) as e:
        print(f"[错误] 完成门禁 provider 不可用: {type(e).__name__}")
        return 1
    try:
        results = full_health_check(path=p)
        if not isinstance(results, Mapping) or not results:
            raise ValueError("provider must return a non-empty mapping of checks")
        normalized = []
        for name, result in results.items():
            passed = result.passed
            failures = result.failures
            warnings = result.warnings
            if not isinstance(passed, bool):
                raise ValueError("each check must expose boolean passed")
            if isinstance(failures, (str, bytes)) or isinstance(warnings, (str, bytes)):
                raise ValueError("failures and warnings must be iterables of messages")
            normalized.append((name, passed, list(failures), list(warnings)))
    except Exception as e:
        print(f"[错误] 门禁执行失败或返回格式无效: {type(e).__name__}")
        return 1
    all_pass = True
    for name, passed, failures, warnings in normalized:
        mark = "PASS" if passed else "FAIL"
        print(f"[{mark}] {name}")
        for f in failures:
            print(f"       ✗ {f}")
        for wtext in warnings:
            print(f"       ⚠ {wtext}")
        all_pass = all_pass and passed
    print(f"\n{'✅ 全部门禁通过，可以宣告完成' if all_pass else '❌ 存在未通过门禁，禁止宣告完成'}")
    return 0 if all_pass else 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ensure_dirs()
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    # gate 等独立命令无需读取 agents.json。
    agent_commands = {"list", "run", "send", "broadcast", "relay", "read"}
    agents = load_agents(runtime=True) if cmd in agent_commands else None
    if cmd == "list":
        cmd_list(agents)
    elif cmd == "register" and len(args) >= 3:
        try:
            argv = json.loads(args[2])
            cmd_register(args[1], argv)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"[错误] register command 必须是 JSON argv 数组: {exc}")
            return 2
    elif cmd == "run" and len(args) >= 3:
        ok, out = run_agent(agents, args[1], args[2])
        print(out)
        print(f"\n[{'成功' if ok else '失败'}]")
        return 0 if ok else 1
    elif cmd == "send" and len(args) >= 4:
        cmd_send(agents, args[1], args[2], args[3])
    elif cmd == "broadcast" and len(args) >= 3:
        cmd_broadcast(agents, args[1], args[2])
    elif cmd == "relay" and len(args) >= 4:
        return 0 if cmd_relay(agents, args[1], args[2], args[3]) else 1
    elif cmd == "read" and len(args) >= 2:
        try:
            n = int(args[2]) if len(args) > 2 else 20
        except ValueError:
            print("[提示] 条数参数无效，使用默认 20")
            n = 20
        cmd_read(agents, args[1], n)
    elif cmd == "log":
        try:
            n = int(args[1]) if len(args) > 1 else 20
        except ValueError:
            print("[提示] 条数参数无效，使用默认 20")
            n = 20
        cmd_log(agents, n)
    elif cmd == "view":
        cmd_view()
    elif cmd == "chat":
        cmd_chat()
    elif cmd == "clear":
        cmd_clear()
    elif cmd == "token":
        return cmd_token()
    elif cmd == "gate" and len(args) >= 2:
        return cmd_gate(args[1])
    else:
        print(__doc__)


if __name__ == "__main__":
    raise SystemExit(main())

