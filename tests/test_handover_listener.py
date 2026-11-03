"""Current verifier integration against fake old listeners; no real services."""
import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class CurrentListenerHandoverTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("listener_handover_integration", ROOT / "portable/package_handover.py")
        self.handover = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.handover)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = Path(self.temp.name) / "旧包 (1)"
        self.base = self.old / "wechat-weflow-bridge-ob11"
        self.base.mkdir(parents=True)
        self.pidfile = self.base / "bridge.pid"
        # An obsolete source must never execute during the handover.
        (self.base / "startup_control.py").write_text("raise RuntimeError('old verifier executed')", encoding="utf-8")
        (self.base / "main.py").write_text("raise RuntimeError('old main executed')", encoding="utf-8")
        self.exe = str(self.old / "portable/runtime/python/cpython/python.exe")
        self.argv = [self.exe, "-X", "utf8", "-B", str(self.base / "main.py")]
        self.env = {"WECHATBOT_PORTABLE_ROOT": str(self.old)}
        self.connections = {8766: 123}
        self.native = {"status": "alive", "executable": self.exe, "creation": "123456"}
        self.requests, self.terminated, self.helpers = [], [], []
        self.on_response = lambda request: None
        self.stop_result = {"running": False, "uia_restore_result": {"ok": True}}
        self.loader = self.handover._load_current
        process = types.SimpleNamespace(environ=lambda: dict(self.env), cmdline=lambda: list(self.argv))
        for patcher in (
            patch.object(self.handover.psutil, "net_connections", side_effect=self.snapshot),
            patch.object(self.handover.psutil, "Process", side_effect=lambda pid: process),
            patch.object(self.handover, "_load_current", side_effect=self.load_helper),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def snapshot(self, **kwargs):
        self.assertEqual(kwargs, {"kind": "tcp"})
        return [types.SimpleNamespace(status=self.handover.psutil.CONN_LISTEN,
                                      laddr=("127.0.0.1", port), pid=pid)
                for port, pid in self.connections.items()]

    def load_helper(self, current, kind, previous):
        self.assertEqual((current, kind, previous), (ROOT.resolve(), "bridge", self.old.resolve()))
        helper = self.loader(current, kind, previous)
        self.assertEqual(Path(helper.__file__).resolve(), (ROOT / "wechat-weflow-bridge-ob11/startup_control.py").resolve())
        self.assertEqual(Path(helper.BASE), self.base)
        self.helpers.append(helper)
        for patcher in (
            patch.object(helper, "_pid_guard", lambda path: nullcontext()),
            patch.object(helper, "_native_process", side_effect=lambda pid: dict(self.native)),
            patch.object(helper, "_listener_pid", side_effect=lambda port: self.connections.get(port)),
            patch.object(helper, "_panel_session", return_value=object()),
            patch.object(helper, "_panel_response", side_effect=self.response),
            patch.object(helper, "_terminate_verified", side_effect=self.terminate),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return helper

    def response(self, session, request):
        self.requests.append(request)
        self.on_response(request)
        if request.get_method() == "GET":
            return ('<meta name="panel-csrf" content="' + "a" * 43 + '">').encode()
        return json.dumps(self.stop_result).encode()

    def terminate(self, info):
        self.terminated.append(dict(info))
        self.connections.pop(8766)

    def record(self, **changes):
        value = {"version": 2, "pid": 123, "executable": self.exe, "creation": "123456",
                 "project": str(self.base), "command": str(self.base / "main.py"),
                 "panel_url": "http://127.0.0.1:8766"}
        value.update(changes)
        return json.dumps(value).encode()

    def run_handover(self):
        return self.handover.handover(ROOT, [8766])

    def test_missing_record_stops_actual_current_verified_listener_without_creating_record(self):
        self.assertEqual(self.run_handover(), 1)
        self.assertFalse(self.pidfile.exists())
        self.assertEqual([request.get_method() for request in self.requests], ["GET", "POST"])
        self.assertEqual(self.requests[-1].full_url, "http://127.0.0.1:8766/stop")
        self.assertEqual(self.terminated[0]["executable"], self.exe)
        self.assertEqual(self.terminated[0]["creation"], "123456")

    def test_corrupt_legacy_record_is_not_rewritten_or_deleted(self):
        original = b"123\n"
        self.pidfile.write_bytes(original)
        self.assertEqual(self.run_handover(), 1)
        self.assertEqual(self.pidfile.read_bytes(), original)

    def test_obsolete_redirector_record_does_not_block_native_identity_or_get_deleted(self):
        original = self.record(executable=str(self.old / "portable/runtime/bridge-venv/Scripts/python.exe"))
        self.pidfile.write_bytes(original)
        self.assertEqual(self.run_handover(), 1)
        self.assertEqual(self.terminated[0]["executable"], self.exe)
        self.assertEqual(self.pidfile.read_bytes(), original)

    def test_exact_matching_record_is_removed_after_confirmed_restore_and_stop(self):
        self.pidfile.write_bytes(self.record())
        self.assertEqual(self.run_handover(), 1)
        self.assertFalse(self.pidfile.exists())

    def test_concurrent_record_replacement_survives_old_listener_exit(self):
        self.pidfile.write_bytes(self.record())
        replacement = self.record(pid=789, creation="999999")
        def replace(request):
            if request.get_method() == "POST":
                self.pidfile.write_bytes(replacement)
        self.on_response = replace
        self.assertEqual(self.run_handover(), 1)
        self.assertEqual(self.pidfile.read_bytes(), replacement)

    def test_relative_script_and_python_code_arguments_cannot_authorize_stop(self):
        for argv in ([self.exe, "main.py"], [self.exe, "-c", str(self.base / "main.py")]):
            with self.subTest(argv=argv):
                self.argv = argv
                with self.assertRaisesRegex(RuntimeError, "无法核验为旧版"):
                    self.run_handover()
                self.assertEqual(self.requests, [])
                self.assertEqual(self.terminated, [])

    def test_foreign_entry_is_not_adopted_even_with_package_environment(self):
        self.argv = [self.exe, str(self.old / "foreign.py")]
        with self.assertRaisesRegex(RuntimeError, "无法核验为旧版"):
            self.run_handover()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.terminated, [])

    def test_unproven_argv_interpreter_alias_is_not_allowed(self):
        self.argv[0] = str(self.old / "foreign-python.exe")
        with self.assertRaisesRegex(RuntimeError, "实时进程核验失败"):
            self.run_handover()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.terminated, [])

    def test_creation_change_after_csrf_get_prevents_stop_post_and_termination(self):
        def change(request):
            if request.get_method() == "GET":
                self.native["creation"] = "999999"
        self.on_response = change
        with self.assertRaisesRegex(RuntimeError, "未确认正常退出"):
            self.run_handover()
        self.assertEqual([request.get_method() for request in self.requests], ["GET"])
        self.assertEqual(self.terminated, [])

    def test_listener_replacement_after_csrf_get_receives_no_stop_post(self):
        def change(request):
            if request.get_method() == "GET":
                self.connections[8766] = 999
        self.on_response = change
        with self.assertRaisesRegex(RuntimeError, "未确认正常退出"):
            self.run_handover()
        self.assertEqual([request.get_method() for request in self.requests], ["GET"])
        self.assertEqual(self.terminated, [])

    def test_restore_failure_preserves_process_and_exact_record(self):
        original = self.record()
        self.pidfile.write_bytes(original)
        self.stop_result["uia_restore_result"]["ok"] = False
        with self.assertRaisesRegex(RuntimeError, "未确认正常退出"):
            self.run_handover()
        self.assertEqual(self.terminated, [])
        self.assertEqual(self.connections, {8766: 123})
        self.assertEqual(self.pidfile.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
