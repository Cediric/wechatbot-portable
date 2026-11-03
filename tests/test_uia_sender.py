"""Synthetic UI trees only. These tests never touch the real desktop or contacts."""
import importlib.util
import pathlib
import sys
import threading
import time
import types
import unittest
from contextlib import nullcontext
from collections import namedtuple
from enum import IntEnum
from unittest.mock import patch

BRIDGE = pathlib.Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))
from desktop_coordinator import ClipboardLease, DesktopCancelled, NativeClipboard, desktop_operation

Rect = namedtuple("Rect", "left top right bottom")


class FakeClipboard:
    def __init__(self):
        self.data = {13: "用户原剪贴板"}
        self.seq = 1
        self.writes = 0

    def sequence(self):
        return self.seq

    def snapshot(self):
        return dict(self.data)

    def replace(self, data, expected_sequence=None):
        if expected_sequence is not None and expected_sequence != self.seq:
            return False
        self.data = dict(data)
        self.seq += 1
        self.writes += 1
        return self.seq

    def set_text(self, text, expected_sequence=None):
        return self.replace({13: text}, expected_sequence)

    def matching_text_sequence(self, text):
        return self.seq if self.data.get(13) == text else False

    def set_files(self, paths, expected_sequence=None):
        return self.replace({15: list(paths)}, expected_sequence)

    def set_image(self, path, expected_sequence=None):
        return self.replace({8: path}, expected_sequence)


class Control:
    _next = 1

    def __init__(self, auto, name="", kind="PaneControl", rect=None, aid="", klass="", pid=10, handle=0):
        self.auto, self.Name, self.ControlTypeName = auto, name, kind
        self.AutomationId, self.ClassName = aid, klass
        self.ProcessId, self.NativeWindowHandle = pid, handle
        self.BoundingRectangle = rect or Rect(0, 0, 1000, 800)
        self.IsOffscreen, self.IsEnabled = False, True
        self.parent, self.children = None, []
        self.text = ""
        self.value_available = kind == "EditControl"
        self.rid = Control._next
        Control._next += 1
        self.click_action = None
        self.right_click_action = None

    def add(self, child):
        child.parent = self
        self.children.append(child)
        return child

    def GetChildren(self):
        return self.children

    def GetParentControl(self):
        return self.parent

    def GetRuntimeId(self):
        return [self.rid]

    def GetValuePattern(self):
        return types.SimpleNamespace(Value=self.text) if self.value_available else None

    def SetFocus(self):
        self.auto.focused = self
        return True

    def SetActive(self):
        self.auto.foreground = self

    def Click(self, **kwargs):
        self.auto.clicks.append(self.Name)
        if self.click_action:
            self.click_action()
        else:
            self.SetFocus()

    def RightClick(self, **kwargs):
        self.auto.right_clicks.append(self.Name)
        if self.right_click_action:
            self.right_click_action()


class FakeAuto:
    def __init__(self, clip, contacts=None):
        self.clip = clip
        self.contacts = contacts if contacts is not None else [("Alice", "a"), ("Bob", "b")]
        self.launched, self.exposed = True, True
        self.keys, self.clicks, self.delivered, self.com_threads = [], [], [], []
        self.right_clicks = []
        self.on_paste = self.on_enter = None
        self.extra_windows = []
        self.root = Control(self, "Desktop", pid=0)
        self.main = self.root.add(Control(self, "微信", "WindowControl", klass="mmui::MainWindow", handle=100))
        self.search = self.main.add(Control(self, "搜索", "EditControl", Rect(30, 40, 200, 65), "search_input"))
        self.results = self.main.add(Control(self, "搜索结果", "ListControl", Rect(20, 80, 230, 600), "search_list"))
        self.header = self.main.add(Control(self, "Bob", "TextControl", Rect(280, 45, 500, 75)))
        self.composer = self.main.add(Control(self, rect=Rect(265, 590, 970, 780)))
        self.editor = self.composer.add(Control(self, "输入", "EditControl", Rect(275, 620, 950, 745), "chat_input_field"))
        self.search.IsOffscreen = self.results.IsOffscreen = True
        self.foreground, self.focused = self.main, self.editor
        self.drafts = {}

    def UIAutomationInitializerInThread(self):
        auto = self
        class Context:
            def __enter__(self):
                auto.com_threads.append(threading.get_ident())
            def __exit__(self, *unused):
                return False
        return Context()

    def GetRootControl(self):
        self.root.children = [self.main] + self.extra_windows if self.launched else []
        return self.root

    def GetForegroundControl(self):
        return self.foreground

    def GetFocusedControl(self):
        return self.focused

    def populate_results(self):
        self.results.children = []
        for name, username in self.contacts:
            if self.search.text.casefold() not in name.casefold():
                continue
            row = self.results.add(Control(self, name, "ListItemControl", Rect(20, 90, 240, 125), "search_item_" + username))
            row.add(Control(self, "微信号：" + username, "TextControl"))
            def select(name=name):
                self.header.Name = name
                self.editor.text = self.drafts.get(name, "")
                self.search.IsOffscreen = self.results.IsOffscreen = True
                self.results.children = []
                self.focused = self.editor
            row.click_action = select

    def SendKeys(self, key, **kwargs):
        self.keys.append(key)
        if key == "{Ctrl}f":
            self.search.IsOffscreen = self.results.IsOffscreen = False
            self.focused = self.search
        elif key in {"{Ctrl}a", "{Ctrl}a{Backspace}"}:
            self.focused.text = ""
            if key == "{Ctrl}a{Backspace}" and self.focused is self.editor:
                self.composer.children = [self.editor]
        elif key == "{Ctrl}v":
            if 13 in self.clip.data:
                self.focused.text = self.clip.data[13]
            elif self.focused is self.editor:
                self.composer.add(Control(self, "图片预览", "ImageControl", Rect(280, 590, 360, 635)))
            if self.focused is self.search:
                self.populate_results()
            elif self.on_paste:
                self.on_paste()
        elif key == "{Enter}":
            self.delivered.append((self.header.Name, self.editor.text))
            self.editor.text = ""
            self.composer.children = [self.editor]
            if self.on_enter:
                self.on_enter()


class FakeGate:
    """Source integration fixture: no native process APIs are available here."""
    def __init__(self, allowed=True):
        self.allowed, self.active = allowed, False
        self.ensures, self.closes = [], 0
        self.on_ensure = None

    def snapshot(self):
        return {"enabled": self.allowed, "active": self.active, "status": "mock"}

    def ensure(self, hwnd, pid):
        self.ensures.append((hwnd, pid))
        if self.on_ensure:
            self.on_ensure()
        self.active = self.allowed
        return self.allowed

    def close(self):
        self.closes += 1
        self.active = False
        return {"ok": True, "status": "restored", "wrote": False}


