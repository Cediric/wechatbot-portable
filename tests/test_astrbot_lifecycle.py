"""AstrBot ownership and graceful shutdown checks with synthetic processes."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "portable" / "astrbot_lifecycle.py"


class ReadinessOwnershipTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("isolated_ready_lifecycle", SOURCE)
        self.life = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.life)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.instance = self.runtime / "astrbot-instance"; self.instance.mkdir()
        self.scripts = self.runtime / "astrbot-venv" / "Scripts"; self.scripts.mkdir(parents=True)
        self.cli = self.scripts / "astrbot.exe"; self.cli.touch()
        self.python = self.scripts / "python.exe"; self.python.touch()
        self.managed = self.runtime / "python" / "cpython-3.12.12" / "python.exe"
        self.managed.parent.mkdir(parents=True); self.managed.touch()
        self.life.RUNTIME, self.life.INSTANCE = self.runtime, self.instance
        self.life.RECORD = self.runtime / "astrbot-process.json"
        self.root = types.SimpleNamespace(pid=101, children=Mock(return_value=[]),
                                         create_time=Mock(return_value=1.0), is_running=Mock(return_value=False), terminate=Mock())
        self.child = types.SimpleNamespace(pid=202, create_time=Mock(return_value=2.0),
                                          is_running=Mock(return_value=False), terminate=Mock())
        self.root.terminate.side_effect = lambda: setattr(self.root.is_running, 'return_value', False)
        self.child.terminate.side_effect = lambda: setattr(self.child.is_running, 'return_value', False)
        self.value = {"pid": 101, "created": 1.0, "executable": str(self.cli), "command": [str(self.cli), "run", "--port", "6185"], "instance": str(self.instance), "cwd": str(self.instance)}
        self.values = {101: self.value, 202: {"pid": 202, "created": 2.0, "executable": str(self.managed), "command": [str(self.managed), "-m", "astrbot.cli", "run"], "instance": str(self.instance), "cwd": "unused"}}
        self.life.RECORD.write_text(json.dumps(self.value), encoding="utf-8")
        process = patch.object(self.life.psutil, "Process", side_effect=lambda pid: {101: self.root, 202: self.child}[pid]); process.start(); self.addCleanup(process.stop)
        fingerprint = patch.object(self.life, "fingerprint", side_effect=lambda p: dict(self.values[p.pid])); fingerprint.start(); self.addCleanup(fingerprint.stop)
        self.before = self.life.RECORD.read_bytes()

    def assert_rejected(self, listener=101, spawned=101):
        with self.assertRaises(RuntimeError):
            self.life.ready_listener(listener, spawned)
        self.assertEqual(self.life.RECORD.read_bytes(), self.before)

    def test_official_cli_root_listener_and_readonly_record_pass(self):
        self.assertTrue(self.life.ready_listener(101, 101))
        self.assertEqual(self.life.verified_record(), self.value)
        self.assertEqual(self.life.RECORD.read_bytes(), self.before)

    def test_verified_cli_child_with_same_instance_and_managed_python_passes(self):
        self.root.children.return_value = [self.child]
        self.assertTrue(self.life.ready_listener(202, 101))
        self.assertEqual(self.life.RECORD.read_bytes(), self.before)

    def test_distlib_child_cli_argument_and_exact_sdk_entry_pass(self):
        self.root.children.return_value = [self.child]
        for command in ([str(self.managed), str(self.cli), "run"],
                        [str(self.managed), "-X", "utf8", "-B", str(self.runtime / "astrbot-venv/Lib/site-packages/astrbot/cli/__main__.py"), "run"]):
            self.values[202]["command"] = command
            self.assertTrue(self.life.ready_listener(202, 101))

    def test_foreign_listener_and_old_launch_record_are_rejected(self):
        self.assert_rejected(listener=999)
        self.assert_rejected(spawned=303)

    def test_other_package_child_and_unknown_sdk_entry_are_rejected(self):
        self.root.children.return_value = [self.child]
        for key, replacement in (("instance", str(self.runtime / "other-package/instance")),
                                 ("executable", str(self.runtime / "other-package/python.exe")),
                                 ("command", [str(self.managed), "-m", "foreign_product", "run"]),
                                 ("command", [str(self.managed), "-c", str(self.cli), "run"]),
                                 ("created", .5)):
            original = self.values[202][key]; self.values[202][key] = replacement
            self.assert_rejected(listener=202)
            self.values[202][key] = original

    def test_reused_root_pid_creation_or_argv_change_rejected_without_signal(self):
        for key, replacement in (("created", 9.0), ("command", [str(self.cli), "init"]), ("instance", str(self.runtime / "other"))):
            original = self.value[key]; self.value[key] = replacement
            self.assert_rejected()
            self.value[key] = original

    def test_access_denied_or_dead_root_preserves_record(self):
        with patch.object(self.life.psutil, "Process", side_effect=self.life.psutil.AccessDenied(101)):
            self.assert_rejected()
        with patch.object(self.life.psutil, "Process", side_effect=self.life.psutil.NoSuchProcess(101)):
            self.assertIsNone(self.life.verified_record())
            self.assert_rejected()

    def test_child_removed_from_tree_during_readiness_is_rejected(self):
        self.root.children.side_effect = [[self.child], []]
        self.assert_rejected(listener=202)

    def test_child_creation_changes_during_readiness_is_rejected(self):
        self.root.children.return_value = [self.child]
        calls = [0]
        def snapshots(p):
            value = dict(self.values[p.pid])
            if p.pid == 202:
                calls[0] += 1
                if calls[0] > 1: value["created"] = 3.0
            return value
        with patch.object(self.life, "fingerprint", side_effect=snapshots):
            self.assert_rejected(listener=202)

    def test_missing_or_malformed_record_cannot_authorize_listener(self):
        self.life.RECORD.unlink()
        with self.assertRaises(RuntimeError): self.life.ready_listener(101, 101)
        self.assertFalse(self.life.RECORD.exists())
        for payload in ("not json", json.dumps({"pid": True}), "x" * 17000):
            self.life.RECORD.write_text(payload, encoding="utf-8"); self.before = self.life.RECORD.read_bytes()
            self.assert_rejected()

    def test_record_access_unknown_never_means_no_owned_process(self):
        with patch.object(type(self.life.RECORD), "stat", side_effect=PermissionError("synthetic record denied")):
            self.assert_rejected()

    def test_missing_record_reports_unknown_port_owner_without_creating_record(self):
        self.life.RECORD.unlink()
        self.assertIsNone(self.life.verified_record())
        with self.assertRaisesRegex(RuntimeError, "端口已有监听.*当前包没有可核验的进程记录.*不会接管"):
            self.life.ready_listener(999)
        self.assertFalse(self.life.RECORD.exists())

    def test_exited_record_root_has_distinct_reason_and_keeps_evidence(self):
        with patch.object(self.life.psutil, "Process", side_effect=self.life.psutil.NoSuchProcess(101)):
            self.assertIsNone(self.life.verified_record())
            with self.assertRaisesRegex(RuntimeError, "根进程已经退出.*已保留记录"):
                self.life.ready_listener(999)
        self.assertEqual(self.life.RECORD.read_bytes(), self.before)

    def test_other_launch_record_has_distinct_reason_without_exposing_identity(self):
        with self.assertRaisesRegex(RuntimeError, "本轮启动进程不匹配.*已保留记录") as raised:
            self.life.ready_listener(101, spawned_pid=303)
        self.assertNotIn(str(self.instance), str(raised.exception))
        self.assertNotIn(str(self.cli), str(raised.exception))
        self.assertEqual(self.life.RECORD.read_bytes(), self.before)

    def test_graceful_stop_accepts_official_module_child_and_keeps_console_checks(self):
        self.root.children.return_value = [self.child]
        with patch.object(self.life.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertTrue(self.life.stop_owned())
        command = run.call_args.args[0]
        self.assertEqual(Path(command[3]).name, "console_control.py")
        records = json.loads(command[4])
        self.assertEqual([item["pid"] for item in records], [101, 202])
        self.assertEqual(records[1]["command"], self.values[202]["command"])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertEqual(run.call_args.kwargs["stdin"], self.life.subprocess.DEVNULL)
        self.assertFalse(self.life.RECORD.exists())

    def test_graceful_stop_accepts_venv_distlib_and_official_sdk_children(self):
        self.root.children.return_value = [self.child]
        for command in ([str(self.python), str(self.cli), "run"],
                        [str(self.managed), str(self.cli), "run", "--port", "6185"],
                        [str(self.managed), "-m", "astrbot.cli.__main__", "run"],
                        [str(self.managed), "-X", "utf8", "-B", str(self.runtime / "astrbot-venv/Lib/site-packages/astrbot/cli/__main__.py"), "run"]):
            with self.subTest(command=command):
                self.values[202].update(executable=command[0], command=command)
                self.life.RECORD.write_bytes(self.before)
                with patch.object(self.life.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
                    self.assertTrue(self.life.stop_owned())
                run.assert_called_once()
                self.assertFalse(self.life.RECORD.exists())

    def test_registration_and_stop_share_strict_python_cli_root_entrypoint(self):
        self.value.update(executable=str(self.managed), command=[str(self.managed), str(self.cli), "run", "--port", "6185"])
        self.life.record_process(101)
        self.assertEqual(json.loads(self.life.RECORD.read_bytes()), self.value)
        with patch.object(self.life.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertTrue(self.life.stop_owned())
        run.assert_called_once()
        for command in ([str(self.managed), "-m", "astrbot.cli", "run"],
                        [str(self.managed), "-c", "arbitrary code", str(self.cli), "run"],
                        [str(self.cli), "init"]):
            self.value["command"] = command
            with self.subTest(command=command), self.assertRaises(RuntimeError):
                self.life.record_process(101)
        self.assertFalse(self.life.RECORD.exists())

    def test_stop_rejects_foreign_child_or_changed_root_without_signal(self):
        self.root.children.return_value = [self.child]
        for key, replacement in (("instance", str(self.runtime / "other-package/instance")),
                                 ("executable", str(self.runtime / "foreign-python.exe")),
                                 ("command", [str(self.managed), "-m", "foreign_product", "run"]),
                                 ("command", [str(self.managed), "-c", str(self.cli), "run"])):
            original = self.values[202][key]
            self.values[202][key] = replacement
            with self.subTest(key=key, replacement=replacement), patch.object(self.life.subprocess, "run") as run:
                with self.assertRaises(RuntimeError):
                    self.life.stop_owned()
                run.assert_not_called()
                self.assertEqual(self.life.RECORD.read_bytes(), self.before)
            self.values[202][key] = original
        self.value["created"] = 9.0
        with patch.object(self.life.subprocess, "run") as run, self.assertRaises(RuntimeError):
            self.life.stop_owned()
        run.assert_not_called()
        self.assertEqual(self.life.RECORD.read_bytes(), self.before)

    def test_stop_preserves_concurrently_replaced_process_record(self):
        replacement = b'{"pid":303,"created":9.0}'
        def signal(*args, **kwargs):
            self.life.RECORD.write_bytes(replacement)
            return types.SimpleNamespace(returncode=0)
        with patch.object(self.life.subprocess, "run", side_effect=signal):
            self.assertTrue(self.life.stop_owned())
        self.assertEqual(self.life.RECORD.read_bytes(), replacement)

    def test_root_exit_alone_does_not_complete_stop_while_child_is_running(self):
        self.root.children.return_value = [self.child]
        self.child.is_running.side_effect = [True, False]
        with patch.object(self.life.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)), \
             patch.object(self.life.time, 'sleep') as sleep:
            self.assertTrue(self.life.stop_owned(timeout=1))
        sleep.assert_called_once_with(.15)
        self.child.terminate.assert_not_called()
        self.assertFalse(self.life.RECORD.exists())

    def test_grace_period_ends_only_verified_survivors_and_waits_for_exit(self):
        self.root.children.return_value = [self.child]
        self.root.is_running.return_value = True
        self.child.is_running.return_value = True
        with patch.object(self.life.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)):
            self.assertTrue(self.life.stop_owned(timeout=0))
        self.root.terminate.assert_called_once()
        self.child.terminate.assert_called_once()
        self.assertFalse(self.life.RECORD.exists())

    def test_failed_console_signal_can_finish_owned_process_without_name_scan(self):
        self.root.is_running.return_value = True
        with patch.object(self.life.subprocess, 'run', return_value=types.SimpleNamespace(returncode=1)), \
             patch.object(self.life.psutil, 'process_iter') as scan:
            self.assertTrue(self.life.stop_owned())
        self.root.terminate.assert_called_once()
        scan.assert_not_called()

    def test_root_gone_retry_uses_saved_child_identity_without_resending_console_signal(self):
        self.root.children.return_value = [self.child]
        self.child.is_running.return_value = True
        self.child.terminate.side_effect = self.life.psutil.AccessDenied(202)
        with patch.object(self.life.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)), \
             self.assertRaises(self.life.psutil.AccessDenied):
            self.life.stop_owned(timeout=0)
        pending = json.loads(self.life.RECORD.read_bytes())
        self.assertEqual([item['pid'] for item in pending['shutdown_members']], [101, 202])
        self.child.terminate.side_effect = lambda: setattr(self.child.is_running, 'return_value', False)
        def root_gone(pid):
            if pid == 101:
                raise self.life.psutil.NoSuchProcess(pid)
            self.assertEqual(pid, 202)
            return self.child
        with patch.object(self.life.psutil, 'Process', side_effect=root_gone), \
             patch.object(self.life.subprocess, 'run') as signal:
            self.assertTrue(self.life.stop_owned(timeout=0))
        signal.assert_not_called()
        self.assertFalse(self.life.RECORD.exists())

    def test_reused_child_pid_is_not_terminated(self):
        self.root.children.return_value = [self.child]
        def signal(*args, **kwargs):
            self.child.is_running.return_value = True
            self.child.create_time.return_value = 9.0
            return types.SimpleNamespace(returncode=0)
        with patch.object(self.life.subprocess, 'run', side_effect=signal):
            self.assertTrue(self.life.stop_owned(timeout=0))
        self.child.terminate.assert_not_called()
        self.assertFalse(self.life.RECORD.exists())

    def test_surviving_child_with_changed_entrypoint_is_preserved(self):
        self.root.children.return_value = [self.child]
        self.child.is_running.return_value = True
        def signal(*args, **kwargs):
            self.values[202]['command'] = [str(self.managed), '-m', 'other_application']
            return types.SimpleNamespace(returncode=0)
        with patch.object(self.life.subprocess, 'run', side_effect=signal), self.assertRaises(RuntimeError):
            self.life.stop_owned(timeout=0)
        self.child.terminate.assert_not_called()
        self.assertTrue(self.life.RECORD.exists())

    def test_termination_not_confirmed_keeps_retriable_member_record(self):
        self.root.children.return_value = [self.child]
        self.child.is_running.return_value = True
        self.child.terminate.side_effect = None
        with patch.object(self.life.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)), \
             patch.object(self.life.time, 'monotonic', side_effect=[0, 0, 1, 5]), \
             self.assertRaisesRegex(RuntimeError, '已保留停止记录'):
            self.life.stop_owned(timeout=0)
        self.assertIn('shutdown_members', json.loads(self.life.RECORD.read_bytes()))

    def test_missing_record_is_noop_and_does_not_scan_external_astrbot(self):
        self.life.RECORD.unlink()
        with patch.object(self.life.psutil, 'Process') as process, \
             patch.object(self.life.psutil, 'process_iter') as scan, \
             patch.object(self.life.subprocess, 'run') as signal:
            self.assertTrue(self.life.stop_owned())
        process.assert_not_called()
        scan.assert_not_called()
        signal.assert_not_called()

    def test_launch_cleanup_leaves_normal_instance_and_missing_record_alone(self):
        with patch.object(self.life, 'stop_owned') as stop:
            self.assertTrue(self.life.finish_pending_stop())
            self.assertEqual(self.life.RECORD.read_bytes(), self.before)
            self.life.RECORD.unlink()
            self.assertTrue(self.life.finish_pending_stop())
        stop.assert_not_called()

    def test_launch_cleanup_finishes_partial_stop_before_new_process_can_start(self):
        self.life.RECORD.write_text(json.dumps(dict(self.value, shutdown_members=[self.value, self.values[202]])), encoding='utf-8')
        self.child.is_running.return_value = True
        def root_gone(pid):
            if pid == 101:
                raise self.life.psutil.NoSuchProcess(pid)
            return self.child
        with patch.object(self.life.psutil, 'Process', side_effect=root_gone), \
             patch.object(self.life.subprocess, 'run') as signal:
            self.assertTrue(self.life.finish_pending_stop(timeout=8))
        signal.assert_not_called()
        self.child.terminate.assert_called_once()
        self.assertFalse(self.life.RECORD.exists())

    @unittest.skipUnless(os.name == "nt", "Windows read-only file attributes")
    def test_verified_exit_removes_readonly_unchanged_record(self):
        os.chmod(self.life.RECORD, self.life.stat.S_IREAD)
        self.addCleanup(lambda: os.chmod(self.life.RECORD, self.life.stat.S_IWRITE) if self.life.RECORD.exists() else None)
        with patch.object(self.life.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)):
            self.assertTrue(self.life.stop_owned())
        self.assertFalse(self.life.RECORD.exists())

    @unittest.skipUnless(os.name == "nt", "Windows read-only file attributes")
    def test_proven_dead_pid_removes_readonly_unchanged_record_without_signal(self):
        os.chmod(self.life.RECORD, self.life.stat.S_IREAD)
        self.addCleanup(lambda: os.chmod(self.life.RECORD, self.life.stat.S_IWRITE) if self.life.RECORD.exists() else None)
        with patch.object(self.life.psutil, "Process", side_effect=self.life.psutil.NoSuchProcess(101)), \
             patch.object(self.life.subprocess, "run") as run:
            self.assertTrue(self.life.stop_owned())
        run.assert_not_called()
        self.assertFalse(self.life.RECORD.exists())


if __name__ == "__main__":
    unittest.main()
