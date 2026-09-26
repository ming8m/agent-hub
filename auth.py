# -*- coding: utf-8 -*-
"""Local WebUI token storage using the bundled Windows DPAPI implementation.

The standard-library adapter uses current-user DPAPI on Windows.
A token file that exists but is damaged is rejected and never silently replaced.
"""
import getpass
import os
import re
import secrets as _secrets
import subprocess
import tempfile
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
_DEFAULT_HOME = Path(os.environ.get("APPDATA") or os.environ.get("XDG_DATA_HOME") or
                       (Path.home() / ".local" / "share"))
DATA_DIR = Path(os.environ.get("AGENT_COMM_HOME", _DEFAULT_HOME / "AgentCommBus"))
TOKEN_FILE = DATA_DIR / ".bus-token"
_SESSION_TOKEN = None
_SESSION_TOKEN_LOCK = threading.Lock()


def _secure_backend():
    if os.name != "nt":
        raise RuntimeError("Secure token storage requires Windows DPAPI")
    import win_dpapi as backend
    required = ("protect_text", "unprotect_text")
    if not all(callable(getattr(backend, name, None)) for name in required):
        raise RuntimeError("Bundled Windows DPAPI adapter has an invalid interface")
    return backend

def _harden_acl(path: Path) -> None:
    """去掉继承权限，仅保留当前用户完全控制（Windows；失败不致命）。"""
    if os.name != "nt":
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _read_stored_token(backend) -> str:
    if TOKEN_FILE.is_symlink():
        raise RuntimeError("Stored WebUI token path is not a regular file")
    try:
        protected = TOKEN_FILE.read_text(encoding="utf-8").strip()
        token = backend.unprotect_text(protected)
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise ValueError("invalid token format")
        return token
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError(
            "Stored WebUI token is damaged or invalid; refusing to replace it"
        ) from None


def get_or_create_token() -> str:
    """Return this process's WebUI token.

    Windows stores a DPAPI-protected token for stable local use. Other platforms
    use a process-only token; it is never written to disk or shared with a
    different server process.
    """
    global _SESSION_TOKEN
    if os.name != "nt":
        with _SESSION_TOKEN_LOCK:
            if _SESSION_TOKEN is None:
                _SESSION_TOKEN = _secrets.token_hex(32)
            return _SESSION_TOKEN

    backend = _secure_backend()
    if TOKEN_FILE.exists() or TOKEN_FILE.is_symlink():
        return _read_stored_token(backend)

    token = _secrets.token_hex(32)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    protected = backend.protect_text(token)
    fd, temp_name = tempfile.mkstemp(prefix=".bus-token-", dir=str(TOKEN_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(protected)
            stream.flush()
            os.fsync(stream.fileno())
        _harden_acl(Path(temp_name))
        try:
            # Hard-link publication is atomic and fails if another process won.
            os.link(temp_name, TOKEN_FILE)
        except FileExistsError:
            return _read_stored_token(backend)
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
    _harden_acl(TOKEN_FILE)
    return token
