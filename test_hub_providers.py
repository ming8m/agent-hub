"""Wire-format and transport tests for explicitly supported provider adapters."""
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import hub_providers


class _ProviderServer(ThreadingHTTPServer):
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.server.paths.append(self.path)
        self.server.headers_seen.append(dict(self.headers))
        self.server.payloads.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))))
        if self.server.redirect_to:
            self.send_response(307)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(self.server.response_payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def _serve(response_payload=None, redirect_to=None):
    server = _ProviderServer(("127.0.0.1", 0), _Handler)
    server.paths, server.headers_seen, server.payloads = [], [], []
    server.response_payload = response_payload or {}
    server.redirect_to = redirect_to
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class ProviderAdapterTests(unittest.TestCase):
    def test_output_limit_finish_reasons_and_blank_content_fail_closed(self):
        truncated = (
            ("openai-chat-completions", {"choices": [{"finish_reason": "length",
                "message": {"content": "partial"}}]}),
            ("anthropic-messages", {"stop_reason": "max_tokens",
                "content": [{"type": "text", "text": "partial"}]}),
            ("ollama-chat", {"done_reason": "length", "message": {"content": "partial"}}),
        )
        for protocol, response in truncated:
            with self.subTest(protocol=protocol), self.assertRaisesRegex(
                    hub_providers.ProviderError, "maximum output tokens"):
                hub_providers._content(protocol, response)
        for protocol, response in (
            ("openai-chat-completions", {"choices": [{"message": {"content": "  "}}]}),
            ("anthropic-messages", {"content": [{"type": "thinking", "thinking": "private"}]}),
            ("ollama-chat", {"message": {"content": "\n"}}),
        ):
            with self.subTest(protocol=protocol), self.assertRaisesRegex(
                    hub_providers.ProviderError, "empty content"):
                hub_providers._content(protocol, response)
        self.assertEqual("ok", hub_providers._content("openai-chat-completions",
            {"choices": [{"message": {"content": "ok"}}]}))

    def test_slow_trickle_respects_total_timeout(self):
        class SlowHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Length", "20")
                self.end_headers()
                for _ in range(20):
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        break
                    time.sleep(0.05)
            def log_message(self, *_): pass
        server = _ProviderServer(("127.0.0.1", 0), SlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            agent = {"provider": {"protocol": "openai-chat-completions",
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "allow_insecure_loopback": True}, "model": "fixture"}
            started = time.monotonic()
            with self.assertRaisesRegex(hub_providers.ProviderError, "timed out"):
                hub_providers.complete(agent, "prompt", timeout=0.2)
            self.assertLess(time.monotonic() - started, 0.6)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_error_does_not_drain_slow_body(self):
        class ErrorHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(429)
                self.send_header("Content-Length", "20")
                self.end_headers()
                for _ in range(20):
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        break
                    time.sleep(0.05)
            def log_message(self, *_): pass
        server = _ProviderServer(("127.0.0.1", 0), ErrorHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            agent = {"provider": {"protocol": "openai-chat-completions",
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "allow_insecure_loopback": True}, "model": "fixture"}
            started = time.monotonic()
            with self.assertRaisesRegex(hub_providers.ProviderError, "HTTP 429"):
                hub_providers.complete(agent, "prompt", timeout=0.2)
            self.assertLess(time.monotonic() - started, 0.6)
        finally:
            server.shutdown()
            server.server_close()

    def test_max_tokens_reaches_each_provider_wire_format(self):
        cases = [
            ("openai-chat-completions", {"max_tokens": 37}),
            ("anthropic-messages", {"max_tokens": 37}),
            ("ollama-chat", {"options": {"num_predict": 37}}),
        ]
        responses = {
            "openai-chat-completions": {"choices": [{"message": {"content": "ok"}}]},
            "anthropic-messages": {"content": [{"type": "text", "text": "ok"}]},
            "ollama-chat": {"message": {"content": "ok"}},
        }
        for protocol, expected in cases:
            with self.subTest(protocol=protocol):
                server, _ = _serve(responses[protocol])
                try:
                    agent = {"provider": {"protocol": protocol,
                        "base_url": f"http://127.0.0.1:{server.server_port}",
                        "allow_insecure_loopback": True}, "model": "fixture", "max_tokens": 37}
                    self.assertEqual("ok", hub_providers.complete(agent, "prompt"))
                    self.assertTrue(expected.items() <= server.payloads[0].items())
                finally:
                    server.shutdown()
                    server.server_close()

    def test_rejects_invalid_max_tokens_before_request(self):
        for invalid in (0, -1, True, 1.5, "37", 100001):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(hub_providers.ProviderError, "max_tokens"):
                hub_providers.complete({"provider": {"protocol": "openai-chat-completions",
                    "base_url": "https://example.invalid"}, "model": "fixture", "max_tokens": invalid}, "prompt")

    def test_openai_explicit_new_output_limit_field(self):
        server, _ = _serve({"choices": [{"message": {"content": "ok"}}]})
        try:
            agent = {"provider": {"protocol": "openai-chat-completions",
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "allow_insecure_loopback": True, "output_limit_field": "max_completion_tokens"},
                "model": "fixture", "max_tokens": 37}
            self.assertEqual("ok", hub_providers.complete(agent, "prompt"))
            self.assertEqual(37, server.payloads[0]["max_completion_tokens"])
            self.assertNotIn("max_tokens", server.payloads[0])
        finally:
            server.shutdown()
            server.server_close()
        for value in ("unsupported", 42, None):
            with self.subTest(value=value), self.assertRaisesRegex(hub_providers.ProviderError, "output_limit_field"):
                hub_providers.complete({"provider": {"protocol": "openai-chat-completions",
                    "base_url": "https://example.invalid", "output_limit_field": value},
                    "model": "fixture"}, "prompt")

    def test_provider_specific_wire_shapes_model_ids_and_endpoint_forms(self):
        cases = [
            ("openai-chat-completions", "", "/v1/chat/completions",
             {"choices": [{"message": {"content": "openai text"}}]}, "openai text"),
            ("openai-chat-completions", "/v1", "/v1/chat/completions",
             {"choices": [{"message": {"content": [{"type": "text", "text": "openai parts"}]}}]}, "openai parts"),
            ("anthropic-messages", "/v1", "/v1/messages",
             {"content": [{"type": "thinking", "thinking": "private"}, {"type": "text", "text": "anthropic text"}]}, "anthropic text"),
            ("anthropic-messages", "/v1/messages", "/v1/messages",
             {"content": [{"type": "text", "text": "anthropic full endpoint"}]}, "anthropic full endpoint"),
            ("ollama-chat", "/api/chat", "/api/chat",
             {"message": {"content": "ollama text"}}, "ollama text"),
        ]
        for protocol, path, expected_path, response, expected_text in cases:
            with self.subTest(protocol=protocol, path=path):
                server, thread = _serve(response)
                try:
                    base = f"http://127.0.0.1:{server.server_port}{path}"
                    result = hub_providers.complete({"provider": {"protocol": protocol, "base_url": base,
                        "allow_insecure_loopback": True}, "model": "vendor/arbitrary-model:2026-rc1"}, "fixture prompt")
                    self.assertEqual(expected_text, result)
                    self.assertEqual([expected_path], server.paths)
                    self.assertEqual("vendor/arbitrary-model:2026-rc1", server.payloads[0]["model"])
                    self.assertEqual(False, server.payloads[0]["stream"])
                    if protocol == "anthropic-messages":
                        self.assertEqual("2023-06-01", server.headers_seen[0]["Anthropic-Version"])
                        self.assertEqual(4096, server.payloads[0]["max_tokens"])
                    if protocol == "ollama-chat":
                        self.assertNotIn("Authorization", server.headers_seen[0])
                finally:
                    server.shutdown()
                    server.server_close()

    def test_custom_prefix_full_endpoint_and_safe_url_validation(self):
        self.assertEqual("https://example.invalid/prefix/v1/chat/completions",
            hub_providers._endpoint("openai-chat-completions", "https://example.invalid/prefix/v1"))
        self.assertEqual("https://example.invalid/prefix/v1/chat/completions",
            hub_providers._endpoint("openai-chat-completions", "https://example.invalid/prefix/v1/chat/completions"))
        self.assertEqual("https://example.invalid/prefix/v1/messages",
            hub_providers._endpoint("anthropic-messages", "https://example.invalid/prefix/v1/messages"))
        self.assertEqual("https://example.invalid/prefix/api/chat",
            hub_providers._endpoint("ollama-chat", "https://example.invalid/prefix/api/chat"))
        for url in ("https://user:pass@example.invalid/v1", "https://example.invalid/?key=x",
                    "https://example.invalid/#fragment", "https://example.invalid/a b",
                    "https://example.invalid/a\tpath", "http://example.invalid/v1"):
            with self.subTest(url=url), self.assertRaises(hub_providers.ProviderError):
                hub_providers.validate_base_url("openai-chat-completions", url, allow_insecure_loopback=True)
        self.assertEqual("https://example.invalid/a%2Fb",
            hub_providers.validate_base_url("openai-chat-completions", "https://example.invalid/a%2Fb"))

    def test_redirect_is_rejected_and_loopback_http_ignores_proxy(self):
        target, target_thread = _serve({"choices": [{"message": {"content": "ok"}}]})
        redirect, redirect_thread = _serve(redirect_to=f"http://127.0.0.1:{target.server_port}/capture")
        proxy, proxy_thread = _serve({"choices": [{"message": {"content": "proxy"}}]})
        try:
            with patch.dict(os.environ, {"http_proxy": f"http://127.0.0.1:{proxy.server_port}", "no_proxy": ""}, clear=False):
                spec = {"provider": {"protocol": "openai-chat-completions",
                    "base_url": f"http://127.0.0.1:{target.server_port}", "allow_insecure_loopback": True},
                    "model": "custom-model"}
                self.assertEqual("ok", hub_providers.complete(spec, "prompt"))
                self.assertEqual(["/v1/chat/completions"], target.paths)
                self.assertEqual([], proxy.paths)
                spec["provider"]["base_url"] = f"http://127.0.0.1:{redirect.server_port}"
                with self.assertRaisesRegex(hub_providers.ProviderError, "HTTP 307"):
                    hub_providers.complete(spec, "prompt")
                self.assertEqual([], target.paths[1:])
                self.assertEqual(["/v1/chat/completions"], redirect.paths)
        finally:
            for server in (target, redirect, proxy):
                server.shutdown()
                server.server_close()

    def test_loopback_https_transport_disables_environment_proxy(self):
        class Response:
            def __init__(self): self.body = b'{"choices":[{"message":{"content":"ok"}}]}'
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, size):
                chunk, self.body = self.body[:size], self.body[size:]
                return chunk

        class Opener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return Response()

        opener = Opener()
        captured = []
        def capture(*handlers):
            captured.extend(handlers)
            return opener
        with patch.object(hub_providers.urllib.request, "build_opener", side_effect=capture):
            spec = {"provider": {"protocol": "openai-chat-completions",
                "base_url": "https://localhost:9443", "api_key": "test-only-key"}, "model": "fixture"}
            self.assertEqual("ok", hub_providers.complete(spec, "fixture prompt"))
        self.assertTrue(any(isinstance(handler, hub_providers.urllib.request.ProxyHandler)
                            and handler.proxies == {} for handler in captured))

    def test_remote_ollama_uses_bearer_key_and_local_ollama_omits_it(self):
        class Response:
            def __init__(self): self.body = b'{"message":{"content":"ok"}}'
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, size):
                chunk, self.body = self.body[:size], self.body[size:]
                return chunk

        class Opener:
            def __init__(self): self.request = None
            def open(self, request, timeout):
                self.request = request
                return Response()

        opener = Opener()
        with patch.object(hub_providers.urllib.request, "build_opener", return_value=opener):
            spec = {"provider": {"protocol": "ollama-chat", "base_url": "https://ollama.com/api",
                                  "api_key": "ollama-test-key"}, "model": "gemma3"}
            self.assertEqual("ok", hub_providers.complete(spec, "prompt"))
        self.assertEqual("Bearer ollama-test-key", opener.request.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
