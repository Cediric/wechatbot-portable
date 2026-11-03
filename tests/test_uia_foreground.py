"""Synthetic native HWND/queue checks; never inspect or activate a real desktop."""
import ctypes
import threading
import types
import unittest
from unittest.mock import Mock, patch

import test_uia_sender as fixtures


class NativeDesktop:
    def __init__(self):
        self.front = 200
        self.pids = {100: 10, 101: 10, 102: 10, 200: 20, 300: 30}
        self.threads = {100: 70, 101: 70, 102: 70, 200: 80, 300: 90}
        self.children = {101: 100}
        self.roots = {101: 100, 102: 100}  # An owned popup is still not IsChild.
        self.minimized = set()
        self.allowed = True
        self.on_attach = None
        self.on_owner = None
        self.classes = {100: "mmui::MainWindow"}
        self.EnumWindows = Mock(side_effect=self.enumerate)
        self.IsWindowVisible = Mock(return_value=True)
        self.GetClassNameW = Mock(side_effect=self.class_name)
        self.GetForegroundWindow = Mock(side_effect=lambda: self.front)
        self.GetWindowThreadProcessId = Mock(side_effect=self.owner)
        self.IsChild = Mock(side_effect=lambda parent, child: self.children.get(child) == parent)
        self.GetAncestor = Mock(side_effect=lambda hwnd, flag: self.roots.get(hwnd, hwnd))
        self.IsIconic = Mock(side_effect=lambda hwnd: hwnd in self.minimized)
        self.ShowWindow = Mock(side_effect=self.show)
        self.SetForegroundWindow = Mock(side_effect=self.activate)
        self.BringWindowToTop = Mock(return_value=1)
        self.AttachThreadInput = Mock(side_effect=self.attach)

    def enumerate(self, callback, unused):
        for hwnd in self.classes:
            callback(hwnd, 0)
        return 1

    def class_name(self, hwnd, target, unused):
        target.value = self.classes.get(hwnd, "")
        return len(target.value)

    def owner(self, hwnd, pid):
        if pid is not None:
            pid._obj.value = self.pids.get(hwnd, 0)
        if self.on_owner:
            self.on_owner(hwnd)
        return self.threads.get(hwnd, 0)

    def show(self, hwnd, state):
        if state == 9:
            self.minimized.discard(hwnd)
        return 0  # ShowWindow's result is previous visibility, not success.

    def activate(self, hwnd):
        if self.allowed:
            self.front = hwnd
        return int(self.allowed)

    def attach(self, left, right, enabled):
        return self.on_attach(left, right, enabled) if self.on_attach else 1


class NativeForegroundTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.UiaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.sender, self.auto, self.uia = self.fixture.sender, self.fixture.auto, self.fixture.uia
        self.sender._window = self.auto.main
        self.sender._cancelled = lambda: False
        self.native = NativeDesktop()
        self.auto.GetForegroundWindow = self.native.GetForegroundWindow  # Provider's native capability.
        self.auto.ControlFromHandle = lambda hwnd: self.auto.main if hwnd == 100 else None
        self.auto.main.SetActive = Mock(return_value=False)
        kernel = types.SimpleNamespace(GetCurrentThreadId=Mock(return_value=50))
        self.patch = patch.object(self.uia.ctypes, "windll", types.SimpleNamespace(user32=self.native, kernel32=kernel), create=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        platform = patch.object(self.uia.os, "name", "nt")
        platform.start()
        self.addCleanup(platform.stop)

    def assert_no_input(self):
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.auto.delivered, [])

    def test_native_main_foreground_overrides_qt_uia_hwnd_zero(self):
        self.native.front = 100
        avatar = fixtures.Control(self.auto, "Avatar", pid=10, handle=0)
        self.auto.GetForegroundControl = Mock(return_value=avatar)
        self.assertTrue(self.sender._foreground())
        self.auto.GetForegroundControl.assert_not_called()
        self.assertTrue(self.sender._activate())
        self.auto.main.SetActive.assert_not_called()
        self.assert_no_input()

    def test_real_provider_uses_native_hwnd_even_without_exported_helper(self):
        self.auto.__name__ = "uiautomation"
        del self.auto.GetForegroundWindow
        self.native.front = 100
        self.auto.GetForegroundControl = Mock(return_value=fixtures.Control(self.auto, "Qt leaf", pid=10, handle=0))
        self.assertTrue(self.sender._foreground())
        self.auto.GetForegroundControl.assert_not_called()
        self.assertTrue(self.sender._activate())
        self.assert_no_input()

    def test_other_native_process_is_not_overridden_by_stale_uia_main(self):
        self.auto.GetForegroundControl = Mock(return_value=self.auto.main)
        self.assertFalse(self.sender._foreground())
        self.auto.GetForegroundControl.assert_not_called()
        self.assertEqual(self.sender._activation_failure, "foreground_other_process")

    def test_same_pid_owned_popup_is_not_a_main_window_foreground(self):
        self.native.front = 102
        self.assertFalse(self.sender._foreground())
        self.assert_no_input()

    def test_only_true_same_pid_child_with_exact_target_root_is_allowed(self):
        self.native.front = 101
        self.assertTrue(self.sender._foreground())
        self.native.roots[101] = 300
        self.assertFalse(self.sender._foreground())
        self.native.roots[101] = 100
        self.native.pids[101] = 30
        self.assertFalse(self.sender._foreground())

    def test_explicit_verified_dialog_has_its_own_foreground_identity(self):
        self.native.front = 102
        dialog = fixtures.Control(self.auto, "Composer", "WindowControl", pid=10, handle=102)
        self.assertTrue(self.sender._foreground(dialog))
        self.assertFalse(self.sender._foreground(self.auto.main))

    def test_native_target_pid_or_live_uia_identity_drift_is_rejected(self):
        self.native.front = 100
        self.native.pids[100] = 20
        self.assertFalse(self.sender._foreground())
        self.native.pids[100] = 10
        self.native.on_owner = lambda hwnd: setattr(self.auto.main, "ProcessId", 20)
        self.assertFalse(self.sender._foreground())

    def test_native_failure_does_not_fall_back_to_successful_uia_foreground(self):
        self.auto.GetForegroundControl = Mock(return_value=self.auto.main)
        self.native.GetForegroundWindow.side_effect = RuntimeError("input desktop unavailable")
        self.assertFalse(self.sender._foreground())
        self.assertEqual(self.sender._activation_failure, "foreground_query_failed")
        self.auto.GetForegroundControl.assert_not_called()

    def test_minimized_target_is_restored_even_when_native_foreground_matches(self):
        self.native.front = 100
        self.native.minimized.add(100)
        self.assertFalse(self.sender._foreground())
        self.assertTrue(self.sender._activate())
        self.native.ShowWindow.assert_called_once_with(100, 9)
        self.assert_no_input()

    def test_maximized_or_normal_target_is_not_restored_or_resized(self):
        self.assertTrue(self.sender._activate())
        self.native.ShowWindow.assert_not_called()
        self.assertEqual(self.native.front, 100)
        self.assert_no_input()

    def test_attach_foreground_and_target_threads_then_detach_in_reverse_order(self):
        self.assertTrue(self.sender._activate())
        self.assertEqual([call.args for call in self.native.AttachThreadInput.call_args_list],
                         [(50, 70, True), (80, 70, True), (80, 70, False), (50, 70, False)])
        self.auto.main.SetActive.assert_called_once_with(waitTime=0)
        self.assertGreater(self.native.GetWindowThreadProcessId.call_count, 4)
        self.assert_no_input()

    def test_failed_attach_is_not_detached_and_final_foreground_is_still_required(self):
        self.native.on_attach = lambda left, right, enabled: 0
        self.native.allowed = False
        self.assertFalse(self.sender._activate())
        self.assertEqual([call.args for call in self.native.AttachThreadInput.call_args_list],
                         [(50, 70, True), (80, 70, True)])
        self.assertEqual(self.native.front, 200)
        self.assert_no_input()

    def test_native_activation_exception_releases_every_successful_attachment(self):
        self.native.SetForegroundWindow.side_effect = RuntimeError("native activation failure")
        self.assertFalse(self.sender._activate())
        self.assertEqual([call.args for call in self.native.AttachThreadInput.call_args_list][-2:],
                         [(80, 70, False), (50, 70, False)])
        self.assert_no_input()

    def test_cancellation_during_attach_releases_and_stops_the_next_attachment(self):
        cancelled = threading.Event()
        self.sender._cancelled = cancelled.is_set
        def attach(left, right, enabled):
            if enabled:
                cancelled.set()
            return 1
        self.native.on_attach = attach
        with self.assertRaises(fixtures.DesktopCancelled):
            self.sender._activate()
        self.assertEqual([call.args for call in self.native.AttachThreadInput.call_args_list],
                         [(50, 70, True), (50, 70, False)])
        self.native.SetForegroundWindow.assert_not_called()
        self.assert_no_input()

    def test_target_replacement_after_uia_activation_blocks_native_fallback(self):
        self.auto.main.SetActive.side_effect = lambda **kwargs: setattr(self.auto.main, "NativeWindowHandle", 102)
        self.assertFalse(self.sender._activate())
        self.native.AttachThreadInput.assert_not_called()
        self.native.SetForegroundWindow.assert_not_called()
        self.assert_no_input()

    def test_user_foreground_change_during_attachment_stops_native_activation(self):
        def attach(left, right, enabled):
            if enabled:
                self.native.front = 300
            return 1
        self.native.on_attach = attach
        self.assertFalse(self.sender._activate())
        self.assertEqual(self.sender._activation_failure, "foreground_changed_during_activation")
        self.native.SetForegroundWindow.assert_not_called()
        self.assertEqual([call.args for call in self.native.AttachThreadInput.call_args_list][-2:],
                         [(80, 70, False), (50, 70, False)])
        self.assert_no_input()

    def test_failed_detach_prevents_success_even_if_target_was_activated(self):
        self.native.on_attach = lambda left, right, enabled: int(enabled)
        self.assertFalse(self.sender._activate())
        self.assertEqual(self.sender._activation_failure, "input_detach_failed")
        self.assert_no_input()

    def test_no_input_desktop_foreground_is_an_accurate_failure(self):
        self.native.front = 0
        self.assertFalse(self.sender._activate())
        self.assertEqual(self.sender._activation_failure, "native_foreground_unavailable")
        self.native.AttachThreadInput.assert_not_called()
        self.native.SetForegroundWindow.assert_not_called()
        self.assert_no_input()

    def test_cancellation_inside_foreground_success_never_authorizes_activation(self):
        for source in ("native_getter", "predicate"):
            with self.subTest(source=source):
                cancelled = threading.Event()
                self.sender._cancelled = cancelled.is_set
                self.native.front = 100
                def native_getter():
                    cancelled.set()
                    return 100
                def predicate(*args):
                    cancelled.set()
                    return True
                context = (patch.object(self.native, "GetForegroundWindow", Mock(side_effect=native_getter))
                           if source == "native_getter" else patch.object(self.sender, "_foreground", side_effect=predicate))
                with context, self.assertRaises(fixtures.DesktopCancelled):
                    self.sender._activate()
                self.auto.main.SetActive.assert_not_called()
                self.native.SetForegroundWindow.assert_not_called()
                self.assert_no_input()

    def test_last_native_owner_query_cannot_authorize_a_changed_hwnd(self):
        self.native.front = 100
        calls = [0]
        def replace_during_last_owner(hwnd):
            calls[0] += 1
            if calls[0] == 4:
                self.auto.main.NativeWindowHandle = 102
        self.native.on_owner = replace_during_last_owner
        self.assertFalse(self.sender._activate())
        self.auto.main.SetActive.assert_not_called()
        self.native.SetForegroundWindow.assert_not_called()
        self.assert_no_input()

    def test_wait_success_is_rechecked_after_observed_cancel_or_target_drift(self):
        for change in ("cancel", "target"):
            with self.subTest(change=change):
                cancelled = threading.Event()
                self.sender._cancelled = cancelled.is_set
                self.native.front = 200
                self.auto.main.NativeWindowHandle = 100
                def waited(predicate, timeout, cancellation):
                    observed = predicate()
                    self.assertTrue(observed)
                    if change == "cancel":
                        cancelled.set()
                    else:
                        self.auto.main.NativeWindowHandle = 102
                    return observed
                with patch.object(self.uia, "wait_until", side_effect=waited):
                    if change == "cancel":
                        with self.assertRaises(fixtures.DesktopCancelled):
                            self.sender._activate()
                    else:
                        self.assertFalse(self.sender._activate())
                self.assert_no_input()


if __name__ == "__main__":
    unittest.main()