class UiaTests(unittest.TestCase):
    def setUp(self):
        self.clip = FakeClipboard()
        self.auto = FakeAuto(self.clip)
        self.cfg = types.SimpleNamespace(UIA_TIMEOUT=0.4, IMAGE_SEND_METHOD="cf_hdrop", VOICE_ENABLED=True,
                                         VOICE_RIGHT_ALT_ENABLED=False)
        self.state = types.SimpleNamespace(bridge_generation=7, note_sent_text=lambda *a, **k: None)
        self.human = types.SimpleNamespace(prepare_bubbles=lambda text: [text] if text else [],
                                         wait_remaining=lambda started,minimum,cancelled:not cancelled())
        self.modules = patch.dict(sys.modules, {
            "uiautomation": self.auto, "config": self.cfg, "audio_helper": types.ModuleType("audio_helper"),
            "state": self.state, "humanize": self.human,
        })
        self.modules.start()
        spec = importlib.util.spec_from_file_location("uia_isolated", BRIDGE / "uia_sender.py")
        self.uia = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.uia)
        self.process_patch = patch.object(self.uia, "process_name", lambda pid: "weixin.exe" if pid == 10 else "chrome.exe")
        self.clip_patch = patch.object(self.uia, "ClipboardLease", lambda: ClipboardLease(self.clip))
        self.screen_patch = patch.object(self.uia.UiaSender, "_screen_reader_active", staticmethod(lambda: False))
        self.process_patch.start()
        self.clip_patch.start()
        self.screen_patch.start()
        self.sender = self.uia.UiaSender()

    def tearDown(self):
        self.screen_patch.stop()
        self.clip_patch.stop()
        self.process_patch.stop()
        self.modules.stop()

    def test_manual_switch_never_uses_cached_contact(self):
        self.sender._last_contact = "Alice"
        self.auto.header.Name = "Bob"
        self.assertTrue(self.sender.send_text("Alice", "你好"))
        self.assertEqual(self.auto.delivered, [("Alice", "你好")])
        self.assertIn("Alice", self.auto.clicks)

    def _unsupported_clipboard(self):
        self.clip.data = {13: b"original text\0", 0xC010: b"opaque OLE data"}
        saved = dict(self.clip.data), self.clip.seq
        self.snapshot_calls = 0
        def snapshot():
            self.snapshot_calls += 1
            return None
        self.clip.snapshot = snapshot
        return saved

    def test_unbackable_clipboard_does_not_switch_to_unverified_unicode_input(self):
        saved = self._unsupported_clipboard()
        contact, text = "测试群{Enter}", "收到{Enter}（🌙）"
        self.auto.contacts = [(contact, "synthetic@chatroom")]
        self.assertFalse(self.sender.send_text(contact, text,
            identity={"username": "synthetic@chatroom", "is_group": True}))
        self.assertEqual(self.auto.delivered, [])
        self.assertNotIn("{Enter}", self.auto.keys)
        self.assertNotIn("{Ctrl}v", self.auto.keys)
        self.assertEqual((self.clip.data, self.clip.seq), saved)
        self.assertEqual(self.clip.writes, 0)
        self.assertEqual(self.snapshot_calls, 1)
        self.assertIn("无法完整备份剪贴板", self.sender.last_send_result()["error"])

    def test_unbackable_clipboard_private_text_preserves_payload(self):
        saved = self._unsupported_clipboard()
        self.native_identity_header()
        self.assertFalse(self.sender.send_text("Bob", "你好", identity={"username": "b"}))
        self.assertEqual(self.auto.delivered, [])
        self.assertEqual((self.clip.data, self.clip.seq), saved)
        self.assertNotIn("{Ctrl}f", self.auto.keys)

    def test_group_query_and_body_remain_literal_clipboard_pastes(self):
        contact, text = "测试群{Enter}", "收到{Enter}（🌙）"
        self.auto.contacts = [(contact, "synthetic@chatroom")]
        self.assertTrue(self.sender.send_text(contact, text,
            identity={"username": "synthetic@chatroom", "is_group": True}))
        self.assertEqual(self.auto.delivered, [(contact, text)])
        self.assertEqual(self.auto.keys.count("{Ctrl}v"), 2)
        self.assertEqual(self.auto.keys.count("{Enter}"), 1)
        self.assertEqual(self.clip.data, {13: "用户原剪贴板"})

    def test_user_copy_after_contact_search_does_not_type_or_overwrite_new_clipboard(self):
        populate = self.auto.populate_results
        def populate_with_user_copy():
            populate()
            for row in self.auto.results.children:
                select = row.click_action
                def selected(select=select):
                    select()
                    self.clip.set_text("用户搜索期间新复制的内容")
                row.click_action = selected
        self.auto.populate_results = populate_with_user_copy
        self.assertFalse(self.sender.send_text("Alice", "本次回复"))
        self.assertEqual(self.auto.delivered, [])
        self.assertEqual(self.auto.editor.text, "")
        self.assertEqual(self.clip.data, {13: "用户搜索期间新复制的内容"})
        self.assertEqual(self.clip.writes, 2)  # Search query plus the user's copy.

    def test_unsupported_clipboard_still_blocks_image_copy_without_losing_user_data(self):
        saved = self._unsupported_clipboard()
        self.assertFalse(self.sender.send_image("Alice", __file__))
        self.assertEqual(self.auto.delivered, [])
        self.assertEqual(self.clip.writes, 0)
        self.assertEqual((self.clip.data, self.clip.seq), saved)
        self.assertNotIn("{Ctrl}v", self.auto.keys)
        self.assertIn("无法完整备份剪贴板", self.sender.last_send_result()["error"])

    def _sns_consumes_search(self):
        active = {"sns": True}
        original = self.auto.SendKeys
        def send_keys(key, **kwargs):
            if active["sns"] and key == "{Ctrl}f":
                self.auto.keys.append(key)
            else:
                original(key, **kwargs)
        self.auto.SendKeys = send_keys
        entry = self.auto.main.add(Control(self.auto, "聊天", "ButtonControl", Rect(5, 100, 45, 145)))
        entry.click_action = lambda: active.update(sns=False)
        return entry

    def test_sns_to_private_chat_uses_main_sidebar_before_retrying_search(self):
        self._sns_consumes_search()
        self.assertTrue(self.sender.send_text("Alice", "你好"))
        self.assertEqual(self.auto.clicks[:2], ["聊天", "Alice"])
        self.assertEqual(self.auto.delivered, [("Alice", "你好")])

    def test_ambiguous_sidebar_never_types_into_sns_or_selects_arbitrary_entry(self):
        self._sns_consumes_search()
        self.auto.main.add(Control(self.auto, "聊天", "ButtonControl", Rect(50, 100, 90, 145)))
        self.assertFalse(self.sender.send_text("Alice", "你好"))
        self.assertEqual(self.auto.delivered, [])
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.clip.writes, 0)

    def test_search_provider_repeats_same_accessible_node_only_once(self):
        self.auto.main.children.append(self.auto.search)
        self.assertTrue(self.sender.send_text("Alice", "你好"))
        self.assertEqual(self.auto.delivered, [("Alice", "你好")])

    def test_sns_to_chat_still_preserves_existing_target_draft(self):
        self._sns_consumes_search()
        self.auto.drafts["Alice"] = "用户原草稿"
        self.assertFalse(self.sender.send_text("Alice", "你好"))
        self.assertEqual(self.auto.editor.text, "用户原草稿")
        self.assertEqual(self.auto.delivered, [])

    def test_sidebar_focus_change_does_not_send_shortcut_to_other_application(self):
        entry = self._sns_consumes_search()
        entry.click_action = lambda: setattr(self.auto, "foreground", Control(self.auto, "Other", "WindowControl", pid=20, handle=200))
        self.assertFalse(self.sender.send_text("Alice", "你好"))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 1)
        self.assertEqual(self.auto.delivered, [])

    def test_sns_comment_draft_blocks_navigation_without_clearing_it(self):
        self._sns_consumes_search()
        draft = self.auto.main.add(Control(self.auto, "评论", "EditControl", klass="mmui::TimelineCommentEditor"))
        draft.text = "未发的评论"
        self.assertFalse(self.sender.send_text("Alice", "你好"))
        self.assertEqual(draft.text, "未发的评论")
        self.assertEqual(self.auto.clicks, [])

    def native_identity_header(self):
        identity = self.auto.main.add(Control(self.auto, "微信号：b", "TextControl", Rect(280, 80, 500, 100)))
        populate = self.auto.populate_results
        def populate_with_identity():
            populate()
            for row in self.auto.results.children:
                username = row.children[0].Name.split("：", 1)[1]
                select = row.click_action
                def selected(select=select, username=username):
                    select()
                    identity.Name = "微信号：" + username
                row.click_action = selected
        self.auto.populate_results = populate_with_identity
        return identity

    def test_verified_native_identity_reuses_current_chat_for_stream_sentences(self):
        self.native_identity_header()
        for text in ("第一句。", "第二句。", "第三句。"):
            self.assertTrue(self.sender.send_text("Alice", text, identity={"username": "a"}, stream_sentence=True))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 1)
        self.assertEqual(self.auto.delivered, [("Alice", text) for text in ("第一句。", "第二句。", "第三句。")])
        self.assertIsNone(self.sender._window)
        self.assertIsNone(self.sender._tree_nodes)

    def test_native_fast_path_rechecks_manual_switch_and_target_change(self):
        identity = self.native_identity_header()
        self.assertTrue(self.sender.send_text("Alice", "first", identity={"username": "a"}))
        self.auto.header.Name, identity.Name = "Bob", "微信号：b"
        self.assertTrue(self.sender.send_text("Alice", "after manual switch", identity={"username": "a"}))
        self.assertTrue(self.sender.send_text("Bob", "other target", identity={"username": "b"}))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 3)
        self.assertEqual(self.auto.delivered, [("Alice", "first"), ("Alice", "after manual switch"), ("Bob", "other target")])

    def test_selected_native_target_reuses_plain_title_and_operation_controls(self):
        with patch.object(self.uia, "_walk", wraps=self.uia._walk) as walks, patch.object(self.sender, "_search_results", wraps=self.sender._search_results) as searches:
            self.assertTrue(self.sender.send_text("Alice", "first", identity={"username": "a"}))
            self.assertTrue(self.sender.send_text("Alice", "second", identity={"username": "a"}))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 1)
        main_walks = sum(call.args[0] is self.auto.main for call in walks.call_args_list)
        # Only the first send needs availability, search and opened-chat trees.
        # Search settling is dynamic and counted separately; commit/snapshot reuse.
        self.assertEqual(main_walks - searches.call_count, 3)

    def test_recent_selected_plain_title_reuses_target_across_incoming_turns(self):
        self.assertTrue(self.sender.send_text("Alice", "first", identity={"username":"a"}, stream_sentence=True))
        with patch.object(self.uia,"_walk",wraps=self.uia._walk) as walks:
            self.assertTrue(self.sender.send_text("Alice", "next turn", identity={"username":"a"}, stream_sentence=True))
            self.assertTrue(self.sender.send_text("Alice", "last turn", identity={"username":"a"}, stream_sentence=True))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"),1)
        self.assertFalse(any(call.args[0] is self.auto.main for call in walks.call_args_list))

    def test_other_desktop_input_invalidates_plain_title_selection_proof(self):
        stamp = [10]
        self.sender._input_stamp = lambda: stamp[0]
        self.assertTrue(self.sender.send_text("Alice", "first", identity={"username": "a"}))
        stamp[0] += 1
        self.assertTrue(self.sender.send_text("Alice", "after desktop input", identity={"username": "a"}))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 2)

    def test_same_group_with_moved_editor_does_not_search_again(self):
        identity = {"username": "room@chatroom", "is_group": True}
        self.assertTrue(self.sender.send_text("Alice", "first", identity=identity))
        sibling = self.auto.composer.add(Control(self.auto, kind="PaneControl"))
        self.auto.composer.children.remove(sibling)
        self.auto.composer.children.insert(0, sibling)
        self.assertTrue(self.sender.send_text("Alice", "second", identity=identity))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 1)

    def test_plain_title_selection_proof_cannot_follow_a_replaced_editor(self):
        self.assertTrue(self.sender.send_text("Alice", "first", identity={"username": "a"}))
        old = self.auto.editor
        self.auto.composer.children.remove(old)
        old.parent = None
        self.auto.editor = self.auto.composer.add(Control(self.auto, "输入", "EditControl", Rect(275, 620, 950, 745), "chat_input_field"))
        self.auto.focused = self.auto.editor
        self.assertTrue(self.sender.send_text("Alice", "replacement", identity={"username": "a"}))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 2)

    def test_selection_proof_keeps_no_controls_between_com_apartments(self):
        self.assertTrue(self.sender.send_text("Alice", "first", identity={"username": "a"}))
        def primitive(value):
            if isinstance(value, dict):
                return all(primitive(item) for item in value.values())
            if isinstance(value, (list, tuple)):
                return all(primitive(item) for item in value)
            return value is None or isinstance(value, (str, bool, int))
        self.assertTrue(primitive(self.sender._target_proof))
        self.assertIsNone(self.sender._editor_node)

    def test_control_path_reads_target_id_once_per_level_not_once_per_sibling(self):
        for _ in range(40):
            self.auto.main.add(Control(self.auto, "unrelated"))
        self.sender._window = self.auto.main
        with patch.object(self.auto.header, "GetRuntimeId", wraps=self.auto.header.GetRuntimeId) as identity:
            self.assertEqual(self.sender._control_path(self.auto.header), (2,))
        self.assertEqual(identity.call_count, 3)  # Root comparison, wanted ID, own sibling.

    def test_cancel_after_owned_text_paste_cleans_only_that_unsubmitted_draft(self):
        event=threading.Event();self.auto.on_paste=event.set
        self.assertFalse(self.sender.send_text("Alice","owned pending text",identity={"username":"a"},cancel_event=event))
        self.assertEqual(self.auto.editor.text,"")
        self.assertEqual(self.auto.delivered,[])
        self.assertEqual(self.sender.last_send_result()["draft_cleanup"], "cleared_owned_unsubmitted")

    def test_paste_provider_failure_after_effect_cleans_only_unsubmitted_text(self):
        original = self.auto.SendKeys
        def send(key, **kwargs):
            original(key, **kwargs)
            if key == "{Ctrl}v" and self.auto.focused is self.auto.editor:
                raise OSError("provider failed after paste")
        self.auto.SendKeys = send
        self.assertFalse(self.sender.send_text("Alice", "own paste", identity={"username": "a"}))
        self.assertEqual(self.auto.editor.text, "")
        self.assertEqual(self.auto.delivered, [])
        self.assertEqual(self.sender.last_send_result()["draft_cleanup"], "cleared_owned_unsubmitted")

    def test_text_send_reports_local_search_and_commit_substages(self):
        self.assertTrue(self.sender.send_text("Alice", "hello", identity={"username": "a"}))
        timings = self.sender.last_send_result()["timings"]
        self.assertTrue({"window_discovery_seconds", "contact_search_seconds", "editor_focus_seconds",
                         "draft_precheck_seconds", "paste_seconds", "paste_readback_seconds",
                         "commit_guard_seconds", "enter_accept_seconds"} <= timings.keys())

    def test_cancel_after_paste_preserves_user_changed_draft(self):
        event = threading.Event()
        def changed():
            self.auto.editor.text = "用户刚补的草稿"
            event.set()
        self.auto.on_paste = changed
        self.assertFalse(self.sender.send_text("Alice", "owned pending", identity={"username": "a"}, cancel_event=event))
        self.assertEqual(self.auto.editor.text, "用户刚补的草稿")
        self.assertNotIn("{Ctrl}a{Backspace}", self.auto.keys)

    def test_cancel_after_paste_preserves_draft_when_target_changed(self):
        event = threading.Event()
        def changed():
            self.auto.header.Name = "Bob"
            event.set()
        self.auto.on_paste = changed
        self.assertFalse(self.sender.send_text("Alice", "owned pending", identity={"username": "a"}, cancel_event=event))
        self.assertEqual(self.auto.editor.text, "owned pending")
        self.assertNotIn("{Ctrl}a{Backspace}", self.auto.keys)

    def test_cancel_after_enter_does_not_erase_or_resend_uncertain_commit(self):
        event = threading.Event()
        original = self.auto.SendKeys
        def send(key, **kwargs):
            if key == "{Enter}":
                self.auto.keys.append(key)
                event.set()  # UI accepted key; text clearing was not observed.
            else:
                original(key, **kwargs)
        self.auto.SendKeys = send
        self.assertFalse(self.sender.send_text("Alice", "uncertain", identity={"username": "a"}, cancel_event=event))
        self.assertEqual(self.auto.editor.text, "uncertain")
        self.assertEqual(self.auto.keys.count("{Enter}"), 1)
        self.assertNotIn("{Ctrl}a{Backspace}", self.auto.keys)

    def test_editor_descendant_keyboard_focus_is_the_same_composer(self):
        child=self.auto.editor.add(Control(self.auto,"正文焦点","TextControl"))
        self.auto.GetFocusedControl=lambda:child if self.auto.focused is self.auto.editor else self.auto.focused
        self.assertTrue(self.sender.send_text("Alice","same composer",identity={"username":"a"}))
        self.assertEqual(self.auto.delivered,[("Alice","same composer")])

    def test_live_header_checks_do_not_reread_chat_history_properties(self):
        identity = self.native_identity_header()
        self.auto.header.Name, identity.Name = "Alice", "微信号：a"
        history = self.auto.main.add(Control(self.auto, "聊天记录", "ListControl", Rect(260, 100, 1000, 560)))
        noise = {history.add(Control(self.auto, "旧消息", "TextControl", Rect(280, 200, 700, 230))) for _ in range(300)}
        self.sender._window, self.sender._identity = self.auto.main, {"username": "a"}
        self.sender._require_stable_header = True
        reads = []
        prop = self.uia._prop
        def counted(control, key, default=None):
            if control in noise:
                reads.append(key)
            return prop(control, key, default)
        with patch.object(self.uia, "_prop", side_effect=counted):
            self.assertTrue(self.sender._chat_verified("Alice", self.auto.editor))
            self.assertGreaterEqual(len(reads), 300)
            reads.clear()
            for _ in range(10):
                self.assertTrue(self.sender._chat_verified("Alice", self.auto.editor))
            self.assertEqual(reads, [])
            identity.Name = "微信号：different"
            self.assertFalse(self.sender._chat_verified("Alice", self.auto.editor))
            self.auto.main.children.remove(identity)
            identity.parent = None
            self.assertFalse(self.sender._chat_verified("Alice", self.auto.editor))

    def test_tree_walk_cancellation_does_not_continue_through_history(self):
        cancel = threading.Event()
        children = self.auto.main.GetChildren
        def cancel_during_children():
            cancel.set()
            return children()
        self.auto.main.GetChildren = cancel_during_children
        walked = []
        with self.assertRaises(DesktopCancelled):
            for node, _ in self.uia._walk(self.auto.main, cancelled=cancel.is_set):
                walked.append(node)
        self.assertEqual(walked, [self.auto.main])

    def test_native_fast_path_invalidated_editor_falls_back_to_search(self):
        identity = self.native_identity_header()
        self.auto.header.Name, identity.Name = "Alice", "微信号：a"
        old_editor = self.auto.editor
        activate = self.auto.main.SetActive
        def replace_once():
            activate()
            self.auto.main.SetActive = activate
            old_editor.parent, old_editor.IsOffscreen = None, True
            self.auto.composer.children.remove(old_editor)
            self.auto.editor = self.auto.composer.add(Control(self.auto, "输入", "EditControl", Rect(275, 620, 950, 745), "chat_input_field"))
        self.auto.main.SetActive = replace_once
        self.assertTrue(self.sender.send_text("Alice", "fresh editor", identity={"username": "a"}))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 1)
        self.assertEqual(self.auto.delivered, [("Alice", "fresh editor")])

    def test_native_fast_path_preserves_existing_draft(self):
        identity = self.native_identity_header()
        self.auto.header.Name, identity.Name = "Alice", "微信号：a"
        self.auto.editor.text = self.auto.drafts["Alice"] = "user draft"
        self.assertFalse(self.sender.send_text("Alice", "new body", identity={"username": "a"}))
        self.assertEqual(self.auto.editor.text, "user draft")
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_native_identity_changed_after_fast_paste_cancels_before_enter(self):
        identity = self.native_identity_header()
        for change in ("identity", "removed", "foreground", "cancelled"):
            with self.subTest(change=change):
                self.auto.keys.clear()
                self.auto.editor.text = ""
                self.auto.foreground = self.auto.main
                self.auto.header.Name, identity.Name = "Alice", "微信号：a"
                if identity not in self.auto.main.children:
                    self.auto.main.add(identity)
                cancellation = threading.Event()
                def changed_after_paste():
                    if change == "identity":
                        identity.Name = "微信号：a2"
                    elif change == "removed":
                        self.auto.main.children.remove(identity)
                        identity.parent = None
                    elif change == "foreground":
                        self.auto.foreground = Control(self.auto, "Other app", "WindowControl", pid=20, handle=200)
                    else:
                        cancellation.set()
                self.auto.on_paste = changed_after_paste
                self.assertFalse(self.sender.send_text("Alice", "must stop", identity={"username": "a"}, cancel_event=cancellation))
                self.assertEqual(self.auto.keys.count("{Ctrl}f"), 0)
                self.assertNotIn("{Enter}", self.auto.keys)
                self.assertEqual(self.auto.delivered, [])

    def test_native_fast_path_window_handle_or_pid_drift_never_inputs(self):
        identity = self.native_identity_header()
        self.auto.header.Name, identity.Name = "Alice", "微信号：a"
        activate = self.auto.main.SetActive
        for field, original, changed in (("NativeWindowHandle", 100, 200), ("ProcessId", 10, 20)):
            with self.subTest(field=field):
                self.auto.keys.clear()
                def drift(field=field, changed=changed):
                    activate()
                    setattr(self.auto.main, field, changed)
                self.auto.main.SetActive = drift
                try:
                    self.assertFalse(self.sender.send_text("Alice", "must stop", identity={"username": "a"}))
                    self.assertEqual(self.auto.keys, [])
                    self.assertEqual(self.auto.delivered, [])
                finally:
                    setattr(self.auto.main, field, original)
                    self.auto.main.SetActive = activate

    def test_visible_native_id_contradiction_cannot_fall_back_to_name_only(self):
        identity = self.native_identity_header()
        self.auto.contacts = [("Alice", "a")]
        self.auto.header.Name, identity.Name = "Alice", "微信号：a"
        self.assertFalse(self.sender.send_text("Alice", "wrong person", identity={"username": "a2"}))
        self.assertNotIn("{Enter}", self.auto.keys)
        self.assertEqual(self.auto.delivered, [])

    def test_split_bubbles_use_configured_remaining_interval(self):
        now, sent, waits = [10.0], [], []
        self.cfg.HUMAN_BUBBLE_INTERVAL_SECONDS = 0.25
        self.human.prepare_bubbles=lambda text:["first", "second"]
        self.sender._operation=lambda *args:nullcontext()
        self.sender._cancelled=lambda:False
        self.sender._paste_and_enter=lambda bubble,contact:sent.append((bubble,now[0])) or True
        def wait(started,minimum,cancelled):
            waits.append((started,minimum))
            now[0]=max(now[0],started+minimum)
            return not cancelled()
        self.human.wait_remaining=wait
        with patch.object(self.uia.time,"monotonic",side_effect=lambda:now[0]):
            self.assertTrue(self.sender.send_text("Alice","two bubbles"))
        self.assertEqual(sent,[("first",10.0),("second",10.25)])
        self.assertEqual(waits,[(10.0,0.25)])

    def test_cancel_between_split_bubbles_preserves_truthful_partial_receipt(self):
        cancellation=threading.Event()
        sent=[]
        self.human.prepare_bubbles=lambda text:["first", "second"]
        self.sender._operation=lambda *args:nullcontext()
        self.sender._cancelled=cancellation.is_set
        self.sender._paste_and_enter=lambda bubble,contact:sent.append(bubble) or True
        def wait(started,minimum,cancelled):
            cancellation.set()
            return not cancelled()
        self.human.wait_remaining=wait
        self.assertFalse(self.sender.send_text("Alice","two bubbles",cancel_event=cancellation))
        self.assertEqual(sent,["first"])
        receipt=self.sender.last_send_result()
        self.assertEqual(receipt["status"],"partial")
        self.assertEqual(receipt["sent_parts"],1)
        self.assertEqual(self.clip.data, {13: "用户原剪贴板"})

    def test_duplicate_exact_names_select_first_display_result(self):
        self.auto.contacts = [("Alice", "a"), ("Alice", "a2")]
        self.assertTrue(self.sender.send_text("Alice", "private"))
        self.assertEqual(self.auto.delivered, [("Alice", "private")])
        self.assertEqual(self.auto.clicks, ["Alice"])

    def test_search_username_alone_cannot_verify_duplicate_chat(self):
        self.auto.contacts = [("Alice", "a"), ("Alice", "a2")]
        self.assertFalse(self.sender.send_text("Alice", "correct", identity={"username": "a2"}))
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_visible_chat_username_can_verify_first_duplicate_name(self):
        self.auto.contacts = [("Alice", "a"), ("Alice", "a2")]
        self.auto.main.add(Control(self.auto, "微信号：a", "TextControl", Rect(280, 80, 500, 100)))
        self.assertTrue(self.sender.send_text("Alice", "correct", identity={"username": "a"}))
        self.assertEqual(self.auto.delivered, [("Alice", "correct")])

    def test_native_chat_result_overrides_anonymous_same_name_web_row(self):
        self.sender._window = self.auto.main
        self.auto.search.IsOffscreen = self.auto.results.IsOffscreen = False
        self.auto.search.text = "Alice"
        self.auto.populate_results()
        native = self.auto.results.children[0]
        anonymous = Control(self.auto, "Alice", "ListItemControl", Rect(20, 60, 240, 80))
        self.auto.results.add(anonymous)
        self.assertEqual(self.sender._search_results("Alice"), [native])

    def test_search_child_labels_and_group_member_matches_are_not_contacts(self):
        self.sender._window = self.auto.main
        self.auto.search.IsOffscreen = self.auto.results.IsOffscreen = False
        self.auto.search.text = "Alice"
        self.auto.populate_results()
        native = self.auto.results.children[0]
        native.add(Control(self.auto, "Alice", "PaneControl", aid="MainView.search_item_a.child"))
        group = self.auto.results.add(Control(self.auto, "Group", "ListItemControl", aid="search_item_group"))
        group.add(Control(self.auto, "Alice", "TextControl"))
        self.assertEqual(self.sender._search_results("Alice"), [native])

    def test_first_result_follows_visible_order_and_known_id_still_must_match_header(self):
        self.auto.contacts = [("Alice", "a"), ("Alice", "a2")]
        self.sender._window = self.auto.main
        self.auto.search.IsOffscreen = self.auto.results.IsOffscreen = False
        self.auto.search.text = "Alice"
        self.auto.populate_results()
        first, second = self.auto.results.children
        first.BoundingRectangle = Rect(20, 130, 240, 165)
        second.BoundingRectangle = Rect(20, 90, 240, 125)
        self.assertEqual(self.sender._search_results("Alice"), [second, first])
        self.auto.main.add(Control(self.auto, "微信号：a", "TextControl", Rect(280, 80, 500, 100)))
        self.assertFalse(self.sender.send_text("Alice", "wrong", identity={"username": "a2"}))
        self.assertEqual(self.auto.delivered, [])
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_explicit_contact_and_group_sections_match_requested_chat_type(self):
        self.sender._window = self.auto.main
        self.auto.search.IsOffscreen = self.auto.results.IsOffscreen = False
        self.auto.search.text = "Alice"
        self.auto.populate_results()
        contact = self.auto.results.children[0]
        self.auto.results.Name = "联系人"
        groups = self.auto.main.add(Control(self.auto, "群聊", "ListControl", aid="group_search_results"))
        group = groups.add(Control(self.auto, "Alice", "ListItemControl", aid="search_item_room"))
        self.assertEqual(self.sender._search_results("Alice", {"is_group": False}), [contact])
        self.assertEqual(self.sender._search_results("Alice", {"is_group": True}), [group])

    def test_deep_qt_hierarchy_remains_supported(self):
        original = list(self.auto.main.children)
        self.auto.main.children = []
        pane = self.auto.main
        for _ in range(20):
            pane = pane.add(Control(self.auto))
        for control in original:
            pane.add(control)
        self.assertTrue(self.sender.send_text("Alice", "Qt draft"))
        self.assertEqual(self.auto.delivered, [("Alice", "Qt draft")])

    def test_dotted_qt_ids_and_native_search_class_are_supported(self):
        self.auto.editor.AutomationId = "MainView.splitter.chat_view.chat_input_field"
        self.auto.search.AutomationId = "MainView.left_view.query"
        self.auto.search.ClassName = "mmui::XValidatorTextEdit"
        self.auto.search.Name = ""
        self.auto.results.AutomationId = "MainView.left_view.search_list"
        populate = self.auto.populate_results
        def dotted_results():
            populate()
            for row in self.auto.results.children:
                row.ControlTypeName = "PaneControl"
                row.AutomationId = "MainView.left_view.search_list." + row.AutomationId
        self.auto.populate_results = dotted_results
        # An embedded editor cannot override the verified native composer.
        self.auto.composer.add(Control(self.auto, "", "EditControl", Rect(300, 625, 940, 740), "embedded.web_input"))
        self.assertTrue(self.sender.send_text("Alice", "dotted profile"))
        self.assertEqual(self.auto.delivered, [("Alice", "dotted profile")])

    def test_dotted_ids_match_whole_segments_and_not_lookalikes(self):
        editor = self.auto.editor
        editor.AutomationId = "MainView.not_chat_input_field_suffix"
        self.assertFalse(self.uia._aid_segment(editor, "chat_input_field"))
        editor.AutomationId = "MainView.chat_input_field.child"
        self.assertTrue(self.uia._aid_segment(editor, "chat_input_field"))
        row = Control(self.auto, aid="MainView.fake_search_item_a")
        self.assertFalse(self.uia._aid_segment_prefix(row, "search_item_"))
        row.AutomationId = "MainView.search_item_a.child"
        self.assertTrue(self.uia._aid_segment_prefix(row, "search_item_"))

    def test_qt_legacy_value_reads_draft_instead_of_accessible_chat_name(self):
        self.auto.editor.value_available = False
        self.auto.editor.Name = "Alice按住鼠标 语音输入文字"
        self.auto.editor.GetLegacyIAccessiblePattern = lambda: types.SimpleNamespace(Value=self.auto.editor.text)
        self.assertTrue(self.sender.send_text("Alice", "legacy value"))
        self.assertEqual(self.auto.delivered, [("Alice", "legacy value")])

    def test_qt_empty_value_does_not_hide_live_text_pattern_reply(self):
        self.auto.editor.GetValuePattern = lambda: types.SimpleNamespace(Value="")
        self.auto.editor.GetTextPattern = lambda: types.SimpleNamespace(
            DocumentRange=types.SimpleNamespace(GetText=lambda length: self.auto.editor.text))
        self.assertTrue(self.sender.send_text("Alice", "这条要发出去"))
        self.assertEqual(self.auto.delivered, [("Alice", "这条要发出去")])

    def test_qt_empty_value_does_not_hide_existing_text_pattern_draft(self):
        self.auto.editor.GetValuePattern = lambda: types.SimpleNamespace(Value="")
        self.auto.editor.GetTextPattern = lambda: types.SimpleNamespace(
            DocumentRange=types.SimpleNamespace(GetText=lambda length: self.auto.editor.text))
        self.auto.drafts["Alice"] = "用户原草稿"
        self.assertFalse(self.sender.send_text("Alice", "不可覆盖"))
        self.assertEqual(self.auto.editor.text, "用户原草稿")
        self.assertEqual(self.auto.delivered, [])

    def test_qt_accessible_name_alone_cannot_prove_empty_draft(self):
        self.auto.editor.value_available = False
        self.auto.editor.Name = "Alice按住鼠标 语音输入文字"
        self.auto.drafts["Alice"] = "private user draft"
        self.assertFalse(self.sender.send_text("Alice", "new text"))
        self.assertEqual(self.auto.editor.text, "private user draft")
        self.assertNotIn("{Enter}", self.auto.keys)
        self.assertEqual(self.auto.keys.count("{Ctrl}v"), 1)  # Only the search query was pasted.

    def test_fuzzy_only_result_is_not_sent(self):
        self.auto.contacts = [("Alice Smith", "a")]
        self.assertFalse(self.sender.send_text("Alice", "private"))
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_late_window_launch_and_restart_recovers(self):
        self.auto.launched = False
        self.assertFalse(self.sender.send_text("Alice", "first"))
        self.auto.launched = True
        self.assertTrue(self.sender.send_text("Alice", "second"))
        self.assertIsNone(self.sender._window)
        self.assertEqual(self.auto.delivered, [("Alice", "second")])

    def test_title_spoof_from_another_process_is_rejected(self):
        self.auto.main.ProcessId = 20
        self.assertFalse(self.sender.send_text("Alice", "private"))
        self.assertEqual(self.auto.keys, [])

    def test_main_window_not_popout_or_settings_is_selected(self):
        popup = Control(self.auto, "Alice", "WindowControl", klass="mmui::ChatWindow", handle=101)
        settings = Control(self.auto, "微信设置", "WindowControl", klass="Qt51514QWindowIcon", handle=102)
        self.auto.extra_windows = [popup, settings]
        self.assertTrue(self.sender.send_text("Alice", "main window only"))
        self.assertEqual(self.auto.delivered, [("Alice", "main window only")])
        self.assertIs(self.auto.foreground, self.auto.main)
        self.assertEqual(self.auto.clicks, ["Alice"])  # Fake controls expose no DoubleClick.

    def test_popout_alone_cannot_replace_main_window(self):
        self.auto.main.ClassName = "mmui::ChatWindow"
        self.auto.main.Name = "Alice"
        self.assertFalse(self.sender.send_text("Alice", "private"))
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.clicks, [])

    def test_qt_main_unread_count_requires_exact_title_profile(self):
        self.auto.main.ClassName = "Qt51514QWindowIcon"
        self.auto.main.Name = "微信(3)"
        self.assertTrue(self.sender.send_text("Alice", "main profile"))
        self.auto.main.Name = "第三方微信助手"
        self.auto.keys.clear()
        self.assertFalse(self.sender.send_text("Alice", "private"))
        self.assertEqual(self.auto.keys, [])

    def test_last_result_is_thread_local_and_returned_as_copy(self):
        self.sender._result(False, "text", "Alice", "partial", sent=1, attempted=2)
        observed = []
        def other_request():
            observed.append(self.sender.last_send_result())
            self.sender._result(False, "image", "Bob", "other request")
            observed.append(self.sender.last_send_result())
        worker = threading.Thread(target=other_request)
        worker.start()
        worker.join(2)
        self.assertEqual(observed[0], {})
        self.assertEqual(observed[1]["contact"], "Bob")
        self.assertEqual(observed[1]["result_sequence"], 1)
        result = self.sender.last_send_result()
        self.assertEqual(result["contact"], "Alice")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["result_sequence"], 1)
        result["status"] = "mutated"
        self.assertEqual(self.sender.last_send_result()["status"], "partial")
        self.sender._result(False, "text", "Alice", "cancelled")
        self.assertEqual(self.sender.last_send_result()["result_sequence"], 2)

    def test_inflight_failure_diagnostic_is_not_replaced_by_other_thread(self):
        self.sender._set_error("当前调用草稿不为空")
        thread = threading.Thread(target=lambda: self.sender._result(False, "image", "Bob", "另一请求图片不存在"))
        thread.start()
        thread.join(1)
        self.sender._result(False, "text", "Alice")
        self.assertEqual(self.sender.last_send_result()["error"], "当前调用草稿不为空")

    def test_opaque_weixin_4_tree_is_diagnosed_without_keys(self):
        self.auto.main.children = [Control(self.auto, klass="MMUIRenderSubWindowHW")]
        diagnostic = self.sender.diagnostics()
        self.assertEqual(diagnostic["compatibility"], "controls_unavailable")
        self.assertFalse(diagnostic["ready"])
        self.assertFalse(self.sender.send_text("Alice", "private"))
        self.assertEqual(self.auto.keys, [])

    def _cold_recovery_fixture(self, allowed=True):
        rich = list(self.auto.main.children)
        self.auto.main.children = [Control(self.auto, klass="MMUIRenderSubWindowHW")]
        gate = FakeGate(allowed)
        self.sender._gate_manager = gate
        reads = []
        self.auto.ControlFromHandle = lambda hwnd: reads.append(hwnd) or self.auto.main
        original_keys = self.auto.SendKeys
        def refresh_keys(key, **kwargs):
            original_keys(key, **kwargs)
            if key == "{Tab}" and gate.active:
                self.auto.main.children = rich
        self.auto.SendKeys = refresh_keys
        return gate, reads

    def test_prepare_cold_tree_refreshes_same_main_provider_with_one_tab(self):
        gate, reads = self._cold_recovery_fixture()
        self.assertTrue(self.sender.prepare(generation=7))
        self.assertEqual(gate.ensures, [(100, 10)])
        self.assertEqual(self.auto.keys, ["{Tab}"])
        self.assertGreaterEqual(len(reads), 2)
        self.assertEqual(set(reads), {100})
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.clip.writes, 0)
        self.assertIsNone(self.sender._window)
        self.assertTrue(self.sender._snapshot["ready"])

    def test_title_bar_buttons_cannot_bypass_cold_tree_recovery(self):
        gate, _ = self._cold_recovery_fixture()
        self.auto.main.add(Control(self.auto, "关闭", "ButtonControl"))
        self.assertTrue(self.sender.prepare())
        self.assertEqual(gate.ensures, [(100, 10)])
        self.assertEqual(self.auto.keys, ["{Tab}"])

    def test_unsupported_recovery_does_not_focus_tab_or_enter(self):
        gate, reads = self._cold_recovery_fixture(allowed=False)
        other = Control(self.auto, "Other", "WindowControl", pid=20, handle=200)
        self.auto.foreground = other
        self.assertFalse(self.sender.prepare())
        self.assertEqual(gate.ensures, [(100, 10)])
        self.assertEqual(reads, [])
        self.assertIs(self.auto.foreground, other)
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.clicks, [])

    def test_recovery_provider_identity_change_prevents_tab(self):
        gate, _ = self._cold_recovery_fixture()
        self.auto.ControlFromHandle = lambda hwnd: Control(self.auto, "微信", "WindowControl", klass="mmui::MainWindow", pid=20, handle=100)
        self.assertFalse(self.sender.prepare())
        self.assertEqual(gate.ensures, [(100, 10)])
        self.assertEqual(self.auto.keys, [])

    def test_cancellation_during_gate_check_prevents_tab_refresh(self):
        gate, _ = self._cold_recovery_fixture()
        cancelled = threading.Event()
        gate.on_ensure = cancelled.set
        self.assertFalse(self.sender.prepare(cancel_event=cancelled))
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.clip.writes, 0)

    def test_closed_waiting_prepare_cannot_reactivate_shared_gate(self):
        gate, _ = self._cold_recovery_fixture()
        started, result = threading.Event(), []
        def queued_prepare():
            started.set()
            result.append(self.sender.prepare())
        with desktop_operation():
            thread = threading.Thread(target=queued_prepare)
            thread.start()
            self.assertTrue(started.wait(1))
            self.sender.close()
            thread.join(1)
        self.assertEqual(result, [False])
        self.assertEqual(gate.ensures, [])
        self.assertEqual(gate.closes, 1)
        self.assertEqual(self.auto.keys, [])

    def test_diagnostics_never_activates_recovery_even_when_enabled(self):
        gate, _ = self._cold_recovery_fixture()
        self.assertEqual(self.sender.diagnostics()["gate_recovery"]["enabled"], True)
        self.assertEqual(gate.ensures, [])
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.clicks, [])

    def test_unknown_build_diagnostic_explains_version_without_activating(self):
        gate, _ = self._cold_recovery_fixture()
        gate.snapshot = lambda: {"observed_build": {"file_version": "4.1.99.1"},
                                 "build_check": {"status": "version_mismatch"}}
        result = self.sender.diagnostics()
        self.assertIn("4.1.99.1", result["unavailable_reason"])
        self.assertIn("尚未适配", result["unavailable_reason"])
        self.assertEqual(gate.ensures, [])
        self.assertEqual(self.auto.keys, [])

    def test_active_shared_lease_is_checked_when_controls_are_already_rich(self):
        gate = FakeGate()
        gate.active = True
        self.sender._gate_manager = gate
        self.assertTrue(self.sender.prepare())
        self.assertEqual(gate.ensures, [(100, 10)])
        self.assertEqual(self.auto.keys, [])

    def test_prepare_caller_predicate_cancels_before_com_or_gate(self):
        gate, _ = self._cold_recovery_fixture()
        self.assertFalse(self.sender.prepare(cancelled=lambda: True))
        self.assertEqual(gate.ensures, [])
        self.assertEqual(self.auto.com_threads, [])

    def _expired_real_manager_fixture(self, *, delayed_watchdog=False):
        # Real lease/manager state transitions, with synthetic process bytes and
        # deterministic timers. Never opens a native process or touches WeChat.
        from tests.test_uia_recovery import ControlledTimers, FakeProcess
        import uia_recovery
        rich = list(self.auto.main.children)
        self.auto.main.children = [Control(self.auto, klass="MMUIRenderSubWindowHW")]
        now, timers, adapters = [0.0], ControlledTimers(), []
        def factory(*_args, **_kwargs):
            adapter = FakeProcess()
            adapter.current_identity['pid'] = 10
            adapters.append(adapter)
            return adapter
        manager = uia_recovery.GateRecoveryManager(True, factory, lambda hwnd: 10,
                                                  lease_seconds=10, clock=lambda: now[0],
                                                  timer_factory=timers)
        self.addCleanup(manager.close)
        self.assertTrue(manager.ensure(100, 10))
        now[0] = 11.0
        if not delayed_watchdog:
            timers.items[0].fire()
        self.sender._gate_manager = manager
        self.auto.ControlFromHandle = lambda hwnd: self.auto.main
        keys = self.auto.SendKeys
        def initialized(key, **kwargs):
            keys(key, **kwargs)
            if key == '{Tab}' and manager.snapshot()['active']:
                self.auto.main.children = rich
        self.auto.SendKeys = initialized
        return manager, timers, adapters

    def test_first_prepare_after_restored_expiry_starts_one_fresh_lease(self):
        manager, timers, adapters = self._expired_real_manager_fixture()
        self.assertTrue(self.sender.prepare(generation=7))
        self.assertEqual(len(adapters), 2)
        self.assertEqual([value for _, value in adapters[0].writes], [1, 0])
        self.assertEqual([value for _, value in adapters[1].writes], [1])
        self.assertEqual(self.auto.keys, ['{Tab}'])
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.clip.writes, 0)
        self.assertTrue(manager.snapshot()['active'])
        timers.items[0].fire(force=True)
        self.assertEqual([value for _, value in adapters[1].writes], [1])

    def test_delayed_watchdog_expiry_is_finished_before_first_chat_send(self):
        manager, _, adapters = self._expired_real_manager_fixture(delayed_watchdog=True)
        self.assertTrue(self.sender.send_text('Alice', '你好', generation=7))
        self.assertEqual(self.auto.delivered, [('Alice', '你好')])
        self.assertEqual(self.auto.keys.count('{Enter}'), 1)
        self.assertEqual([value for _, value in adapters[0].writes], [1, 0])
        self.assertEqual([value for _, value in adapters[1].writes], [1])
        self.assertTrue(manager.snapshot()['active'])

    def test_expiry_recovery_still_preserves_existing_draft(self):
        self._expired_real_manager_fixture()
        self.auto.drafts['Alice'] = '用户未写完的草稿'
        self.assertFalse(self.sender.send_text('Alice', '不能覆盖', generation=7))
        self.assertEqual(self.auto.editor.text, '用户未写完的草稿')
        self.assertEqual(self.auto.delivered, [])
        self.assertNotIn('{Enter}', self.auto.keys)

    def test_expired_recovery_cancellation_and_changed_window_prevent_new_lease(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                manager, _, adapters = self._expired_real_manager_fixture()
                original = manager.ensure
                event = threading.Event()
                def first(hwnd, pid):
                    result = original(hwnd, pid)
                    if cancelled:
                        event.set()
                    else:
                        self.auto.ControlFromHandle = lambda _: Control(
                            self.auto, '微信', 'WindowControl', klass='mmui::MainWindow', pid=20, handle=100)
                    return result
                manager.ensure = first
                self.assertFalse(self.sender.prepare(cancel_event=event, generation=7))
                self.assertEqual(len(adapters), 1)
                self.assertEqual(self.auto.keys, [])
                self.assertEqual(self.clip.writes, 0)

    def test_unsafe_expiry_states_never_trigger_a_second_ensure(self):
        import copy
        safe_before = {'enabled': True, 'busy': False, 'active': False, 'status': 'lease_expired',
                       'build_check': {'status': 'matched'},
                       'lease': {'expired': True, 'restored': True, 'remaining_seconds': 0, 'watchdog_error': ''}}
        safe_after = {'enabled': True, 'busy': False, 'active': False, 'status': 'lease_expired',
                      'build_check': {'status': 'matched'}, 'lease': None,
                      'restore_result': {'ok': True, 'status': 'already_restored'}}
        cases = [
            ('before', ('busy',), True), ('before', ('enabled',), False),
            ('before', ('status',), 'already_active_external'),
            ('before', ('lease', 'restored'), False),
            ('before', ('lease', 'watchdog_error'), 'restore_error'),
            ('before', ('build_check', 'status'), 'sha256_mismatch'),
            ('after', ('busy',), True), ('after', ('active',), True),
            ('after', ('lease',), {}), ('after', ('status',), 'unavailable'),
            ('after', ('restore_result', 'ok'), False),
            ('after', ('restore_result', 'status'), 'process_exited'),
            ('after', ('build_check', 'status'), 'version_mismatch'),
        ]
        for target, keys, value in cases:
            with self.subTest(target=target, keys=keys, value=value):
                before, after = copy.deepcopy(safe_before), copy.deepcopy(safe_after)
                mapping = before if target == 'before' else after
                for key in keys[:-1]:
                    mapping = mapping[key]
                mapping[keys[-1]] = value
                class UnsafeGate:
                    calls = 0
                    def snapshot(inner):
                        return before if not inner.calls else after
                    def ensure(inner, hwnd, pid):
                        inner.calls += 1
                        return False
                gate = UnsafeGate()
                self.sender._gate_manager = gate
                self.sender._window = self.auto.main
                self.assertFalse(self.sender._ensure_recovery_gate(100, 10))
                self.assertEqual(gate.calls, 1)
                self.assertEqual(self.auto.keys, [])
                self.assertEqual(self.clip.writes, 0)

    def test_second_ensure_failure_does_not_start_a_third_attempt(self):
        manager, _, adapters = self._expired_real_manager_fixture()
        original, attempts = manager.ensure, []
        def refused(hwnd, pid):
            attempts.append((hwnd, pid))
            return original(hwnd, pid) if len(attempts) == 1 else False
        manager.ensure = refused
        self.assertFalse(self.sender.prepare(generation=7))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(adapters), 1)
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.clip.writes, 0)

    def test_external_activation_during_fresh_ensure_cannot_license_input(self):
        manager, _, adapters = self._expired_real_manager_fixture()
        original, attempts = manager.ensure, []
        def external(hwnd, pid):
            attempts.append((hwnd, pid))
            if len(attempts) == 1:
                return original(hwnd, pid)
            manager._status = 'already_active_external'
            return True
        manager.ensure = external
        self.assertFalse(self.sender.prepare(generation=7))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(adapters), 1)
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.clip.writes, 0)

    def test_preexisting_draft_is_preserved(self):
        self.auto.drafts["Alice"] = "用户正在写的草稿"
        self.assertFalse(self.sender.send_text("Alice", "new text"))
        self.assertEqual(self.auto.editor.text, "用户正在写的草稿")
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_chat_header_change_after_paste_aborts(self):
        self.auto.on_paste = lambda: setattr(self.auto.header, "Name", "Bob")
        self.assertFalse(self.sender.send_text("Alice", "private"))
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_native_chat_title_overrides_same_name_in_message_history(self):
        self.auto.header.AutomationId = "MainView.chat_view.current_chat_name_label"
        self.auto.main.add(Control(self.auto, "Alice", "TextControl", Rect(280, 110, 500, 140)))
        self.auto.on_paste = lambda: setattr(self.auto.header, "Name", "Bob")
        self.assertFalse(self.sender.send_text("Alice", "private"))
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_native_chat_title_still_requires_visible_stable_id_for_duplicates(self):
        self.auto.header.AutomationId = "MainView.current_chat_name_label"
        self.auto.contacts = [("Alice", "a"), ("Alice", "a2")]
        self.auto.main.add(Control(self.auto, "微信号：a", "TextControl", Rect(280, 80, 500, 100)))
        self.assertTrue(self.sender.send_text("Alice", "correct", identity={"username": "a"}))
        self.assertEqual(self.auto.delivered, [("Alice", "correct")])

    def test_conflicting_native_chat_titles_never_confirm_target(self):
        self.auto.header.AutomationId = "MainView.current_chat_name_label"
        self.auto.main.add(Control(self.auto, "Bob", "TextControl", Rect(280, 45, 500, 75), "OtherView.current_chat_name_label"))
        self.assertFalse(self.sender.send_text("Alice", "private"))
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_same_name_native_titles_are_distinct_controls_and_ambiguous(self):
        self.auto.header.AutomationId = "MainView.current_chat_name_label"
        self.auto.main.add(Control(self.auto, "Alice", "TextControl", Rect(280, 45, 500, 75), "OtherView.current_chat_name_label"))
        self.assertFalse(self.sender.send_text("Alice", "private"))
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_history_message_cannot_supply_duplicate_name_identity_proof(self):
        self.auto.header.AutomationId = "MainView.current_chat_name_label"
        self.auto.contacts = [("Alice", "a"), ("Alice", "a2")]
        history = self.auto.main.add(Control(self.auto, "聊天记录", "ListControl", Rect(260, 100, 1000, 560), "MainView.chat_view.chat_history_list"))
        history.add(Control(self.auto, "微信号：a2", "TextControl", Rect(280, 110, 500, 140)))
        self.assertFalse(self.sender.send_text("Alice", "private", identity={"username": "a2"}))
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_focus_change_after_paste_aborts(self):
        self.auto.on_paste = lambda: setattr(self.auto, "focused", self.auto.search)
        self.assertFalse(self.sender.send_text("Alice", "private"))
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_cancelled_before_send_has_no_ui_input(self):
        cancelled = threading.Event()
        cancelled.set()
        self.assertFalse(self.sender.send_text("Alice", "private", cancel_event=cancelled))
        self.assertEqual(self.auto.keys, [])

    def test_cancelled_after_paste_never_enters(self):
        cancelled = threading.Event()
        self.auto.on_paste = cancelled.set
        self.assertFalse(self.sender.send_text("Alice", "private", cancel_event=cancelled))
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_old_generation_never_enters(self):
        self.assertFalse(self.sender.send_text("Alice", "private", generation=6))
        self.assertEqual(self.auto.keys, [])

    def test_partial_bubbles_are_reported_as_failure(self):
        sys.modules["humanize"].prepare_bubbles = lambda text: ["one", "two"]
        self.auto.on_enter = lambda: setattr(self.auto.header, "Name", "Bob")
        self.assertFalse(self.sender.send_text("Alice", "ignored"))
        self.assertEqual(self.auto.delivered, [("Alice", "one")])
        self.assertEqual(self.sender._last_send["status"], "partial")
        self.assertEqual(self.sender._last_send["sent_parts"], 1)

    def test_no_readback_means_no_enter(self):
        self.auto.on_paste = lambda: setattr(self.auto.editor, "text", "unexpected")
        self.assertFalse(self.sender.send_text("Alice", "expected"))
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_image_requires_preview_and_clears_it(self):
        self.assertTrue(self.sender.send_image("Alice", str(__file__)))
        self.assertEqual(len(self.auto.delivered), 1)
        self.assertEqual(self.clip.data, {13: "用户原剪贴板"})

    def test_image_and_following_text_reuse_same_target_after_own_input(self):
        stamp = [10]
        self.sender._input_stamp = lambda: stamp[0]
        original = self.auto.SendKeys
        def send(key, **kwargs):
            stamp[0] += 1
            original(key, **kwargs)
        self.auto.SendKeys = send
        identity = {"username": "a"}
        self.assertTrue(self.sender.send_image("Alice", __file__, identity=identity))
        self.assertTrue(self.sender.send_image("Alice", __file__, identity=identity))
        self.assertTrue(self.sender.send_text("Alice", "next turn", identity=identity))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 1)

    def test_image_commit_rebuilt_header_and_editor_keep_current_target_without_search(self):
        def rebuilt():
            old_editor, old_header = self.auto.editor, self.auto.header
            self.auto.composer.children.remove(old_editor)
            self.auto.main.children.remove(old_header)
            old_editor.parent = old_header.parent = None
            self.auto.header = Control(self.auto, "Alice", "TextControl", Rect(280, 45, 500, 75))
            self.auto.main.children.insert(2, self.auto.header)
            self.auto.header.parent = self.auto.main
            self.auto.editor = self.auto.composer.add(Control(self.auto, "输入", "EditControl", Rect(275, 620, 950, 745), "chat_input_field"))
            self.auto.focused = self.auto.editor
            self.auto.on_enter = None
        self.auto.on_enter = rebuilt
        identity = {"username": "a"}
        self.assertTrue(self.sender.send_image("Alice", __file__, identity=identity))
        searches = self.auto.keys.count("{Ctrl}f")
        original = self.auto.SendKeys
        def no_more_search(key, **kwargs):
            if key == "{Ctrl}f":
                self.fail("Current verified image target should not be searched again")
            return original(key, **kwargs)
        self.auto.SendKeys = no_more_search
        self.assertTrue(self.sender.send_text("Alice", "after image", identity=identity))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), searches)

    def test_image_commit_replaces_controls_even_when_old_wrappers_remain_readable(self):
        def rebuilt():
            # Qt can leave old wrappers readable briefly, with stale parents.
            self.auto.main.children.remove(self.auto.header)
            self.auto.header = Control(self.auto, "Alice", "TextControl", Rect(280, 45, 500, 75))
            self.auto.main.children.insert(2, self.auto.header)
            self.auto.header.parent = self.auto.main
            self.auto.editor = Control(self.auto, "输入", "EditControl", Rect(275, 620, 950, 745), "chat_input_field")
            self.auto.composer.children = []
            self.auto.composer.add(self.auto.editor)
            self.auto.focused = self.auto.editor
            self.auto.on_enter = None
        self.auto.on_enter = rebuilt
        identity = {"username": "a"}
        self.assertTrue(self.sender.send_image("Alice", __file__, identity=identity))
        self.assertTrue(self.sender.send_text("Alice", "new nodes", identity=identity))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 1)

    def test_image_preview_focus_change_never_commits_or_clears_user_input(self):
        self.auto.on_paste = lambda: setattr(self.auto, "focused", self.auto.search)
        self.assertFalse(self.sender.send_image("Alice", __file__, identity={"username": "a"}))
        self.assertNotIn("{Enter}", self.auto.keys)
        self.assertNotIn("{Ctrl}a{Backspace}", self.auto.keys)
        self.assertEqual(self.auto.delivered, [])

    def test_cancelled_owned_image_preview_is_removed_and_next_text_can_send(self):
        event = threading.Event()
        self.auto.on_paste = event.set
        self.assertFalse(self.sender.send_image("Alice", __file__, identity={"username": "a"}, cancel_event=event))
        self.assertNotIn("{Enter}", self.auto.keys)
        self.assertIn("{Ctrl}a{Backspace}", self.auto.keys)
        self.assertFalse(any(control.Name == "图片预览" for control in self.auto.composer.children))
        self.assertEqual(self.sender.last_send_result()["draft_cleanup"], "cleared_owned_unsubmitted_image")
        self.auto.on_paste = None
        self.assertTrue(self.sender.send_text("Alice", "new turn", identity={"username": "a"}))

    def test_image_provider_error_after_paste_cleans_owned_preview(self):
        stamp = [1]
        self.sender._input_stamp = lambda: stamp[0]
        original = self.auto.SendKeys
        def send(key, **kwargs):
            original(key, **kwargs)
            stamp[0] += 1  # Our injected keys also change Win32 LastInputInfo.
            if key == "{Ctrl}v" and self.auto.focused is self.auto.editor:
                self.auto.composer.children[-1].Name = pathlib.Path(__file__).name
                raise OSError("provider failed after image insertion")
        self.auto.SendKeys = send
        self.assertFalse(self.sender.send_image("Alice", __file__, identity={"username": "a"}))
        self.assertEqual(self.auto.composer.children, [self.auto.editor])
        self.assertEqual(self.sender.last_send_result()["draft_cleanup"], "cleared_owned_unsubmitted_image")

    def test_image_input_change_before_first_observation_requires_explicit_file_marker(self):
        for explicit_file in (False, True):
            with self.subTest(explicit_file=explicit_file):
                self.auto.keys.clear()
                self.auto.editor.text = ""
                self.auto.composer.children = [self.auto.editor]
                stamp = [1]
                self.sender._input_stamp = lambda: stamp[0]
                event = threading.Event()
                def replaced_before_observation():
                    # A drag/drop can replace the single preview without
                    # modifying the clipboard or moving editor focus.
                    self.auto.composer.children = [self.auto.editor]
                    name = pathlib.Path(__file__).name if explicit_file else "图片预览"
                    self.auto.composer.add(Control(self.auto, name, "ImageControl", Rect(280, 590, 360, 635)))
                    stamp[0] += 1
                    event.set()
                self.auto.on_paste = replaced_before_observation
                self.assertFalse(self.sender.send_image("Alice", __file__, identity={"username": "a"}, cancel_event=event))
                if explicit_file:
                    self.assertEqual(self.auto.composer.children, [self.auto.editor])
                    self.assertIn("{Ctrl}a{Backspace}", self.auto.keys)
                else:
                    self.assertEqual(len(self.auto.composer.children), 2)
                    self.assertNotIn("{Ctrl}a{Backspace}", self.auto.keys)
                    self.assertEqual(self.sender.last_send_result()["draft_cleanup"], "preserved_image_draft_manual_check_required")

    def test_cancelled_image_with_user_caption_or_multiple_previews_is_preserved(self):
        for change in ("caption", "another_image", "no_runtime_id", "clipboard"):
            with self.subTest(change=change):
                self.auto.keys.clear()
                self.auto.editor.text = ""
                self.auto.composer.children = [self.auto.editor]
                event = threading.Event()
                def changed():
                    if change == "caption":
                        self.auto.editor.text = "用户新草稿"
                    elif change == "another_image":
                        self.auto.composer.add(Control(self.auto, "图片预览", "ImageControl", Rect(280, 590, 360, 635)))
                    elif change == "no_runtime_id":
                        self.auto.composer.children[-1].GetRuntimeId = lambda: []
                    else:
                        self.clip.seq += 1
                    event.set()
                self.auto.on_paste = changed
                self.assertFalse(self.sender.send_image("Alice", __file__, identity={"username": "a"}, cancel_event=event))
                self.assertNotIn("{Ctrl}a{Backspace}", self.auto.keys)
                self.assertTrue(any(control.Name == "图片预览" for control in self.auto.composer.children))

    def test_image_live_preview_replacement_or_external_input_after_observation_is_preserved(self):
        for change in ("preview", "input", "focus", "target"):
            with self.subTest(change=change):
                self.auto.keys.clear()
                self.auto.editor.text = ""
                self.auto.composer.children = [self.auto.editor]
                self.auto.focused = self.auto.editor
                self.auto.header.Name = "Alice"
                stamp = [10]
                self.sender._input_stamp = lambda: stamp[0]
                owned = self.sender._owned_image_draft
                observed = [False]
                def capture(*args):
                    proof = owned(*args)
                    if proof and not observed[0]:
                        observed[0] = True
                        if change == "preview":
                            self.auto.composer.children = [self.auto.editor]
                            self.auto.composer.add(Control(self.auto, "图片预览", "ImageControl", Rect(280, 590, 360, 635)))
                        elif change == "input":
                            stamp[0] += 1
                        elif change == "focus":
                            self.auto.focused = self.auto.search
                        else:
                            self.auto.header.Name = "Bob"
                    return proof
                with patch.object(self.sender, "_owned_image_draft", side_effect=capture):
                    self.assertFalse(self.sender.send_image("Alice", __file__, identity={"username": "a"}))
                self.assertNotIn("{Enter}", self.auto.keys)
                self.assertNotIn("{Ctrl}a{Backspace}", self.auto.keys)
                self.assertTrue(any(control.Name == "图片预览" for control in self.auto.composer.children))

    def test_image_enter_attempt_then_cancel_never_clears_uncertain_submission(self):
        event = threading.Event()
        original = self.auto.SendKeys
        def send(key, **kwargs):
            if key == "{Enter}":
                self.auto.keys.append(key)
                event.set()
            else:
                original(key, **kwargs)
        self.auto.SendKeys = send
        self.assertFalse(self.sender.send_image("Alice", __file__, identity={"username": "a"}, cancel_event=event))
        self.assertEqual(self.auto.keys.count("{Enter}"), 1)
        self.assertNotIn("{Ctrl}a{Backspace}", self.auto.keys)

    def test_static_emoji_and_toolbar_icons_are_not_attachments(self):
        toolbar = self.auto.composer.add(Control(self.auto, "工具栏", "ToolBarControl", Rect(275, 592, 950, 620)))
        toolbar.add(Control(self.auto, "表情", "ImageControl", Rect(280, 598, 310, 618)))
        button = self.auto.composer.add(Control(self.auto, "截图", "ButtonControl", Rect(315, 615, 345, 645)))
        button.add(Control(self.auto, "截图图标", "ImageControl", Rect(318, 620, 340, 640)))
        self.auto.composer.add(Control(self.auto, "静态图标", "ImageControl", Rect(360, 620, 390, 650)))
        self.assertTrue(self.sender.send_text("Alice", "text with toolbar"))
        self.assertEqual(self.auto.delivered, [("Alice", "text with toolbar")])

    def test_diagnostics_are_read_only_and_com_thread_local(self):
        foreground, focused, clipboard = self.auto.foreground, self.auto.focused, dict(self.clip.data)
        results = []
        thread = threading.Thread(target=lambda: results.append(self.sender.diagnostics()))
        thread.start()
        thread.join(2)
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.clicks, [])
        self.assertIs(self.auto.foreground, foreground)
        self.assertIs(self.auto.focused, focused)
        self.assertEqual(self.clip.data, clipboard)
        self.assertIn(thread.ident, self.auto.com_threads)
        self.assertIsNone(self.sender._window)

    def _voice_fixture(self, cancel_event=None):
        audio = sys.modules["audio_helper"]
        audio.has_cable_device = audio.has_cable_recording_device = lambda: True
        audio.list_playback_devices = lambda **k: [{"id": "cable_play", "name": "CABLE Input"}]
        audio.get_default_playback_device_info = lambda: {"id": "original_play"}
        audio.get_default_recording_device_info = lambda: {"id": "original_record"}
        audio.list_recording_devices = lambda **k: [{"id": "cable_rec", "name": "CABLE Output"}]
        audio.set_default_playback_device = lambda **k: {"id": "cable_play"}
        audio.set_default_recording_device = lambda **k: {"id": "cable_rec"}
        self.restored = []
        audio.restore_default_playback_device = lambda device, **k: self.restored.append(("play", device)) or True
        audio.restore_default_recording_device = lambda device, **k: self.restored.append(("record", device)) or True
        self.cfg.VOICE_HOLD_MS = self.cfg.VOICE_EXTRA_PADDING_MS = 0
        self.sound_calls = []
        def play():
            self.sound_calls.append("play")
            if cancel_event:
                cancel_event.set()
        sound = types.SimpleNamespace(get_length=lambda: 0.015, play=play,
                                      stop=lambda: self.sound_calls.append("stop"))
        mixer = types.SimpleNamespace(get_init=lambda: False, init=lambda: None,
                                      Sound=lambda path: sound, find_channel=lambda: types.SimpleNamespace(play=lambda clip: clip.play()),
                                      quit=lambda: self.sound_calls.append("quit"))
        self.pygame_patch = patch.dict(sys.modules, {"pygame": types.SimpleNamespace(mixer=mixer)})
        self.pygame_patch.start()
        self.addCleanup(self.pygame_patch.stop)
        button = self.auto.main.add(Control(self.auto, "按住说话", "ButtonControl", Rect(275, 600, 400, 635)))
        self.sender._mouse_down = lambda *args: setattr(button, "Name", "松开发送")
        self.sender._mouse_up = lambda *args: setattr(button, "Name", "按住说话")
        return button

    def test_voice_uses_observed_recording_and_restores_devices(self):
        self._voice_fixture()
        self.assertTrue(self.sender.send_voice("Alice", __file__))
        self.assertIn("play", self.sound_calls)
        self.assertIn("quit", self.sound_calls)
        self.assertEqual(self.restored, [("play", "original_play"), ("record", "original_record")])

    def test_cancelled_own_text_draft_does_not_block_the_next_voice(self):
        event = threading.Event()
        self.auto.on_paste = event.set
        self.assertFalse(self.sender.send_text("Alice", "expired", identity={"username": "a"}, cancel_event=event))
        self.auto.on_paste = None
        self._voice_fixture()
        self.assertTrue(self.sender.send_voice("Alice", __file__, identity={"username": "a"}))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 1)
        self.assertEqual(self.auto.editor.text, "")

    def test_voice_device_role_change_reacquires_replaced_editor_and_descendant_focus(self):
        self._voice_fixture()
        audio = sys.modules["audio_helper"]
        def changed(**unused):
            old = self.auto.editor
            self.auto.composer.children.remove(old)
            old.parent = None
            self.auto.editor = self.auto.composer.add(Control(self.auto, "输入", "EditControl", Rect(275, 620, 950, 745), "chat_input_field"))
            child = self.auto.editor.add(Control(self.auto, "正文", "TextControl"))
            self.auto.focused = self.auto.main
            self.auto.GetFocusedControl = lambda: child if self.auto.focused is self.auto.editor else self.auto.focused
        audio.set_default_recording_device = changed
        self.assertTrue(self.sender.send_voice("Alice", __file__, identity={"username": "a"}))
        self.assertIn("play", self.sound_calls)
        self.assertIn("voice_editor_refresh_seconds", self.sender.last_send_result()["timings"])

    def test_voice_device_role_change_cannot_hide_user_draft_or_changed_target(self):
        self._voice_fixture()
        audio = sys.modules["audio_helper"]
        def changed(**unused):
            self.auto.editor.text = "用户草稿"
        audio.set_default_recording_device = changed
        self.assertFalse(self.sender.send_voice("Alice", __file__, identity={"username": "a"}))
        self.assertIn("已有草稿", self.sender.last_send_result()["error"])
        self.assertEqual(self.auto.editor.text, "用户草稿")
        self.assertNotIn("play", self.sound_calls)

    def test_voice_device_rebuild_refreshes_only_when_old_readable_editor_cannot_focus(self):
        self._voice_fixture()
        def changed(**unused):
            old = self.auto.editor
            self.auto.editor = Control(self.auto, "输入", "EditControl", Rect(275, 620, 950, 745), "chat_input_field")
            self.auto.composer.children = []
            self.auto.composer.add(self.auto.editor)
            self.auto.focused = self.auto.editor
            old.SetFocus = lambda: setattr(self.auto, "focused", self.auto.editor)
            old.Click = lambda **kwargs: setattr(self.auto, "focused", self.auto.editor)
        sys.modules["audio_helper"].set_default_recording_device = changed
        self.assertTrue(self.sender.send_voice("Alice", __file__, identity={"username": "a"}))
        self.assertIn("play", self.sound_calls)

    def test_voice_unreadable_editor_has_specific_error_without_recording(self):
        self._voice_fixture()
        self.auto.editor.value_available = False
        self.assertFalse(self.sender.send_voice("Alice", __file__, identity={"username": "a"}))
        self.assertIn("无法读取编辑器内容", self.sender.last_send_result()["error"])
        self.assertNotIn("play", self.sound_calls)

    def test_voice_player_is_prepared_before_hold_and_starts_after_live_confirmation(self):
        button = self._voice_fixture()
        self.sender._operation = lambda *args: nullcontext()
        self.sender._window = self.auto.main
        self.sender._send_window = (self.auto.main.NativeWindowHandle, self.auto.main.ProcessId)
        self.sender._cancelled = lambda: False
        self.auto.header.Name = "Alice"
        now, events = [20.0], []
        mixer = sys.modules["pygame"].mixer
        sound = mixer.Sound(__file__)
        def mark(name, elapsed=0):
            events.append((name, now[0]))
            now[0] += elapsed
        mixer.init = lambda: mark("init", 2.0)
        mixer.Sound = lambda path: (mark("decode", 1.0), sound)[1]
        mixer.find_channel = lambda: (mark("channel", .1), types.SimpleNamespace(play=lambda clip: (mark("play"), clip.play())))[1]
        def press(*unused):
            mark("hold")
            button.Name = "松开发送"
        self.sender._mouse_down = press
        observe = self.sender._voice_recording_state
        def observed():
            value = observe()
            if value is not None:
                mark("observed", .03)
            return value
        self.sender._voice_recording_state = observed
        verify = self.sender._chat_verified
        def verified(*args):
            result = verify(*args)
            if button.Name == "松开发送":
                now[0] += .02
            return result
        self.sender._chat_verified = verified
        with patch.object(self.uia.time, "monotonic", side_effect=lambda: now[0]), patch.object(self.uia.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            self.assertTrue(self.sender.send_voice("Alice", __file__))
        order = [name for name, _ in events]
        self.assertEqual(order[:6], ["init", "decode", "channel", "hold", "observed", "play"])
        at = dict(events)
        self.assertAlmostEqual(at["hold"] - at["init"], 3.1)
        self.assertAlmostEqual(at["play"] - at["hold"], .03)
        self.assertAlmostEqual(self.sender.last_send_result()["timings"]["gesture_to_play_seconds"], .03)
        self.assertEqual(self.restored, [("play", "original_play"), ("record", "original_record")])

    def test_voice_no_available_playback_channel_never_starts_recording(self):
        self._voice_fixture()
        sys.modules["pygame"].mixer.find_channel = lambda: None
        self.sender._mouse_down = lambda *a: self.fail("Channel must be ready before recording")
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertNotIn("play", self.sound_calls)
        self.assertIn("quit", self.sound_calls)
        self.assertEqual(len(self.restored), 2)

    def test_voice_accepted_with_cleanup_failure_is_truthful_and_warns(self):
        self._voice_fixture()
        sys.modules["audio_helper"].restore_default_playback_device = lambda *args, **kwargs: False
        self.assertTrue(self.sender.send_voice("Alice", __file__))
        result = self.sender.last_send_result()
        self.assertEqual(result["status"], "accepted_by_ui")
        self.assertEqual(len(result["cleanup_warnings"]), 1)
        self.assertIn("播放设备恢复失败", self.sender._last_error)
        self.assertIn(("record", "original_record"), self.restored)
        result["cleanup_warnings"].clear()
        self.assertEqual(len(self.sender.last_send_result()["cleanup_warnings"]), 1)

    def test_playback_restore_exception_still_attempts_recording_restore(self):
        self._voice_fixture()
        def failed(*args, **kwargs):
            raise OSError("device removed")
        sys.modules["audio_helper"].restore_default_playback_device = failed
        self.assertTrue(self.sender.send_voice("Alice", __file__))
        self.assertIn(("record", "original_record"), self.restored)
        self.assertIn("device removed", self.sender.last_send_result()["cleanup_warnings"][0])

    def test_voice_cancel_stops_playback_releases_hold_and_restores(self):
        cancel = threading.Event()
        button = self._voice_fixture(cancel)
        self.assertFalse(self.sender.send_voice("Alice", __file__, cancel_event=cancel))
        self.assertEqual(button.Name, "按住说话")
        self.assertIn("{Esc}", self.auto.keys)
        self.assertIn("stop", self.sound_calls)
        self.assertEqual(len(self.restored), 2)

    def test_voice_audio_endpoints_are_enumerated_once_per_send(self):
        self._voice_fixture()
        audio = sys.modules["audio_helper"]
        with patch.object(audio, "list_playback_devices", wraps=audio.list_playback_devices) as outputs, patch.object(audio, "list_recording_devices", wraps=audio.list_recording_devices) as inputs, patch.object(audio, "set_default_playback_device", wraps=audio.set_default_playback_device) as switch_play, patch.object(audio, "set_default_recording_device", wraps=audio.set_default_recording_device) as switch_record:
            self.assertTrue(self.sender.send_voice("Alice", __file__))
        outputs.assert_called_once_with(include_default=False)
        inputs.assert_called_once_with(include_default=False)
        self.assertEqual(switch_play.call_args.kwargs["devices"], [{"id": "cable_play", "name": "CABLE Input"}])
        self.assertEqual(switch_record.call_args.kwargs["devices"], [{"id": "cable_rec", "name": "CABLE Output"}])
        timings = self.sender.last_send_result()["timings"]
        self.assertTrue({"target_seconds", "audio_prepare_seconds", "voice_playback_seconds", "audio_restore_seconds", "uia_total_seconds"} <= timings.keys())

    def test_voice_cancel_during_endpoint_scan_avoids_device_switch_and_recording(self):
        cancel = threading.Event()
        self._voice_fixture()
        audio = sys.modules["audio_helper"]
        def enumerate_and_cancel(**kwargs):
            cancel.set()
            return [{"id": "cable_play", "name": "CABLE Input"}]
        audio.list_playback_devices = enumerate_and_cancel
        audio.list_recording_devices = lambda **k: self.fail("Expired voice must stop before the next scan")
        audio.set_default_playback_device = lambda **k: self.fail("Expired voice must not switch devices")
        self.assertFalse(self.sender.send_voice("Alice", __file__, cancel_event=cancel))
        self.assertEqual(self.restored, [])
        self.assertNotIn("play", self.sound_calls)

    def test_voice_cancel_after_playback_switch_restores_it_without_switching_capture(self):
        cancel = threading.Event()
        self._voice_fixture()
        audio = sys.modules["audio_helper"]
        def switch_and_cancel(**kwargs):
            cancel.set()
            return {"id": "cable_play"}
        audio.set_default_playback_device = switch_and_cancel
        audio.set_default_recording_device = lambda **k: self.fail("Expired voice must not switch capture")
        self.assertFalse(self.sender.send_voice("Alice", __file__, cancel_event=cancel))
        self.assertEqual(self.restored, [("play", "original_play")])
        self.assertNotIn("play", self.sound_calls)

    def test_voice_cancel_during_recording_observation_releases_hold_before_playback(self):
        cancel = threading.Event()
        button = self._voice_fixture()
        observe = self.sender._voice_recording_state
        def observe_and_cancel():
            recording = observe()
            if recording:
                cancel.set()
            return recording
        self.sender._voice_recording_state = observe_and_cancel
        self.assertFalse(self.sender.send_voice("Alice", __file__, cancel_event=cancel))
        self.assertEqual(button.Name, "按住说话")
        self.assertIn("{Esc}", self.auto.keys)
        self.assertNotIn("play", self.sound_calls)
        self.assertEqual(len(self.restored), 2)

    def test_voice_without_confirmed_control_does_not_press_alt(self):
        button = self._voice_fixture()
        self.auto.main.children.remove(button)
        downs = []
        self.sender._key_down = downs.append
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertEqual(downs, [])
        self.assertNotIn("play", self.sound_calls)
        self.assertEqual(len(self.restored), 2)

    def test_voice_enabled_right_alt_precedes_original_talk_button(self):
        button = self._voice_fixture()
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        gestures = []
        self.sender._mouse_down = lambda *args: self.fail("Enabled Alt must go first")
        self.sender._key_down = lambda vk: (gestures.append(("down", vk)), setattr(button, "Name", "松开发送"))
        self.sender._key_up = lambda vk: (gestures.append(("up", vk)), setattr(button, "Name", "按住说话"))
        with self.assertLogs(self.uia.log, level="INFO") as messages:
            self.assertTrue(self.sender.send_voice("Alice", __file__))
        self.assertEqual(gestures, [("down", 0xA5), ("up", 0xA5)])
        self.assertTrue(any("gesture=right_alt recording_button=True" in line for line in messages.output))
        self.assertIn("play", self.sound_calls)

    def _inline_voice_fixture(self, cancel_event=None):
        hold = self._voice_fixture(cancel_event)
        self.auto.main.children.remove(hold)
        toolbar = self.auto.composer.add(Control(self.auto, klass="mmui::ChatInputToolbarRightView"))
        entry = toolbar.add(Control(self.auto, "发语音 ( 按住右 Alt )", "ButtonControl", klass="mmui::XButton"))
        view = Control(self.auto, klass="mmui::ChatVoiceRecordView")
        layout = view.add(Control(self.auto, klass="mmui::XHBoxView"))
        cancel = layout.add(Control(self.auto, "取消", "ButtonControl"))
        submit = layout.add(Control(self.auto, "发送语音", "ButtonControl"))
        entry.click_action = lambda: toolbar.add(view)
        cancel.click_action = submit.click_action = lambda: toolbar.children.remove(view)
        self.sender._key_down = self.sender._key_up = self.sender._mouse_down = self.sender._mouse_up = (
            lambda *unused: self.fail("Inline recorder must never use a hold gesture"))
        return toolbar, view, entry, cancel, submit

    def test_inline_voice_clicks_entry_then_sends_exactly_once(self):
        toolbar, view, entry, cancel, send = self._inline_voice_fixture()
        self.auto.composer.add(Control(self.auto, "发送语音", "ButtonControl")).click_action = lambda: self.fail("Wrong send")
        self.assertTrue(self.sender.send_voice("Alice", __file__))
        self.assertEqual([n for n in self.auto.clicks if n in {entry.Name, cancel.Name, send.Name}], [entry.Name, send.Name])
        self.assertIn("play", self.sound_calls)
        self.assertNotIn(view, toolbar.children)
        self.assertEqual(len(self.restored), 2)

    def test_right_alt_confirmed_inline_recorder_submits_by_release_alone(self):
        toolbar, view, entry, _, send = self._inline_voice_fixture()
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        gestures = []
        self.sender._key_down = lambda vk: (gestures.append("down"), toolbar.add(view))
        self.sender._key_up = lambda vk: (gestures.append("up"), toolbar.children.remove(view))
        with self.assertLogs(self.uia.log, level="INFO") as messages, \
                patch.object(self.sender, "_inline_voice_actions", wraps=self.sender._inline_voice_actions) as actions:
            self.assertTrue(self.sender.send_voice("Alice", __file__))
        actions.assert_called_once_with(view)
        self.assertTrue(any("evidence=inline_actions" in line for line in messages.output))
        self.assertEqual(gestures, ["down", "up"])
        self.assertNotIn(entry.Name, self.auto.clicks)
        self.assertNotIn(send.Name, self.auto.clicks)
        self.assertEqual(self.sound_calls.count("play"), 1)

    def test_right_alt_detects_labelled_popup_even_with_idle_inline_toolbar(self):
        _, _, entry, _, send = self._inline_voice_fixture()
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        popup = Control(self.auto, "", "WindowControl", klass="mmui::ChatVoiceRecordView", handle=200)
        popup.add(Control(self.auto, "松开 发送", "TextControl"))
        self.sender._key_down = lambda vk: self.auto.extra_windows.append(popup)
        self.sender._key_up = lambda vk: self.auto.extra_windows.remove(popup)
        with self.assertLogs(self.uia.log, level="INFO") as messages:
            self.assertTrue(self.sender.send_voice("Alice", __file__))
        self.assertTrue(any("evidence=same_process_popup_label" in line for line in messages.output))
        self.assertEqual(self.sound_calls.count("play"), 1)
        self.assertNotIn(entry.Name, self.auto.clicks)
        self.assertNotIn(send.Name, self.auto.clicks)

    def test_right_alt_finds_deep_toolbar_when_idle_microphone_label_changed(self):
        toolbar, view, entry, _, send = self._inline_voice_fixture()
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        self.auto.composer.children.remove(toolbar)
        parent = self.auto.main
        for _ in range(17):
            parent = parent.add(Control(self.auto, kind="GroupControl"))
        parent.add(toolbar)
        entry.Name = "发语音"
        self.sender._key_down = lambda vk: toolbar.add(view)
        self.sender._key_up = lambda vk: toolbar.children.remove(view)
        roots = self.sender._recorder_roots
        def popups():
            self.assertNotIn(view, toolbar.children, "present inline recorder must be read before other windows")
            return roots()
        self.sender._recorder_roots = popups
        self.assertTrue(self.sender.send_voice("Alice", __file__))
        self.assertEqual(self.sound_calls.count("play"), 1)
        self.assertNotIn(entry.Name, self.auto.clicks)
        self.assertNotIn(send.Name, self.auto.clicks)

    def test_right_alt_timeout_releases_once_and_does_not_try_button(self):
        _, _, entry, _, send = self._inline_voice_fixture()
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        self.sender._key_down = lambda vk: self.auto.clicks.append("down")
        self.sender._key_up = lambda vk: self.auto.clicks.append("up")
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertEqual(self.auto.clicks[-2:], ["down", "up"])
        self.assertNotIn(entry.Name, self.auto.clicks)
        self.assertNotIn(send.Name, self.auto.clicks)
        self.assertNotIn("play", self.sound_calls)

    def test_failed_own_alt_hold_preserves_selected_chat_only_without_other_input(self):
        self._inline_voice_fixture()
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        stamp = [100]
        self.sender._input_stamp = lambda: stamp[0]
        def input_event(vk):
            stamp[0] += 1
        self.sender._key_down = self.sender._key_up = input_event
        identity = {"username":"a"}
        self.assertFalse(self.sender.send_voice("Alice", __file__, identity=identity))
        self.assertTrue(self.sender.send_text("Alice", "same target", identity=identity))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 1)
        observe = self.sender._voice_recording_state
        def intervening_input():
            if self.sender._voice_watch is not None:
                input_event(None)
            return observe()
        self.sender._voice_recording_state = intervening_input
        self.assertFalse(self.sender.send_voice("Alice", __file__, identity=identity))
        self.assertTrue(self.sender.send_text("Alice", "verify again", identity=identity))
        self.assertEqual(self.auto.keys.count("{Ctrl}f"), 2)
        self.assertNotIn("play", self.sound_calls)

    def test_right_alt_cancel_clicks_owned_panel_before_releasing_key(self):
        event = threading.Event()
        toolbar, view, entry, cancel, send = self._inline_voice_fixture(event)
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        self.sender._key_down = lambda vk: toolbar.add(view)
        self.sender._key_up = lambda vk: self.auto.clicks.append("release")
        self.assertFalse(self.sender.send_voice("Alice", __file__, cancel_event=event))
        self.assertEqual(self.auto.clicks[-2:], [cancel.Name, "release"])
        self.assertNotIn(entry.Name, self.auto.clicks)
        self.assertNotIn(send.Name, self.auto.clicks)
        self.assertNotIn("{Esc}", self.auto.keys)
        self.assertNotIn(view, toolbar.children)
        self.assertEqual(len(self.restored), 2)

    def test_right_alt_unobserved_recorder_waits_for_cancel_to_close_before_release(self):
        toolbar, view, _, cancel, send = self._inline_voice_fixture()
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        self.sender._key_down = lambda vk: toolbar.add(view)
        self.sender._voice_recording_state = lambda: None
        events = []
        def cancel_async():
            events.append("cancel")
            reads = 0
            def children():
                nonlocal reads
                reads += 1
                if reads == 2:
                    toolbar.children.remove(view)
                    events.append("closed")
                return toolbar.children
            toolbar.GetChildren = children
        cancel.click_action = cancel_async
        self.sender._key_up = lambda vk: events.append("release")
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertEqual(events, ["cancel", "closed", "release"])
        self.assertNotIn("{Esc}", self.auto.keys)
        self.assertNotIn(send.Name, self.auto.clicks)
        self.assertNotIn("play", self.sound_calls)

    def test_right_alt_unobserved_recorder_still_open_after_cancel_uses_scoped_escape(self):
        toolbar, view, _, cancel, send = self._inline_voice_fixture()
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        self.sender._key_down = lambda vk: toolbar.add(view)
        self.sender._voice_recording_state = lambda: None
        events = []
        cancel.click_action = lambda: events.append("cancel")
        original = self.sender._cancel_recording
        def scoped_cancel(current):
            self.assertIs(current, view)
            original(current)
            events.append("escape")
        self.sender._cancel_recording = scoped_cancel
        self.sender._key_up = lambda vk: events.append("release")
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertEqual(events, ["cancel", "escape", "release"])
        self.assertIn("{Esc}", self.auto.keys)
        self.assertNotIn(send.Name, self.auto.clicks)
        self.assertNotIn("play", self.sound_calls)

    def test_right_alt_release_with_still_open_recorder_never_clicks_send(self):
        toolbar, view, entry, cancel, send = self._inline_voice_fixture()
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        self.sender._key_down = lambda vk: toolbar.add(view)
        self.sender._key_up = lambda vk: None
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertNotIn(entry.Name, self.auto.clicks)
        self.assertNotIn(send.Name, self.auto.clicks)
        self.assertEqual(self.auto.clicks.count(cancel.Name), 1)
        self.assertEqual(self.sound_calls.count("play"), 1)

    def test_inline_voice_cancel_clicks_only_owned_recorder(self):
        event = threading.Event()
        toolbar, view, entry, cancel, send = self._inline_voice_fixture(event)
        self.auto.composer.add(Control(self.auto, "取消", "ButtonControl")).click_action = lambda: self.fail("Wrong cancel")
        self.assertFalse(self.sender.send_voice("Alice", __file__, cancel_event=event))
        self.assertEqual([n for n in self.auto.clicks if n in {entry.Name, cancel.Name, send.Name}], [entry.Name, cancel.Name])
        self.assertNotIn("{Esc}", self.auto.keys)
        self.assertNotIn(view, toolbar.children)
        self.assertEqual(len(self.restored), 2)

    def test_inline_voice_incomplete_panel_never_plays_or_commits(self):
        toolbar, view, _, _, send = self._inline_voice_fixture()
        send.IsOffscreen = True
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertNotIn("play", self.sound_calls)
        self.assertNotIn(send.Name, self.auto.clicks)
        self.assertNotIn(view, toolbar.children)

    def test_inline_voice_replaced_recorder_is_neither_sent_nor_cancelled(self):
        toolbar, view, _, _, _ = self._inline_voice_fixture()
        mixer = sys.modules["pygame"].mixer
        def replace_panel(clip):
            clip.play()
            toolbar.children.remove(view)
            other = toolbar.add(Control(self.auto, klass=view.ClassName))
            other.add(Control(self.auto, "取消", "ButtonControl"))
            other.add(Control(self.auto, "发送语音", "ButtonControl"))
        mixer.find_channel = lambda: types.SimpleNamespace(play=replace_panel)
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertIn("play", self.sound_calls)
        self.assertNotIn("发送语音", self.auto.clicks)
        self.assertNotIn("取消", self.auto.clicks)

    def test_voice_speech_to_text_button_is_not_a_hold_to_talk_button(self):
        button = self._voice_fixture()
        button.Name = "语音输入"
        self.sender._mouse_down = lambda *args: self.fail("Dictation must not be pressed as a recorder")
        self.sender._key_down = lambda *args: self.fail("Alt fallback remains disabled")
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertNotIn("play", self.sound_calls)

    def test_voice_right_alt_preserves_original_extended_key_flags(self):
        events = []
        user = types.SimpleNamespace(keybd_event=lambda *args: events.append(args))
        with patch.object(self.uia.ctypes, "windll", types.SimpleNamespace(user32=user)):
            self.sender._key_down(0xA5)
            self.sender._key_up(0xA5)
        self.assertEqual(events, [(0xA5, 0, 0x0001, 0), (0xA5, 0, 0x0003, 0)])

    def test_voice_4x_button_and_recording_labels_use_live_nodes(self):
        button = self._voice_fixture()
        button.Name = "按住 说话（右侧Alt）"
        created = []
        def press(*unused):
            pane = self.auto.main.add(Control(self.auto, klass="mmui::ChatVoiceRecordView"))
            created.append(pane)
            pane.add(Control(self.auto, "正在发送语音，松开发送，或者按Esc/Backspace键取消", "TextControl"))
        self.sender._mouse_down = press
        self.sender._mouse_up = lambda *a: self.auto.main.children.remove(created[0])
        self.assertTrue(self.sender.send_voice("Alice", __file__))
        self.assertIn("play", self.sound_calls)
        self.assertNotIn(created[0], self.auto.main.children)

    def test_voice_recording_nested_in_composer_does_not_require_direct_main_child(self):
        self._voice_fixture()
        made = []
        def press(*unused):
            layout = self.auto.composer.add(Control(self.auto, klass="mmui::Layout"))
            recorder = layout.add(Control(self.auto, kind="DocumentControl", klass="mmui::ChatVoiceRecordView"))
            recorder.add(Control(self.auto, "正在录音", "TextControl"))
            made.append(layout)
        self.sender._mouse_down = press
        self.sender._mouse_up = lambda *unused: self.auto.composer.children.remove(made[0])
        self.assertTrue(self.sender.send_voice("Alice", __file__))
        self.assertIn("play", self.sound_calls)

    def test_voice_preflight_native_filter_never_opens_unrelated_desktop_provider(self):
        self._voice_fixture()
        self.sender._operation = lambda *args: nullcontext()
        self.sender._window = self.auto.main
        self.sender._send_window = (100, 10)
        self.sender._cancelled = lambda: False
        self.auto.header.Name = "Alice"
        self.auto.GetRootControl = lambda: self.fail("voice must not enumerate desktop UIA")
        self.auto.ControlFromHandle = lambda hwnd: self.fail("no recorder popup exists")
        with patch.object(self.uia, "native_top_windows", return_value=[]) as native:
            self.assertTrue(self.sender.send_voice("Alice", __file__))
        self.assertGreater(native.call_count, 0)
        self.assertIn("voice_preflight_seconds", self.sender.last_send_result()["timings"])

    def _voice_popup_fixture(self, cancel_event=None, *, pid=10, klass="mmui::ChatVoiceRecordView"):
        button = self._voice_fixture(cancel_event)
        self.auto.main.children.remove(button)
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        popup = Control(self.auto, "", "WindowControl", klass=klass, pid=pid, handle=200)
        popup.add(Control(self.auto, "松开 发送", "TextControl", pid=pid))
        gestures = []
        def down(vk):
            gestures.append(("down", vk))
            self.auto.extra_windows.append(popup)
            self.auto.foreground = popup
        def up(vk):
            gestures.append(("up", vk))
            self.auto.extra_windows.remove(popup)
            self.auto.foreground = self.auto.main
        self.sender._key_down, self.sender._key_up = down, up
        self.posted_keys = []
        def post_message(*args):
            self.posted_keys.append(args)
        native_patch = patch.object(self.uia.ctypes, "windll", types.SimpleNamespace(
            user32=types.SimpleNamespace(PostMessageW=post_message)))
        native_patch.start()
        self.addCleanup(native_patch.stop)
        return gestures

    def test_voice_right_alt_observes_same_process_recording_popup(self):
        gestures = self._voice_popup_fixture()
        with self.assertLogs(self.uia.log, level="INFO") as messages:
            self.assertTrue(self.sender.send_voice("Alice", __file__))
        self.assertEqual(gestures, [("down", 0xA5), ("up", 0xA5)])
        self.assertTrue(any("gesture=right_alt recording_button=False" in line for line in messages.output))
        self.assertIn("play", self.sound_calls)

    def test_voice_recording_popup_is_checked_before_full_chat_tree(self):
        self.sender._window = self.auto.main
        popup = Control(self.auto, "", "WindowControl", klass="mmui::ChatVoiceRecordView", handle=200)
        label = popup.add(Control(self.auto, "松开 发送", "TextControl"))
        self.auto.extra_windows.append(popup)
        with patch.object(self.uia, "_walk", wraps=self.uia._walk) as walks:
            self.assertIs(self.sender._voice_recording_state(), label)
        self.assertEqual([call.args[0] for call in walks.call_args_list], [popup])

    def test_voice_recording_scan_skips_history_and_dictation_but_reads_live_composer(self):
        self.sender._window = self.auto.main
        history = self.auto.main.add(Control(self.auto, "聊天记录", "ListControl", aid="MainView.chat_history_list"))
        for _ in range(300):
            history.add(Control(self.auto, "旧消息", "TextControl"))
        history.add(Control(self.auto, "松开发送", "TextControl"))
        dictation = self.auto.main.add(Control(self.auto, klass="mmui::VoiceInputView"))
        dictation.add(Control(self.auto, "正在录音", "TextControl"))
        queried = []
        history.GetChildren = lambda: queried.append("history") or history.children
        dictation.GetChildren = lambda: queried.append("dictation") or dictation.children
        self.assertIsNone(self.sender._voice_recording_state())
        self.assertEqual(queried, [])
        recorder = self.auto.composer.add(Control(self.auto, kind="DocumentControl", klass="mmui::ChatVoiceRecordView"))
        content = recorder.add(Control(self.auto, kind="DocumentControl"))
        label = content.add(Control(self.auto, "松开 发送", "TextControl"))
        self.assertIs(self.sender._voice_recording_state(), label)
        # The recorder replaces its live node when the key is released; a
        # previous observed Name must never act as cached recording proof.
        self.auto.composer.children.remove(recorder)
        recorder.parent = None
        self.assertIsNone(self.sender._voice_recording_state())
        self.assertEqual(queried, [])

    def test_voice_popup_cancel_sends_escape_then_releases_alt_and_restores(self):
        cancel = threading.Event()
        gestures = self._voice_popup_fixture(cancel)
        self.assertFalse(self.sender.send_voice("Alice", __file__, cancel_event=cancel))
        self.assertIn("{Esc}", self.auto.keys)
        self.assertEqual(gestures, [("down", 0xA5), ("up", 0xA5)])
        self.assertEqual(len(self.restored), 2)

    def test_voice_popup_from_other_process_is_not_recording_proof(self):
        gestures = self._voice_popup_fixture(pid=20)
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertEqual(gestures, [("down", 0xA5), ("up", 0xA5)])
        self.assertNotIn("play", self.sound_calls)
        self.assertNotIn("{Esc}", self.auto.keys)

    def test_voice_dictation_popup_is_not_voice_message_recording(self):
        gestures = self._voice_popup_fixture(klass="mmui::VoiceInputWindow")
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertEqual(gestures, [("down", 0xA5), ("up", 0xA5)])
        self.assertNotIn("play", self.sound_calls)

    def test_voice_existing_recording_state_does_not_start_another_hold(self):
        button = self._voice_fixture()
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        button.Name = "松开 发送"
        self.sender._key_down = lambda *a: self.fail("Existing recording must be left alone")
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertNotIn("play", self.sound_calls)

    def test_voice_focus_change_during_recording_preflight_does_not_press_alt(self):
        self._voice_fixture()
        self.cfg.VOICE_RIGHT_ALT_ENABLED = True
        def inspect():
            self.auto.foreground = Control(self.auto, "Other app", pid=20, handle=200)
            return None
        self.sender._voice_recording_state = inspect
        self.sender._key_down = lambda *a: self.fail("Focus changed before the gesture")
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertNotIn("play", self.sound_calls)

    def test_voice_enabled_false_prevents_every_ui_action(self):
        self.cfg.VOICE_ENABLED = False
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertEqual(self.auto.keys, [])

    def test_partial_audio_role_switch_still_restores_originals(self):
        self._voice_fixture()
        def partial_failure(**kwargs):
            raise OSError("communications role failed after console role changed")
        sys.modules["audio_helper"].set_default_recording_device = partial_failure
        self.assertFalse(self.sender.send_voice("Alice", __file__))
        self.assertEqual(self.restored, [("play", "original_play"), ("record", "original_record")])
        self.assertNotIn("play", self.sound_calls)


