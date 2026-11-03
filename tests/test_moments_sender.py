"""Bound main HWND lookups using synthetic UIA controls, never the desktop."""
import unittest
from unittest.mock import Mock, call, patch

import test_uia_sender as fixtures
from test_uia_sender import Control


class MainWindowLookupTests(unittest.TestCase):
    setUp = fixtures.UiaTests.setUp
    tearDown = fixtures.UiaTests.tearDown
    load_moments = fixtures.MomentsTests.load_moments

    def fixture(self):
        self.load_moments()
        self.moment._main_handle = self.auto.main.NativeWindowHandle
        self.moment._main_pid = self.auto.main.ProcessId

    def test_bound_handle_returns_fresh_verified_provider_without_desktop_enumeration(self):
        self.fixture()
        fresh = Control(self.auto, "微信", "WindowControl", klass="mmui::MainWindow", handle=100)
        self.auto.ControlFromHandle = Mock(side_effect=[self.auto.main, fresh])
        root = self.auto.GetRootControl = Mock(side_effect=AssertionError("must not enumerate desktop"))
        self.assertIs(self.moment._main_window(), self.auto.main)
        self.assertIs(self.moment._main_window(), fresh)
        self.assertNotEqual(tuple(fresh.GetRuntimeId()), tuple(self.auto.main.GetRuntimeId()))
        self.assertEqual(self.auto.ControlFromHandle.call_args_list, [call(100), call(100)])
        root.assert_not_called()

    def test_existing_handle_api_rejects_explicit_other_identity_popout_or_hidden(self):
        for invalid in ("handle", "pid", "popout", "offscreen", "visibility_unknown"):
            with self.subTest(invalid=invalid):
                self.fixture()
                window = Control(self.auto, "微信", "WindowControl", klass="mmui::MainWindow", handle=100)
                if invalid == "handle":
                    window.NativeWindowHandle = 101
                elif invalid == "pid":
                    window.ProcessId = 20
                elif invalid == "popout":
                    window.ClassName = "mmui::ChatWindow"
                elif invalid == "offscreen":
                    window.IsOffscreen = True
                elif invalid == "visibility_unknown":
                    window.IsOffscreen = None
                getter = Mock(return_value=None if invalid == "none" else window)
                if invalid == "exception":
                    getter.side_effect = OSError("stale provider")
                self.auto.ControlFromHandle = None if invalid == "noncallable" else getter
                root = self.auto.GetRootControl = Mock(side_effect=AssertionError("must not broaden lookup"))
                self.assertIsNone(self.moment._main_window())
                root.assert_not_called()

    def test_handle_provider_with_wrong_process_name_is_rejected(self):
        self.fixture()
        self.auto.ControlFromHandle = Mock(return_value=self.auto.main)
        self.auto.GetRootControl = Mock(side_effect=AssertionError("must not enumerate desktop"))
        self.moment._is_wechat = Mock(return_value=False)
        self.assertIsNone(self.moment._main_window())
        self.moment._is_wechat.assert_called_once_with(self.auto.main)

    def test_missing_handle_api_keeps_exact_unique_main_enumeration(self):
        self.fixture()
        root = self.auto.root
        self.auto.GetRootControl = Mock(return_value=root)
        popout = root.add(Control(self.auto, "Alice", "WindowControl", klass="mmui::ChatWindow", handle=101))
        self.assertIs(self.moment._main_window(), self.auto.main)
        root.children.remove(self.auto.main)
        self.assertIsNone(self.moment._main_window())
        self.assertIn(popout, root.children)
        root.add(self.auto.main)
        root.add(Control(self.auto, "微信", "WindowControl", klass="mmui::MainWindow", handle=100))
        self.assertIs(self.moment._main_window(), self.auto.main)

    def test_missing_handle_api_with_failed_enumeration_returns_none(self):
        self.fixture()
        self.auto.GetRootControl = Mock(side_effect=OSError("desktop provider unavailable"))
        self.assertIsNone(self.moment._main_window())

    def test_native_cold_main_lookup_never_enumerates_desktop_provider(self):
        self.fixture()
        self.auto.ControlFromHandle = Mock(return_value=None)
        self.auto.GetRootControl = Mock(side_effect=AssertionError("no desktop UIA enumeration"))
        with patch.object(self._moment_module, "native_top_windows", return_value=[{"hwnd": 100, "pid": 10, "class": "mmui::MainWindow"}]):
            self.assertIsNone(self.moment._main_window())
        self.auto.ControlFromHandle.assert_called_once_with(100)
        self.auto.GetRootControl.assert_not_called()

    def test_native_popup_lookup_opens_only_filtered_wechat_handles(self):
        self.fixture()
        popup = Control(self.auto, "", "WindowControl", klass="mmui::ProfileUniquePop", handle=200)
        self.auto.ControlFromHandle = Mock(side_effect=lambda hwnd: {100: self.auto.main, 200: popup}[hwnd])
        self.auto.GetRootControl = Mock(side_effect=AssertionError("no desktop UIA enumeration"))
        rows = [{"hwnd": 100, "pid": 10, "class": "mmui::MainWindow"},
                {"hwnd": 200, "pid": 10, "class": "mmui::ProfileUniquePop"}]
        with patch.object(self._moment_module, "native_top_windows", return_value=rows) as native:
            self.assertEqual(self.moment._roots(), [self.auto.main, popup])
        native.assert_called_once_with(self.auto, pid=10)
        self.assertEqual(self.auto.ControlFromHandle.call_args_list, [call(100), call(200)])
        self.auto.GetRootControl.assert_not_called()

    def test_zero_hwnd_child_handle_provider_resolves_only_original_physical_ancestor(self):
        self.fixture()
        child = self.auto.main.add(Control(self.auto, "虚拟视口"))
        self.auto.ControlFromHandle = Mock(return_value=child)
        self.assertIs(self.moment._main_window(), self.auto.main)
        self.auto.foreground = child
        self.assertTrue(self.moment._foreground(self.auto.main))
        other = Control(self.auto, "其它窗口", "WindowControl", klass="mmui::ChatWindow", handle=110)
        other.add(child)
        self.assertIsNone(self.moment._main_window())
        self.assertFalse(self.moment._foreground(self.auto.main))

    def test_cold_handle_lookup_can_recover_exact_original_root_without_switching_windows(self):
        for kind in ("none", "exception", "noncallable"):
            with self.subTest(kind=kind):
                self.fixture()
                getter = Mock(return_value=None)
                if kind == "exception": getter.side_effect = OSError("cold Qt provider")
                self.auto.ControlFromHandle = None if kind == "noncallable" else getter
                self.auto.root.children = [self.auto.main]
                self.auto.GetRootControl = Mock(return_value=self.auto.root)
                self.assertIs(self.moment._main_window(), self.auto.main)
                self.auto.root.children.remove(self.auto.main)
                self.auto.root.add(Control(self.auto, "其它微信", "WindowControl", klass="mmui::MainWindow", handle=110))
                self.assertIsNone(self.moment._main_window())

    def test_bound_qt_stub_with_missing_accessible_title_keeps_same_physical_main(self):
        self.fixture()
        stub = Control(self.auto, "", "WindowControl", klass="Qt51514QWindowIcon", handle=100)
        self.auto.ControlFromHandle = Mock(return_value=stub)
        self.assertIs(self.moment._main_window(), stub)
        stub.NativeWindowHandle = 110
        self.assertIsNone(self.moment._main_window())
        stub.NativeWindowHandle, stub.ProcessId = 100, 20
        self.assertIsNone(self.moment._main_window())

    def test_virtual_pane_reporting_owner_hwnd_and_missing_parent_link_use_exact_main_tree(self):
        self.fixture()
        pane = self.auto.main.add(Control(self.auto, kind="PaneControl", handle=100, klass="mmui::LayoutPane"))
        button = pane.add(Control(self.auto, "刷新", "ButtonControl"))
        self.assertIs(self.moment._root_for(button), self.auto.main)
        button.parent = None
        self.assertIs(self.moment._root_for(button), self.auto.main)
        pane.children.remove(button)
        self.assertIsNone(self.moment._root_for(button))

    def test_unbound_window_never_queries_handles_or_enumerates_desktop(self):
        self.fixture()
        self.auto.ControlFromHandle = Mock(side_effect=AssertionError("unbound handle"))
        root = self.auto.GetRootControl = Mock(side_effect=AssertionError("unbound desktop"))
        for attr in ("_main_handle", "_main_pid"):
            with self.subTest(attr=attr):
                before = getattr(self.moment, attr)
                setattr(self.moment, attr, 0)
                self.assertIsNone(self.moment._main_window())
                setattr(self.moment, attr, before)
        self.auto.ControlFromHandle.assert_not_called()
        root.assert_not_called()

    def test_root_accepts_original_main_pane_only_through_control_parent_chain(self):
        self.fixture()
        self.auto.main.ControlTypeName = "PaneControl"
        button = self.auto.main.add(Control(self.auto, "刷新", "ButtonControl"))
        self.assertIs(self.moment._root_for(button), self.auto.main)

    def test_root_rejects_untrusted_main_pane_identity_or_visibility(self):
        for invalid in ("handle", "pid", "class", "offscreen", "visibility_unknown"):
            with self.subTest(invalid=invalid):
                self.fixture()
                self.auto.main.ControlTypeName = "PaneControl"
                button = self.auto.main.add(Control(self.auto, "刷新", "ButtonControl"))
                if invalid == "handle": self.auto.main.NativeWindowHandle = 101
                elif invalid == "pid": self.auto.main.ProcessId = 20
                elif invalid == "class": self.auto.main.ClassName = "mmui::ChatWindow"
                elif invalid == "offscreen": self.auto.main.IsOffscreen = True
                else: self.auto.main.IsOffscreen = None
                self.assertIsNone(self.moment._root_for(button))

    def test_root_does_not_cross_other_physical_pane_to_original_main(self):
        self.fixture()
        pane = self.auto.main.add(Control(self.auto, kind="PaneControl", handle=110))
        button = pane.add(Control(self.auto, "刷新", "ButtonControl"))
        self.assertIsNone(self.moment._root_for(button))


if __name__ == "__main__":
    unittest.main()
