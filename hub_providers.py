"""Explicit adapters for supported provider request protocols.

The adapters share only transport and output limits. Their request and
response schemas remain provider-specific; arbitrary APIs are not assumed to
be compatible.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


PROTOCOLS = {"openai-chat-completions", "anthropic-messages", "ollama-chat"}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PROMPT_CHARS = 100_000
_MAX_TIMEOUT = 86_400


class ProviderError(ValueError):
    """A provider request failed without exposing request credentials."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_base_url(protocol: str, base_url: str, allow_insecure_loopback: bool = False) -> str:
    if protocol not in PROTOCOLS:
        raise ProviderError("Unsupported provider protocol")
    if (not isinstance(base_url, str) or not base_url or len(base_url) > 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in base_url)):
        raise ProviderError("Provider base_url must be a non-empty URL")
    try:
        parsed = urllib.parse.urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise ProviderError("Provider base_url is invalid") from exc
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ProviderError("Provider base_url must not contain credentials, query, or fragment")
    if parsed.scheme == "http" and not (allow_insecure_loopback and _loopback(parsed.hostname)):
        raise ProviderError("Provider requests require HTTPS; HTTP is allowed only for explicitly enabled loopback")
    if port is not None and not 1 <= port <= 65535:
        raise ProviderError("Provider base_url has an invalid port")
    return base_url.rstrip("/")


def _endpoint(protocol: str, base_url: str) -> str:
    base = base_url.rstrip("/")
    path = urllib.parse.urlsplit(base).path.rstrip("/")
    if protocol == "openai-chat-completions":
        if path.endswith("/chat/completions"):
            return base
        suffix = "/chat/completions" if path.endswith("/v1") else "/v1/chat/completions"
    elif protocol == "anthropic-messages":
        if path.endswith("/v1/messages"):
            return base
        suffix = "/messages" if path.endswith("/v1") else "/v1/messages"
    else:
        if path.endswith("/api/chat"):
            return base
        suffix = "/api/chat"
    return base + suffix


def _read_limited(response, deadline) -> bytes:
    size = 0
    chunks = []
    reader = getattr(response, "read1", response.read)
    # HTTPResponse.read(n) may wait for all n bytes while a peer trickles data.
    # read1 returns available bytes; update the socket timeout for each read so
    # a later stall cannot restart the full request timeout.
    socket_obj = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("Provider request timed out")
        if socket_obj is not None:
            socket_obj.settimeout(remaining)
        chunk = reader(min(65536, MAX_RESPONSE_BYTES + 1 - size))
        if time.monotonic() >= deadline:
            raise ProviderError("Provider request timed out")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ProviderError("Provider response exceeded the configured size limit")


def _content(protocol: str, payload: dict) -> str:
    try:
        if protocol in {"openai-chat-completions", "ollama-chat"}:
            value = payload["choices"][0]["message"]["content"] if protocol.startswith("openai-") else payload["message"]["content"]
            if isinstance(value, list):
                value = "".join(part.get("text", "") for part in value if isinstance(part, dict))
        else:
            value = "".join(part.get("text", "") for part in payload["content"] if isinstance(part, dict)
                            and part.get("type") == "text")
        if not isinstance(value, str):
            raise TypeError
        return value
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError("Provider response did not match the configured protocol") from exc


def complete(agent: dict, prompt: str, timeout=None) -> str:
    if not isinstance(agent, dict) or not isinstance(agent.get("provider"), dict):
        raise ProviderError("Provider agent configuration is unavailable")
    provider = agent["provider"]
    protocol = provider.get("protocol")
    allow_http = provider.get("allow_insecure_loopback") is True
    base_url = validate_base_url(protocol, provider.get("base_url"), allow_http)
    model = agent.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 500:
        raise ProviderError("A configured model identifier is required")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        raise ProviderError("Prompt must be non-empty text within the configured limit")
    timeout = agent.get("timeout", 600) if timeout is None else timeout
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= _MAX_TIMEOUT:
        raise ProviderError("Provider timeout is out of range")
    max_tokens = agent.get("max_tokens", 4096)
    if type(max_tokens) is not int or not 1 <= max_tokens <= 100_000:
        raise ProviderError("Provider max_tokens is out of range")

    api_key = provider.get("api_key")
    if api_key is not None and (not isinstance(api_key, str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in api_key)):
        raise ProviderError("Provider credential is invalid")
    if protocol == "openai-chat-completions":
        output_limit_field = provider.get("output_limit_field", "max_tokens")
        if not isinstance(output_limit_field, str) or output_limit_field not in {"max_tokens", "max_completion_tokens"}:
            raise ProviderError("Provider output_limit_field is invalid")
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
        payload[output_limit_field] = max_tokens
        if api_key:
            headers = {"Authorization": "Bearer " + api_key}
        else:
            headers = {}
    elif protocol == "anthropic-messages":
        payload = {"model": model, "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": prompt}], "stream": False}
        headers = {"anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
    else:
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
        payload["options"] = {"num_predict": max_tokens}
        headers = {}
        # Ollama's hosted API uses Bearer auth; local Ollama does not need it.
        if api_key and not _loopback(urllib.parse.urlsplit(base_url).hostname):
            headers["Authorization"] = "Bearer " + api_key
    headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(_endpoint(protocol, base_url), data=data, headers=headers, method="POST")
    deadline = time.monotonic() + float(timeout)
    try:
        parsed_url = urllib.parse.urlsplit(base_url)
        opener = (urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
                  if _loopback(parsed_url.hostname) else urllib.request.build_opener(_NoRedirect()))
        with opener.open(request, timeout=float(timeout)) as response:
            body = _read_limited(response, deadline)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise ProviderError(f"Provider returned HTTP {code}") from None
    except (TimeoutError, socket.timeout):
        raise ProviderError("Provider request timed out") from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            raise ProviderError(f"Provider returned HTTP {exc.code}") from None
        raise ProviderError("Provider request failed") from None
    try:
        response_payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProviderError("Provider returned invalid JSON") from exc
    if not isinstance(response_payload, dict):
        raise ProviderError("Provider response must be a JSON object")
    return _content(protocol, response_payload)
