"""Provider-call budgets using synthetic trees; never access a real desktop."""
from collections import Counter
import types
import sys
import unittest
from unittest.mock import patch

import test_uia_sender as fixtures
from test_uia_sender import Control, Rect
from test_uia_foreground import NativeDesktop


class UiaSendPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.UiaTests()
        self.fixture.setUp()
        self.auto, self.sender, self.uia = self.fixture.auto, self.fixture.sender, self.fixture.uia
        self.auto.ControlFromHandle = lambda hwnd: next((root for root in
            [self.auto.main]+self.auto.extra_windows if root.NativeWindowHandle == hwnd), None)

    def tearDown(self):
        self.fixture.tearDown()

    def add_history(self, count=300):
        history = self.auto.main.add(Control(self.auto, "聊天记录", "ListControl", Rect(260, 100, 1000, 560),
                                             "MainView.chat_view.chat_history_list"))
        return {history.add(Control(self.auto, "旧消息", "TextControl", Rect(280, 200, 700, 230),
                                    klass="HistoryMessageCanary")) for _ in range(count)}

    def test_foreground_activation_does_not_repeat_pre_owner_identity_reads(self):
        native = NativeDesktop()
        native.front = 100
        self.sender._window = self.auto.main
        self.sender._native_user32 = lambda: native
        with patch.object(self.sender, "_window_identity", wraps=self.sender._window_identity) as identities:
            self.assertTrue(self.sender._activate())
        # Previously ten identity reads (twenty cross-process properties).
        # Keep the post-owner checks, including the final activation check.
        self.assertLessEqual(identities.call_count, 6)
        self.assertEqual(native.GetWindowThreadProcessId.call_count, 5)
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.clicks, [])

    def test_header_name_is_read_once_per_guard_and_is_live_next_time(self):
        self.sender._window = self.auto.main
        self.auto.header.Name = "Alice"
        editor = self.sender._find_editor()
        prop, names = self.uia._prop, []
        def counted_prop(control, key, default=None):
            if control is self.auto.header and key == "Name":
                names.append(control.Name)
            return prop(control, key, default)
        with patch.object(self.uia, "_prop", side_effect=counted_prop):
            self.assertIn("Alice", self.sender._header_names(editor))
            self.auto.header.Name = "Bob"
            self.assertNotIn("Alice", self.sender._header_names(editor))
        self.assertEqual(names, ["Alice", "Bob"])

    def test_native_sentence_has_bounded_provider_work_with_large_history(self):
        identity = self.fixture.native_identity_header()
        self.auto.header.Name, identity.Name = "Alice", "微信号：a"
        history = self.add_history()
        counts = Counter()
        prop, parent = self.uia._prop, self.uia._parent

        def counted_prop(control, key, default=None):
            counts["properties"] += 1
            counts[key] += 1
            counts["history_properties"] += control in history
            return prop(control, key, default)

        def counted_parent(control):
            counts["parents"] += 1
            return parent(control)

        with patch.object(self.uia, "_prop", side_effect=counted_prop), patch.object(self.uia, "_parent", side_effect=counted_parent):
            self.assertTrue(self.sender.send_text("Alice", "one safe sentence.", identity={"username": "a"}, stream_sentence=True))
        # The pre-change fixture required 2,586 property reads / 75 parents.
        # The budget allows fresh safety checks but prevents an O(history) send.
        self.assertLess(counts["properties"], 500, dict(counts))
        self.assertLess(counts["parents"], 75, dict(counts))
        self.assertEqual(counts["history_properties"], 0)
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 0)
        self.assertEqual(self.auto.delivered, [("Alice", "one safe sentence.")])
        self.assertEqual(self.fixture.clip.data, {13: "用户原剪贴板"})

    def test_full_read_only_diagnostics_still_observe_history(self):
        self.add_history()
        diagnostic = self.sender.diagnostics()
        self.assertIn("HistoryMessageCanary", diagnostic["tree_classes"])
        self.assertGreaterEqual(diagnostic["control_count"], 300)
        self.assertEqual(diagnostic["snapshot_source"], "fresh_diagnostic")
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.fixture.clip.writes, 0)

    def test_known_main_window_avoids_unrelated_desktop_providers_on_next_send(self):
        self.assertTrue(self.sender.send_text("Alice", "first", identity={"username": "a"}))
        self.assertEqual(self.sender._known_window, (100, 10))
        self.assertIsNone(self.sender._window)
        handles = []
        self.auto.ControlFromHandle = lambda hwnd: handles.append(hwnd) or self.auto.main
        self.auto.GetRootControl = lambda: self.fail("Do not enumerate unrelated desktop UIA providers")
        self.assertTrue(self.sender.send_text("Alice", "second", identity={"username": "a"}))
        self.assertEqual(handles, [100])
        self.assertEqual(self.auto.delivered, [("Alice", "first"), ("Alice", "second")])
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 1)

    def test_known_window_is_reacquired_in_new_com_context_without_retaining_control(self):
        self.sender._known_window = (100, 10)
        fresh = Control(self.auto, "微信", "WindowControl", klass="mmui::MainWindow", handle=100)
        self.auto.ControlFromHandle = lambda hwnd: fresh
        self.auto.GetRootControl = lambda: self.fail("Known live native window needs no desktop enumeration")
        native = NativeDesktop()
        self.sender._native_user32 = lambda: native
        with self.sender._uia_context():
            self.assertTrue(self.sender._ensure_window())
            self.assertIs(self.sender._window, fresh)
            self.assertIsNot(self.sender._window, self.auto.main)
        self.assertIsNone(self.sender._window)
        self.assertEqual(self.sender._known_window, (100, 10))

    def test_stale_native_window_owner_falls_back_to_fresh_discovery(self):
        self.sender._known_window = (100, 10)
        self.auto.main.NativeWindowHandle = 110
        native = NativeDesktop()
        native.pids[100] = 20  # Same HWND has been reused by another process.
        self.sender._native_user32 = lambda: native
        self.auto.ControlFromHandle = lambda hwnd: self.fail("Do not acquire the foreign reused HWND")
        with self.sender._uia_context():
            self.assertTrue(self.sender._ensure_window())
            self.assertIs(self.sender._window, self.auto.main)
            self.assertEqual(self.sender._known_window, (110, 10))

    def test_changed_configured_pid_does_not_reuse_previous_window(self):
        self.sender._known_window = (100, 10)
        self.fixture.cfg.UIA_PROCESS_ID = 30
        selected = Control(self.auto, "微信", "WindowControl", klass="mmui::MainWindow", pid=30, handle=300)
        self.auto.extra_windows = [selected]
        self.auto.ControlFromHandle = lambda hwnd: self.fail("Configured PID changed; old HWND is not eligible")
        with patch.object(self.uia, "process_name", lambda pid: "weixin.exe" if pid in {10, 30} else "chrome.exe"):
            with self.sender._uia_context():
                self.assertTrue(self.sender._ensure_window())
                self.assertIs(self.sender._window, selected)
                self.assertEqual(self.sender._known_window, (300, 30))

    def test_known_handle_with_non_main_class_falls_back_to_main_discovery(self):
        self.sender._known_window = (100, 10)
        popup = Control(self.auto, "Alice", "WindowControl", klass="mmui::ChatWindow", handle=100)
        self.auto.ControlFromHandle = lambda hwnd: popup
        with self.sender._uia_context():
            self.assertTrue(self.sender._ensure_window())
            self.assertIs(self.sender._window, self.auto.main)

    def test_send_snapshot_saves_primitive_observations_without_provider_calls(self):
        self.sender._ready = self.sender._observed_controls_available = True
        self.sender._send_window = (100, 10)
        self.sender._observed_editor_info = {"name": "输入", "type": "EditControl", "automation_id": "chat_input_field"}
        self.sender._observed_headers = ["Alice", "微信号：a"]
        with patch.object(self.uia, "_prop", side_effect=AssertionError("snapshot property read")), \
                patch.object(self.uia, "_walk", side_effect=AssertionError("snapshot tree read")), \
                patch.object(self.sender, "_find_editor", side_effect=AssertionError("snapshot editor discovery")):
            self.sender._update_operation_snapshot("Alice")
        self.assertEqual(self.sender._snapshot["snapshot_source"], "operation_observation")
        self.assertEqual(self.sender._snapshot["current_chat"], "Alice")
        self.sender._observed_editor_info["name"] = "changed"
        self.sender._observed_headers[0] = "Bob"
        self.assertEqual(self.sender._snapshot["editor"]["name"], "输入")
        self.assertEqual(self.sender._snapshot["header_candidates"][0], "Alice")

    def test_search_results_and_anonymous_composer_container_are_not_pruned(self):
        self.auto.search.IsOffscreen = self.auto.results.IsOffscreen = False
        self.auto.search.text = "Alice"
        self.auto.populate_results()
        self.sender._window = self.auto.main
        row = self.auto.results.children[0]
        self.assertIn(row, [control for control, _ in self.sender._control_nodes()])
        anonymous = self.auto.main.add(Control(self.auto, kind="DocumentControl"))
        self.auto.main.children.remove(self.auto.composer)
        anonymous.add(self.auto.composer)
        self.assertFalse(self.sender._chat_tree_skip(anonymous))
        self.sender._tree_nodes = None
        self.assertIs(self.sender._find_editor(), self.auto.editor)

    def test_search_does_not_visit_recent_chat_rows(self):
        sessions = self.auto.main.add(Control(self.auto, kind="ListControl", aid="MainView.session_list"))
        with patch.object(sessions, "GetChildren", wraps=sessions.GetChildren) as children:
            self.assertTrue(self.sender.send_text("Alice", "hi.", identity={"username": "a"}))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 1)
        children.assert_not_called()

    def test_next_stream_operation_checks_identity_and_generation_after_pruned_send(self):
        identity = self.fixture.native_identity_header()
        self.add_history()
        self.assertTrue(self.sender.send_text("Alice", "first.", identity={"username": "a"}, generation=7, stream_sentence=True))
        self.assertIsNone(self.sender._window)
        self.assertIsNone(self.sender._tree_nodes)
        self.assertIsNone(self.sender._observed_editor_info)
        self.assertEqual(self.sender._observed_headers, [])
        self.fixture.state.bridge_generation = 8
        self.assertFalse(self.sender.send_text("Alice", "obsolete.", identity={"username": "a"}, generation=7, stream_sentence=True))
        self.assertEqual(self.auto.delivered, [("Alice", "first.")])
        self.auto.on_paste = lambda: setattr(identity, "Name", "微信号：different")
        self.auto.keys.clear()
        self.assertFalse(self.sender.send_text("Alice", "must stop.", identity={"username": "a"}, generation=8, stream_sentence=True))
        self.assertNotIn("{Enter}", self.auto.keys)
        self.assertEqual(self.auto.delivered, [("Alice", "first.")])

    def test_slow_provider_does_not_add_chat_scans_or_guards_to_recorded_leading_silence(self):
        self.fixture._voice_fixture()
        native = NativeDesktop()
        native.front = 100
        self.auto.GetForegroundWindow = native.GetForegroundWindow
        kernel = types.SimpleNamespace(GetCurrentThreadId=lambda: 50)
        now, holding, counts, events = [20.0], [False], Counter(), {}
        prop, walk, verify = self.uia._prop, self.uia._walk, self.sender._chat_verified
        down, up = self.sender._mouse_down, self.sender._mouse_up
        def slow_prop(*args):
            if holding[0]:
                counts["held_properties"] += 1
                now[0] += .025  # Synthetic slow cross-process UIA call.
            return prop(*args)
        def scanned(root, *args, **kwargs):
            if holding[0] and root is self.auto.main:
                counts["held_main_scans"] += 1
            return walk(root, *args, **kwargs)
        def verified(*args):
            if holding[0]:
                counts["held_chat_guards"] += 1
            return verify(*args)
        def press(*args):
            events["hold"] = now[0]
            holding[0] = True
            down(*args)
        def release(*args):
            holding[0] = False
            up(*args)
        sound = sys.modules["pygame"].mixer.Sound(__file__)
        sound.get_length = lambda: .4
        sound.play = lambda: events.update(play=now[0])
        self.sender._mouse_down, self.sender._mouse_up = press, release
        with patch.object(self.uia.ctypes, "windll", types.SimpleNamespace(user32=native, kernel32=kernel), create=True), \
                patch.object(self.uia, "_prop", side_effect=slow_prop), \
                patch.object(self.uia, "_walk", side_effect=scanned), \
                patch.object(self.sender, "_chat_verified", side_effect=verified), \
                patch.object(self.uia.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(self.uia.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            self.assertTrue(self.sender.send_voice("Alice", __file__))
        self.assertEqual(counts["held_main_scans"], 0, dict(counts))
        self.assertEqual(counts["held_chat_guards"], 0, dict(counts))
        self.assertLessEqual(counts["held_properties"], 5, dict(counts))
        self.assertLess(events["play"] - events["hold"], .15, (events, dict(counts)))

    def test_slow_popup_provider_observes_small_recorder_then_plays_without_chat_guard(self):
        self.fixture._voice_popup_fixture()
        native = NativeDesktop()
        native.front, native.pids[200] = 100, 10
        self.auto.GetForegroundWindow = native.GetForegroundWindow
        now, holding, counts, events = [20.0], [False], Counter(), {}
        prop, verify, walk = self.uia._prop, self.sender._chat_verified, self.uia._walk
        down, up = self.sender._key_down, self.sender._key_up
        def slow_prop(*args):
            if holding[0]:
                counts["held_properties"] += 1
                now[0] += .025
            return prop(*args)
        def verified(*args):
            if holding[0]:
                self.fail("Chat verification belongs before the recording gesture")
            return verify(*args)
        def scanned(root, *args, **kwargs):
            if holding[0] and root is self.auto.main:
                self.fail("The holding path must not walk the main chat window")
            return walk(root, *args, **kwargs)
        def press(vk):
            events["hold"] = now[0]
            holding[0] = True
            native.front = 200
            down(vk)
        def release(vk):
            holding[0] = False
            native.front = 100
            up(vk)
        sound = sys.modules["pygame"].mixer.Sound(__file__)
        sound.get_length = lambda: .4
        sound.play = lambda: events.update(play=now[0], properties_at_play=counts["held_properties"])
        self.sender._key_down, self.sender._key_up = press, release
        with patch.object(self.uia.ctypes, "windll", types.SimpleNamespace(user32=native, kernel32=types.SimpleNamespace(GetCurrentThreadId=lambda: 50)), create=True), \
                patch.object(self.uia, "_prop", side_effect=slow_prop), \
                patch.object(self.uia, "_walk", side_effect=scanned), \
                patch.object(self.sender, "_chat_verified", side_effect=verified), \
                patch.object(self.uia.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(self.uia.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            self.assertTrue(self.sender.send_voice("Alice", __file__))
        self.assertLessEqual(events["properties_at_play"], 10, (events, dict(counts)))
        self.assertEqual(events["properties_at_play"], counts["held_properties"])
        self.assertLess(events["play"] - events["hold"], .3, (events, dict(counts)))

    def test_native_input_during_hold_cancels_without_ui_chat_revalidation(self):
        self.fixture._voice_fixture()
        native, stamp, holding = NativeDesktop(), [0], [False]
        native.front = 100
        def input_info(value):
            value._obj.dwTime = stamp[0]
            return 1
        native.GetLastInputInfo = input_info
        self.auto.GetForegroundWindow = native.GetForegroundWindow
        down, up, verify = self.sender._mouse_down, self.sender._mouse_up, self.sender._chat_verified
        def press(*args):
            holding[0] = True
            return down(*args)
        def release(*args):
            holding[0] = False
            return up(*args)
        def verified(*args):
            if holding[0]:
                self.fail("No full chat guard is needed while holding")
            return verify(*args)
        self.sender._mouse_down, self.sender._mouse_up = press, release
        sound = sys.modules["pygame"].mixer.Sound(__file__)
        sound.play = lambda: stamp.__setitem__(0, stamp[0] + 1)
        with patch.object(self.uia.ctypes, "windll", types.SimpleNamespace(user32=native, kernel32=types.SimpleNamespace(GetCurrentThreadId=lambda: 50)), create=True), \
                patch.object(self.sender, "_chat_verified", side_effect=verified):
            self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertIn("{Esc}", self.auto.keys)
        self.assertFalse(holding[0])


if __name__ == "__main__":
    unittest.main()
