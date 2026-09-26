"""Offline security and isolation checks. No real agents or user configuration."""
import json
import http.client
import os
from pathlib import Path
from email.message import Message
import sys
import threading
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth
import bus
import server


class OfflineSecurityTests(unittest.TestCase):
    def test_full_response_button_passes_stable_id_without_js_path_escapes(self):
        self.assertIn('data-id="${esc(e.id||\'\')}" onclick="showFull(this.dataset.id)"', server.PAGE)
        self.assertIn("encodeURIComponent(rid)", server.PAGE)
        self.assertNotIn("showFull('${e.full}')", server.PAGE)

    def test_full_response_uses_in_page_dialog_after_fetch_not_popup(self):
        self.assertIn('<dialog id="full-dialog"', server.PAGE)
        self.assertIn('fullText.textContent=d.text', server.PAGE)
        self.assertIn('fullDialog.showModal()', server.PAGE)
        self.assertIn("$('#full-close').onclick=()=>fullDialog.close()", server.PAGE)
        self.assertNotIn('window.open(', server.PAGE)

    def test_full_response_endpoint_returns_fixture_text_for_stable_id(self):
        token='synthetic-full-response-token'
        response_id='a1b2c3d4'
        fixture_text='SYNTHETIC FULL RESPONSE 中文'
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(server, 'TOKEN', token), \
             patch.object(bus, 'RESP_DIR', str(Path(tmp)/'responses')):
            response_dir=Path(bus.RESP_DIR)
            response_dir.mkdir()
            (response_dir/(response_id+'.txt')).write_text(fixture_text, encoding='utf-8')
            httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
            port=httpd.server_port
            thread=threading.Thread(target=httpd.serve_forever,daemon=True)
            thread.start()
            try:
                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                conn.request('GET',f'/api/full?id={response_id}',headers={
                    'Host':f'127.0.0.1:{port}',
                    'X-Agent-Bus-Token':token,
                })
                response=conn.getresponse()
                status=response.status
                body=json.loads(response.read().decode('utf-8'))
                conn.close()
                self.assertEqual(200,status)
                self.assertEqual(fixture_text,body['text'])
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_name_allowlist_and_unregistered_target(self):
        for name in ("../escape", "a/b", "x" * 33, ""):
            with self.assertRaises(ValueError):
                bus.check_name(name)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(bus, "LOG_DIR", str(Path(tmp)/"log")), \
                 patch.object(bus, "RESP_DIR", str(Path(tmp)/"log"/"responses")), \
                 patch.object(bus, "INBOX_DIR", str(Path(tmp)/"inbox")), \
                 patch.object(bus, "LOG_FILE", str(Path(tmp)/"log"/"chat.log")), \
                 patch("builtins.print"):
                bus.cmd_send({}, "user", "ghost", "synthetic")
            self.assertFalse((Path(tmp)/"inbox").exists())

    def test_argv_executes_synthetic_process_and_shell_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(bus, "LOG_DIR", str(Path(tmp)/"log")), \
                 patch.object(bus, "RESP_DIR", str(Path(tmp)/"log"/"responses")), \
                 patch.object(bus, "INBOX_DIR", str(Path(tmp)/"inbox")), \
                 patch.object(bus, "LOG_FILE", str(Path(tmp)/"log"/"chat.log")):
                ok, out = bus.run_agent({"echo": {"command": [sys.executable, "-c", "print(7)"]}}, "echo", "synthetic")
                self.assertTrue(ok)
                self.assertIn("7", out)
                ok, out = bus.run_agent({"bad": {"command": "echo x | calc"}}, "bad", "synthetic")
                self.assertFalse(ok)
                self.assertIn("拒绝", out)

    def test_no_backend_allows_secretless_cli_config_but_rejects_secrets_and_tokens(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(bus, "_sec", None), \
             patch.object(bus, "_require_secrets", side_effect=RuntimeError("backend unavailable")), \
             patch.object(auth, "_secure_backend", side_effect=RuntimeError("backend unavailable")):
            config=Path(tmp)/"agents.json"
            with patch.object(bus, "AGENTS_FILE", str(config)):
                bus.save_agents({"x": {"command": ["echo"], "desc": "safe"}})
                self.assertIn("x", bus.load_agents(runtime=True))
                with self.assertRaises(RuntimeError):
                    bus.save_agents({"x": {"command": ["echo"], "env": {"API_KEY": "synthetic-secret"}}})
                bus.save_agents({"x": {"command": ["echo"], "env": {"API_KEY": "${ENV:TEST_AGENT_API_KEY}"}}})
                with patch.dict(os.environ, {"TEST_AGENT_API_KEY": "test-only-value"}):
                    self.assertEqual("test-only-value", bus.load_agents(runtime=True)["x"]["env"]["API_KEY"])
                config.write_text(json.dumps({"x": {"command": ["echo"], "env": {"API_KEY": "dpapi:invalid"}}}), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    bus.load_agents(runtime=True)
                self.assertIn("dpapi:invalid", config.read_text(encoding="utf-8"))
            token_file=Path(tmp)/".bus-token"
            with patch.object(auth, "TOKEN_FILE", token_file), patch.object(auth, "_SESSION_TOKEN", None):
                if os.name == "nt":
                    with self.assertRaises(RuntimeError):
                        auth.get_or_create_token()
                else:
                    token=auth.get_or_create_token()
                    self.assertRegex(token, r"^[0-9a-f]{64}$")
                    self.assertEqual(token, auth.get_or_create_token())
                    self.assertFalse(token_file.exists())

    @unittest.skipIf(os.name == "nt", "Windows uses a stable DPAPI-protected token")
    def test_session_token_is_thread_safe_and_never_written(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(auth, "TOKEN_FILE", Path(tmp)/".bus-token"), \
             patch.object(auth, "_SESSION_TOKEN", None):
            tokens=[]
            threads=[threading.Thread(target=lambda: tokens.append(auth.get_or_create_token()))
                     for _ in range(12)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(1, len(set(tokens)))
            self.assertEqual(64, len(tokens[0]))
            self.assertFalse((Path(tmp)/".bus-token").exists())
            self.assertFalse(hasattr(auth, "machine_fingerprint"))

    def test_secret_env_reference_and_configured_cli_output_are_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret="test-only-secret-91f0e72a"
            env_name="TEST_AGENT_PROVIDER_SECRET"
            cfg=Path(tmp)/"agents.json"
            with patch.object(bus, "AGENTS_FILE", str(cfg)), \
                 patch.object(bus, "LOG_FILE", str(Path(tmp)/"log"/"chat.log")), \
                 patch.object(bus, "RESP_DIR", str(Path(tmp)/"log"/"responses")), \
                 patch.object(bus, "INBOX_DIR", str(Path(tmp)/"inbox")), \
                 patch.dict(os.environ, {env_name: secret, "PARENT_API_KEY": "parent-only-secret"}):
                config={"echo": {"command": [sys.executable, "-c",
                    "import os,sys; print(os.environ['API_KEY']); print(os.environ.get('PARENT_API_KEY','missing')); print('stderr:'+os.environ['API_KEY'], file=sys.stderr)"],
                    "env": {"API_KEY": f"${{ENV:{env_name}}}"}}}
                bus.save_agents(config)
                stored=cfg.read_text(encoding="utf-8")
                self.assertIn("${ENV:"+env_name+"}", stored)
                self.assertNotIn(secret, stored)
                runtime=bus.load_agents(runtime=True)
                self.assertEqual(secret, runtime["echo"]["env"]["API_KEY"])
                ok, output=bus.run_agent(runtime, "echo", f"task mentions {secret}")
                self.assertTrue(ok)
                self.assertNotIn(secret, output)
                self.assertNotIn("parent-only-secret", output)
                self.assertIn("missing", output)
                persisted="\n".join(p.read_text(encoding="utf-8") for p in
                    [Path(tmp)/"log"/"chat.log", Path(tmp)/"inbox"/"echo.jsonl"] +
                    list((Path(tmp)/"log"/"responses").glob("*.txt")))
                self.assertNotIn(secret, persisted)
                self.assertNotIn("parent-only-secret", persisted)

    def test_common_secret_patterns_are_redacted_without_values(self):
        sample="prefix Authorization: Bearer abcdefghijklmnop and sk-proj-abcdefghijklmnopqrstuvwxyz012345"
        redacted=bus.redact_sensitive_text(sample, extra_secrets=("private-known-value-92837",))
        self.assertNotIn("abcdefghijklmnop", redacted)
        self.assertNotIn("sk-proj-abcdefghijklmnopqrstuvwxyz012345", redacted)
        self.assertNotIn("private-known-value-92837", bus.redact_sensitive_text(
            "private-known-value-92837", extra_secrets=("private-known-value-92837",)))
        self.assertIn("[REDACTED]", redacted)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration requires Windows")
    def test_windows_dpapi_text_tree_and_token_roundtrip(self):
        import win_dpapi
        sample="synthetic-only-secret-中文-\"quoted\"-\\path-\x00-tail"
        protected=win_dpapi.protect_text(sample)
        self.assertTrue(protected.startswith("dpapi:"))
        self.assertNotIn(sample, protected)
        self.assertEqual(sample, win_dpapi.unprotect_text(protected))
        prefixed_plaintext="dpapi:YWJj"
        protected_prefix=win_dpapi.protect_text(prefixed_plaintext)
        self.assertNotEqual(prefixed_plaintext, protected_prefix)
        self.assertEqual(prefixed_plaintext, win_dpapi.unprotect_text(protected_prefix))
        tree={"agent": {"env": {"SYNTHETIC_API_KEY": sample}, "profiles": [{"ACCESS_TOKEN": sample}], "command": ["python"], "n": 3}}
        original=json.loads(json.dumps(tree, ensure_ascii=False))
        protected_tree=win_dpapi.protect_tree(tree)
        self.assertEqual(tree, original)
        self.assertNotIn(sample, json.dumps(protected_tree, ensure_ascii=False))
        self.assertEqual(tree, win_dpapi.unprotect_tree(protected_tree))
        self.assertEqual(protected_tree, win_dpapi.protect_tree(protected_tree))
        env_tree={"env": {"SYNTHETIC_API_KEY": "${ENV:SYNTHETIC_API_KEY}"}}
        self.assertEqual(env_tree, win_dpapi.protect_tree(env_tree))
        self.assertEqual(env_tree, win_dpapi.unprotect_tree(env_tree))
        with self.assertRaises(ValueError):
            win_dpapi.unprotect_tree({"env": {"SYNTHETIC_API_KEY": "plaintext"}})
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Path(tmp)/"agents.json"
            with patch.object(bus, "AGENTS_FILE", str(cfg)), patch.object(bus, "_sec", None):
                bus.save_agents(tree)
                disk=cfg.read_text(encoding="utf-8")
                self.assertIn("dpapi:", disk)
                self.assertNotIn(sample, disk)
                self.assertEqual(sample, bus.load_agents(runtime=True)["agent"]["env"]["SYNTHETIC_API_KEY"])
                cfg.write_text(json.dumps({"agent": {"env": {"SYNTHETIC_API_KEY": "dpapi:invalid"}}}), encoding="utf-8")
                with self.assertRaises((ValueError, RuntimeError)):
                    bus.load_agents(runtime=True)
                self.assertIn("dpapi:invalid", cfg.read_text(encoding="utf-8"))
            token_file=Path(tmp)/".bus-token"
            with patch.object(auth, "TOKEN_FILE", token_file):
                token=auth.get_or_create_token()
                stored=token_file.read_text(encoding="utf-8")
                self.assertTrue(stored.startswith("dpapi:"))
                self.assertNotIn(token, stored)
                self.assertEqual(token, auth.get_or_create_token())

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration requires Windows")
    def test_corrupt_short_token_is_not_overwritten(self):
        import win_dpapi
        with tempfile.TemporaryDirectory() as tmp:
            token_file=Path(tmp)/".bus-token"
            for invalid in ("short", "dpapi:", "dpapi:%%%", win_dpapi.protect_text("short")):
                token_file.write_text(invalid, encoding="utf-8")
                with patch.object(auth, "TOKEN_FILE", token_file), patch.object(auth, "_secure_backend", return_value=win_dpapi):
                    with self.assertRaises(RuntimeError):
                        auth.get_or_create_token()
                self.assertEqual(invalid, token_file.read_text(encoding="utf-8"))

    def test_portable_env_reference_and_session_token_use_only_temporary_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path=Path(tmp)/".bus-token"
            with patch.object(auth, "TOKEN_FILE", token_path), patch.object(auth, "_SESSION_TOKEN", None):
                token=auth.get_or_create_token()
                self.assertEqual(token, auth.get_or_create_token())
                if os.name != "nt":
                    self.assertFalse(token_path.exists())
            cfg=Path(tmp)/"agents.json"
            env_name="SYNTHETIC_API_KEY"
            config_value={"synthetic": {"command": [sys.executable], "env": {env_name: f"${{ENV:{env_name}}}"}}}
            with patch.object(bus, "AGENTS_FILE", str(cfg)), patch.dict(os.environ, {env_name: "test-only-secret"}):
                bus.save_agents(config_value)
                self.assertNotIn("test-only-secret", cfg.read_text(encoding="utf-8"))
                self.assertEqual("test-only-secret", bus.load_agents(runtime=True)["synthetic"]["env"][env_name])

    def test_json_redaction_happens_before_serialization_and_handles_short_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"chat.jsonl"
            long_secret='quote"and\\slash-value-4455'
            short_secret="xy"
            entry={"content": f"provider said {long_secret}; short token={short_secret}.",
                   "nested": {"body": long_secret}}
            with patch.object(bus, "LOG_FILE", str(path)):
                bus.append_log(entry, (long_secret, short_secret))
            encoded=path.read_text(encoding="utf-8").strip()
            parsed=json.loads(encoded)
            self.assertEqual("provider said [REDACTED]; short token=[REDACTED].", parsed["content"])
            self.assertEqual("[REDACTED]", parsed["nested"]["body"])
            self.assertNotIn(long_secret, encoded)

    def test_raw_json_envelope_redacts_json_escaped_known_secret(self):
        secret='quoted "key"\\segment 中文'
        raw=json.dumps({"content": secret}, ensure_ascii=True)
        redacted=bus.redact_sensitive_text(raw, extra_secrets=(secret,))
        parsed=json.loads(redacted)
        self.assertEqual("[REDACTED]", parsed["content"])
        self.assertNotIn(secret, json.dumps(parsed, ensure_ascii=False))

    def test_gate_without_or_with_malformed_provider_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            board=Path(tmp)/"blackboard.json"
            board.write_text("{}",encoding="utf-8")
            with patch.dict(os.environ, {"AGENT_COMM_GATE_MODULE": ""}), patch("builtins.print"):
                self.assertNotEqual(0, bus.cmd_gate(tmp))
            bad_provider=SimpleNamespace(full_health_check=lambda path: {})
            with patch.dict(os.environ, {"AGENT_COMM_GATE_MODULE": "synthetic_gate_test"}), \
                 patch.dict(sys.modules, {"synthetic_gate_test": bad_provider}), patch("builtins.print"):
                self.assertNotEqual(0, bus.cmd_gate(tmp))

    def _handler(self, headers, include_host=True):
        handler=object.__new__(server.Handler)
        handler.headers=Message()
        if include_host and not any(key.lower() == "host" for key in headers):
            handler.headers["Host"]="127.0.0.1:{}".format(server.PORT)
        for key, value in headers.items():
            handler.headers[key]=value
        handler.client_address=("127.0.0.1", 12345)
        handler.server=SimpleNamespace(server_port=server.PORT,
                                       server_address=("127.0.0.1", server.PORT))
        handler._send=unittest.mock.Mock()
        return handler

    def test_host_header_requires_exact_loopback_authority(self):
        port=server.PORT
        for raw in (None, "attacker.invalid:{}".format(port),
                    "localhost.evil:{}".format(port), "127.0.0.1:{}".format(port+1),
                    "user@localhost:{}".format(port), "localhost:", "[::1]:bad"):
            h=self._handler({} if raw is None else {"Host":raw}, include_host=False)
            self.assertFalse(h._host_ok(), raw)
        duplicate=self._handler({"Host":"127.0.0.1:{}".format(port),
                                 "host":"localhost:{}".format(port)}, include_host=False)
        self.assertFalse(duplicate._host_ok())
        self.assertTrue(self._handler({"Host":"127.0.0.1:{}".format(port)})._host_ok())
        self.assertTrue(self._handler({"Host":"localhost:{}".format(port)})._host_ok())
        ipv6=self._handler({"Host":"[::1]:{}".format(port)})
        ipv6.server=SimpleNamespace(server_port=port,server_address=("::1",port,0,0))
        self.assertTrue(ipv6._host_ok())

    def test_dns_rebinding_host_guard_over_real_loopback_http(self):
        synthetic_token="b" * 64
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(server, "TOKEN", synthetic_token), \
             patch.object(bus, "load_agents", return_value={"synthetic": {"command": [sys.executable]}}), \
             patch.object(bus, "LOG_FILE", str(Path(tmp)/"chat.log")):
            httpd=server.ThreadingHTTPServer(("127.0.0.1",0),server.Handler)
            port=httpd.server_port
            thread=threading.Thread(target=httpd.serve_forever,daemon=True)
            thread.start()
            def get(path, host, token=None, origin=None):
                conn=http.client.HTTPConnection("127.0.0.1",port,timeout=3)
                headers={"Host":host}
                if token is not None:
                    headers["X-Agent-Bus-Token"]=token
                if origin is not None:
                    headers["Origin"]=origin
                conn.request("GET",path,headers=headers)
                response=conn.getresponse()
                status=response.status
                body=response.read().decode("utf-8",errors="replace")
                conn.close()
                return status,body
            try:
                bad_status,bad_body=get("/",f"attacker.invalid:{port}",origin=f"http://attacker.invalid:{port}")
                self.assertEqual(403,bad_status)
                self.assertNotIn(synthetic_token,bad_body)
                page_status,page=get("/",f"127.0.0.1:{port}")
                self.assertEqual(200,page_status)
                self.assertIn(synthetic_token,page)
                api_bad_status,api_bad_body=get("/api/state",f"attacker.invalid:{port}",token=synthetic_token)
                self.assertEqual(403,api_bad_status)
                self.assertNotIn(synthetic_token,api_bad_body)
                api_status,api_body=get("/api/state",f"localhost:{port}",token=synthetic_token)
                self.assertEqual(200,api_status)
                self.assertIn("synthetic",api_body)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=3)

    def test_origin_exact_host_port_and_fetch_metadata(self):
        good=self._handler({"Origin": f"http://127.0.0.1:{server.PORT}"})
        self.assertTrue(good._origin_ok())
        for origin in ("null", f"http://127.0.0.1.evil:{server.PORT}",
                       f"http://localhost:{server.PORT+1}", "not a url"):
            self.assertFalse(self._handler({"Origin": origin})._origin_ok(), origin)
        self.assertFalse(self._handler({"Sec-Fetch-Site":"cross-site"})._origin_ok())

    def test_post_guard_rejects_invalid_lengths_and_media_types(self):
        cases=[({"Content-Type":"application/jsonp", "Content-Length":"2"},415),
               ({"Content-Type":"application/json"},400),
               ({"Content-Type":"application/json", "Content-Length":"-1"},400),
               ({"Content-Type":"application/json", "Content-Length":"x"},400),
               ({"Content-Type":"application/json", "Content-Length":"2", "Transfer-Encoding":"chunked"},400),
               ({"Content-Type":"application/json", "Content-Length":str(server.MAX_BODY+1)},413)]
        for extra, code in cases:
            headers={"X-Agent-Bus-Token":"synthetic-token", **extra}
            h=self._handler(headers)
            with patch.object(server, "TOKEN", "synthetic-token"):
                self.assertFalse(h._guard(need_body=True))
            self.assertEqual(code, h._send.call_args.args[0])
        dup=self._handler({"X-Agent-Bus-Token":"synthetic-token",
                           "Content-Type":"application/json",
                           "Content-Length":"1", "content-length":"1"})
        with patch.object(server, "TOKEN", "synthetic-token"):
            self.assertFalse(dup._guard(need_body=True))
        self.assertEqual(400, dup._send.call_args.args[0])
        boundary=self._handler({"X-Agent-Bus-Token":"synthetic-token",
                                "Content-Type":"application/json; charset=utf-8",
                                "Content-Length":str(server.MAX_BODY)})
        with patch.object(server, "TOKEN", "synthetic-token"):
            self.assertTrue(boundary._guard(need_body=True))

    def test_clear_requires_boolean_true_and_valid_request(self):
        import io
        with patch.object(server, "TOKEN", "synthetic-token"), patch.object(bus, "clear_log") as clear:
            for body in (b'{}', b'{"confirm": false}', b'{"confirm": "yes"}', b'{"confirm": 1}'):
                h=self._handler({"X-Agent-Bus-Token":"synthetic-token",
                                 "Content-Type":"application/json",
                                 "Content-Length":str(len(body))})
                h.path="/api/clear"
                h.rfile=io.BytesIO(body)
                h.do_POST()
                clear.assert_not_called()
                self.assertEqual(400, h._send.call_args.args[0])
            body=b'{"confirm": true}'
            h=self._handler({"X-Agent-Bus-Token":"synthetic-token",
                             "Content-Type":"application/json",
                             "Content-Length":str(len(body))})
            h.path="/api/clear"
            h.rfile=io.BytesIO(body)
            h.do_POST()
            clear.assert_called_once()
            self.assertEqual(200, h._send.call_args.args[0])

    def test_authentication_and_loopback_required(self):
        h=self._handler({})
        self.assertFalse(h._authorized())
        h.headers=Message()
        h.headers["x-agent-bus-token"]="synthetic-token"
        with patch.object(server, "TOKEN", "synthetic-token"):
            self.assertTrue(h._authorized())
        h.client_address=("192.0.2.1", 44)
        self.assertFalse(h._client_ok())
        self.assertIn("/api", Path(__file__).with_name("server.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
