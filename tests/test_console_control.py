"""The console signal is mocked; no actual desktop process receives Ctrl+C."""
import copy
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "portable" / "console_control.py"


class ConsoleControlTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("console_control_test", SOURCE)
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        self.records = [
            {"pid": 101, "created": 100.0, "executable": "C:/test/astrbot.exe", "command": ["C:/test/astrbot.exe", "run"], "instance": "C:/test/instance"},
            {"pid": 202, "created": 101.0, "executable": "C:/test/python.exe", "command": ["C:/test/python.exe", "C:/test/astrbot.exe", "run"], "instance": "C:/test/instance"},
        ]
        self.state = {r["pid"]: copy.deepcopy(r) for r in self.records}
        self.parents = {202: 101}
        self.members = [101, 202, os.getpid()]
        self.handles = {}
        self.closed = []
        self.next_handle = 500
        self.after_attach = lambda: None

        def native_open(access, inherit, pid):
            self.assertEqual(access, 0x1000 | 0x00100000)
            if pid not in self.state:
                return 0
            self.next_handle += 1
            self.handles[self.next_handle] = copy.deepcopy(self.state[pid])
            return self.next_handle

        def native_wait(handle, timeout):
            old = self.handles[handle]
            new = self.state.get(old["pid"])
            return 258 if new and new["created"] == old["created"] else 0

        def native_exe(handle, flags, buffer, length):
            buffer.value = self.handles[handle]["executable"]
            return 1

        def native_times(handle, created, exited, kernel, user):
            value = int((self.handles[handle]["created"] + 11644473600) * 10000000)
            ctypes.cast(created, ctypes.POINTER(ctypes.c_uint64)).contents.value = value
            return 1

        def native_close(handle):
            self.closed.append(handle)
            del self.handles[handle]
            return 1

        def native_attach(pid):
            self.after_attach()
            return 1

        def native_members(buffer, size):
            for index, pid in enumerate(self.members[:size]):
                buffer[index] = pid
            return len(self.members)

        self.native = types.SimpleNamespace(
            OpenProcess=Mock(side_effect=native_open),
            GetProcessId=Mock(side_effect=lambda handle: self.handles[handle]["pid"]),
            QueryFullProcessImageNameW=Mock(side_effect=native_exe),
            GetProcessTimes=Mock(side_effect=native_times),
            WaitForSingleObject=Mock(side_effect=native_wait),
            CloseHandle=Mock(side_effect=native_close),
            AttachConsole=Mock(side_effect=native_attach),
            FreeConsole=Mock(return_value=1),
            GetConsoleProcessList=Mock(side_effect=native_members),
            SetConsoleCtrlHandler=Mock(return_value=1),
            GenerateConsoleCtrlEvent=Mock(return_value=1),
        )

        owner = self
        class Process:
            def __init__(self, pid):
                self.pid = pid
            def create_time(self):
                return owner.state[self.pid]["created"]
            def exe(self):
                return owner.state[self.pid]["executable"]
            def cmdline(self):
                return owner.state[self.pid]["command"]
            def environ(self):
                return {"ASTRBOT_ROOT": owner.state[self.pid]["instance"]}
            def ppid(self):
                return owner.parents.get(self.pid, 0)
            def children(self, recursive=False):
                return [Process(pid) for pid in owner.parents]
        self.process_type = Process

    def invoke(self):
        with patch.object(self.helper.os, "name", "nt"), \
             patch.object(self.helper.ctypes, "WinDLL", return_value=self.native), \
             patch.object(self.helper.psutil, "Process", side_effect=self.process_type), \
             patch.object(self.helper.sys, "argv", [str(SOURCE), json.dumps(self.records)]):
            result = self.helper.main()
        self.assertEqual(self.handles, {})
        return result

    def assert_no_signal(self):
        self.native.GenerateConsoleCtrlEvent.assert_not_called()

    def test_pid_reuse_after_attach_never_signals_the_new_process(self):
        self.after_attach = lambda: self.state[101].update(created=999.0)
        self.assertEqual(self.invoke(), 1)
        self.assert_no_signal()

    def test_verified_dedicated_tree_signals_once_while_original_handles_are_held(self):
        def signal(event, group):
            self.assertEqual((event, group), (0, 0))
            self.assertEqual({value["pid"] for value in self.handles.values()}, {101, 202})
            return 1
        self.native.GenerateConsoleCtrlEvent.side_effect = signal
        self.assertEqual(self.invoke(), 0)
        self.native.GenerateConsoleCtrlEvent.assert_called_once_with(0, 0)
        self.assertEqual(len(self.closed), 2)
        self.assertEqual(self.native.FreeConsole.call_count, 2)

    def test_child_identity_changes_after_attach_are_rejected(self):
        for name, value in (("created", 1000.0), ("executable", "C:/foreign/python.exe"),
                            ("command", ["C:/test/python.exe", "-c", "foreign"]),
                            ("instance", "C:/other/instance")):
            with self.subTest(field=name):
                self.state = {r["pid"]: copy.deepcopy(r) for r in self.records}
                self.after_attach = lambda: self.state[202].update({name: value})
                self.assertEqual(self.invoke(), 1)
                self.assert_no_signal()

    def test_unrecorded_child_after_attach_is_rejected_even_outside_console(self):
        def changed():
            self.parents[303] = 101
            self.state[303] = dict(self.records[1], pid=303, created=102.0)
        self.after_attach = changed
        self.assertEqual(self.invoke(), 1)
        self.assert_no_signal()

    def test_child_parent_chain_change_after_attach_is_rejected(self):
        self.after_attach = lambda: self.parents.update({202: 909})
        self.assertEqual(self.invoke(), 1)
        self.assert_no_signal()

    def test_conhost_is_checked_in_tree_but_receives_no_application_signal(self):
        self.parents[303] = 101
        self.state[303] = dict(self.records[1], pid=303, created=102.0,
            executable=os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "System32", "conhost.exe"))
        self.assertEqual(self.invoke(), 0)
        self.native.GenerateConsoleCtrlEvent.assert_called_once_with(0, 0)
        self.assertEqual(len(self.closed), 2)

    def test_foreign_member_joining_after_first_console_query_is_rejected(self):
        original = self.native.GetConsoleProcessList.side_effect
        calls = 0
        def query(buffer, count):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.members.append(909)
            return original(buffer, count)
        self.native.GetConsoleProcessList.side_effect = query
        self.assertEqual(self.invoke(), 1)
        self.assert_no_signal()

    def test_missing_recorded_console_member_is_rejected(self):
        self.members.remove(202)
        self.assertEqual(self.invoke(), 1)
        self.assert_no_signal()

    def test_identity_change_while_installing_handler_is_rejected(self):
        def handler(callback, enabled):
            self.state[202]["instance"] = "C:/other/instance"
            return 1
        self.native.SetConsoleCtrlHandler.side_effect = handler
        self.assertEqual(self.invoke(), 1)
        self.assert_no_signal()

    def test_native_access_failure_closes_already_held_handles_without_attaching(self):
        original = self.native.OpenProcess.side_effect
        self.native.OpenProcess.side_effect = lambda access, inherited, pid: 0 if pid == 202 else original(access, inherited, pid)
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(len(self.closed), 1)
        self.native.AttachConsole.assert_not_called()
        self.assert_no_signal()

    def test_cannot_ignore_ctrl_c_never_sends_it(self):
        self.native.SetConsoleCtrlHandler.return_value = 0
        self.assertEqual(self.invoke(), 1)
        self.assert_no_signal()

    def test_duplicate_pid_or_malformed_snapshot_is_rejected_before_native_work(self):
        for records in ([], {}, [dict(self.records[0], pid=True)], [self.records[0], self.records[0]],
                        [dict(self.records[0], created=float("nan"))], [dict(self.records[0], command="text")]):
            with self.subTest(records=records):
                self.records = records
                self.assertEqual(self.invoke(), 1)
                self.native.OpenProcess.assert_not_called()
                self.native.AttachConsole.assert_not_called()
                self.assert_no_signal()


if __name__ == "__main__":
    unittest.main()