class ClipboardTests(unittest.TestCase):
    def native_snapshot_fixture(self, formats, *, delayed_render=False):
        import ctypes
        native = object.__new__(NativeClipboard)
        buffers = {fmt: ctypes.create_string_buffer(raw) for fmt, (_, raw) in formats.items() if raw is not None}
        ordered, current = list(formats), {"sequence": 1, "locked": False}
        def get_data(fmt):
            if delayed_render:
                current["sequence"] += 1
            return fmt if fmt in buffers else 0
        def format_name(fmt, target, length):
            target.value = formats[fmt][0]
            return len(target.value)
        def sequence():
            self.assertTrue(current["locked"])
            return current["sequence"]
        from contextlib import contextmanager
        @contextmanager
        def opened():
            current["locked"] = True
            try:
                yield
            finally:
                current["locked"] = False
        native.opened = opened
        native.user = types.SimpleNamespace(
            EnumClipboardFormats=lambda previous: ordered[0] if not previous and ordered else (
                ordered[ordered.index(previous) + 1] if ordered.index(previous) + 1 < len(ordered) else 0),
            GetClipboardData=get_data, IsClipboardFormatAvailable=lambda fmt: fmt in formats,
            GetClipboardFormatNameW=format_name, GetClipboardSequenceNumber=sequence)
        native.kernel = types.SimpleNamespace(GlobalSize=lambda handle: ctypes.sizeof(buffers[handle]),
            GlobalLock=lambda handle: ctypes.addressof(buffers[handle]), GlobalUnlock=lambda handle: None)
        return native, current

    def test_native_snapshot_preserves_payload_and_readable_rich_formats_with_ole_markers(self):
        native, current = self.native_snapshot_fixture({
            13: ("", "原文字\0".encode("utf-16-le")),
            0xC010: ("Ole Private Data", None), 0xC011: ("DataObject", None),
            0xC012: ("HTML Format", b"<b>original</b>"),
        }, delayed_render=True)
        data, sequence = native.snapshot_with_sequence()
        self.assertEqual(set(data), {13, 0xC012})
        self.assertTrue(data[13].startswith("原文字\0".encode("utf-16-le")))
        self.assertTrue(data[0xC012].startswith(b"<b>original</b>"))
        self.assertEqual(sequence, current["sequence"])
        self.assertGreater(sequence, 1)

    def test_native_snapshot_does_not_discard_unknown_or_only_ole_payload(self):
        for formats in ({13: ("", b"original"), 0xC010: ("Application Document", None)},
                        {0xC010: ("Ole Private Data", None)}):
            with self.subTest(formats=list(formats)):
                native, _ = self.native_snapshot_fixture(formats)
                self.assertIsNone(native.snapshot_with_sequence()[0])

    def test_native_snapshot_accepts_remote_metadata_only_with_recoverable_content(self):
        native, _ = self.native_snapshot_fixture({15: ("", b"original file list"),
            0xC010: ("CanUploadToCloudClipboard", None)})
        self.assertEqual(set(native.snapshot()), {15})

    def test_native_bitmap_needs_a_backed_up_dib_or_dibv5_payload(self):
        native, _ = self.native_snapshot_fixture({2: ("", None), 17: ("", b"original DIBV5 image")})
        self.assertEqual(set(native.snapshot()), {17})
        for formats in ({2: ("", None)}, {2: ("", None), 8: ("", None)}):
            with self.subTest(formats=list(formats)):
                native, _ = self.native_snapshot_fixture(formats)
                self.assertIsNone(native.snapshot())

    def test_locked_snapshot_sequence_handles_delayed_rendering_without_rejecting_first_write(self):
        clipboard = FakeClipboard()
        def snapshot_with_sequence():
            clipboard.seq += 1  # System renders original content while locked.
            return dict(clipboard.data), clipboard.seq
        clipboard.snapshot_with_sequence = snapshot_with_sequence
        with ClipboardLease(clipboard) as lease:
            lease.set_text("temporary")
        self.assertTrue(lease.restored)
        self.assertEqual(clipboard.data, {13: "用户原剪贴板"})

    def test_new_copy_after_locked_snapshot_is_not_overwritten(self):
        clipboard = FakeClipboard()
        def snapshot_with_sequence():
            snapshot, sequence = dict(clipboard.data), clipboard.seq
            clipboard.set_text("用户刚复制的新内容")
            return snapshot, sequence
        clipboard.snapshot_with_sequence = snapshot_with_sequence
        with self.assertRaises(OSError):
            with ClipboardLease(clipboard) as lease:
                lease.set_text("temporary")
        self.assertEqual(clipboard.data, {13: "用户刚复制的新内容"})

    def test_unused_lease_does_not_snapshot_or_modify_clipboard(self):
        clipboard = FakeClipboard()
        clipboard.snapshot = lambda: self.fail("A navigation/voice operation does not need a snapshot")
        with ClipboardLease(clipboard):
            pass
        self.assertEqual(clipboard.writes, 0)
        self.assertEqual(clipboard.seq, 1)

    def test_clipboard_conflict_stops_before_paste_instead_of_unicode_fallback(self):
        clipboard = FakeClipboard()
        with ClipboardLease(clipboard) as lease:
            lease.set_text("search query")
            clipboard.set_text("用户新复制的内容")
            with self.assertRaises(OSError):
                lease.set_text("message draft")
        self.assertEqual(clipboard.data, {13: "用户新复制的内容"})
        self.assertFalse(lease.restored)

    def test_unknown_clipboard_write_failure_does_not_allow_a_second_input(self):
        clipboard = FakeClipboard()
        write = clipboard.set_text
        def uncertain(text, expected_sequence=None):
            write(text, expected_sequence)
            raise OSError("provider raised after write took effect")
        clipboard.set_text = uncertain
        with ClipboardLease(clipboard) as lease:
            with self.assertRaises(OSError):
                lease.set_text("possibly written text")
        self.assertEqual(clipboard.writes, 1)
        self.assertEqual(clipboard.data, {13: "possibly written text"})

    def test_native_matching_text_reads_unicode_content_and_unlocks(self):
        import ctypes
        raw = ("search query" + "\0").encode("utf-16-le")
        buffer = ctypes.create_string_buffer(raw)
        native = object.__new__(NativeClipboard)
        unlocked = []
        native.user = types.SimpleNamespace(GetClipboardData=lambda fmt: 91,
                                           GetClipboardSequenceNumber=lambda: 17)
        native.kernel = types.SimpleNamespace(GlobalSize=lambda handle: len(raw),
                                              GlobalLock=lambda handle: ctypes.addressof(buffer),
                                              GlobalUnlock=lambda handle: unlocked.append(handle))
        native.opened = nullcontext
        self.assertEqual(native.matching_text_sequence("search query"), 17)
        self.assertIs(native.matching_text_sequence("different query"), False)
        self.assertEqual(unlocked, [91, 91])

    def test_native_clipboard_has_hidden_owner_and_cleans_up_on_failure(self):
        calls = []
        hwnd = 0x100000001  # Exercise a handle that cannot fit in 32 bits.
        native = object.__new__(NativeClipboard)
        def create(*args):
            calls.append(("create", args))
            return hwnd
        native.user = types.SimpleNamespace(
            CreateWindowExW=create,
            OpenClipboard=lambda owner: calls.append(("open", owner)) or True,
            CloseClipboard=lambda: calls.append(("close",)),
            DestroyWindow=lambda owner: calls.append(("destroy", owner)),
        )
        with self.assertRaises(ValueError):
            with native.opened():
                raise ValueError("synthetic write failure")
        self.assertEqual(calls[1], ("open", hwnd))
        self.assertEqual(calls[2:], [("close",), ("destroy", hwnd)])
        self.assertEqual(calls[0][1][3], 0)  # No visible window style.
        self.assertNotEqual(calls[0][1][8].value, 0)  # HWND_MESSAGE, never NULL.

    def test_restores_owned_text_and_files(self):
        clipboard = FakeClipboard()
        with ClipboardLease(clipboard) as lease:
            lease.set_text("temporary")
            lease.set_files(["sample.gif"])
        self.assertEqual(clipboard.data, {13: "用户原剪贴板"})
        self.assertTrue(lease.restored)

    def test_newer_user_clipboard_wins(self):
        clipboard = FakeClipboard()
        with ClipboardLease(clipboard) as lease:
            lease.set_text("temporary")
            clipboard.set_text("用户新复制的内容")
        self.assertEqual(clipboard.data, {13: "用户新复制的内容"})
        self.assertFalse(lease.restored)

    def test_exception_still_restores_clipboard(self):
        clipboard = FakeClipboard()
        with self.assertRaises(ValueError):
            with ClipboardLease(clipboard) as lease:
                lease.set_text("temporary")
                raise ValueError("failed UI")
        self.assertEqual(clipboard.data, {13: "用户原剪贴板"})

    def test_unsupported_snapshot_is_not_lossily_restored(self):
        clipboard = FakeClipboard()
        clipboard.snapshot = lambda: None
        with self.assertRaises(OSError):
            with ClipboardLease(clipboard) as lease:
                lease.set_text("temporary")
        self.assertEqual(clipboard.data, {13: "用户原剪贴板"})
        self.assertEqual(clipboard.writes, 0)

    def test_new_user_copy_between_lease_writes_is_not_overwritten(self):
        clipboard = FakeClipboard()
        with self.assertRaises(OSError):
            with ClipboardLease(clipboard) as lease:
                lease.set_text("search query")
                clipboard.set_text("用户新复制的内容")
                lease.set_text("message draft")
        self.assertEqual(clipboard.data, {13: "用户新复制的内容"})
        self.assertFalse(lease.restored)

    def test_same_text_reannouncement_allows_next_lease_write(self):
        clipboard = FakeClipboard()
        with ClipboardLease(clipboard) as lease:
            lease.set_text("search query")
            clipboard.set_text("search query")
            lease.set_text("message draft")
            self.assertEqual(clipboard.data, {13: "message draft"})
        self.assertTrue(lease.restored)
        self.assertEqual(clipboard.data, {13: "用户原剪贴板"})

    def test_copy_after_same_text_check_is_not_overwritten(self):
        clipboard = FakeClipboard()
        def matching(text):
            sequence = clipboard.seq if clipboard.data.get(13) == text else False
            clipboard.set_text("用户新复制的内容")
            return sequence
        clipboard.matching_text_sequence = matching
        with self.assertRaises(OSError):
            with ClipboardLease(clipboard) as lease:
                lease.set_text("search query")
                clipboard.set_text("search query")
                lease.set_text("message draft")
        self.assertEqual(clipboard.data, {13: "用户新复制的内容"})
        self.assertFalse(lease.restored)

    def test_same_text_reannouncement_after_final_write_is_not_restored(self):
        clipboard = FakeClipboard()
        with ClipboardLease(clipboard) as lease:
            lease.set_text("message draft")
            clipboard.set_text("message draft")
        self.assertEqual(clipboard.data, {13: "message draft"})
        self.assertFalse(lease.restored)

    def test_copy_during_snapshot_cancels_before_first_lease_write(self):
        clipboard = FakeClipboard()
        original_snapshot = clipboard.snapshot
        def snapshot_race():
            saved = original_snapshot()
            clipboard.set_text("用户新复制的内容")
            return saved
        clipboard.snapshot = snapshot_race
        with self.assertRaises(OSError):
            with ClipboardLease(clipboard) as lease:
                lease.set_text("automatic")
        self.assertEqual(clipboard.data, {13: "用户新复制的内容"})


