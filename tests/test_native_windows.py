"""Native window metadata fixtures; these never enumerate the real desktop."""
import ctypes
import types
import unittest
from unittest.mock import Mock, patch

from desktop_coordinator import native_top_windows
import test_uia_sender as fixtures


class NativeWindowTests(unittest.TestCase):
    def test_injected_provider_does_not_read_native_desktop(self):
        self.assertIsNone(native_top_windows(types.SimpleNamespace()))

    def test_native_pid_filter_happens_before_class_or_uia_access(self):
        rows = {100: (10, "mmui::MainWindow", True),
                200: (20, "unrelated_slow_provider", True),
                300: (10, "mmui::ChatVoiceRecordView", True),
                400: (10, "hidden", False)}
        def enumerate_(callback, unused):
            for hwnd in rows:
                callback(hwnd, 0)
            return 1
        def owner(hwnd, target):
            ctypes.cast(target, ctypes.POINTER(ctypes.c_uint32)).contents.value = rows[hwnd][0]
            return 1
        def klass(hwnd, target, unused):
            target.value = rows[hwnd][1]
            return len(target.value)
        user = types.SimpleNamespace(EnumWindows=Mock(side_effect=enumerate_),
            GetWindowThreadProcessId=Mock(side_effect=owner),
            GetClassNameW=Mock(side_effect=klass),
            IsWindowVisible=Mock(side_effect=lambda hwnd: rows[hwnd][2]))
        self.assertEqual(native_top_windows(types.SimpleNamespace(), pid=10, user=user), [
            {"hwnd": 100, "pid": 10, "class": "mmui::MainWindow"},
            {"hwnd": 300, "pid": 10, "class": "mmui::ChatVoiceRecordView"}])
        self.assertEqual([call.args[0] for call in user.GetClassNameW.call_args_list], [100, 300])


class FilteredProviderTests(unittest.TestCase):
    setUp = fixtures.UiaTests.setUp
    tearDown = fixtures.UiaTests.tearDown

    def test_first_chat_lookup_filters_native_foreign_windows_before_uia(self):
        self.auto.GetRootControl = Mock(side_effect=AssertionError("no desktop UIA enumeration"))
        self.auto.ControlFromHandle = Mock(side_effect=lambda hwnd: self.auto.main if hwnd == 100 else self.fail("foreign UIA provider"))
        rows = [{"hwnd": 200, "pid": 20, "class": "AnyNativeWrapperClass"},
                {"hwnd": 300, "pid": 20, "class": "Chrome_WidgetWin_1"},
                {"hwnd": 100, "pid": 10, "class": "AnyNativeWrapperClass"}]
        with patch.object(self.uia, "native_top_windows", return_value=rows), \
                patch.object(self.uia, "process_name", wraps=self.uia.process_name) as names:
            self.assertTrue(self.sender._find_window())
        self.assertEqual([call.args[0] for call in names.call_args_list].count(20), 1)
        self.auto.ControlFromHandle.assert_called_once_with(100)
        self.auto.GetRootControl.assert_not_called()

    def test_native_recorder_popup_is_observed_even_if_main_keeps_foreground(self):
        self.sender._window = self.auto.main
        popup = fixtures.Control(self.auto, "", "WindowControl", klass="mmui::ChatVoiceRecordView", handle=200)
        label = popup.add(fixtures.Control(self.auto, "松开发送", "TextControl"))
        self.auto.ControlFromHandle = Mock(return_value=popup)
        self.auto.GetRootControl = Mock(side_effect=AssertionError("no desktop UIA enumeration"))
        rows = [{"hwnd": 200, "pid": 10, "class": "Qt51514QWindowToolSaveBits"}]
        watch = {"button": None, "composer": self.auto.composer, "main": (100, 10),
                 "recording_window": (100, 10)}
        with patch.object(self.uia, "native_top_windows", return_value=rows):
            self.assertIs(self.sender._voice_watched_state(watch), label)
        self.auto.ControlFromHandle.assert_called_once_with(200)
        self.auto.GetRootControl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
