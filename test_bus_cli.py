"""Synthetic CLI and log-storage regressions; no user data or agents are used."""
import json
import os
import time
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import bus


class BusCliRegressionTests(unittest.TestCase):
    def _isolated_home(self, tmp):
        root = Path(tmp)
        return patch.multiple(
            bus,
            LOG_DIR=str(root / "log"),
            RESP_DIR=str(root / "log" / "responses"),
            INBOX_DIR=str(root / "inbox"),
            LOG_FILE=str(root / "log" / "chat.log"),
        )

    def test_failed_agent_output_keeps_stderr_with_stdout(self):
        command = [sys.executable, "-c",
                   "import sys; print('partial'); print('FATAL: denied', file=sys.stderr); sys.exit(2)"]
        with tempfile.TemporaryDirectory(prefix="bus-cli-test-") as tmp, self._isolated_home(tmp):
            ok, output = bus.run_agent({"synthetic": {"command": command}}, "synthetic", "fixture")

        self.assertFalse(ok)
        self.assertIn("partial", output)
        self.assertIn("[stderr]", output)
        self.assertIn("FATAL: denied", output)

    @staticmethod
    def _pid_alive(pid):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not handle:
                return False
            try:
                return kernel.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_timeout_kills_synthetic_cli_and_descendant(self):
        with tempfile.TemporaryDirectory(prefix="bus-tree-test-") as tmp, self._isolated_home(tmp):
            pid_file = Path(tmp) / "child.pid"
            grandchild_file = Path(tmp) / "grandchild.pid"
            grandchild_code = "import time; time.sleep(60)"
            child_code = (
                "import json,subprocess,sys,time; "
                f"grandchild=subprocess.Popen([sys.executable,'-c',{json.dumps(grandchild_code)}], "
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                f"open({json.dumps(str(grandchild_file))},'w').write(str(grandchild.pid)); "
                "time.sleep(60)"
            )
            parent_code = (
                "import json,subprocess,sys,time; "
                f"child=subprocess.Popen([sys.executable,'-c',{json.dumps(child_code)}], "
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                f"open({json.dumps(str(pid_file))},'w').write(str(child.pid)); "
                "time.sleep(60)"
            )
            with patch.object(bus, "_taskkill_process_tree",
                              side_effect=AssertionError("Job Object path unexpectedly used taskkill")):
                ok, output = bus.run_agent({"synthetic": {"command": [sys.executable, "-c", parent_code]}},
                                           "synthetic", "fixture", timeout=1.5)
            self.assertFalse(ok)
            self.assertIn("超时", output)
            self.assertTrue(pid_file.exists(), "synthetic CLI did not start its child")
            self.assertTrue(grandchild_file.exists(), "synthetic child did not start its grandchild")
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            grandchild_pid = int(grandchild_file.read_text(encoding="utf-8"))
            for pid in (child_pid, grandchild_pid):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and self._pid_alive(pid):
                    time.sleep(0.05)
                self.assertFalse(self._pid_alive(pid), f"CLI descendant {pid} survived timeout")

    @unittest.skipUnless(os.name == "nt", "taskkill fallback is Windows-specific")
    def test_timeout_tree_cleanup_uses_validated_taskkill_fallback_when_job_unavailable(self):
        with tempfile.TemporaryDirectory(prefix="bus-tree-fallback-") as tmp, self._isolated_home(tmp):
            pid_file = Path(tmp) / "child.pid"
            grandchild_file = Path(tmp) / "grandchild.pid"
            grandchild_code = "import time; time.sleep(60)"
            child_code = (
                "import json,subprocess,sys,time; "
                f"grandchild=subprocess.Popen([sys.executable,'-c',{json.dumps(grandchild_code)}], "
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                f"open({json.dumps(str(grandchild_file))},'w').write(str(grandchild.pid)); "
                "time.sleep(60)"
            )
            parent_code = (
                "import json,subprocess,sys,time; "
                f"child=subprocess.Popen([sys.executable,'-c',{json.dumps(child_code)}], "
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                f"open({json.dumps(str(pid_file))},'w').write(str(child.pid)); "
                "time.sleep(60)"
            )
            with patch.object(bus, "_windows_job_object", return_value=None), \
                 patch.object(bus, "_taskkill_process_tree", wraps=bus._taskkill_process_tree) as kill_tree:
                ok, output = bus.run_agent({"synthetic": {"command": [sys.executable, "-c", parent_code]}},
                                           "synthetic", "fixture", timeout=1.5)
            kill_tree.assert_called_once()
            self.assertFalse(ok)
            self.assertIn("超时", output)
            self.assertTrue(pid_file.exists(), "synthetic CLI did not start its child")
            self.assertTrue(grandchild_file.exists(), "synthetic child did not start its grandchild")
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            grandchild_pid = int(grandchild_file.read_text(encoding="utf-8"))
            for pid in (child_pid, grandchild_pid):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and self._pid_alive(pid):
                    time.sleep(0.05)
                self.assertFalse(self._pid_alive(pid), f"taskkill fallback left descendant {pid} alive")

    @unittest.skipUnless(os.name == "nt", "suspended-process setup is Windows-specific")
    def test_failed_resume_terminates_suspended_process_and_closes_job(self):
        with tempfile.TemporaryDirectory(prefix="bus-resume-failure-") as tmp, self._isolated_home(tmp):
            real_popen = bus.subprocess.Popen
            created = []
            closed_jobs = []
            def capture_process(*args, **kwargs):
                proc = real_popen(*args, **kwargs)
                created.append(proc)
                return proc
            create_job = bus._windows_job_object
            def capture_job():
                info = create_job()
                if info is None:
                    return None
                kernel, job = info
                close_handle = kernel.CloseHandle
                def close(handle):
                    if int(handle) == int(job):
                        closed_jobs.append(True)
                    return close_handle(handle)
                kernel.CloseHandle = close
                return kernel, job
            command = [sys.executable, "-c", "import time; time.sleep(60)"]
            with patch.object(bus, "_windows_job_object", side_effect=capture_job), \
                 patch.object(bus.subprocess, "Popen", side_effect=capture_process), \
                 patch.object(bus, "_resume_suspended_process", return_value=False):
                ok, output = bus.run_agent({"synthetic": {"command": command}}, "synthetic", "fixture", timeout=3)
            self.assertFalse(ok)
            self.assertIn("执行失败；错误详情已省略", output)
            self.assertNotIn("Unable to resume", output)
            self.assertNotIn("time.sleep(60)", output)
            self.assertEqual(1, len(created))
            self.assertIsNotNone(created[0].poll(), "suspended process survived setup failure")
            self.assertTrue(closed_jobs, "setup failure leaked the Job Object handle")

    def test_cli_log_and_read_skip_truncated_jsonl_rows(self):
        task = json.dumps({"type": "task", "time": "t1", "from": "user",
                           "to": "synthetic", "task": "fixture"})
        message = json.dumps({"type": "message", "time": "t2", "from": "alpha",
                              "to": "synthetic", "content": "kept"})
        with tempfile.TemporaryDirectory(prefix="bus-cli-test-") as tmp, self._isolated_home(tmp):
            Path(bus.LOG_FILE).parent.mkdir(parents=True)
            Path(bus.LOG_FILE).write_text(task + "\n{broken\n" + message + "\n", encoding="utf-8")
            inbox = Path(bus.INBOX_DIR) / "synthetic.jsonl"
            inbox.parent.mkdir(parents=True)
            inbox.write_text(task + "\n{broken\n" + message + "\n", encoding="utf-8")
            with patch("builtins.print") as output:
                bus.cmd_log({}, 20)
                bus.cmd_read({"synthetic": {}}, "synthetic", 20)

        rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("fixture", rendered)
        self.assertIn("kept", rendered)

    def test_clear_serializes_with_append_and_later_append_survives(self):
        with tempfile.TemporaryDirectory(prefix="bus-cli-test-") as tmp, self._isolated_home(tmp), \
             patch("builtins.print"):
            bus.append_log({"type": "task", "task": "before"})
            entered = threading.Event()
            cleared = threading.Event()

            def clear_when_unlocked():
                entered.set()
                bus.clear_log()
                cleared.set()

            with bus._path_lock(bus.LOG_FILE):
                worker = threading.Thread(target=clear_when_unlocked)
                worker.start()
                self.assertTrue(entered.wait(1))
                self.assertFalse(cleared.wait(0.1))
            worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertTrue(cleared.is_set())
            self.assertFalse(Path(bus.LOG_FILE).exists())
            bus.append_log({"type": "task", "task": "after"})
            self.assertIn('"after"', Path(bus.LOG_FILE).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