class MomentsTests(unittest.TestCase):
    setUp = UiaTests.setUp
    tearDown = UiaTests.tearDown

    def load_moments(self):
        with patch.dict(sys.modules, {"uia_sender": self.uia}):
            spec = importlib.util.spec_from_file_location("moments_isolated", BRIDGE / "moments_sender.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self._moment_module = module
        self.moment = module.MomentsSender()
        self.moment_clip_patch = patch.object(module, "ClipboardLease", lambda: ClipboardLease(self.clip))
        self.moment_clip_patch.start()
        self.addCleanup(self.moment_clip_patch.stop)

    def test_moments_publish_renews_expiry_before_opening_any_composer(self):
        self.load_moments()
        self.sender = self.moment
        manager, _, adapters = UiaTests._expired_real_manager_fixture(self)
        with patch.object(self.moment, '_open_composer', side_effect=RuntimeError('synthetic stop before composer')) as opening:
            self.assertFalse(self.moment.send_moment('新动态', generation=7))
        opening.assert_called_once_with('')
        self.assertEqual(len(adapters), 2)
        self.assertTrue(manager.snapshot()['active'])
        self.assertEqual(self.auto.keys, ['{Tab}'])
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.clip.writes, 0)

    def test_moments_social_uses_same_fresh_pre_input_lease(self):
        self.load_moments()
        with patch.dict(sys.modules, {'uia_sender': self.uia,
                                     'moments_sender': self._moment_module}):
            spec = importlib.util.spec_from_file_location('social_expiry_isolated', BRIDGE / 'moments_social_sender.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        social = module.MomentsSocialSender(feed_client=types.SimpleNamespace())
        self.sender = social
        manager, _, adapters = UiaTests._expired_real_manager_fixture(self)
        with social._social_operation(None, 7):
            self.assertTrue(manager.snapshot()['active'])
        self.assertEqual(len(adapters), 2)
        self.assertEqual(self.auto.keys, ['{Tab}'])
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.clip.writes, 0)

    def fixture(self, with_editor=True, draft=""):
        self.load_moments()
        entry = self.auto.main.add(Control(self.auto, "朋友圈", "ButtonControl"))
        camera = Control(self.auto, "相机", "ButtonControl")
        dialog = Control(self.auto, "发表朋友圈", "WindowControl", klass="mmui::MomentEditor", handle=101)
        moment_editor = dialog.add(Control(self.auto, "这一刻的想法", "EditControl", Rect(50, 200, 800, 500))) if with_editor else None
        if moment_editor:
            moment_editor.text = draft
        final = dialog.add(Control(self.auto, "发表", "ButtonControl", Rect(700, 550, 800, 600)))
        final.click_action = lambda: self.auto.extra_windows.remove(dialog)
        entry.click_action = lambda: self.auto.main.add(camera)
        camera.click_action = lambda: self.auto.extra_windows.append(dialog)
        return dialog, moment_editor, final

    def embedded_fixture(self, *, opened=True, draft="", with_editor=True):
        self.load_moments()
        entry = self.auto.main.add(Control(self.auto, "朋友圈", "ButtonControl"))
        feed = Control(self.auto, "朋友圈", "ListControl", aid="MainView.sns_list")
        toolbar = Control(self.auto, kind="ToolBarControl", aid="MainView.sns_window_tool_bar")
        camera = toolbar.add(Control(self.auto, "发表", "ButtonControl"))
        menu = Control(self.auto, "Weixin", "WindowControl", klass="mmui::XMenu", handle=102)
        text_item = menu.add(Control(self.auto, "发表文字", "MenuItemControl", aid="XMenuItem", klass="mmui::XMenuView"))
        panel = Control(self.auto, "朋友圈", "WindowControl", aid="SnsPublishPanel")
        editor = panel.add(Control(self.auto, "", "EditControl", Rect(50, 200, 800, 500))) if with_editor else None
        if editor:
            editor.text = draft
        final = panel.add(Control(self.auto, "发表", "ButtonControl", Rect(700, 550, 800, 600)))
        cancel = panel.add(Control(self.auto, "取消", "ButtonControl"))
        final.IsEnabled = bool(draft)
        def enter():
            if feed in self.auto.main.children:
                self.auto.main.children.remove(feed)
                self.auto.main.children.remove(toolbar)
            else:
                self.auto.main.add(feed)
                self.auto.main.add(toolbar)
        def open_panel():
            self.auto.extra_windows.remove(menu)
            self.auto.main.add(panel)
        entry.click_action = enter
        camera.click_action = lambda: self.fail("text flow left-clicked the file selector")
        camera.right_click_action = lambda: self.auto.extra_windows.append(menu)
        text_item.click_action = open_panel
        final.click_action = lambda: self.auto.main.children.remove(panel)
        self.auto.on_paste = lambda: setattr(final, "IsEnabled", bool(editor and editor.text))
        if opened:
            self.auto.main.add(feed)
            self.auto.main.add(toolbar)
        return types.SimpleNamespace(entry=entry, feed=feed, toolbar=toolbar, camera=camera,
                                     menu=menu, text_item=text_item, panel=panel, editor=editor,
                                     final=final, cancel=cancel)

    def test_embedded_feed_already_open_does_not_toggle_entry(self):
        ui = self.embedded_fixture()
        self.assertTrue(self.moment.send_moment("today"))
        self.assertNotIn("朋友圈", self.auto.clicks)
        self.assertEqual(self.auto.right_clicks, ["发表"])
        self.assertEqual(self.auto.clicks, ["发表文字", "发表"])
        self.assertIn(ui.feed, self.auto.main.children)
        self.assertEqual(self.auto.main.NativeWindowHandle, 100)
        result = self.moment.last_send_result()
        self.assertTrue(result["submit_attempted"])
        self.assertFalse(result["outcome_uncertain"])
        self.assertEqual(result["status"], "accepted_by_ui")
        self.assertFalse(result["delivery_verified"])

    def test_embedded_feed_navigation_keeps_main_window(self):
        self.embedded_fixture(opened=False)
        self.assertTrue(self.moment.send_moment("today"))
        self.assertEqual(self.auto.clicks, ["朋友圈", "发表文字", "发表"])
        self.assertEqual(self.auto.right_clicks, ["发表"])

    def test_text_popup_owned_by_main_foreground_is_clicked_without_activation(self):
        ui = self.embedded_fixture()
        ui.menu.SetActive = lambda: self.fail("activating XMenu would dismiss the popup")
        self.fresh_control_wrappers()
        self.assertTrue(self.moment.send_moment("today"))
        self.assertEqual(self.auto.clicks, ["发表文字", "发表"])
        self.assertEqual(self.auto.right_clicks, ["发表"])

    def test_text_popup_can_itself_be_foreground_without_reactivation(self):
        ui = self.embedded_fixture()
        original = ui.camera.right_click_action
        def menu_foreground():
            original()
            self.auto.foreground = ui.menu
        ui.camera.right_click_action = menu_foreground
        ui.menu.SetActive = lambda: self.fail("text popup must not be activated")
        self.assertTrue(self.moment.send_moment("today"))
        self.assertEqual(self.auto.clicks, ["发表文字", "发表"])

    def test_text_popup_other_pid_duplicate_wrong_class_or_missing_id_never_clicks(self):
        for invalid in ("root_pid", "item_pid", "duplicate", "root_class", "item_class", "empty_id"):
            with self.subTest(invalid=invalid):
                self.auto = FakeAuto(self.clip)
                sys.modules["uiautomation"] = self.auto
                ui = self.embedded_fixture()
                if invalid == "root_pid":
                    ui.menu.ProcessId = 20
                elif invalid == "item_pid":
                    ui.text_item.ProcessId = 20
                elif invalid == "duplicate":
                    ui.menu.add(Control(self.auto, "发表文字", "MenuItemControl", aid="XMenuItem", klass="mmui::XMenuView"))
                elif invalid == "root_class":
                    ui.menu.ClassName = "mmui::OtherPopup"
                elif invalid == "item_class":
                    ui.text_item.ClassName = "mmui::OtherItem"
                else:
                    ui.text_item.GetRuntimeId = lambda: []
                self.assertFalse(self.moment.send_moment("today"))
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.auto.right_clicks, ["发表"])
                self.assertNotIn("{Ctrl}v", self.auto.keys)
                self.assertFalse(self.moment.last_send_result()["submit_attempted"])

    def test_text_popup_fresh_identity_foreground_or_cancellation_drift_never_clicks(self):
        for drift in ("item", "root", "main", "duplicate", "other_pid", "foreground", "cancel"):
            with self.subTest(drift=drift):
                self.auto = FakeAuto(self.clip)
                sys.modules["uiautomation"] = self.auto
                ui = self.embedded_fixture()
                event = threading.Event()
                lookup = self.moment._text_menu_item
                calls = 0
                def changed_menu():
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        if drift == "item":
                            ui.menu.children.remove(ui.text_item)
                            ui.menu.add(Control(self.auto, "发表文字", "MenuItemControl", aid="XMenuItem", klass="mmui::XMenuView"))
                        elif drift == "root":
                            self.auto.extra_windows.remove(ui.menu)
                            replacement = Control(self.auto, "Weixin", "WindowControl", klass="mmui::XMenu", handle=102)
                            replacement.add(ui.text_item)
                            self.auto.extra_windows.append(replacement)
                        elif drift == "main":
                            self.auto.main.rid += 10000
                        elif drift == "duplicate":
                            ui.menu.add(Control(self.auto, "发表文字", "MenuItemControl", aid="XMenuItem", klass="mmui::XMenuView"))
                        elif drift == "other_pid":
                            ui.text_item.ProcessId = 20
                        elif drift == "foreground":
                            self.auto.foreground = Control(self.auto, "Other app", "WindowControl", pid=20, handle=200)
                        else:
                            event.set()
                    return lookup()
                self.moment._text_menu_item = changed_menu
                self.assertFalse(self.moment.send_moment("today", cancel_event=event))
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.auto.right_clicks, ["发表"])
                self.assertNotIn("{Ctrl}v", self.auto.keys)
                self.assertFalse(self.moment.last_send_result()["submit_attempted"])

    def test_embedded_discover_navigation_refreshes_same_cold_main_with_one_tab(self):
        ui = self.embedded_fixture(opened=False)
        self.auto.main.children.remove(ui.entry)
        discover = self.auto.main.add(Control(self.auto, "发现", "ButtonControl"))
        saved = list(self.auto.main.children)
        discover.click_action = lambda: setattr(self.auto.main, "children", [])
        gate = FakeGate()
        self.moment._gate_manager = gate
        original_keys = self.auto.SendKeys
        def keys(key, **kwargs):
            original_keys(key, **kwargs)
            if key == "{Tab}":
                self.auto.main.children = saved
                self.auto.main.add(ui.entry)
        self.auto.SendKeys = keys
        self.assertTrue(self.moment.send_moment("today"))
        self.assertEqual(self.auto.keys.count("{Tab}"), 1)
        self.assertEqual(gate.ensures, [(100, 10)])
        self.assertEqual(self.auto.clicks, ["发现", "朋友圈", "发表文字", "发表"])

    def test_embedded_navigation_does_not_use_popout_discover_button(self):
        ui = self.embedded_fixture(opened=False)
        self.auto.main.children.remove(ui.entry)
        popout = Control(self.auto, "Other chat", "WindowControl", handle=104)
        popout.add(Control(self.auto, "发现", "ButtonControl"))
        popout.add(Control(self.auto, "朋友圈", "ButtonControl"))
        self.auto.extra_windows.append(popout)
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(self.auto.clicks, [])

    def test_embedded_existing_composer_is_untouched_even_if_empty(self):
        for draft in ("", "user draft"):
            with self.subTest(draft=draft):
                ui = self.embedded_fixture(draft=draft)
                self.auto.main.add(ui.panel)
                self.assertFalse(self.moment.send_moment("today"))
                self.assertEqual(ui.editor.text, draft)
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.auto.right_clicks, [])
                self.assertNotIn("{Ctrl}v", self.auto.keys)
                self.auto.main.children.remove(ui.panel)

    def test_embedded_multiple_editors_never_paste_or_submit(self):
        ui = self.embedded_fixture()
        ui.panel.add(Control(self.auto, "", "EditControl"))
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(self.auto.clicks, ["发表文字"])
        self.assertNotIn("{Ctrl}v", self.auto.keys)

    def test_embedded_existing_duplicate_or_editorless_panels_never_navigate(self):
        ui = self.embedded_fixture(with_editor=False)
        self.auto.main.add(ui.panel)
        self.auto.main.add(Control(self.auto, "朋友圈", "WindowControl", aid="SnsPublishPanel"))
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.auto.right_clicks, [])

    def test_embedded_replaced_editor_before_submit_is_rejected(self):
        ui = self.embedded_fixture()
        def replace():
            ui.panel.children.remove(ui.editor)
            other = ui.panel.add(Control(self.auto, "", "EditControl"))
            other.text = "today"
            ui.final.IsEnabled = True
        self.auto.on_paste = replace
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(self.auto.clicks, ["发表文字"])

    def test_embedded_main_window_disappearing_is_not_publication_success(self):
        ui = self.embedded_fixture()
        ui.final.click_action = lambda: setattr(self.auto, "launched", False)
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(self.moment.last_send_result()["status"], "failed")

    def test_embedded_provider_going_cold_is_not_composer_closure(self):
        ui = self.embedded_fixture()
        ui.final.click_action = lambda: setattr(self.auto.main, "children", [])
        self.assertFalse(self.moment.send_moment("today"))

    def test_embedded_unreadable_draft_never_submits(self):
        ui = self.embedded_fixture()
        ui.editor.value_available = False
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(self.auto.clicks, ["发表文字"])
        self.assertNotIn("{Ctrl}v", self.auto.keys)
        self.assertIn("内容无法读取", self.moment.last_send_result()["error"])

    def test_moment_editor_pattern_can_become_readable_before_first_paste(self):
        ui = self.embedded_fixture()
        original = ui.editor.GetValuePattern
        reads = []
        def later_pattern():
            reads.append(1)
            if len(reads) <= 2:
                self.assertNotIn("{Ctrl}v", self.auto.keys)
                return None
            return original()
        ui.editor.GetValuePattern = later_pattern
        self.fresh_control_wrappers()
        self.assertTrue(self.moment.send_moment("today"))
        self.assertGreaterEqual(len(reads), 3)
        self.assertEqual(self.auto.clicks, ["发表文字", "发表"])

    def test_waiting_for_readable_draft_never_treats_nonempty_text_as_empty(self):
        ui = self.embedded_fixture()
        original = ui.editor.GetValuePattern
        reads = 0
        def later_draft():
            nonlocal reads
            reads += 1
            if reads == 1:
                return None
            ui.editor.text = "user draft arrived"
            return original()
        ui.editor.GetValuePattern = later_draft
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(ui.editor.text, "user draft arrived")
        self.assertNotIn("{Ctrl}v", self.auto.keys)
        self.assertIn("已有草稿", self.moment.last_send_result()["error"])

    def test_empty_moment_value_pattern_does_not_hide_text_pattern_draft(self):
        ui = self.embedded_fixture()
        ui.editor.text = "用户尚未写完的草稿"
        ui.editor.GetValuePattern = lambda: types.SimpleNamespace(Value="")
        ui.editor.GetTextPattern = lambda: types.SimpleNamespace(
            DocumentRange=types.SimpleNamespace(GetText=lambda unused: ui.editor.text))
        self.assertEqual(self.moment._moment_text(ui.editor), "用户尚未写完的草稿")
        self.assertFalse(self.moment.send_moment("new post"))
        self.assertEqual(ui.editor.text, "用户尚未写完的草稿")
        self.assertNotIn("{Ctrl}v", self.auto.keys)
        self.assertIn("已有草稿", self.moment.last_send_result()["error"])

    def test_readable_wait_rejects_editor_replacement_and_focus_change(self):
        for change in ("editor", "focus"):
            with self.subTest(change=change):
                self.auto = FakeAuto(self.clip)
                sys.modules["uiautomation"] = self.auto
                ui = self.embedded_fixture()
                def change_during_read():
                    if self.auto.focused is not ui.editor:
                        return None
                    if change == "editor":
                        ui.panel.children.remove(ui.editor)
                        ui.panel.add(Control(self.auto, "", "EditControl"))
                    else:
                        self.auto.focused = ui.cancel
                    return None
                ui.editor.GetValuePattern = change_during_read
                self.assertFalse(self.moment.send_moment("today"))
                self.assertNotIn("{Ctrl}v", self.auto.keys)
                self.assertEqual(self.auto.clicks, ["发表文字"])
                self.assertFalse(self.moment.last_send_result()["submit_attempted"])

    def test_moment_focus_failure_has_separate_error_and_never_pastes(self):
        self.embedded_fixture()
        self.moment._focus = lambda control: False
        self.assertFalse(self.moment.send_moment("today"))
        self.assertIn("输入焦点无法确认", self.moment.last_send_result()["error"])
        self.assertNotIn("{Ctrl}v", self.auto.keys)

    def direct_value_fixture(self):
        ui = self.embedded_fixture()
        ui.value_writes = []
        class Pattern:
            IsReadOnly = False
            @property
            def Value(self):
                return ui.editor.text
            def SetValue(self, value):
                ui.value_writes.append((ui.editor.rid, value))
                ui.editor.text = value
                ui.final.IsEnabled = bool(value)
        ui.pattern = Pattern()
        ui.editor.GetValuePattern = lambda: ui.pattern
        self.auto.GetFocusedControl = lambda: ui.panel  # Observed Qt provider behavior.
        return ui

    def test_direct_value_writes_verified_editor_when_qt_reports_panel_focus(self):
        ui = self.direct_value_fixture()
        self.fresh_control_wrappers()
        self.assertTrue(self.moment.send_moment("today"))
        self.assertEqual(ui.value_writes, [(ui.editor.rid, "today")])
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.clip.writes, 0)
        self.assertEqual(self.auto.clicks, ["发表文字", "发表"])
        self.assertTrue(self.moment.last_send_result()["submit_attempted"])

    def test_direct_value_failure_after_effect_never_falls_back_or_submits(self):
        ui = self.direct_value_fixture()
        setter = ui.pattern.SetValue
        def changed_then_raise(value):
            setter(value)
            raise OSError("provider lost after SetValue took effect")
        ui.pattern.SetValue = changed_then_raise
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(ui.editor.text, "today")
        self.assertEqual(ui.value_writes, [(ui.editor.rid, "today")])
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.clicks, ["发表文字"])
        self.assertFalse(self.moment.last_send_result()["submit_attempted"])

    def test_direct_value_false_offset_or_cancellation_never_submits(self):
        for failure in ("false", "offset", "cancel", "editor"):
            with self.subTest(failure=failure):
                self.auto = FakeAuto(self.clip)
                sys.modules["uiautomation"] = self.auto
                ui = self.direct_value_fixture()
                event = threading.Event()
                setter = ui.pattern.SetValue
                def unsafe_set(value):
                    if failure == "false":
                        return False
                    if failure == "offset":
                        setter(value + " changed")
                    else:
                        setter(value)
                        if failure == "cancel":
                            event.set()
                        else:
                            ui.panel.children.remove(ui.editor)
                            ui.panel.add(Control(self.auto, "", "EditControl")).text = value
                ui.pattern.SetValue = unsafe_set
                self.assertFalse(self.moment.send_moment("today", cancel_event=event))
                self.assertEqual(self.auto.keys, [])
                self.assertEqual(self.auto.clicks, ["发表文字"])
                self.assertFalse(self.moment.last_send_result()["submit_attempted"])

    def test_direct_value_metadata_drift_preserves_draft_foreground_and_composer(self):
        for drift in ("draft", "foreground", "composer"):
            with self.subTest(drift=drift):
                self.auto = FakeAuto(self.clip)
                sys.modules["uiautomation"] = self.auto
                ui = self.direct_value_fixture()
                initial = ui.pattern
                foreign = Control(self.auto, "Other app", "WindowControl", pid=20, handle=200)
                calls = 0
                class Pattern:
                    IsReadOnly = False
                    @property
                    def Value(self):
                        return initial.Value
                    @property
                    def SetValue(self):
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            if drift == "draft":
                                ui.editor.text = "late private draft"
                            elif drift == "foreground":
                                self_auto.foreground = foreign
                            else:
                                self_auto.main.children.remove(ui.panel)
                                replacement = self_auto.main.add(Control(self_auto, "朋友圈", "WindowControl", aid="SnsPublishPanel"))
                                replacement.add(ui.editor)
                        return initial.SetValue
                self_auto = self.auto
                ui.pattern = Pattern()
                self.assertFalse(self.moment.send_moment("today"))
                self.assertEqual(ui.value_writes, [])
                self.assertEqual(self.auto.keys, [])
                self.assertEqual(self.auto.clicks, ["发表文字"])
                self.assertFalse(self.moment.last_send_result()["submit_attempted"])
                if drift == "draft":
                    self.assertEqual(ui.editor.text, "late private draft")
                elif drift == "foreground":
                    self.assertIs(self.auto.foreground, foreign)

    def test_direct_value_waits_for_final_button_to_enable_without_keyboard(self):
        ui = self.direct_value_fixture()
        setter = ui.pattern.SetValue
        def delayed_enable(value):
            setter(value)
            ui.final.IsEnabled = False
        ui.pattern.SetValue = delayed_enable
        original_children = ui.panel.GetChildren
        polls = 0
        def later_enabled():
            nonlocal polls
            if ui.editor.text == "today":
                polls += 1
                if polls >= 4:
                    ui.final.IsEnabled = True
            return original_children()
        ui.panel.GetChildren = later_enabled
        self.assertTrue(self.moment.send_moment("today"))
        self.assertGreaterEqual(polls, 4)
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.clicks, ["发表文字", "发表"])

    def test_cancel_during_final_draft_read_stops_before_direct_value_write(self):
        ui = self.direct_value_fixture()
        original = ui.pattern
        event = threading.Event()
        setter_requests = 0
        class Pattern:
            IsReadOnly = False
            @property
            def Value(self):
                if setter_requests >= 2:
                    event.set()
                return original.Value
            @property
            def SetValue(self):
                nonlocal setter_requests
                setter_requests += 1
                return original.SetValue
        ui.pattern = Pattern()
        self.assertFalse(self.moment.send_moment("today", cancel_event=event))
        self.assertTrue(event.is_set())
        self.assertEqual(ui.value_writes, [])
        self.assertEqual(ui.editor.text, "")
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.clicks, ["发表文字"])
        self.assertFalse(self.moment.last_send_result()["submit_attempted"])

    def test_unsupported_value_pattern_keeps_exact_keyboard_focus_requirement(self):
        self.embedded_fixture()
        self.auto.GetFocusedControl = lambda: self.auto.main
        self.assertFalse(self.moment.send_moment("today"))
        self.assertNotIn("{Ctrl}v", self.auto.keys)
        self.assertFalse(self.moment.last_send_result()["submit_attempted"])

    def test_embedded_cancel_after_paste_never_submits(self):
        self.embedded_fixture()
        event = threading.Event()
        self.auto.on_paste = event.set
        self.assertFalse(self.moment.send_moment("today", cancel_event=event))
        self.assertEqual(self.auto.clicks, ["发表文字"])
        self.assertFalse(self.moment.last_send_result()["submit_attempted"])
        self.assertFalse(self.moment.last_send_result()["outcome_uncertain"])

    def test_embedded_text_draft_still_disables_final_until_valid(self):
        ui = self.embedded_fixture()
        self.auto.on_paste = lambda: None
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(ui.editor.text, "today")
        self.assertEqual(self.auto.clicks, ["发表文字"])

    def embedded_image_fixture(self, *, preview=True):
        ui = self.embedded_fixture()
        dialog = Control(self.auto, "打开", "WindowControl", klass="#32770", handle=103)
        combo = dialog.add(Control(self.auto, "文件名(N):", "ComboBoxControl", aid="1148"))
        filename = combo.add(Control(self.auto, "文件名(N):", "EditControl", aid="1148"))
        rename = dialog.add(Control(self.auto, "文件项改名", "EditControl", aid="System.ItemNameDisplay"))
        opening = dialog.add(Control(self.auto, "打开(O)", "SplitButtonControl", aid="1"))
        dialog.add(Control(self.auto, "取消", "ButtonControl", aid="2"))
        def pick():
            self.auto.main.add(dialog)
            self.auto.main.IsEnabled = False
        def open_file():
            self.auto.main.children.remove(dialog)
            self.auto.main.IsEnabled = True
            self.auto.main.add(ui.panel)
            ui.final.IsEnabled = preview
            if preview:
                ui.panel.add(Control(self.auto, "图片预览", "ImageControl", aid="image_preview"))
        ui.camera.click_action = pick
        opening.click_action = open_file
        ui.dialog, ui.filename, ui.rename, ui.opening = dialog, filename, rename, opening
        return ui

    def embedded_grid_image_fixture(self):
        ui = self.embedded_image_fixture(preview=False)
        ui.panel.ClassName = "mmui::SnsPublishPanel"
        grid = Control(self.auto, kind="ListControl", klass="mmui::XDragGridView")
        image = grid.add(Control(self.auto, "图片", "ListItemControl", klass="mmui::PublishImageGridCell"))
        image.add(Control(self.auto, kind="ButtonControl", klass="mmui::XImage"))
        adding = grid.add(Control(self.auto, "添加图片", "ListItemControl", klass="mmui::PublishImageAddGridCell"))
        adding.add(Control(self.auto, kind="ButtonControl", klass="mmui::XImage"))
        opening = ui.opening.click_action
        def open_file():
            opening()
            ui.panel.add(grid)
            ui.final.IsEnabled = True
        ui.opening.click_action = open_file
        ui.grid, ui.image, ui.adding = grid, image, adding
        return ui

    def test_observed_single_image_grid_with_recreated_wrappers_submits(self):
        self.embedded_grid_image_fixture()
        self.fresh_control_wrappers()
        with patch("os.path.isfile", return_value=True):
            self.assertTrue(self.moment.send_moment("today", "example.png"))
        self.assertEqual(self.auto.clicks, ["发表", "打开(O)", "发表"])
        self.assertFalse(self.moment.last_send_result()["outcome_uncertain"])

    def test_add_image_and_anonymous_icon_cannot_prove_grid_attachment(self):
        ui = self.embedded_grid_image_fixture()
        ui.grid.children.remove(ui.image)
        ui.adding.Name = "图片预览"
        ui.adding.AutomationId = "image_preview"
        with patch("os.path.isfile", return_value=True):
            self.assertFalse(self.moment.send_moment("today", "example.png"))
        self.assertEqual(self.auto.clicks, ["发表", "打开(O)"])
        self.assertFalse(self.moment.last_send_result()["submit_attempted"])

    def test_grid_preview_wrong_parent_class_or_multiple_images_never_submit(self):
        for mismatch in ("parent", "grid_class", "cell_class", "two_images", "two_grids", "empty_id", "missing_id"):
            with self.subTest(mismatch=mismatch):
                self.auto = FakeAuto(self.clip)
                sys.modules["uiautomation"] = self.auto
                ui = self.embedded_grid_image_fixture()
                if mismatch == "parent":
                    ui.image.parent = Control(self.auto, kind="ListControl", klass="mmui::XDragGridView")
                elif mismatch == "grid_class":
                    ui.grid.ClassName = "mmui::OtherGrid"
                elif mismatch == "cell_class":
                    ui.image.ClassName = "mmui::PublishImageAddGridCell"
                elif mismatch == "two_images":
                    ui.grid.add(Control(self.auto, "图片", "ListItemControl", klass="mmui::PublishImageGridCell"))
                elif mismatch == "two_grids":
                    ui.panel.add(Control(self.auto, kind="ListControl", klass="mmui::XDragGridView"))
                elif mismatch == "empty_id":
                    ui.image.GetRuntimeId = lambda: []
                else:
                    def lost_id():
                        raise OSError("runtime identity unavailable")
                    ui.image.GetRuntimeId = lost_id
                with patch("os.path.isfile", return_value=True):
                    self.assertFalse(self.moment.send_moment("today", "example.png"))
                self.assertEqual(self.auto.clicks, ["发表", "打开(O)"])
                self.assertFalse(self.moment.last_send_result()["submit_attempted"])
                self.auto.main.children.remove(ui.panel)

    def test_observed_grid_or_image_replaced_after_paste_never_submits(self):
        for replaced in ("grid", "image"):
            with self.subTest(replaced=replaced):
                self.auto = FakeAuto(self.clip)
                sys.modules["uiautomation"] = self.auto
                ui = self.embedded_grid_image_fixture()
                def replace():
                    if self.auto.focused is not ui.editor:
                        return
                    if replaced == "grid":
                        ui.panel.children.remove(ui.grid)
                        new_grid = ui.panel.add(Control(self.auto, kind="ListControl", klass="mmui::XDragGridView"))
                        new_grid.add(ui.image)
                        new_grid.add(ui.adding)
                    else:
                        ui.grid.children.remove(ui.image)
                        ui.grid.add(Control(self.auto, "图片", "ListItemControl", klass="mmui::PublishImageGridCell"))
                    ui.final.IsEnabled = True
                self.auto.on_paste = replace
                with patch("os.path.isfile", return_value=True):
                    self.assertFalse(self.moment.send_moment("today", "example.png"))
                self.assertEqual(self.auto.clicks, ["发表", "打开(O)"])
                self.assertFalse(self.moment.last_send_result()["submit_attempted"])
                self.auto.main.children.remove(ui.panel)

    def fresh_control_wrappers(self):
        # Match real UIA: each traversal creates a new Python wrapper over the
        # same native element. Actions and focus still refer to the real node.
        class Wrapper:
            def __init__(self, control):
                self.control = control
            def __getattr__(self, key):
                return getattr(self.control, key)
        children, parent = Control.GetChildren, Control.GetParentControl
        child_patch = patch.object(Control, "GetChildren", lambda c: [Wrapper(child) for child in children(c)])
        parent_patch = patch.object(Control, "GetParentControl", lambda c: Wrapper(parent(c)) if parent(c) is not None else None)
        child_patch.start(); self.addCleanup(child_patch.stop)
        parent_patch.start(); self.addCleanup(parent_patch.stop)

    def test_embedded_file_dialog_and_image_submit_work_with_recreated_wrappers(self):
        ui = self.embedded_image_fixture()
        self.fresh_control_wrappers()
        self.assertIsNot(ui.filename.GetParentControl(), ui.filename.GetParentControl())
        with patch("os.path.isfile", return_value=True):
            self.assertTrue(self.moment.send_moment("today", "example.png"))
        self.assertEqual(ui.filename.text, str(pathlib.Path("example.png").resolve()))
        self.assertEqual(ui.rename.text, "")
        self.assertEqual(self.auto.clicks, ["发表", "打开(O)", "发表"])
        self.assertTrue(self.moment.last_send_result()["submit_attempted"])
        self.assertFalse(self.moment.last_send_result()["outcome_uncertain"])

    def test_shell_file_dialog_may_be_exposed_twice_with_focus_on_search(self):
        ui = self.embedded_image_fixture()
        ui.dialog.Name = "选择文件"
        combo = ui.filename.GetParentControl()
        ui.dialog.children.remove(combo)
        host = ui.dialog.add(Control(self.auto, kind="PaneControl", klass="ComboBoxEx32", aid="1148", handle=104))
        host.add(combo)
        ui.opening.ControlTypeName = "ButtonControl"
        search = ui.dialog.add(Control(self.auto, "搜索框", "EditControl", aid="SearchEditBox"))
        pick = ui.camera.click_action
        def open_picker():
            pick()
            search.SetFocus()
        ui.camera.click_action = open_picker
        original_roots = self.moment._roots
        def roots():
            result = original_roots()
            if ui.dialog in self.auto.main.children:
                result.append(ui.dialog)
            return result
        with patch.object(self.moment, "_roots", side_effect=roots), patch("os.path.isfile", return_value=True):
            self.assertTrue(self.moment.send_moment("today", "example.png"))
        self.assertEqual(ui.filename.text, str(pathlib.Path("example.png").resolve()))
        self.assertEqual(search.text, "")
        self.assertEqual(self.auto.clicks, ["发表", "打开(O)", "发表"])

    def test_preview_ancestry_stops_at_same_composer_with_recreated_wrapper(self):
        ui = self.embedded_fixture()
        outer_toolbar = self.auto.main.add(Control(self.auto, kind="ToolBarControl"))
        outer_toolbar.add(ui.panel)
        preview = ui.panel.add(Control(self.auto, "图片预览", "ImageControl", aid="image_preview"))
        self.moment._main_pid = 10
        self.fresh_control_wrappers()
        self.assertTrue(self.moment._moment_attachment(ui.editor, "example.png"))
        # A toolbar icon inside the composer remains ineligible even though an
        # image has the same apparent name as a valid attachment preview.
        ui.panel.children.remove(preview)
        ui.cancel.add(preview)
        self.assertFalse(self.moment._moment_attachment(ui.editor, "example.png"))

    def test_file_dialog_with_empty_runtime_id_never_opens_selected_file(self):
        ui = self.embedded_image_fixture()
        ui.dialog.GetRuntimeId = lambda: []
        self.fresh_control_wrappers()
        with patch("os.path.isfile", return_value=True):
            self.assertFalse(self.moment.send_moment("today", "example.png"))
        self.assertEqual(ui.filename.text, "")
        self.assertEqual(self.auto.clicks, ["发表"])
        self.assertFalse(self.moment.last_send_result()["submit_attempted"])

    def test_preview_with_empty_runtime_id_never_submits(self):
        ui = self.embedded_image_fixture()
        opening = ui.opening.click_action
        def choose_file():
            opening()
            for control in ui.panel.children:
                if control.ControlTypeName == "ImageControl":
                    control.GetRuntimeId = lambda: []
        ui.opening.click_action = choose_file
        self.fresh_control_wrappers()
        with patch("os.path.isfile", return_value=True):
            self.assertFalse(self.moment.send_moment("today", "example.png"))
        self.assertEqual(self.auto.clicks, ["发表", "打开(O)"])
        self.assertFalse(self.moment.last_send_result()["submit_attempted"])

    def test_embedded_image_uses_filename_1148_not_item_rename(self):
        ui = self.embedded_image_fixture()
        with patch("os.path.isfile", return_value=True):
            self.assertTrue(self.moment.send_moment("today", "example.png"))
        self.assertEqual(ui.filename.text, str(pathlib.Path("example.png").resolve()))
        self.assertEqual(ui.rename.text, "")
        self.assertEqual(self.auto.right_clicks, [])
        self.assertEqual(self.auto.clicks, ["发表", "打开(O)", "发表"])
        self.assertIn(ui.feed, self.auto.main.children)
        self.assertEqual(self.clip.data, {13: "用户原剪贴板"})

    def test_embedded_image_without_preview_never_submits(self):
        self.embedded_image_fixture(preview=False)
        with patch("os.path.isfile", return_value=True):
            self.assertFalse(self.moment.send_moment("today", "example.png"))
        self.assertEqual(self.auto.clicks, ["发表", "打开(O)"])
        self.assertIn("图片预览未能确认", self.moment.last_send_result()["error"])
        self.assertFalse(self.moment.last_send_result()["submit_attempted"])
        self.assertFalse(self.moment.last_send_result()["outcome_uncertain"])

    def test_embedded_static_image_icon_does_not_prove_preview(self):
        ui = self.embedded_image_fixture(preview=False)
        ui.cancel.add(Control(self.auto, "图片预览", "ImageControl", aid="image_preview"))
        with patch("os.path.isfile", return_value=True):
            self.assertFalse(self.moment.send_moment("today", "example.png"))
        self.assertEqual(self.auto.clicks, ["发表", "打开(O)"])

    def test_embedded_image_cancelled_after_path_paste_never_opens_file(self):
        self.embedded_image_fixture()
        event = threading.Event()
        self.auto.on_paste = event.set
        with patch("os.path.isfile", return_value=True):
            self.assertFalse(self.moment.send_moment("today", "example.png", cancel_event=event))
        self.assertEqual(self.auto.clicks, ["发表"])

    def test_embedded_preexisting_file_dialog_is_preserved(self):
        ui = self.embedded_image_fixture()
        self.auto.main.add(ui.dialog)
        ui.filename.text = "user.png"
        with patch("os.path.isfile", return_value=True):
            self.assertFalse(self.moment.send_moment("today", "example.png"))
        self.assertEqual(ui.filename.text, "user.png")
        self.assertEqual(self.auto.clicks, [])

    def test_embedded_image_unreadable_filename_never_opens_file(self):
        ui = self.embedded_image_fixture()
        ui.filename.value_available = False
        with patch("os.path.isfile", return_value=True):
            self.assertFalse(self.moment.send_moment("today", "example.png"))
        self.assertEqual(self.auto.clicks, ["发表"])
        self.assertNotIn("{Ctrl}v", self.auto.keys)

    def test_embedded_image_path_changed_in_same_editor_before_open_is_preserved(self):
        ui = self.embedded_image_fixture()
        original_dialog = self.moment._file_dialog
        calls = 0
        def changed_path():
            nonlocal calls
            calls += 1
            if calls == 3:
                ui.filename.text = "another-user-photo.png"
            return original_dialog()
        self.moment._file_dialog = changed_path
        with patch("os.path.isfile", return_value=True):
            self.assertFalse(self.moment.send_moment("today", "requested.png"))
        self.assertEqual(ui.filename.text, "another-user-photo.png")
        self.assertEqual(self.auto.clicks, ["发表"])

    def test_embedded_commit_click_does_not_reactivate_changed_foreground(self):
        ui = self.embedded_fixture()
        original_click = self.moment._click_control
        foreign = Control(self.auto, "Other app", "WindowControl", pid=20, handle=200)
        def focus_changes(control, **kwargs):
            if control is ui.final:
                self.auto.foreground = foreign
            return original_click(control, **kwargs)
        self.moment._click_control = focus_changes
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(self.auto.clicks, ["发表文字"])
        self.assertIs(self.auto.foreground, foreign)

    def test_embedded_image_only_late_user_text_is_preserved_without_submit(self):
        ui = self.embedded_image_fixture()
        original_preview = self.moment._moment_attachment
        def user_draft(editor, path):
            ui.editor.text = "private user draft"
            return original_preview(editor, path)
        self.moment._moment_attachment = user_draft
        with patch("os.path.isfile", return_value=True):
            self.assertFalse(self.moment.send_moment("", "requested.png"))
        self.assertEqual(ui.editor.text, "private user draft")
        self.assertEqual(self.auto.clicks, ["发表", "打开(O)"])

    def test_embedded_preview_replaced_before_submit_is_rejected(self):
        ui = self.embedded_image_fixture()
        def replace_preview():
            ui.panel.children = [c for c in ui.panel.children if c.ControlTypeName != "ImageControl"]
            ui.panel.add(Control(self.auto, "图片预览", "ImageControl", aid="image_preview"))
            ui.final.IsEnabled = True
        self.auto.on_paste = replace_preview
        with patch("os.path.isfile", return_value=True):
            self.assertFalse(self.moment.send_moment("today", "requested.png"))
        self.assertEqual(self.auto.clicks, ["发表", "打开(O)"])

    def test_embedded_final_button_replaced_after_guard_is_not_clicked(self):
        ui = self.embedded_fixture()
        original_id = ui.final.GetRuntimeId
        replaced = False
        def replace_after_id_capture():
            nonlocal replaced
            token = original_id()
            if not replaced:
                replaced = True
                ui.panel.children.remove(ui.final)
                replacement = ui.panel.add(Control(self.auto, "发表", "ButtonControl"))
                replacement.click_action = lambda: self.fail("replacement final clicked")
            return token
        ui.final.GetRuntimeId = replace_after_id_capture
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(self.auto.clicks, ["发表文字"])
        self.assertFalse(self.moment.last_send_result()["submit_attempted"])
        self.assertFalse(self.moment.last_send_result()["outcome_uncertain"])

    def test_embedded_click_takes_effect_then_raises_is_uncertain(self):
        ui = self.embedded_fixture()
        def close_then_raise():
            self.auto.main.children.remove(ui.panel)
            raise OSError("UI provider disconnected after the click")
        ui.final.click_action = close_then_raise
        self.assertFalse(self.moment.send_moment("today"))
        self.assertNotIn(ui.panel, self.auto.main.children)
        self.assertEqual(self.auto.clicks, ["发表文字", "发表"])
        result = self.moment.last_send_result()
        self.assertTrue(result["submit_attempted"])
        self.assertTrue(result["outcome_uncertain"])
        self.assertFalse(result["delivery_verified"])
        self.assertEqual(result["status"], "failed")

    def test_embedded_final_click_helper_raises_is_uncertain(self):
        ui = self.embedded_fixture()
        original_click = self.moment._click_control
        def raise_at_submit(control, **kwargs):
            if control is ui.final:
                raise RuntimeError("publication call interrupted")
            return original_click(control, **kwargs)
        self.moment._click_control = raise_at_submit
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(self.auto.clicks, ["发表文字"])
        self.assertTrue(self.moment.last_send_result()["submit_attempted"])
        self.assertTrue(self.moment.last_send_result()["outcome_uncertain"])

    def test_embedded_cancel_after_final_click_is_uncertain(self):
        ui = self.embedded_fixture()
        event = threading.Event()
        ui.final.click_action = event.set
        self.assertFalse(self.moment.send_moment("today", cancel_event=event))
        self.assertEqual(self.auto.clicks, ["发表文字", "发表"])
        self.assertTrue(self.moment.last_send_result()["submit_attempted"])
        self.assertTrue(self.moment.last_send_result()["outcome_uncertain"])

    def test_moment_early_invalid_input_resets_previous_submit_flags(self):
        self.fixture()
        self.assertTrue(self.moment.send_moment("today"))
        self.assertFalse(self.moment.send_moment(""))
        result = self.moment.last_send_result()
        self.assertFalse(result["submit_attempted"])
        self.assertFalse(result["outcome_uncertain"])
        self.assertEqual(result["attempted_parts"], 0)
        with patch("os.path.isfile", return_value=False):
            self.assertFalse(self.moment.send_moment("today", "missing.png"))
        result = self.moment.last_send_result()
        self.assertFalse(result["submit_attempted"])
        self.assertFalse(result["outcome_uncertain"])
        self.assertEqual(result["attempted_parts"], 0)

    def test_moment_submit_flags_remain_with_calling_thread_result(self):
        self.load_moments()
        self.moment._moment_result(True, sent=1, attempted=1, submit_attempted=True)
        observed = []
        def other_request():
            self.moment._moment_result(False, "uncertain", attempted=1, submit_attempted=True)
            observed.append(self.moment.last_send_result())
        thread = threading.Thread(target=other_request)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(observed[0]["outcome_uncertain"])
        result = self.moment.last_send_result()
        self.assertEqual(result["status"], "accepted_by_ui")
        self.assertTrue(result["submit_attempted"])
        self.assertFalse(result["outcome_uncertain"])
        self.assertTrue(self.moment._last_send["outcome_uncertain"])

    def test_verified_composer_closure_is_success(self):
        self.fixture()
        self.assertTrue(self.moment.send_moment("today"))
        self.assertIn("发表", self.auto.clicks)
        self.assertEqual(self.auto.extra_windows, [])
        self.assertEqual(self.clip.data, {13: "用户原剪贴板"})
        self.assertTrue(self.moment.last_send_result()["submit_attempted"])
        self.assertFalse(self.moment.last_send_result()["outcome_uncertain"])

    def test_missing_editor_never_pastes_or_posts(self):
        self.fixture(with_editor=False)
        self.assertFalse(self.moment.send_moment("today"))
        self.assertNotIn("发表", self.auto.clicks)
        self.assertNotIn("{Ctrl}v", self.auto.keys)

    def test_unrelated_app_publish_control_is_never_clicked(self):
        self.fixture(with_editor=False)
        other = Control(self.auto, "Other app", "WindowControl", pid=20, handle=200)
        other.add(Control(self.auto, "发表", "ButtonControl", pid=20))
        other.add(Control(self.auto, "这一刻的想法", "EditControl", pid=20))
        self.auto.extra_windows.append(other)
        self.assertFalse(self.moment.send_moment("today"))
        self.assertNotIn("发表", self.auto.clicks)

    def test_cancel_after_paste_never_posts(self):
        self.fixture()
        event = threading.Event()
        self.auto.on_paste = event.set
        self.assertFalse(self.moment.send_moment("today", cancel_event=event))
        self.assertNotIn("发表", self.auto.clicks)

    def test_existing_moment_draft_is_preserved(self):
        _, editor, _ = self.fixture(draft="user draft")
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(editor.text, "user draft")
        self.assertNotIn("发表", self.auto.clicks)

    def test_real_moment_draft_equal_to_placeholder_is_preserved(self):
        _, editor, _ = self.fixture(draft="这一刻的想法")
        self.assertFalse(self.moment.send_moment("new content"))
        self.assertEqual(editor.text, "这一刻的想法")
        self.assertNotIn("{Ctrl}v", self.auto.keys)
        self.assertNotIn("发表", self.auto.clicks)

    def test_moment_placeholder_name_without_value_is_not_empty_draft_proof(self):
        _, editor, _ = self.fixture()
        editor.value_available = False
        self.assertFalse(self.moment.send_moment("new content"))
        self.assertNotIn("{Ctrl}v", self.auto.keys)
        self.assertNotIn("发表", self.auto.clicks)

    def test_chat_and_moments_share_gate_manager_and_cold_tree_refresh(self):
        self.fixture()
        self.assertIs(self.moment._gate_manager, self.sender._gate_manager)
        gate, _ = UiaTests._cold_recovery_fixture(self)
        self.moment._gate_manager = gate
        self.assertTrue(self.moment.send_moment("today"))
        self.assertEqual(gate.ensures, [(100, 10)])
        self.assertEqual(self.auto.keys.count("{Tab}"), 1)
        self.assertIn("发表", self.auto.clicks)

    def test_click_without_closed_composer_is_not_success(self):
        _, _, final = self.fixture()
        final.click_action = lambda: None
        self.assertFalse(self.moment.send_moment("today"))
        self.assertIn("发表", self.auto.clicks)
        self.assertFalse(self.moment._last_send["delivery_verified"])
        self.assertTrue(self.moment.last_send_result()["submit_attempted"])
        self.assertTrue(self.moment.last_send_result()["outcome_uncertain"])

    def test_legacy_composer_recreated_with_same_handle_is_still_uncertain(self):
        dialog, _, final = self.fixture()
        def recreate():
            self.auto.extra_windows.remove(dialog)
            replacement = Control(self.auto, "发表朋友圈", "WindowControl", klass="mmui::MomentEditor", handle=101)
            replacement.add(Control(self.auto, "这一刻的想法", "EditControl")).text = "today"
            self.auto.extra_windows.append(replacement)
        final.click_action = recreate
        self.assertFalse(self.moment.send_moment("today"))
        self.assertEqual(len(self.auto.extra_windows), 1)
        self.assertTrue(self.moment.last_send_result()["submit_attempted"])
        self.assertTrue(self.moment.last_send_result()["outcome_uncertain"])

    def test_legacy_composer_handle_remaining_with_cold_tree_is_uncertain(self):
        dialog, _, final = self.fixture()
        final.click_action = lambda: setattr(dialog, "children", [])
        self.assertFalse(self.moment.send_moment("today"))
        self.assertIn(dialog, self.auto.extra_windows)
        self.assertTrue(self.moment.last_send_result()["outcome_uncertain"])

    def test_moment_native_identity_error_cannot_use_python_wrapper_identity(self):
        ui = self.embedded_fixture()
        def no_native_id():
            raise OSError("native identity unavailable")
        ui.editor.GetRuntimeId = no_native_id
        self.assertFalse(self.moment.send_moment("today"))
        self.assertNotIn("{Ctrl}v", self.auto.keys)
        self.assertEqual(self.auto.clicks, ["发表文字"])
        self.assertFalse(self.moment.last_send_result()["submit_attempted"])

