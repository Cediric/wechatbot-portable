"""Process/path/launcher checks use mocks or isolated synthetic Python children."""
import importlib.util
import contextlib
import ctypes
from email.message import Message
from http.cookiejar import CookieJar
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import socket
import struct
import stat
import types
import unittest
from contextlib import nullcontext
from unittest.mock import patch, Mock
from urllib.request import BaseHandler, build_opener, HTTPCookieProcessor, ProxyHandler
from urllib.response import addinfourl

BRIDGE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"


def load(name):
    spec = importlib.util.spec_from_file_location("startup_test_" + name, BRIDGE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ListenerTests(unittest.TestCase):
    def query(self, rows, count=None):
        control = load("startup_control")
        data = struct.pack("<I", len(rows) if count is None else count) + b"".join(struct.pack("<6I", *row) for row in rows)
        def table(buffer, size, ordered, family, kind, reserved):
            self.assertEqual((family, kind), (2, 3))
            ctypes.cast(size, ctypes.POINTER(ctypes.c_uint)).contents.value = len(data)
            if buffer is None:
                return 122
            ctypes.memmove(buffer, data, len(data))
            return 0
        api = types.SimpleNamespace(GetExtendedTcpTable=Mock(side_effect=table))
        with patch.object(control.os, "name", "nt"), patch.object(control.ctypes, "WinDLL", return_value=api):
            return control._listener_pid(8877)

    def test_listener_owner_matches_network_byte_order_and_loopback(self):
        address = struct.unpack("<I", socket.inet_aton("127.0.0.1"))[0]
        self.assertEqual(self.query([(2, address, socket.htons(8877), 0, 0, 123),
                                     (2, address, socket.htons(9999), 0, 0, 999)]), 123)

    def test_multiple_owners_or_no_owned_listener_are_rejected(self):
        for rows in ([], [(2, 0, socket.htons(8877), 0, 0, 123), (2, 0, socket.htons(8877), 0, 0, 999)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.query(rows)

    def test_truncated_listener_metadata_is_rejected(self):
        with self.assertRaises(OSError):
            self.query([], count=100)


class CommandLineReaderTests(unittest.TestCase):
    def setUp(self):
        self.control = load("startup_control")

    def test_bundled_reader_verifies_argv_without_starting_shell(self):
        args = [sys.executable, "-u", str(BRIDGE / "main.py")]
        process = Mock(); process.cmdline.return_value = args
        native = types.SimpleNamespace(Process=Mock(return_value=process), Error=RuntimeError, NoSuchProcess=LookupError)
        with patch.dict(sys.modules, {"psutil": native}), patch.object(self.control.subprocess, "run") as shell:
            self.assertEqual(self.control._command_args(123), args)
            shell.assert_not_called()

    def test_native_permission_failure_is_closed_and_has_no_shell_fallback(self):
        native = types.SimpleNamespace(Process=Mock(side_effect=RuntimeError("PRIVATE_QUERY_CANARY")), Error=RuntimeError, NoSuchProcess=LookupError)
        with patch.dict(sys.modules, {"psutil": native}), patch.object(self.control.subprocess, "run") as shell:
            with self.assertRaisesRegex(RuntimeError, "原生命令行") as failure:
                self.control._command_args(123)
            self.assertNotIn("PRIVATE_QUERY_CANARY", str(failure.exception))
            shell.assert_not_called()

    def test_missing_process_and_invalid_native_result_do_not_authorize(self):
        process = Mock()
        native = types.SimpleNamespace(Process=Mock(return_value=process), Error=RuntimeError, NoSuchProcess=LookupError)
        with patch.dict(sys.modules, {"psutil": native}):
            for args in (None, [], "python main.py", [None]):
                process.cmdline.return_value = args
                self.assertIsNone(self.control._command_args(123))
            native.Process.side_effect = LookupError()
            self.assertIsNone(self.control._command_args(123))

    def test_external_interpreter_can_use_cim_when_native_reader_missing(self):
        with patch.dict(sys.modules, {"psutil": None}), patch.object(self.control, "_cim_command_args", return_value=["native-python", "script"]) as fallback:
            self.assertEqual(self.control._command_args(123), ["native-python", "script"])
            fallback.assert_called_once_with(123)


    def test_missing_native_reader_cim_has_isolated_windows_console(self):
        # Exercise the real missing-psutil branch, not a mocked fallback method.
        result = types.SimpleNamespace(returncode=0, stdout='{"CommandLine": null}')
        with patch.dict(sys.modules, {"psutil": None}), patch.object(self.control.os, "name", "nt"), \
             patch.object(self.control.subprocess, "run", return_value=result) as shell:
            self.assertIsNone(self.control._command_args(123))
        shell.assert_called_once()
        self.assertEqual(shell.call_args.kwargs.get("creationflags", 0), 0x08000000)
        self.assertTrue(shell.call_args.kwargs["capture_output"])
        self.assertEqual(shell.call_args.kwargs["timeout"], 5)

    def test_cim_nonwindows_does_not_pass_windows_creation_flags(self):
        result = types.SimpleNamespace(returncode=0, stdout='{"CommandLine": null}')
        with patch.object(self.control.os, "name", "posix"), patch.object(self.control.subprocess, "run", return_value=result) as shell:
            self.assertIsNone(self.control._cim_command_args(123))
        self.assertEqual(shell.call_args.kwargs["creationflags"], 0)

    @unittest.skipUnless(os.name == "nt", "Native console isolation requires Windows")
    def test_actual_missing_native_reader_preserves_synthetic_parent_console(self):
        # Only a hidden, dedicated synthetic console and a harmless PowerShell
        # native-handle read are used. No CIM process query or service runs.
        subprocess = self.control.subprocess
        parent_source = r'''import ctypes,importlib.util,json,os,subprocess,sys
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location("console_fixture_control",sys.argv[1])
control=importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
before=ctypes.windll.kernel32.GetConsoleWindow()
real_run=subprocess.run
proof={}
def harmless_query(argv,**kwargs):
    assert argv[0]=="powershell" and argv[-2]=="-Command"
    script = '[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); '
    script += "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class FixtureConsole { [DllImport(\"kernel32.dll\")] public static extern IntPtr GetConsoleWindow(); }'; "
    script += '[pscustomobject]@{CommandLine=$null;no_console=([FixtureConsole]::GetConsoleWindow() -eq [IntPtr]::Zero)} | ConvertTo-Json -Compress'
    # Keep every production subprocess option; replace only the fixed query and
    # select the actual system PowerShell executable instead of PATH discovery.
    query=[str(Path(os.environ["SystemRoot"])/"System32/WindowsPowerShell/v1.0/powershell.exe"),*argv[1:-1],script]
    value=real_run(query,**kwargs)
    assert value.returncode==0
    proof['child_had_no_console']=json.loads(value.stdout)['no_console']
    proof['creationflags_correct']=kwargs.get('creationflags')==0x08000000
    proof['timeout_unchanged']=kwargs.get('timeout')==5
    return value
with patch.dict(sys.modules,{'psutil':None}), patch.object(control.subprocess,'run',side_effect=harmless_query):
    result=control._command_args(os.getpid())
after=ctypes.windll.kernel32.GetConsoleWindow()
proof.update(parent_had_console=before!=0,parent_console_unchanged=before==after,fallback_returned_none=result is None)
Path(sys.argv[2]).write_text(json.dumps(proof),encoding='utf-8')
'''
        with tempfile.TemporaryDirectory(prefix="cim-console-fixture-") as folder:
            parent=Path(folder)/"parent.py"
            proof_path=Path(folder)/"proof.json"
            parent.write_text(parent_source,encoding="utf-8")
            settings=subprocess.STARTUPINFO()
            settings.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            settings.wShowWindow=0
            result=subprocess.run([sys.executable,"-X","utf8","-B",str(parent),str(BRIDGE/"startup_control.py"),str(proof_path)],
                capture_output=True,encoding="utf-8",timeout=20,creationflags=subprocess.CREATE_NEW_CONSOLE,startupinfo=settings)
            self.assertEqual(result.returncode,0,"Synthetic console parent did not complete")
            proof=json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertEqual(proof,{"child_had_no_console":True,"creationflags_correct":True,"timeout_unchanged":True,
                "parent_had_console":True,"parent_console_unchanged":True,"fallback_returned_none":True})

    def test_cim_timeout_keeps_duration_without_command_values(self):
        failure = self.control.subprocess.TimeoutExpired(["powershell", "PRIVATE_CIM_CANARY"], 5)
        with patch.object(self.control.subprocess, "run", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "CIM.*5 秒") as report:
                self.control._cim_command_args(123)
            self.assertNotIn("PRIVATE_CIM_CANARY", str(report.exception))


class StartupControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.pidfile = self.base / "bridge.pid"
        self.exe = str(self.base / "python.exe")
        self.main = str(self.base / "main.py")
        self.control = load("startup_control")
        self.control.BASE = str(self.base)
        (self.base / "config.json").write_text(json.dumps({"web_port": 8877}), encoding="utf-8")
        self.guard = patch.object(self.control, "_pid_guard", lambda path: nullcontext())
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.record = {"version": 2, "pid": 123, "executable": self.exe, "project": str(self.base),
                       "command": self.main, "creation": "123456", "panel_url": "http://127.0.0.1:8877"}
        self.write_pid()
        self.live = {"status": "alive", "executable": self.exe, "creation": "123456"}
        self.native = patch.object(self.control, "_native_process", lambda pid: dict(self.live))
        self.native.start()
        self.addCleanup(self.native.stop)
        self.args = [self.exe, "-u", self.main]
        self.command = patch.object(self.control, "_command_args", lambda pid: list(self.args))
        self.command.start()
        self.addCleanup(self.command.stop)
        self.terminated = []
        self.terminate = patch.object(self.control, "_terminate_verified", lambda info: self.terminated.append(info["pid"]))
        self.terminate.start()
        self.addCleanup(self.terminate.stop)
        self.requests = []
        self.stop_result = {"running": False, "uia_restore_result": {"ok": True}}
        self.html = '<meta name="panel-csrf" content="' + "a" * 43 + '">'
        self.listener = patch.object(self.control, "_listener_pid", lambda port: 123)
        self.session = patch.object(self.control, "_panel_session", lambda: object())
        def fake_response(session, request):
            self.requests.append(request)
            return self.html.encode("utf-8") if request.get_method() == "GET" else json.dumps(self.stop_result).encode("utf-8")
        self.response = patch.object(self.control, "_panel_response", fake_response)
        for fixture in (self.listener, self.session, self.response):
            fixture.start()
            self.addCleanup(fixture.stop)

    def write_pid(self):
        self.pidfile.write_text(json.dumps(self.record), encoding="utf-8")

    def test_config_json_is_only_panel_port_source(self):
        (self.base / "panel_port.txt").write_text("9999", encoding="utf-8")
        self.assertEqual(self.control.panel_url(), "http://127.0.0.1:8877")

    def test_fractional_panel_port_is_rejected(self):
        (self.base / "config.json").write_text('{"web_port":8877.5}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.control.panel_url()

    def test_invalid_config_root_is_rejected_before_launch(self):
        (self.base / "config.json").write_text('[]', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "config.json"):
            self.control.panel_url()

    def test_stop_only_verified_exact_absolute_script(self):
        self.assertTrue(self.control.stop_owned_process())
        self.assertEqual(self.terminated, [123])
        self.assertFalse(self.pidfile.exists())
        self.assertEqual([r.get_method() for r in self.requests], ["GET", "POST"])
        self.assertEqual(self.requests[-1].full_url, "http://127.0.0.1:8877/stop")
        self.assertEqual(self.requests[-1].get_header("Origin"), "http://127.0.0.1:8877")

    def test_restore_failure_preserves_process_and_pid_metadata(self):
        self.stop_result["uia_restore_result"] = {"ok": False, "status": "restore_failed"}
        before = self.pidfile.read_bytes()
        self.assertFalse(self.control.stop_owned_process())
        self.assertEqual(self.terminated, [])
        self.assertEqual(self.pidfile.read_bytes(), before)

    def test_missing_restore_confirmation_cannot_authorize_termination(self):
        del self.stop_result["uia_restore_result"]
        self.assertFalse(self.control.stop_owned_process())
        self.assertEqual(self.terminated, [])
        self.assertTrue(self.pidfile.exists())

    def test_foreign_listener_receives_no_stop_or_session_request(self):
        with patch.object(self.control, "_listener_pid", return_value=999):
            self.assertFalse(self.control.stop_owned_process())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.terminated, [])

    def test_listener_change_after_session_does_not_receive_stop_request(self):
        with patch.object(self.control, "_listener_pid", side_effect=[123, 999]):
            self.assertFalse(self.control.stop_owned_process())
        self.assertEqual([r.get_method() for r in self.requests], ["GET"])
        self.assertEqual(self.terminated, [])

    def test_duplicate_or_invalid_panel_credential_is_rejected(self):
        for html in (self.html + self.html, '<meta name="panel-csrf" content="bad token">'):
            self.html = html
            self.requests.clear()
            self.assertFalse(self.control.stop_owned_process())
            self.assertEqual([r.get_method() for r in self.requests], ["GET"])
        self.assertEqual(self.terminated, [])

    def test_stop_response_must_confirm_running_false(self):
        self.stop_result["running"] = True
        self.assertFalse(self.control.stop_owned_process())
        self.assertEqual(self.terminated, [])

    def test_stop_uses_verified_startup_port_after_config_change(self):
        (self.base / "config.json").write_text('{"web_port":9999}', encoding="utf-8")
        self.assertTrue(self.control.stop_owned_process())
        self.assertTrue(all(r.full_url.startswith("http://127.0.0.1:8877/") for r in self.requests))

    def test_missing_startup_url_never_forces_termination(self):
        self.record.pop("panel_url")
        self.write_pid()
        self.assertFalse(self.control.stop_owned_process())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.terminated, [])

    def test_path_substring_and_python_c_cannot_authorize_stop(self):
        self.args = [self.exe, "-c", f"print({self.main!r})"]
        self.assertFalse(self.control.stop_owned_process())
        self.assertEqual(self.terminated, [])
        self.assertTrue(self.pidfile.exists())
        self.args = [self.exe, self.main + ".evil"]
        self.assertFalse(self.control.stop_owned_process())
        self.assertEqual(self.terminated, [])

    def test_relative_script_never_authorizes_stop(self):
        self.args = [self.exe, "main.py"]
        self.assertFalse(self.control.stop_owned_process())
        self.assertTrue(self.pidfile.exists())

    def test_wrong_project_executable_and_command_are_preserved(self):
        for key, value in [("project", str(self.base / "other")), ("command", self.main + ".other"),
                           ("executable", "python.exe")]:
            original = self.record[key]
            self.record[key] = value
            self.write_pid()
            self.assertFalse(self.control.stop_owned_process())
            self.assertTrue(self.pidfile.exists())
            self.record[key] = original
        self.assertEqual(self.terminated, [])

    def test_unknown_access_denied_never_deletes_pid(self):
        self.live = {"status": "unknown"}
        before = self.pidfile.read_bytes()
        self.assertFalse(self.control.stop_owned_process())
        self.assertFalse(self.control.acquire_pid(str(self.pidfile)))
        self.assertEqual(self.pidfile.read_bytes(), before)
        self.assertEqual(self.terminated, [])

    def test_proven_missing_process_allows_stale_cleanup(self):
        self.live = {"status": "missing"}
        self.assertTrue(self.control.stop_owned_process())
        self.assertFalse(self.pidfile.exists())
        self.assertEqual(self.terminated, [])

    @unittest.skipUnless(os.name == "nt", "Windows read-only PID regression")
    def test_readonly_pid_is_removed_after_verified_normal_stop(self):
        os.chmod(self.pidfile, stat.S_IREAD)
        self.assertTrue(self.control.stop_owned_process())
        self.assertEqual(self.terminated, [123])
        self.assertFalse(self.pidfile.exists())
        self.assertEqual([r.get_method() for r in self.requests], ["GET", "POST"])

    @unittest.skipUnless(os.name == "nt", "Windows read-only PID regression")
    def test_readonly_stale_pid_can_be_cleaned_without_termination(self):
        os.chmod(self.pidfile, stat.S_IREAD)
        self.live = {"status": "missing"}
        self.assertTrue(self.control.stop_owned_process())
        self.assertFalse(self.pidfile.exists())
        self.assertEqual(self.terminated, [])
        self.assertEqual(self.requests, [])

    @unittest.skipUnless(os.name == "nt", "Windows read-only PID regression")
    def test_changed_readonly_pid_is_preserved_before_attribute_change(self):
        raw = self.pidfile.read_bytes()
        changed = b'{"replacement":true}'
        self.pidfile.write_bytes(changed)
        os.chmod(self.pidfile, stat.S_IREAD)
        attributes = self.pidfile.stat().st_file_attributes
        with self.assertRaisesRegex(RuntimeError, "已被另一个启动操作替换"):
            self.control._unlink_unchanged(str(self.pidfile), raw)
        self.assertEqual(self.pidfile.read_bytes(), changed)
        self.assertEqual(self.pidfile.stat().st_file_attributes, attributes)

    def test_reused_pid_never_terminates_new_process(self):
        self.live["creation"] = "999999"
        self.live["executable"] = str(self.base / "other.exe")
        self.assertTrue(self.control.stop_owned_process())
        self.assertEqual(self.terminated, [])

    def test_malformed_or_legacy_pid_is_preserved(self):
        for payload in ("123", "invalid JSON", json.dumps({"pid": 123})):
            self.pidfile.write_text(payload, encoding="utf-8")
            self.assertFalse(self.control.stop_owned_process())
            self.assertEqual(self.pidfile.read_text(encoding="utf-8"), payload)
        self.assertEqual(self.terminated, [])

    def test_release_never_deletes_replacement_metadata(self):
        self.pidfile.unlink()
        self.assertTrue(self.control.acquire_pid(str(self.pidfile)))
        self.record["pid"] = 789
        self.write_pid()
        before = self.pidfile.read_bytes()
        self.control.release_pid(str(self.pidfile))
        self.assertEqual(self.pidfile.read_bytes(), before)

    def test_release_removes_only_exact_owned_record(self):
        self.pidfile.unlink()
        self.assertTrue(self.control.acquire_pid(str(self.pidfile)))
        self.control.release_pid(str(self.pidfile))
        self.assertFalse(self.pidfile.exists())

    def test_verified_running_checks_creation_and_exact_command(self):
        self.assertEqual(self.control.owned_running()["pid"], 123)
        self.args = [self.exe, "-m", "other"]
        with self.assertRaises(ValueError):
            self.control.owned_running()

    def test_listener_identity_uses_actual_port_without_a_saved_record(self):
        self.pidfile.unlink()
        (self.base / "config.json").write_text('{"web_port":9999}', encoding="utf-8")
        info = self.control.owned_listener(8877)
        self.assertEqual(info, self.record)
        self.assertFalse(self.pidfile.exists())
        self.assertEqual(self.requests, [])

    def test_verified_listener_stops_with_missing_malformed_legacy_or_stale_record(self):
        for payload in (None, b"invalid JSON", b"123", json.dumps({**self.record, "pid": 789}).encode(),
                        json.dumps({**self.record, "creation": "999999"}).encode(),
                        json.dumps({**self.record, "executable": str(self.base / "other.exe")}).encode()):
            with self.subTest(record=payload):
                self.requests.clear()
                self.terminated.clear()
                if payload is None:
                    self.pidfile.unlink(missing_ok=True)
                else:
                    self.pidfile.write_bytes(payload)
                info = self.control.owned_listener(8877)
                self.assertTrue(self.control.stop_listener(info))
                self.assertEqual([request.get_method() for request in self.requests], ["GET", "POST"])
                self.assertEqual(self.terminated, [123])
                if payload is None:
                    self.assertFalse(self.pidfile.exists())
                else:
                    self.assertEqual(self.pidfile.read_bytes(), payload)

    def test_listener_stop_removes_only_matching_unchanged_record(self):
        info = self.control.owned_listener(8877)
        if os.name == "nt":
            os.chmod(self.pidfile, stat.S_IREAD)
        self.assertTrue(self.control.stop_listener(info))
        self.assertFalse(self.pidfile.exists())
        self.assertEqual(self.terminated, [123])

    def test_listener_stop_preserves_concurrent_replacement_record(self):
        info = self.control.owned_listener(8877)
        replacement = json.dumps({**self.record, "pid": 789}).encode()
        original_response = self.control._panel_response
        def replace_during_stop(session, request):
            body = original_response(session, request)
            if request.get_method() == "POST":
                self.pidfile.write_bytes(replacement)
            return body
        with patch.object(self.control, "_panel_response", side_effect=replace_during_stop):
            self.assertTrue(self.control.stop_listener(info))
        self.assertEqual(self.pidfile.read_bytes(), replacement)
        self.assertEqual(self.terminated, [123])

    def test_listener_native_entrypoint_and_creation_proof_cannot_be_replaced_by_metadata(self):
        for args in ([self.exe, "-c", f"print({self.main!r})"], [self.exe, "main.py"],
                     [self.exe, self.main + ".other"], [str(self.base / "other.exe"), self.main]):
            with self.subTest(args=args):
                self.args = args
                with self.assertRaises(ValueError):
                    self.control.owned_listener(8877)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.terminated, [])

    def test_listener_change_or_reused_creation_cancels_before_http(self):
        info = self.control.owned_listener(8877)
        for changed in ({**info, "pid": 999}, {**info, "creation": "different"}):
            with self.subTest(identity=changed):
                self.assertFalse(self.control.stop_listener(changed))
                self.assertEqual(self.requests, [])
                self.assertEqual(self.terminated, [])
        with patch.object(self.control, "_listener_pid", side_effect=[123, 999]):
            with self.assertRaises(ValueError):
                self.control.owned_listener(8877)

    def test_listener_creation_change_during_command_query_is_rejected(self):
        def changed(pid):
            self.live["creation"] = "999999"
            return list(self.args)
        with patch.object(self.control, "_command_args", side_effect=changed):
            with self.assertRaises(ValueError):
                self.control.owned_listener(8877)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.terminated, [])

    def test_listener_change_after_session_does_not_receive_stop(self):
        info = self.control.owned_listener(8877)
        with patch.object(self.control, "_listener_pid", side_effect=[123, 123, 123, 999]):
            self.assertFalse(self.control.stop_listener(info))
        self.assertEqual([request.get_method() for request in self.requests], ["GET"])
        self.assertEqual(self.terminated, [])

    def test_listener_requires_desktop_restore_before_finishing_process(self):
        info = self.control.owned_listener(8877)
        self.stop_result["uia_restore_result"] = {"ok": False}
        before = self.pidfile.read_bytes()
        self.assertFalse(self.control.stop_listener(info))
        self.assertEqual(self.terminated, [])
        self.assertEqual(self.pidfile.read_bytes(), before)

    def test_pid_reuse_during_native_command_query_keeps_record_and_refuses(self):
        before = self.pidfile.read_bytes()
        def changed(pid):
            self.live["creation"] = "999999"
            return list(self.args)
        with patch.object(self.control, "_command_args", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "创建身份发生变化"):
                self.control.owned_running()
        self.assertEqual(self.pidfile.read_bytes(), before)
        self.assertEqual(self.terminated, [])

    def test_pid_record_uses_native_image_instead_of_venv_redirector(self):
        self.pidfile.unlink()
        with patch.object(self.control.sys, "executable", str(self.base / "venv/Scripts/python.exe")):
            self.assertTrue(self.control.acquire_pid(str(self.pidfile)))
        self.assertEqual(json.loads(self.pidfile.read_text())["executable"], self.live["executable"])

    def test_utf8_switch_is_allowed_but_other_x_options_do_not_authorize_stop(self):
        self.args = [self.exe, "-X", "utf8", "-B", "-u", self.main]
        self.assertEqual(self.control.owned_running()["pid"], 123)
        for prefix in (["-X", "other"], ["-X"], ["-c", self.main], ["-m", "package"]):
            self.args = [self.exe, *prefix, self.main]
            with self.assertRaises(ValueError): self.control.owned_running()


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.control, self.astrbot = load("startup_control"), load("start_astrbot")
        self.control.BASE = str(self.base)
        (self.base / "config.json").write_text('{"web_port":8877}', encoding="utf-8")
        with patch.dict(sys.modules, {"startup_control": self.control, "start_astrbot": self.astrbot}):
            self.launcher = load("startup_launcher")
        self.launcher.BASE = self.base
        self.spawned = []
        self.process = Mock()
        self.process.poll.return_value = None
        self.spawn = patch.object(self.launcher, "_spawn", lambda args, log_name, cwd=None: self.spawned.append((args, log_name, cwd)) or self.process)
        self.spawn.start()
        self.addCleanup(self.spawn.stop)
        self.running = patch.object(self.control, "owned_running", lambda: None)
        self.running.start()
        self.addCleanup(self.running.stop)
        self.port = patch.object(self.launcher, "port_open", lambda url: False)
        self.port.start()
        self.addCleanup(self.port.stop)

    def test_bridge_spawn_has_absolute_main_path(self):
        _, url = self.launcher.launch_bridge()
        command = self.spawned[0][0]
        self.assertEqual(command[1:], ["-u", str(self.base / "main.py")])
        self.assertTrue(Path(command[2]).is_absolute())
        self.assertEqual(url, "http://127.0.0.1:8877")

    @unittest.skipUnless(os.name == "nt", "Windows read-only startup log regression")
    def test_readonly_startup_logs_are_appendable_without_changing_old_bytes(self):
        self.spawn.stop()
        original = b"historical log bytes\n"
        for name in ("startup_bridge.log", "startup_astrbot.log"):
            with self.subTest(name=name):
                path = self.base / name
                path.write_bytes(original)
                os.chmod(path, stat.S_IREAD)
                with patch.dict(os.environ, {"WECHATBOT_PORTABLE_ROOT": ""}), \
                        patch.object(self.launcher.subprocess, "Popen", return_value=self.process) as popen, \
                        patch.object(self.launcher, "_capture_bridge_spawn"):
                    self.assertIs(self.launcher._spawn(["synthetic"], name), self.process)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
                self.assertEqual(self.process._wechatbot_startup_log["offset"], len(original))
                popen.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows read-only startup log regression")
    def test_readonly_startup_log_bytes_survive_spawn_failure(self):
        self.spawn.stop()
        path = self.base / "startup_bridge.log"
        original = b"historical log bytes\n"
        path.write_bytes(original)
        os.chmod(path, stat.S_IREAD)
        with patch.object(self.launcher.subprocess, "Popen", side_effect=OSError("synthetic spawn failure")), \
                self.assertRaisesRegex(OSError, "synthetic spawn failure"):
            self.launcher._spawn(["synthetic"], path.name)
        self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(os.name == "nt", "Windows read-only startup log regression")
    def test_unapproved_log_keeps_readonly_attribute_and_is_not_spawned(self):
        self.spawn.stop()
        path = self.base / "other.log"
        original = b"other file bytes"
        path.write_bytes(original)
        os.chmod(path, stat.S_IREAD)
        attributes = path.stat().st_file_attributes
        with patch.object(self.launcher.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(ValueError, "固定日志"):
            self.launcher._spawn(["synthetic"], path.name)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_file_attributes, attributes)
        popen.assert_not_called()

    def test_query_timeout_and_exit_status_survive_redaction_without_argv(self):
        failures = (
            (self.launcher.subprocess.TimeoutExpired(["powershell", "PRIVATE_ARG_CANARY"], 5), "5 秒"),
            (self.launcher.subprocess.CalledProcessError(17, ["powershell", "PRIVATE_ARG_CANARY"]), "退出码 17"),
        )
        for failure, status in failures:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.launcher._report_failure("微信bot", self.launcher._failure_reason(failure), timeout=180)
            report = output.getvalue()
            self.assertIn(status, report)
            self.assertNotIn("PRIVATE_ARG_CANARY", report)
            self.assertNotIn("[子进程命令已隐藏]", report)

    def test_rejected_existing_port_does_not_claim_a_new_log_was_created(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.launcher._report_failure("AstrBot", "本包记录无法核验", timeout=180)
        self.assertIn("预期日志位置（本轮未新建服务日志）", output.getvalue())
        self.assertNotIn("本轮日志：", output.getvalue())
        self.assertFalse((self.base / "startup_astrbot.log").exists())

    def test_occupied_panel_port_never_kills_or_launches(self):
        self.launcher.port_open = lambda url: True
        with patch.object(self.control, "stop_owned_process") as stop:
            with self.assertRaises(RuntimeError):
                self.launcher.launch_bridge()
            stop.assert_not_called()
        self.assertEqual(self.spawned, [])

    def test_remote_astrbot_url_does_not_spawn_local_service(self):
        (self.base / "astrbot_url.txt").write_text("https://example.com/bot", encoding="utf-8")
        _, url = self.launcher.launch_astrbot()
        self.assertEqual(url, "https://example.com/bot")
        self.assertEqual(self.spawned, [])

    def test_astrbot_url_port_parser_and_credential_rejection(self):
        (self.base / "astrbot_url.txt").write_text("http://127.0.0.1:7001/console", encoding="utf-8")
        self.assertEqual(self.launcher.endpoint(self.launcher.astrbot_url()), ("127.0.0.1", 7001))
        (self.base / "astrbot_url.txt").write_text("javascript:alert(1)", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.launcher.astrbot_url()
        (self.base / "astrbot_url.txt").write_text("http://key:secret@127.0.0.1:7001", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.launcher.astrbot_url()

    def test_explicit_missing_astrbot_interpreter_fails_without_fallback(self):
        (self.base / "astrbot_py.txt").write_text(str(self.base / "missing.exe"), encoding="utf-8")
        with self.assertRaises(FileNotFoundError):
            self.launcher.astrbot_python()

    def test_official_cli_run_preserves_lock_path(self):
        scripts = self.base / "Scripts"
        scripts.mkdir()
        python, cli = scripts / "python.exe", scripts / "astrbot.exe"
        python.touch()
        cli.touch()
        with patch.dict(os.environ, {"ASTRBOT_CLI_EXECUTABLE": ""}):
            self.assertEqual(self.astrbot.build_command(6185, str(python)), [str(cli), "run", "--port", "6185"])

    def test_restart_aborts_on_unverified_pid(self):
        with patch.object(self.control, "stop_owned_process", return_value=False):
            self.assertEqual(self.launcher.main(["restart"]), 1)
        self.assertEqual(self.spawned, [])

    def test_independent_services_start_before_readiness_wait(self):
        order = []
        with patch.object(self.launcher, "launch_astrbot", side_effect=lambda: order.append("astrbot") or (None, "http://127.0.0.1:6185")), \
             patch.object(self.launcher, "launch_bridge", side_effect=lambda foreground: order.append("bridge") or (None, "http://127.0.0.1:8877")), \
             patch.object(self.launcher, "open_when_ready", side_effect=lambda targets: order.append("wait") or True):
            self.assertEqual(self.launcher.main(["all"]), 0)
        self.assertEqual(order, ["astrbot", "bridge", "wait"])

    def test_child_failure_does_not_report_ready(self):
        process = Mock()
        process.poll.return_value = 1
        with patch.object(self.launcher.webbrowser, "open") as browser, \
             patch.object(self.launcher, "port_open", return_value=True):
            self.assertFalse(self.launcher.open_when_ready([("桥接", process, "http://127.0.0.1:8877")], timeout=0.1))
            browser.assert_not_called()

    def test_occupied_port_reports_component_port_and_fixed_log_without_spawning(self):
        output = io.StringIO()
        with patch.object(self.launcher, "port_open", return_value=True), contextlib.redirect_stdout(output):
            self.assertEqual(self.launcher.main(["bridge"]), 1)
        text = output.getvalue()
        for detail in ("阶段：微信bot：服务启动失败", "原因：RuntimeError", "端口被占用", "面板端口：8877", str(self.base / "startup_bridge.log")):
            self.assertIn(detail, text)
        self.assertEqual(self.spawned, [])

    def test_missing_astrbot_instance_reports_exact_component_without_claiming_ready(self):
        output = io.StringIO()
        with patch.object(self.launcher, "astrbot_python", return_value=None), \
             patch.object(self.astrbot, "build_command", return_value=["synthetic-cli"]), \
             patch.object(self.astrbot, "_find_astrbot_root", return_value=self.base / "missing-instance"), \
             patch.object(self.launcher, "launch_bridge", side_effect=RuntimeError("synthetic bridge failure")), contextlib.redirect_stdout(output):
            self.assertEqual(self.launcher.main(["all"]), 1)
        text = output.getvalue()
        self.assertIn("阶段：AstrBot：服务启动失败", text)
        self.assertIn("FileNotFoundError", text)
        self.assertIn("工作目录不存在", text)
        self.assertIn("面板端口：6185", text)
        self.assertIn(str(self.base / "startup_astrbot.log"), text)

    def test_portable_deadline_allows_initialization_beyond_sixty_seconds(self):
        clock = [0.0]
        output = io.StringIO()
        with patch.object(self.launcher.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(self.launcher.time, "sleep", side_effect=lambda _: clock.__setitem__(0, clock[0] + 15)), \
             patch.object(self.launcher, "port_open", side_effect=lambda _: clock[0] >= 75), \
             patch.object(self.launcher, "_target_ready", return_value=True), \
             patch.object(self.launcher.webbrowser, "open") as browser, contextlib.redirect_stdout(output):
            self.assertTrue(self.launcher.open_when_ready([("AstrBot", self.process, "http://127.0.0.1:6185")], timeout=180))
        self.assertEqual(clock[0], 75)
        browser.assert_called_once()
        self.assertNotIn("失败", output.getvalue())
        with patch.dict(os.environ, {"WECHATBOT_PORTABLE_ROOT": str(self.base)}), \
             patch.object(self.launcher, "_handover_previous_packages"), \
             patch.object(self.launcher, "launch_astrbot", return_value=(None, "http://127.0.0.1:6185")), \
             patch.object(self.launcher, "launch_bridge", return_value=(None, "http://127.0.0.1:8877")), \
             patch.object(self.launcher, "open_when_ready", return_value=True) as wait:
            self.assertEqual(self.launcher.main(["all"]), 0)
        self.assertEqual(wait.call_args.kwargs["timeout"], 180)
        self.assertTrue(wait.call_args.kwargs["require_bridge_connection"])

    def fake_panel_session(self, statuses, clock, request_cost=0):
        requests = []
        test = self
        class FakeHTTP(BaseHandler):
            handler_order = 100
            def http_open(self, request):
                test.assertEqual(request.host, "127.0.0.1:8766")
                requests.append((request.selector, request.get_header("Cookie"), request.timeout))
                test.assertGreater(request.timeout, 0)
                clock[0] += min(request_cost, request.timeout)
                headers = Message()
                if request.selector == "/":
                    headers["Set-Cookie"] = "akasha_panel_session=TEST_SESSION; Path=/; HttpOnly"
                    body = b'<meta name="panel-csrf" content="PRIVATE_HTML_CANARY">'
                else:
                    test.assertEqual(request.selector, "/api/status")
                    test.assertIn("akasha_panel_session=TEST_SESSION", request.get_header("Cookie"))
                    value = statuses.pop(0) if len(statuses) > 1 else statuses[0]
                    body = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
                response = addinfourl(io.BytesIO(body), headers, request.full_url, 200)
                response.msg = "OK"
                return response
        return build_opener(ProxyHandler({}), FakeHTTP(), HTTPCookieProcessor(CookieJar())), requests

    def test_portable_all_waits_for_actual_onebot_connection_using_reused_cookie_session(self):
        clock = [0.0]
        statuses = [{"running": True, "ob_connected": False}, {"running": True, "ob_connected": True}]
        session, requests = self.fake_panel_session(statuses, clock)
        output = io.StringIO()
        with patch.object(self.launcher.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(self.launcher.time, "sleep", side_effect=lambda _: clock.__setitem__(0, clock[0] + 75)), \
             patch.object(self.launcher, "port_open", return_value=True), \
             patch.object(self.launcher, "_target_ready", return_value=True) as verify, \
             patch.object(self.control, "_panel_session", return_value=session) as make_session, \
             patch.object(self.launcher.socket, "create_connection", side_effect=AssertionError("No real sockets")), \
             patch.object(self.launcher.webbrowser, "open") as browser, contextlib.redirect_stdout(output):
            self.assertTrue(self.launcher.open_when_ready([
                ("AstrBot", self.process, "http://127.0.0.1:6185"),
                ("微信bot", self.process, "http://127.0.0.1:8766")], timeout=180, require_bridge_connection=True))
        self.assertEqual(clock[0], 75)
        self.assertEqual([r[0] for r in requests], ["/", "/api/status", "/", "/api/status"])
        self.assertEqual(make_session.call_count, 1)
        self.assertEqual(verify.call_count, 5)
        self.assertEqual([c.args[0] for c in browser.call_args_list], ["http://127.0.0.1:6185", "http://127.0.0.1:8766"])
        self.assertIn("本包桥接已确认连接 AstrBot OneBot", output.getvalue())
        self.assertNotIn("PRIVATE_HTML_CANARY", output.getvalue())

    def test_panels_ready_but_onebot_disconnected_times_out_without_opening_either_panel(self):
        clock = [0.0]
        session, _ = self.fake_panel_session([{"running": True, "ob_connected": False}], clock)
        output = io.StringIO()
        with patch.object(self.launcher.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(self.launcher.time, "sleep", side_effect=lambda _: clock.__setitem__(0, clock[0] + 60)), \
             patch.object(self.launcher, "port_open", return_value=True), \
             patch.object(self.launcher, "_target_ready", return_value=True), \
             patch.object(self.control, "_panel_session", return_value=session), \
             patch.object(self.launcher.webbrowser, "open") as browser, contextlib.redirect_stdout(output):
            self.assertFalse(self.launcher.open_when_ready([
                ("AstrBot", self.process, "http://127.0.0.1:6185"),
                ("微信bot", self.process, "http://127.0.0.1:8766")], timeout=180, require_bridge_connection=True))
        self.assertEqual(clock[0], 180)
        self.assertIn("仍未确认连接 AstrBot OneBot", output.getvalue())
        self.assertIn("11229", output.getvalue())
        self.assertIn("180 秒", output.getvalue())
        browser.assert_not_called()
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()

    def test_status_http_requests_consume_remaining_startup_deadline(self):
        clock = [179.0]
        session, requests = self.fake_panel_session([{"running": True, "ob_connected": True}], clock, request_cost=0.75)
        with patch.object(self.launcher.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(self.launcher._bridge_connection_ready("http://127.0.0.1:8766", session, 180))
        self.assertEqual([r[2] for r in requests], [1.0, 0.25])
        requests.clear()
        with patch.object(self.launcher.time, "monotonic", return_value=180):
            self.assertFalse(self.launcher._bridge_connection_ready("http://127.0.0.1:8766", session, 180))
        self.assertEqual(requests, [])

    def test_status_requires_real_booleans_and_running_bridge(self):
        clock = [0.0]
        for value in ({"running": False, "ob_connected": True}, {"running": True, "ob_connected": "true"},
                      {"running": 1, "ob_connected": True}, {"running": True}):
            with self.subTest(value=value):
                session, _ = self.fake_panel_session([value], clock)
                self.assertFalse(self.launcher._bridge_connection_ready("http://127.0.0.1:8766", session, time.monotonic() + 10))

    def test_invalid_status_never_replays_response_body(self):
        clock = [0.0]
        session, _ = self.fake_panel_session([b"PRIVATE_JSON_CANARY"], clock)
        output = io.StringIO()
        with patch.object(self.launcher, "port_open", return_value=True), \
             patch.object(self.launcher, "_target_ready", return_value=True), \
             patch.object(self.control, "_panel_session", return_value=session), \
             patch.object(self.launcher.webbrowser, "open") as browser, contextlib.redirect_stdout(output):
            self.assertFalse(self.launcher.open_when_ready([("微信bot", self.process, "http://127.0.0.1:8766")],
                                                         timeout=180, require_bridge_connection=True))
        self.assertIn("有效 JSON", output.getvalue())
        self.assertNotIn("PRIVATE_JSON_CANARY", output.getvalue())
        self.assertNotIn("PRIVATE_HTML_CANARY", output.getvalue())
        browser.assert_not_called()

    def test_owner_changed_after_status_read_does_not_report_connected(self):
        clock = [0.0]
        session, _ = self.fake_panel_session([{"running": True, "ob_connected": True}], clock)
        output = io.StringIO()
        with patch.object(self.launcher, "port_open", return_value=True), \
             patch.object(self.launcher, "_target_ready", side_effect=[True, RuntimeError("本包创建身份改变")]), \
             patch.object(self.control, "_panel_session", return_value=session), \
             patch.object(self.launcher.webbrowser, "open") as browser, contextlib.redirect_stdout(output):
            self.assertFalse(self.launcher.open_when_ready([("微信bot", self.process, "http://127.0.0.1:8766")],
                                                         timeout=180, require_bridge_connection=True))
        self.assertIn("本包创建身份改变", output.getvalue())
        self.assertNotIn("已确认连接", output.getvalue())
        browser.assert_not_called()

    def test_portable_bridge_only_keeps_existing_launch_scope(self):
        with patch.dict(os.environ, {"WECHATBOT_PORTABLE_ROOT": str(self.base)}), \
             patch.object(self.launcher, "launch_astrbot") as astrbot, \
             patch.object(self.launcher, "launch_bridge", return_value=(None, "http://127.0.0.1:8766")), \
             patch.object(self.launcher, "open_when_ready", return_value=True) as wait:
            self.assertEqual(self.launcher.main(["bridge"]), 0)
        astrbot.assert_not_called()
        self.assertFalse(wait.call_args.kwargs["require_bridge_connection"])

    def test_portable_handover_finishes_before_either_service_is_started(self):
        order = []
        with patch.dict(os.environ, {"WECHATBOT_PORTABLE_ROOT": str(self.base)}), \
             patch.object(self.launcher, "_handover_previous_packages", side_effect=lambda: order.append("handover")), \
             patch.object(self.launcher, "launch_astrbot", side_effect=lambda: order.append("astrbot") or (None, "http://127.0.0.1:6185")), \
             patch.object(self.launcher, "launch_bridge", side_effect=lambda _: order.append("bridge") or (None, "http://127.0.0.1:8877")), \
             patch.object(self.launcher, "open_when_ready", return_value=True):
            self.assertEqual(self.launcher.main(["all"]), 0)
        self.assertEqual(order, ["handover", "astrbot", "bridge"])

    def test_failed_handover_prevents_partial_service_startup(self):
        with patch.dict(os.environ, {"WECHATBOT_PORTABLE_ROOT": str(self.base)}), \
             patch.object(self.launcher, "_handover_previous_packages", side_effect=RuntimeError("synthetic occupied port")), \
             patch.object(self.launcher, "launch_astrbot") as astrbot, \
             patch.object(self.launcher, "launch_bridge") as bridge, \
             patch.object(self.launcher, "_report_failure") as report:
            self.assertEqual(self.launcher.main(["all"]), 1)
        astrbot.assert_not_called()
        bridge.assert_not_called()
        self.assertEqual(report.call_args.args[0], "版本切换")

    def test_timeout_reports_deadline_and_never_opens_browser_or_kills_child(self):
        clock = [0.0]
        output = io.StringIO()
        with patch.object(self.launcher.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(self.launcher.time, "sleep", side_effect=lambda _: clock.__setitem__(0, clock[0] + 60)), \
             patch.object(self.launcher.webbrowser, "open") as browser, contextlib.redirect_stdout(output):
            self.assertFalse(self.launcher.open_when_ready([("AstrBot", self.process, "http://127.0.0.1:6185")], timeout=180))
        for detail in ("阶段：AstrBot：服务启动失败", "等待超时", "180 秒", "面板端口：6185", str(self.base / "startup_astrbot.log")):
            self.assertIn(detail, output.getvalue())
        browser.assert_not_called()
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()

    def test_native_child_exit_seventeen_only_shows_new_redacted_complete_log_lines(self):
        self.spawn.stop()
        path = self.base / "startup_bridge.log"
        path.write_text("RuntimeError: HISTORICAL_CHAT_CANARY\n", encoding="utf-8")
        (self.base / "config.json").write_text(json.dumps({"web_port": 8877, "main_llm_api_key": "CONFIG_KEY_CANARY", "voice": {"api_key": "VOICE_KEY_CANARY"}}), encoding="utf-8")
        script = "import sys; print('RuntimeError: fresh import failed CONFIG_KEY_CANARY VOICE_KEY_CANARY ENV_KEY_CANARY password=FIELD_KEY_CANARY', flush=True); print('private chat CHAT_CANARY', flush=True); sys.stdout.write('Error: INCOMPLETE_CANARY'); sys.stdout.flush(); sys.exit(17)"
        output = io.StringIO()
        with patch.dict(os.environ, {"CUSTOM_API_KEY": "ENV_KEY_CANARY"}), contextlib.redirect_stdout(output):
            process = self.launcher._spawn([sys.executable, "-X", "utf8", "-c", script], "startup_bridge.log")
            self.assertEqual(process.wait(timeout=15), 17)
            self.assertFalse(self.launcher.open_when_ready([("微信bot", process, "http://127.0.0.1:8877")], timeout=180))
        text = output.getvalue()
        for detail in ("退出码 17", "180 秒", "fresh import failed", str(path)):
            self.assertIn(detail, text)
        for canary in ("HISTORICAL_CHAT_CANARY", "CONFIG_KEY_CANARY", "VOICE_KEY_CANARY", "ENV_KEY_CANARY", "FIELD_KEY_CANARY", "CHAT_CANARY", "INCOMPLETE_CANARY"):
            self.assertNotIn(canary, text)
        self.assertIn("HISTORICAL_CHAT_CANARY", path.read_text(encoding="utf-8"))

    def test_log_without_spawn_boundary_or_with_truncation_is_not_replayed(self):
        path = self.base / "startup_astrbot.log"
        path.write_text("RuntimeError: OLD_LOG_CANARY\n", encoding="utf-8")
        self.process.poll.return_value = 17
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(self.launcher.open_when_ready([("AstrBot", self.process, "http://127.0.0.1:6185")], timeout=180))
        self.assertNotIn("OLD_LOG_CANARY", output.getvalue())
        status = path.stat()
        self.process._wechatbot_startup_log = {"name": path.name, "path": str(path.absolute()), "offset": status.st_size + 100, "identity": (status.st_dev, status.st_ino)}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(self.launcher.open_when_ready([("AstrBot", self.process, "http://127.0.0.1:6185")], timeout=180))
        self.assertNotIn("OLD_LOG_CANARY", output.getvalue())
        self.assertIn(str(path), output.getvalue())

    def test_current_log_tail_is_bounded_and_missing_log_does_not_mask_exit(self):
        self.spawn.stop()
        path = self.base / "startup_bridge.log"
        path.write_text("RuntimeError: OLD_HISTORY_CANARY\n", encoding="utf-8")
        with patch.object(self.launcher.subprocess, "Popen", return_value=self.process):
            self.launcher._spawn(["synthetic-only"], "startup_bridge.log")
        with path.open("a", encoding="utf-8") as log:
            log.write("RuntimeError: EARLY_CURRENT_CANARY\n" + ("opaque private chat " + " x" * 600 + "\n") * 60)
            log.write("ModuleNotFoundError: No module named synthetic_tail\n")
        self.process.poll.return_value = 17
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(self.launcher.open_when_ready([("微信bot", self.process, "http://127.0.0.1:8877")], timeout=180))
        text = output.getvalue()
        self.assertIn("synthetic_tail", text)
        self.assertLess(len(text), 6000)
        for private in ("OLD_HISTORY_CANARY", "EARLY_CURRENT_CANARY", "private chat"):
            self.assertNotIn(private, text)
        path.unlink()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(self.launcher.open_when_ready([("微信bot", self.process, "http://127.0.0.1:8877")], timeout=180))
        self.assertIn("退出码 17", output.getvalue())
        self.assertIn(str(path), output.getvalue())

    def test_replaced_or_unreadable_config_log_only_reports_path(self):
        path = self.base / "startup_astrbot.log"
        path.write_text("RuntimeError: UNTRUSTED_LOG_CANARY\n", encoding="utf-8")
        status = path.stat()
        self.process._wechatbot_startup_log = {"name": path.name, "path": str(path.absolute()), "offset": 0, "identity": (status.st_dev, status.st_ino + 1)}
        self.process.poll.return_value = 17
        for invalid_config in (False, True):
            if invalid_config:
                self.process._wechatbot_startup_log["identity"] = (status.st_dev, status.st_ino)
                (self.base / "config.json").write_text("invalid json", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertFalse(self.launcher.open_when_ready([("AstrBot", self.process, "http://127.0.0.1:6185")], timeout=180))
            self.assertNotIn("UNTRUSTED_LOG_CANARY", output.getvalue())
            self.assertIn(str(path), output.getvalue())

    def test_truncated_then_regrown_same_inode_log_does_not_replay_wrong_round(self):
        self.spawn.stop()
        path = self.base / "startup_bridge.log"
        path.write_text("RuntimeError: OLD_HISTORY_BOUNDARY\n", encoding="utf-8")
        with patch.object(self.launcher.subprocess, "Popen", return_value=self.process):
            self.launcher._spawn(["synthetic"], path.name)
        identity = path.stat().st_ino
        path.write_text("RuntimeError: REGROWN_WRONG_ROUND_CANARY " + " x" * 200 + "\n", encoding="utf-8")
        self.assertEqual(path.stat().st_ino, identity)
        self.assertGreater(path.stat().st_size, self.process._wechatbot_startup_log["offset"])
        self.process.poll.return_value = 17
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(self.launcher.open_when_ready([("微信bot", self.process, "http://127.0.0.1:8877")], timeout=180))
        self.assertNotIn("REGROWN_WRONG_ROUND_CANARY", output.getvalue())
        self.assertIn(str(path), output.getvalue())

    def test_old_unterminated_line_joined_with_new_output_is_never_replayed(self):
        self.spawn.stop()
        path = self.base / "startup_bridge.log"
        path.write_text("RuntimeError: OLD_UNTERMINATED_CHAT_CANARY ", encoding="utf-8")
        with patch.object(self.launcher.subprocess, "Popen", return_value=self.process):
            self.launcher._spawn(["synthetic"], path.name)
        with path.open("a", encoding="utf-8") as output:
            output.write("Error: JOINED_FIRST_LINE_CANARY\nRuntimeError: new independent failure\n")
        self.process.poll.return_value = 17
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(self.launcher.open_when_ready([("微信bot", self.process, "http://127.0.0.1:8877")], timeout=180))
        for canary in ("OLD_UNTERMINATED_CHAT_CANARY", "JOINED_FIRST_LINE_CANARY"):
            self.assertNotIn(canary, output.getvalue())
        self.assertIn("new independent failure", output.getvalue())

    def bridge_info(self, pid=123):
        return {"pid": pid, "creation": "123456", "executable": str(self.base / "python.exe"), "panel_url": "http://127.0.0.1:8877"}

    def test_wrong_listener_or_unknown_project_never_opens_browser(self):
        output = io.StringIO()
        for info in (None, self.bridge_info()):
            with patch.object(self.control, "owned_running", return_value=info), \
                 patch.object(self.control, "_listener_pid", return_value=999), \
                 patch.object(self.launcher, "port_open", return_value=True), \
                 patch.object(self.launcher.webbrowser, "open") as browser, contextlib.redirect_stdout(output):
                self.assertFalse(self.launcher.open_when_ready([("微信bot", None, "http://127.0.0.1:8877")], timeout=180))
                browser.assert_not_called()
        self.assertIn("服务启动失败", output.getvalue())

    def test_existing_bridge_requires_listener_identity_and_does_not_spawn(self):
        info = self.bridge_info()
        with patch.object(self.control, "owned_running", return_value=info), \
             patch.object(self.control, "_listener_pid", return_value=123), \
             patch.object(self.control, "_panel_session", return_value=self.existing_panel_session({"running": True, "paused": False})[0]), \
             patch.object(self.launcher, "port_open", return_value=True):
            process, _ = self.launcher.launch_bridge()
        self.assertIsNone(process)
        self.assertEqual(self.spawned, [])
        with patch.object(self.control, "owned_running", side_effect=[info, dict(info, creation="999")]), \
             patch.object(self.control, "_listener_pid", return_value=123), self.assertRaises(RuntimeError):
            self.launcher._bridge_listener_ready(None, info["panel_url"])

    def existing_panel_session(self, status, result=None, html=None, after_read=None, result_status=200):
        requests = []
        current = [status]
        test = self
        token = "a" * 43
        class FakeHTTP(BaseHandler):
            handler_order = 100
            def http_open(self, request):
                test.assertEqual(request.host, "127.0.0.1:8877")
                requests.append((request.get_method(), request.selector))
                headers = Message()
                if request.selector == "/":
                    headers["Set-Cookie"] = "akasha_panel_session=SYNTHETIC; Path=/; HttpOnly"
                    body = (html if html is not None else '<meta name="panel-csrf" content="' + token + '">').encode()
                else:
                    test.assertIn("akasha_panel_session=SYNTHETIC", request.get_header("Cookie"))
                    if request.selector == "/api/status":
                        value = current[0]
                        if after_read:
                            after_read()
                    else:
                        test.assertIn(request.selector, {"/start", "/resume"})
                        test.assertEqual(request.get_method(), "POST")
                        test.assertEqual(request.data, b"{}")
                        test.assertEqual(request.get_header("X-panel-token"), token)
                        test.assertEqual(request.get_header("Origin"), "http://127.0.0.1:8877")
                        value = result if result is not None else {"ok": True, "running": True, "paused": False}
                        current[0] = value
                    body = value if isinstance(value, bytes) else json.dumps(value).encode()
                code = result_status if request.get_method() == "POST" else 200
                response = addinfourl(io.BytesIO(body), headers, request.full_url, code)
                response.msg = "Conflict" if code == 409 else "OK"
                return response
        return build_opener(ProxyHandler({}), FakeHTTP(), HTTPCookieProcessor(CookieJar())), requests

    def test_existing_stopped_or_paused_panel_resumes_without_spawning_or_config_changes(self):
        info = self.bridge_info()
        config_before = (self.base / "config.json").read_bytes()
        for status, action in (({"running": False, "paused": False}, "/start"),
                               ({"running": True, "paused": True}, "/resume"),
                               ({"running": True, "paused": False}, None)):
            with self.subTest(status=status):
                session, requests = self.existing_panel_session(status)
                with patch.object(self.control, "owned_running", return_value=info), \
                     patch.object(self.control, "_listener_pid", return_value=123), \
                     patch.object(self.control, "_panel_session", return_value=session), \
                     patch.object(self.launcher, "port_open", return_value=True), \
                     patch.object(self.launcher.socket, "create_connection", side_effect=AssertionError("No real sockets")):
                    self.assertEqual(self.launcher.launch_bridge(), (None, info["panel_url"]))
                expected = [("GET", "/"), ("GET", "/api/status")]
                if action:
                    expected.append(("POST", action))
                self.assertEqual(requests, expected)
                self.assertEqual(self.spawned, [])
                self.assertEqual((self.base / "config.json").read_bytes(), config_before)

    def test_existing_panel_owner_change_prevents_start_request(self):
        info = self.bridge_info()
        current = [info]
        session, requests = self.existing_panel_session({"running": False, "paused": False},
            after_read=lambda: current.__setitem__(0, dict(info, creation="999")))
        with patch.object(self.control, "owned_running", side_effect=lambda: current[0]), \
             patch.object(self.control, "_listener_pid", return_value=123), \
             patch.object(self.control, "_panel_session", return_value=session), \
             patch.object(self.launcher, "port_open", return_value=True), \
             self.assertRaisesRegex(RuntimeError, "创建身份已改变"):
            self.launcher.launch_bridge()
        self.assertEqual(requests, [("GET", "/"), ("GET", "/api/status")])
        self.assertEqual(self.spawned, [])

    def test_portable_all_restarts_idle_panel_before_waiting_for_onebot(self):
        info = self.bridge_info()
        session, requests = self.existing_panel_session({"running": False, "paused": False, "ob_connected": False},
            result={"ok": True, "running": True, "paused": False, "ob_connected": True})
        output = io.StringIO()
        with patch.dict(os.environ, {"WECHATBOT_PORTABLE_ROOT": str(self.base)}), \
             patch.object(self.launcher, "_handover_previous_packages"), \
             patch.object(self.launcher, "launch_astrbot", return_value=(None, "http://127.0.0.1:6185")), \
             patch.object(self.control, "owned_running", return_value=info), \
             patch.object(self.control, "_listener_pid", return_value=123), \
             patch.object(self.control, "_panel_session", return_value=session), \
             patch.object(self.launcher, "_target_ready", return_value=True), \
             patch.object(self.launcher, "port_open", return_value=True), \
             patch.object(self.launcher.webbrowser, "open") as browser, \
             patch.object(self.launcher.time, "sleep", side_effect=AssertionError("Must not wait with a stopped panel")), \
             contextlib.redirect_stdout(output):
            self.assertEqual(self.launcher.main(["all"]), 0)
        self.assertEqual(requests, [("GET", "/"), ("GET", "/api/status"), ("POST", "/start"),
                                    ("GET", "/"), ("GET", "/api/status")])
        self.assertEqual(self.spawned, [])
        self.assertEqual(browser.call_count, 2)
        self.assertIn("本包桥接已确认连接 AstrBot OneBot", output.getvalue())

    def test_existing_panel_rejected_or_invalid_start_fails_without_replaying_response(self):
        info = self.bridge_info()
        for result in ({"ok": False, "running": False, "paused": False, "error": "PRIVATE_CANARY"},
                       {"ok": True, "running": True, "paused": True},
                       {"running": "true", "paused": False}, b"PRIVATE_CANARY"):
            with self.subTest(result=result):
                session, requests = self.existing_panel_session({"running": False, "paused": False}, result=result)
                with patch.object(self.control, "owned_running", return_value=info), \
                     patch.object(self.control, "_listener_pid", return_value=123), \
                     patch.object(self.control, "_panel_session", return_value=session), \
                     patch.object(self.launcher, "port_open", return_value=True), \
                     self.assertRaises(RuntimeError) as caught:
                    self.launcher.launch_bridge()
                self.assertNotIn("PRIVATE_CANARY", str(caught.exception))
                self.assertEqual(requests[-1], ("POST", "/start"))
                self.assertEqual(self.spawned, [])

    def test_existing_stopped_panel_bad_csrf_never_sends_start(self):
        info = self.bridge_info()
        for html in ("", '<meta name="panel-csrf" content="bad token">'):
            session, requests = self.existing_panel_session({"running": False, "paused": False}, html=html)
            with patch.object(self.control, "owned_running", return_value=info), \
                 patch.object(self.control, "_listener_pid", return_value=123), \
                 patch.object(self.control, "_panel_session", return_value=session), \
                 patch.object(self.launcher, "port_open", return_value=True), \
                 self.assertRaisesRegex(RuntimeError, "操作凭证"):
                self.launcher.launch_bridge()
            self.assertEqual(requests, [("GET", "/"), ("GET", "/api/status")])

    def test_existing_panel_http_rejection_fails_without_replaying_body_or_spawning(self):
        info = self.bridge_info()
        session, requests = self.existing_panel_session({"running": False, "paused": False},
            result={"ok": False, "error": "PRIVATE_HTTP_CANARY"}, result_status=409)
        with patch.object(self.control, "owned_running", return_value=info), \
             patch.object(self.control, "_listener_pid", return_value=123), \
             patch.object(self.control, "_panel_session", return_value=session), \
             patch.object(self.launcher, "port_open", return_value=True), \
             patch.object(self.launcher.socket, "create_connection", side_effect=AssertionError("No real sockets")), \
             self.assertRaisesRegex(OSError, "HTTP Error 409") as caught:
            self.launcher.launch_bridge()
        self.assertNotIn("PRIVATE_HTTP_CANARY", str(caught.exception))
        self.assertEqual(requests, [("GET", "/"), ("GET", "/api/status"), ("POST", "/start")])
        self.assertEqual(self.spawned, [])

    def test_new_bridge_record_must_belong_to_current_spawn_or_verified_child(self):
        info = self.bridge_info(202)
        process = types.SimpleNamespace(pid=101, _wechatbot_spawn_identity=dict(self.bridge_info(101)))
        helper = self.launcher._astrbot_lifecycle()
        root = types.SimpleNamespace(children=Mock(return_value=[types.SimpleNamespace(pid=202)]))
        native = {"status": "alive", "creation": "123456", "executable": info["executable"]}
        with patch.object(self.control, "owned_running", return_value=info), \
             patch.object(self.control, "_listener_pid", return_value=202), \
             patch.object(self.control, "_native_process", return_value=native), \
             patch.object(self.control, "_verify_command"), \
             patch.object(self.launcher, "_bridge_interpreter_matches", return_value=True), \
             patch.object(helper.psutil, "Process", return_value=root):
            self.assertEqual(self.launcher._bridge_listener_ready(process, info["panel_url"]), info)
            root.children.return_value = [types.SimpleNamespace(pid=999)]
            with self.assertRaises(RuntimeError): self.launcher._bridge_listener_ready(process, info["panel_url"])
            root.children.return_value = [types.SimpleNamespace(pid=202)]
            native["creation"] = "999999"
            with self.assertRaises(RuntimeError): self.launcher._bridge_listener_ready(process, info["panel_url"])

    def test_portable_existing_astrbot_cannot_accept_other_package_listener(self):
        helper = types.SimpleNamespace(finish_pending_stop=Mock(return_value=True),
                                       ready_listener=Mock(side_effect=RuntimeError("foreign package listener")))
        with patch.dict(os.environ, {"WECHATBOT_PORTABLE_ROOT": str(self.base)}), \
             patch.object(self.launcher, "port_open", return_value=True), \
             patch.object(self.control, "_listener_pid", return_value=999), \
             patch.object(self.launcher, "_astrbot_lifecycle", return_value=helper), self.assertRaises(RuntimeError):
            self.launcher.launch_astrbot()
        helper.ready_listener.assert_called_once_with(999, spawned_pid=None)
        helper.finish_pending_stop.assert_called_once_with(timeout=8)
        self.assertEqual(self.spawned, [])

    def test_portable_launch_finishes_partial_stop_before_readiness_or_new_spawn(self):
        helper = types.SimpleNamespace(finish_pending_stop=Mock(side_effect=RuntimeError("owned child still exiting")))
        with patch.dict(os.environ, {"WECHATBOT_PORTABLE_ROOT": str(self.base)}), \
             patch.object(self.launcher, "_astrbot_lifecycle", return_value=helper), \
             patch.object(self.launcher, "port_open") as port:
            with self.assertRaisesRegex(RuntimeError, "owned child still exiting"):
                self.launcher.launch_astrbot()
        port.assert_not_called()
        self.assertEqual(self.spawned, [])

    def test_explicit_remote_panel_is_only_external_connection(self):
        url = "https://explicit.example.test/dashboard"
        (self.base / "astrbot_url.txt").write_text(url, encoding="utf-8")
        output = io.StringIO()
        with patch.object(self.launcher, "_astrbot_lifecycle") as helper, contextlib.redirect_stdout(output):
            self.assertEqual(self.launcher.launch_astrbot(), (None, url))
            self.assertTrue(self.launcher._target_ready("AstrBot", None, url))
        helper.assert_not_called()
        self.assertIn("不代表本包已启动", output.getvalue())

    def test_nonportable_existing_astrbot_requires_bounded_product_page(self):
        response = Mock(); response.status = 200
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        opener = Mock(); opener.open.return_value = response
        with patch.dict(os.environ, {}, clear=True), patch.object(self.launcher, "build_opener", return_value=opener), \
             patch.object(self.launcher, "port_open", return_value=True):
            response.read.return_value = b'<meta name="description" content="AstrBot Dashboard"><title>AstrBot Dashboard</title>'
            self.assertEqual(self.launcher.launch_astrbot()[0], None)
            response.read.assert_called_with(65537)
            response.read.return_value = b'<title>Other Service</title>'
            with self.assertRaises(RuntimeError): self.launcher.launch_astrbot()
        self.assertEqual(self.spawned, [])

    @unittest.skipUnless(os.name == "nt", "Windows venv redirector proof")
    def test_native_redirector_child_pid_native_image_and_alias_are_verified(self):
        self.spawn.stop(); self.running.stop()
        child = self.base / "main.py"
        marker = self.base / "finish-synthetic-child"
        child.write_text('''import json,os,pathlib,sys,time
sys.path.insert(0,sys.argv[1])
import startup_control as control
control.BASE=str(pathlib.Path(__file__).parent)
if not control.acquire_pid(str(pathlib.Path(control.BASE)/'bridge.pid')): raise SystemExit(3)
deadline=time.monotonic()+30
while not pathlib.Path(sys.argv[2]).exists() and time.monotonic()<deadline: time.sleep(.05)
''', encoding="utf-8")
        try:
            process = self.launcher._spawn([sys.executable, "-X", "utf8", "-B", str(child), str(BRIDGE), str(marker)], "startup_bridge.log")
            deadline = time.monotonic() + 15
            while not (self.base / "bridge.pid").exists() and time.monotonic() < deadline:
                time.sleep(.05)
            info = self.control.owned_running()
            self.assertIsNotNone(info)
            native = self.control._native_process(info["pid"])
            args = self.control._command_args(info["pid"])
            with patch.object(self.control, "_listener_pid", return_value=info["pid"]):
                self.assertTrue(self.launcher._bridge_listener_ready(process, info["panel_url"]))
            self.nativeproof = {"spawn_pid": process.pid, "actual_pid": info["pid"], "different_pids": process.pid != info["pid"], "record_has_native_image": self.control._canonical(info["executable"]) == self.control._canonical(native["executable"]), "argv_alias_is_native_image": self.control._canonical(args[0]) == self.control._canonical(native["executable"]), "readiness_verified": True}
            self.assertTrue(self.nativeproof["record_has_native_image"])
            self.assertTrue(self.nativeproof["argv_alias_is_native_image"])
            self.assertIn("_wechatbot_spawn_identity", process.__dict__)
        finally:
            marker.touch()
            if "process" in locals(): self.assertEqual(process.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
