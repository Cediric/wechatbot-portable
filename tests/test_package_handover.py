"""Portable version handover with synthetic listeners, never real services."""
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("test_package_handover_module", ROOT / "portable" / "package_handover.py")
handover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handover)


class HandoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.current = Path(self.temp.name) / "new"
        self.old = Path(self.temp.name) / "old"
        self.connections, self.processes, self.helpers, self.actions = {}, {}, {}, []
        for patcher in (
            patch.object(handover.psutil, "net_connections", side_effect=self.connections_snapshot),
            patch.object(handover.psutil, "Process", side_effect=lambda pid: self.processes[pid]),
            patch.object(handover, "_load_current", side_effect=lambda current, kind, previous: self.helpers[kind, str(previous)]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def connections_snapshot(self, **kwargs):
        self.assertEqual(kwargs, {"kind": "tcp"})
        return [types.SimpleNamespace(status=handover.psutil.CONN_LISTEN, laddr=("127.0.0.1", port), pid=pid)
                for port, pid in self.connections.items()]

    def add(self, kind, root=None, pid=None, port=None, child=False):
        root = root or self.old
        pid = pid or (101 if kind == "bridge" else 202)
        port = port or (8766 if kind == "bridge" else 6185)
        env = {"WECHATBOT_PORTABLE_ROOT": str(root), "ASTRBOT_ROOT": str(root / "portable/runtime/astrbot-instance")}
        command = [str(root / "portable/runtime/python/cpython/python.exe"), "-u", str(root / "wechat-weflow-bridge-ob11/main.py")] if kind == "bridge" else (
            [str(root / "portable/runtime/python/cpython/python.exe"), "-m", "astrbot.cli", "run"] if child else
            [str(root / "portable/runtime/astrbot-venv/Scripts/astrbot.exe"), "run"])
        self.processes[pid] = types.SimpleNamespace(environ=Mock(return_value=env), cmdline=Mock(return_value=command))
        self.connections[port] = pid
        record = {"pid": 303 if child else pid, "created": 1, "executable": command[0], "command": command,
                  "instance": env["ASTRBOT_ROOT"], "panel_url": "http://127.0.0.1:%s" % port}
        def stop(*args):
            self.actions.append((kind, root))
            for key, owner in list(self.connections.items()):
                if owner == pid:
                    del self.connections[key]
            return True
        helper = types.SimpleNamespace(owned_running=Mock(return_value=record), owned_listener=Mock(return_value=record),
                                       verified_record=Mock(return_value=record),
                                       ready_listener=Mock(return_value=True), stop_listener=Mock(side_effect=stop),
                                       stop_owned=Mock(side_effect=stop))
        self.helpers[kind, str(root)] = helper
        return helper

    def test_no_ports_or_no_listeners_does_nothing(self):
        self.assertEqual(handover.handover(self.current, []), 0)
        self.assertEqual(handover.handover(self.current, [6185, 8766]), 0)
        self.assertEqual(self.actions, [])

    def test_current_package_is_left_to_normal_launcher(self):
        bridge = self.add("bridge", self.current)
        astrbot = self.add("astrbot", self.current)
        self.assertEqual(handover.handover(self.current, [6185, 8766]), 0)
        bridge.owned_running.assert_not_called()
        bridge.owned_listener.assert_not_called()
        astrbot.verified_record.assert_not_called()
        self.assertEqual(self.actions, [])

    def test_older_bridge_stops_before_astrbot_and_multiple_ports_deduplicate(self):
        self.add("astrbot")
        self.connections[11229] = 202
        self.add("bridge")
        self.assertEqual(handover.handover(self.current, [6185, 11229, 8766, 8766]), 2)
        self.assertEqual(self.actions, [("bridge", self.old), ("astrbot", self.old)])

    def test_two_previous_packages_are_verified_independently(self):
        other = self.old.with_name("other old")
        self.add("astrbot", other)
        self.add("bridge")
        self.assertEqual(handover.handover(self.current, [6185, 8766]), 2)
        self.assertEqual(self.actions, [("bridge", self.old), ("astrbot", other)])

    def test_missing_bridge_record_does_not_block_a_verified_listener(self):
        bridge = self.add("bridge")
        self.add("astrbot")
        bridge.owned_running.return_value = None
        self.assertEqual(handover.handover(self.current, [6185, 8766]), 2)
        bridge.owned_running.assert_not_called()
        bridge.owned_listener.assert_called_with(8766)
        bridge.stop_listener.assert_called_once_with(bridge.owned_listener.return_value)
        self.assertEqual(self.actions, [("bridge", self.old), ("astrbot", self.old)])

    def test_unverified_bridge_listener_stops_nothing(self):
        bridge = self.add("bridge")
        self.add("astrbot")
        bridge.owned_listener.side_effect = ValueError("进程命令行未执行本项目绝对路径 main.py")
        with self.assertRaisesRegex(RuntimeError, "8766.*101.*实时进程核验失败") as raised:
            handover.handover(self.current, [6185, 8766])
        self.assertIn("未执行本项目绝对路径 main.py", str(raised.exception))
        self.assertEqual(self.actions, [])

    def test_missing_astrbot_record_stops_nothing(self):
        self.add("bridge")
        astrbot = self.add("astrbot")
        astrbot.verified_record.side_effect = RuntimeError("missing record")
        with self.assertRaisesRegex(RuntimeError, "6185.*202.*记录缺失"):
            handover.handover(self.current, [6185, 8766])
        self.assertEqual(self.actions, [])

    def test_foreign_program_is_reported_without_exposing_argv_or_env(self):
        self.add("bridge")
        self.connections[6185] = 999
        self.processes[999] = types.SimpleNamespace(environ=Mock(return_value={"secret": "private-key"}),
                                                   cmdline=Mock(return_value=["foreign.exe", "private-argument"]))
        with self.assertRaisesRegex(RuntimeError, "6185.*999.*无法核验") as raised:
            handover.handover(self.current, [8766, 6185])
        self.assertNotIn("private", str(raised.exception))
        self.assertEqual(self.actions, [])

    def test_foreign_listener_with_forged_package_env_is_not_stopped(self):
        astrbot = self.add("astrbot")
        astrbot.ready_listener.side_effect = RuntimeError("not official child")
        with self.assertRaisesRegex(RuntimeError, "归属不匹配"):
            handover.handover(self.current, [6185])
        self.assertEqual(self.actions, [])

    def test_failed_bridge_exit_never_stops_astrbot(self):
        bridge = self.add("bridge")
        astrbot = self.add("astrbot")
        bridge.stop_listener.side_effect = None
        bridge.stop_listener.return_value = False
        with self.assertRaisesRegex(RuntimeError, "未确认正常退出"):
            handover.handover(self.current, [6185, 8766])
        astrbot.stop_owned.assert_not_called()

    def test_managed_venv_child_uses_verified_record_parent(self):
        astrbot = self.add("astrbot", child=True)
        self.assertEqual(handover.handover(self.current, [6185]), 1)
        self.assertEqual(astrbot.ready_listener.call_args_list[0].args, (202,))
        astrbot.stop_owned.assert_called_once_with()

    def test_changed_listener_does_not_receive_exit_request(self):
        bridge = self.add("bridge")
        snapshots = [self.connections_snapshot(kind="tcp"),
                     [types.SimpleNamespace(status=handover.psutil.CONN_LISTEN, laddr=("127.0.0.1", 8766), pid=999)]]
        with patch.object(handover.psutil, "net_connections", side_effect=snapshots):
            with self.assertRaisesRegex(RuntimeError, "监听或创建身份已改变"):
                handover.handover(self.current, [8766])
        bridge.stop_listener.assert_not_called()

    def test_changed_process_identity_does_not_receive_exit_request(self):
        bridge = self.add("bridge")
        original = bridge.owned_listener.return_value
        bridge.owned_listener.side_effect = [original, {**original, "created": 2}]
        with self.assertRaisesRegex(RuntimeError, "监听或创建身份已改变"):
            handover.handover(self.current, [8766])
        bridge.stop_listener.assert_not_called()

    def test_absolute_bridge_entry_can_locate_old_package_without_env(self):
        bridge = self.add("bridge")
        self.processes[101].environ.return_value = {}
        self.assertEqual(handover.handover(self.current, [8766]), 1)
        bridge.stop_listener.assert_called_once_with(bridge.owned_listener.return_value)

    def test_python_c_text_cannot_locate_bridge(self):
        self.add("bridge")
        self.processes[101].environ.return_value = {}
        self.processes[101].cmdline.return_value = ["python.exe", "-c", str(self.old / "wechat-weflow-bridge-ob11/main.py")]
        with self.assertRaisesRegex(RuntimeError, "无法核验"):
            handover.handover(self.current, [8766])
        self.assertEqual(self.actions, [])

    def test_exit_confirmation_requires_released_ports(self):
        bridge = self.add("bridge")
        bridge.stop_listener.side_effect = None
        bridge.stop_listener.return_value = True
        with patch.object(handover.time, "monotonic", side_effect=[0, 5]), \
             patch.object(handover.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "8766.*101.*端口仍被占用"):
                handover.handover(self.current, [8766])
        sleep.assert_not_called()

    def test_confirmed_exit_allows_old_interpreter_to_release_port_shortly(self):
        astrbot = self.add("astrbot", child=True)
        astrbot.stop_owned.side_effect = None
        astrbot.stop_owned.return_value = True
        occupied = self.connections_snapshot(kind="tcp")
        with patch.object(handover.psutil, "net_connections", side_effect=[occupied, occupied, occupied, []]), \
             patch.object(handover.time, "monotonic", side_effect=[0, .1]), \
             patch.object(handover.time, "sleep") as sleep:
            self.assertEqual(handover.handover(self.current, [6185]), 1)
        sleep.assert_called_once_with(.1)
        astrbot.stop_owned.assert_called_once_with()

    def test_replacement_listener_fails_immediately_without_another_exit_request(self):
        bridge = self.add("bridge")
        bridge.stop_listener.side_effect = None
        bridge.stop_listener.return_value = True
        occupied = self.connections_snapshot(kind="tcp")
        replacement = [types.SimpleNamespace(status=handover.psutil.CONN_LISTEN, laddr=("127.0.0.1", 8766), pid=999)]
        with patch.object(handover.psutil, "net_connections", side_effect=[occupied, occupied, replacement]), \
             patch.object(handover.time, "monotonic", return_value=0), \
             patch.object(handover.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "8766.*999.*已换成其他进程"):
                handover.handover(self.current, [8766])
        sleep.assert_not_called()
        bridge.stop_listener.assert_called_once_with(bridge.owned_listener.return_value)

    def test_listener_read_failure_after_exit_does_not_send_another_request(self):
        bridge = self.add("bridge")
        occupied = self.connections_snapshot(kind="tcp")
        with patch.object(handover.psutil, "net_connections", side_effect=[occupied, occupied, handover.psutil.AccessDenied()]), \
             patch.object(handover.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "无法读取监听进程"):
                handover.handover(self.current, [8766])
        sleep.assert_not_called()
        bridge.stop_listener.assert_called_once_with(bridge.owned_listener.return_value)

    def real_child_helper(self, root_created=1):
        mocked = self.add("astrbot", child=True)
        # Load the actual current readiness code; only process queries and the
        # final normal-exit action remain synthetic.
        source = ROOT / "portable/astrbot_lifecycle.py"
        module_spec = importlib.util.spec_from_file_location("handover_real_lifecycle", source)
        helper = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(helper)
        helper.ROOT = self.old
        helper.RUNTIME = self.old / "portable/runtime"
        helper.INSTANCE = helper.RUNTIME / "astrbot-instance"
        helper.RECORD = helper.RUNTIME / "astrbot-process.json"
        helper.RUNTIME.mkdir(parents=True)
        cli = str(helper.RUNTIME / "astrbot-venv/Scripts/astrbot.exe")
        managed = helper.RUNTIME / "python/cpython/python.exe"
        managed.parent.mkdir(parents=True)
        managed.touch()
        instance = str(helper.INSTANCE)
        record = {"pid": 303, "created": 1, "executable": cli, "command": [cli, "run"], "instance": instance, "cwd": instance}
        helper.RECORD.write_text(json.dumps(record), encoding="utf-8")
        child = self.processes[202]
        child.pid = 202
        child.create_time = lambda: 2
        child.exe = lambda: str(managed)
        child.cwd = lambda: instance
        self.processes[303] = types.SimpleNamespace(pid=303, create_time=lambda: root_created, exe=lambda: cli,
            cmdline=lambda: [cli, "run"], environ=lambda: {"ASTRBOT_ROOT": instance}, cwd=lambda: instance,
            children=lambda recursive=True: [child])
        helper.stop_owned = mocked.stop_owned
        self.helpers["astrbot", str(self.old)] = helper
        return helper

    def test_actual_current_verifier_accepts_old_managed_child_without_old_code(self):
        helper = self.real_child_helper()
        self.assertEqual(handover.handover(self.current, [6185]), 1)
        helper.stop_owned.assert_called_once_with()

    def test_actual_current_verifier_rejects_reused_old_root_pid(self):
        helper = self.real_child_helper(root_created=9)
        with self.assertRaisesRegex(RuntimeError, "归属不匹配"):
            handover.handover(self.current, [6185])
        helper.stop_owned.assert_not_called()

    def test_permission_failure_reads_no_config_or_sends_exit(self):
        with patch.object(handover.psutil, "net_connections", side_effect=handover.psutil.AccessDenied()):
            with self.assertRaisesRegex(RuntimeError, "无法读取监听进程"):
                handover.handover(self.current, [6185, 8766])
        self.assertEqual(self.actions, [])


class CurrentCodeLoadingTests(unittest.TestCase):
    def test_helpers_use_current_source_but_previous_package_data_paths(self):
        previous = ROOT.with_name("previous-package")
        for kind, source in (("bridge", ROOT / "wechat-weflow-bridge-ob11/startup_control.py"),
                             ("astrbot", ROOT / "portable/astrbot_lifecycle.py")):
            helper = handover._load_current(ROOT, kind, previous)
            self.assertEqual(Path(helper.__file__).resolve(), source.resolve())
            if kind == "bridge":
                self.assertEqual(helper.BASE, str(previous / "wechat-weflow-bridge-ob11"))
            else:
                self.assertEqual(helper.ROOT, previous)
                self.assertEqual(helper.INSTANCE, previous / "portable/runtime/astrbot-instance")
                self.assertEqual(helper.RECORD, previous / "portable/runtime/astrbot-process.json")


if __name__ == "__main__":
    unittest.main()
