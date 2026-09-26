"""Windows current-user DPAPI adapter, implemented with the Python standard library.

This is a new standalone adapter. It does not depend on other workspace modules.
"""
import base64
import ctypes
from ctypes import wintypes
import json
import re

_PREFIX = "dpapi:"
_ENV_REFERENCE = re.compile(r"^\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}$")
_FIELD_SEPARATORS = re.compile(r"[^a-z0-9]+")
_SECRET_WORDS = {"secret", "password", "passwd", "token", "tokens",
                 "credential", "credentials", "key", "authorization",
                 "passphrase", "private"}


def is_env_reference(value):
    return isinstance(value, str) and _ENV_REFERENCE.fullmatch(value) is not None


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_api():
    if __import__("os").name != "nt":
        raise RuntimeError("Windows DPAPI is available only on Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(_DataBlob)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob), ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _blob(data):
    if len(data) > 0xFFFFFFFF:
        raise ValueError("DPAPI input exceeds the DATA_BLOB byte limit")
    storage = ctypes.create_string_buffer(data, max(1, len(data)))
    return _DataBlob(len(data), ctypes.cast(storage, ctypes.POINTER(ctypes.c_ubyte))), storage


def _crypt(data, protect):
    crypt32, kernel32 = _windows_api()
    source, source_storage = _blob(data)
    result = _DataBlob()
    description = wintypes.LPWSTR()
    try:
        if protect:
            ok = crypt32.CryptProtectData(ctypes.byref(source), "Agent Comm Bus",
                None, None, None, 0x1, ctypes.byref(result))  # CRYPTPROTECT_UI_FORBIDDEN
        else:
            ok = crypt32.CryptUnprotectData(ctypes.byref(source), ctypes.byref(description),
                None, None, None, 0x1, ctypes.byref(result))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        if result.pbData:
            kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))
        if description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))
        del source_storage


def protect_text(value):
    if not isinstance(value, str):
        raise TypeError("protect_text expects str")
    if value.startswith(_PREFIX):
        try:
            unprotect_text(value)
        except (OSError, RuntimeError, ValueError):
            # A user-supplied string can resemble our marker. Encrypt it as
            # plaintext unless this process can actually decrypt it.
            pass
        else:
            return value
    ciphertext = _crypt(value.encode("utf-8"), protect=True)
    return _PREFIX + base64.b64encode(ciphertext).decode("ascii")


def unprotect_text(value):
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        raise ValueError("Expected a dpapi: protected value")
    try:
        ciphertext = base64.b64decode(value[len(_PREFIX):].encode("ascii"), validate=True)
        return _crypt(ciphertext, protect=False).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid DPAPI protected value") from exc


def _is_secret_field(name):
    words = [word for word in _FIELD_SEPARATORS.split(str(name).lower()) if word]
    compact = "".join(words)
    return bool(_SECRET_WORDS.intersection(words) or compact in
                {"apikey", "accesstoken", "refreshtoken", "clientsecret",
                 "privatekey", "authtoken"})


def _is_output_limit(key, value):
    return key == "max_tokens" and type(value) is int and 1 <= value <= 100000


def protect_tree(value):
    """Return a JSON-compatible copy, DPAPI-protecting string values in secret fields."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if _is_output_limit(key, item):
                result[key] = item
            elif _is_secret_field(key):
                if not isinstance(item, str):
                    raise ValueError("Secret fields must be strings")
                result[key] = item if is_env_reference(item) else protect_text(item)
            else:
                result[key] = protect_tree(item)
        return result
    if isinstance(value, list):
        return [protect_tree(item) for item in value]
    return value


def unprotect_tree(value):
    """Return a JSON-compatible copy; secret fields must contain DPAPI ciphertext."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if _is_output_limit(key, item):
                result[key] = item
            elif _is_secret_field(key):
                if not isinstance(item, str):
                    raise ValueError("Stored secret fields must be strings")
                if is_env_reference(item):
                    result[key] = item
                    continue
                if not item.startswith(_PREFIX):
                    raise ValueError("Secret configuration value is not DPAPI protected")
                result[key] = unprotect_text(item)
            else:
                result[key] = unprotect_tree(item)
        return result
    if isinstance(value, list):
        return [unprotect_tree(item) for item in value]
    return value