class DesktopLockTests(unittest.TestCase):
    def test_chat_and_moments_transactions_do_not_overlap(self):
        active, maximum = 0, 0
        started = threading.Barrier(3)
        guard = threading.Lock()
        def task():
            nonlocal active, maximum
            started.wait()
            with desktop_operation():
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.04)
                with guard:
                    active -= 1
        threads = [threading.Thread(target=task) for _ in range(2)]
        for thread in threads:
            thread.start()
        started.wait()
        for thread in threads:
            thread.join(2)
        self.assertEqual(maximum, 1)

    def test_waiting_transaction_can_be_cancelled(self):
        cancelled, result = threading.Event(), []
        def blocked():
            try:
                with desktop_operation(cancelled.is_set):
                    result.append("entered")
            except DesktopCancelled:
                result.append("cancelled")
        with desktop_operation():
            thread = threading.Thread(target=blocked)
            thread.start()
            cancelled.set()
            thread.join(1)
            self.assertEqual(result, ["cancelled"])


class AudioRoleTests(unittest.TestCase):
    def setUp(self):
        class Role(IntEnum):
            eConsole = 0
            eMultimedia = 1
            eCommunications = 2
        class Flow(IntEnum):
            eRender = 0
            eCapture = 1
        class DeviceState(IntEnum):
            ACTIVE = 1
        self.roles = Role
        self.calls, self.com = [], []
        def endpoint(flow, role):
            return types.SimpleNamespace(GetId=lambda: f"original_{flow}_{role}")
        self.devices = [types.SimpleNamespace(id="cable", FriendlyName="CABLE Input")]
        utilities = types.SimpleNamespace(
            GetDeviceEnumerator=lambda: types.SimpleNamespace(GetDefaultAudioEndpoint=endpoint),
            GetSpeakers=lambda: types.SimpleNamespace(id="original_0_1", FriendlyName="speakers"),
            GetMicrophone=lambda: endpoint(1, 1), GetAllDevices=lambda **k: self.devices,
            SetDefaultDevice=lambda device, roles=None: self.calls.append((device, [r.value for r in roles])),
        )
        self.modules = patch.dict(sys.modules, {
            "comtypes": types.SimpleNamespace(CoInitialize=lambda: self.com.append("init"),
                                                CoUninitialize=lambda: self.com.append("end")),
            "pycaw.pycaw": types.SimpleNamespace(AudioUtilities=utilities),
            "pycaw.constants": types.SimpleNamespace(ERole=Role, EDataFlow=Flow, DEVICE_STATE=DeviceState),
        })
        self.modules.start()
        spec = importlib.util.spec_from_file_location("audio_isolated", BRIDGE / "audio_helper.py")
        self.audio = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.audio)

    def tearDown(self):
        self.modules.stop()

    def test_switch_routes_all_three_roles(self):
        self.audio.set_default_playback_device(device_id="cable")
        self.assertEqual(self.calls, [("cable", [0, 1, 2])])
        self.assertEqual(self.com.count("init"), self.com.count("end"))

    def test_switch_reuses_prepared_endpoints_without_enumerating_again(self):
        devices = [{"id": "cable", "name": "CABLE Input"}]
        with patch.object(self.audio, "list_playback_devices", side_effect=AssertionError("Already enumerated")):
            self.audio.set_default_playback_device(device_id="cable", devices=devices)
        self.assertEqual(self.calls, [("cable", [0, 1, 2])])
        with self.assertRaises(LookupError):
            self.audio.set_default_playback_device(device_id="missing", devices=devices)
        self.assertEqual(len(self.calls), 1)

    def test_capture_snapshot_and_restoration_keep_each_role(self):
        snapshot = self.audio.get_default_recording_device_info()
        self.assertEqual(snapshot["role_ids"], {"0": "original_1_0", "1": "original_1_1", "2": "original_1_2"})
        self.assertTrue(self.audio.restore_default_recording_device(snapshot["id"], snapshot["role_ids"]))
        self.assertEqual(self.calls, [("original_1_0", [0]), ("original_1_1", [1]), ("original_1_2", [2])])

    def test_ambiguous_device_names_do_not_switch(self):
        self.devices = [types.SimpleNamespace(id="a", FriendlyName="CABLE Input A"),
                        types.SimpleNamespace(id="b", FriendlyName="CABLE Input B")]
        with self.assertRaises(LookupError):
            self.audio.set_default_playback_device(device_name="CABLE Input")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
