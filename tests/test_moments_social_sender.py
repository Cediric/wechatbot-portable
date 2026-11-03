"""Synthetic native/virtual UIA controls only; no desktop or network access."""
import copy
import importlib.util
import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

import test_uia_sender as uia_fixtures
from test_uia_sender import BRIDGE, Control, FakeAuto, FakeGate, Rect


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, BRIDGE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SocialSenderTests(unittest.TestCase):
    setUp = uia_fixtures.UiaTests.setUp
    tearDown = uia_fixtures.UiaTests.tearDown

    def fixture(self):
        """The raw comment cell has no children; focused virtual parents do."""
        self.auto = FakeAuto(self.clip)
        sys.modules["uiautomation"] = self.auto
        self.state.bridge_generation = 7
        self.cfg.BOT_WXID = "wxid_bot"
        stable = load("social_sender_stable", "stable_id.py")
        with patch.dict(sys.modules, {"stable_id": stable, "http_util": types.ModuleType("http_util")}):
            feed_module = load("social_sender_feed", "moments_feed.py")
        with patch.dict(sys.modules, {"uia_sender": self.uia}):
            moments = load("social_sender_moments", "moments_sender.py")
        with patch.dict(sys.modules, {"uia_sender": self.uia, "moments_sender": moments,
                "moments_feed": feed_module, "stable_id": stable}):
            self.module = load("social_sender_isolated", "moments_social_sender.py")
        self.feed_client = Mock()
        self.social = self.module.MomentsSocialSender(self.feed_client)
        self.social._gate_manager = FakeGate(allowed=False)
        self.social._timeout = lambda: .015
        self.social._dpi_scale = lambda: 1
        self.records = {
            "wxid_bot": {"username": "wxid_bot", "alias": "bot_alias"},
            "wxid_author": {"username": "wxid_author", "alias": "author_alias", "nickname": "Alice"},
            "wxid_comment": {"username": "wxid_comment", "alias": "comment_alias", "nickname": "Carol"},
        }
        self.social._fresh_identities = Mock(return_value=self.records)
        self.source = {"post_id": "native-post-10001", "author_id": "wxid_author",
            "author_name": "Alice", "author_names": ["Alice"], "content": "晴天散步",
            "created_at": int(time.time()) - 60, "identity_verified": True,
            "comments": [{"comment_id": "native-comment-20001", "author_id": "wxid_comment",
                "author_name": "Carol", "author_names": ["Carol"], "content": "原评论正文",
                "identity_verified": True}]}
        self.feed_client.fetch.side_effect = lambda **kwargs: [copy.deepcopy(self.source)]
        self.feed_client.read_post_details.return_value = self.source
        self.entry = self.auto.main.add(Control(self.auto, "朋友圈", "ButtonControl"))
        self.entry.click_action = lambda: self.fail("already-open feed was toggled")
        self.feed = self.auto.main.add(Control(self.auto, "朋友圈", "ListControl",
            Rect(10, 100, 990, 790), aid="MainView.sns_list"))
        self.feed.GetScrollPattern = lambda: None
        self.cover = self.feed.add(Control(self.auto, rect=Rect(10, 100, 990, 330),
            klass="mmui::TimelineCell"))
        self.row = self.feed.add(Control(self.auto, "Alice 晴天散步 1分钟前", "ListItemControl",
            Rect(10, 340, 990, 560), klass="mmui::TimelineContentCell"))
        self.cover_avatar = self.cover.add(Control(self.auto, "", "ButtonControl", Rect(920, 270, 980, 325)))
        self.cell = self.feed.add(Control(self.auto, rect=Rect(10, 560, 990, 790),
            klass="mmui::TimelineCommentCell"))
        self.holder = self.cell.add(Control(self.auto, rect=self.cell.BoundingRectangle))
        self.view = self.holder.add(Control(self.auto, rect=Rect(20, 660, 980, 780),
            klass="mmui::CommentReplyTextView"))
        self.comment_editor = self.view.add(Control(self.auto, "", "EditControl",
            Rect(30, 675, 800, 725), klass="mmui::CommentReplyTextEdit"))
        self.send = self.view.add(Control(self.auto, "发送", "ButtonControl", Rect(815, 675, 965, 725)))
        self.original_comment = self.holder.add(Control(self.auto, "Carol：原评论正文", "TextControl",
            Rect(30, 590, 960, 620), klass="mmui::CommentTextView"))
        self.original_comment.click_action = lambda: setattr(self.comment_editor, "HelpText", "回复 Carol")
        self.cell.GetChildren = lambda: []  # The actual Qt virtual-tree limitation.
        self.writes = []
        self.after_write = None
        def set_value(text):
            self.writes.append(text)
            self.comment_editor.text = text
            if self.after_write:
                self.after_write()
            return True
        self.comment_editor.GetValuePattern = lambda: types.SimpleNamespace(
            Value=self.comment_editor.text, SetValue=set_value, IsReadOnly=False)
        self.toolbar = self.auto.main.add(Control(self.auto, kind="ToolBarControl",
            aid="MainView.sns_window_tool_bar"))
        self.refresh = self.toolbar.add(Control(self.auto, "刷新", "ButtonControl"))
        self.popup = Control(self.auto, "", "WindowControl", Rect(600, 510, 940, 558),
            klass="mmui::TimelineFloatMenu")
        self.like = self.popup.add(Control(self.auto, "赞", "ButtonControl"))
        self.comment_button = self.popup.add(Control(self.auto, "评论", "ButtonControl"))
        self.comment_button.click_action = self.open_editor
        def click_like():
            self.like.Name = "取消赞"
            self.auto.main.children.remove(self.popup)
        self.like.click_action = click_like
        self.send.click_action = self.close_editor
        self.cover.click_action = lambda: self.open_profile("bot_alias")
        def click_row(**kwargs):
            self.auto.clicks.append(self.row.Name)
            if kwargs.get("y") == 40:
                self.open_profile("author_alias")
            elif self.popup not in self.auto.main.children:
                self.auto.main.add(self.popup)
        self.row.Click = click_row
        self.auto.ControlFromPoint = lambda x, y: next((c for c in (self.cover_avatar, self.cover, self.row, self.original_comment)
            if c.BoundingRectangle.left <= x < c.BoundingRectangle.right
            and c.BoundingRectangle.top <= y < c.BoundingRectangle.bottom), self.auto.main)
        keys = self.auto.SendKeys
        def send_keys(key, **kwargs):
            if key == "{Esc}":
                self.auto.keys.append(key)
                self.auto.extra_windows = [w for w in self.auto.extra_windows
                    if w.ClassName != "mmui::ProfileUniquePop"]
                if self.popup in self.auto.main.children:
                    self.auto.main.children.remove(self.popup)
                self.auto.focused = self.auto.editor
            else:
                keys(key, **kwargs)
        self.auto.SendKeys = send_keys
        self.pin_main()

    def pin_main(self):
        self.social._main_pid = self.auto.main.ProcessId
        self.social._main_handle = self.auto.main.NativeWindowHandle
        self.social._window = self.auto.main

    def open_profile(self, alias):
        profile = Control(self.auto, "身份卡", "WindowControl", klass="mmui::ProfileUniquePop", handle=103)
        profile.add(Control(self.auto, "微信号：", "TextControl"))
        profile.add(Control(self.auto, alias, "TextControl", aid="profile.basic_line_view",
            klass="mmui::ProfileTextView"))
        self.auto.extra_windows.append(profile)
        return profile

    def open_editor(self):
        self.auto.main.children.remove(self.popup)
        self.auto.focused = self.comment_editor

    def close_editor(self):
        self.holder.children.remove(self.view)
        self.comment_editor.IsOffscreen = True
        self.comment_editor.text = ""
        self.auto.focused = self.auto.editor

    def action(self, kind="comment"):
        return {"type": kind, "source": copy.deepcopy(self.source), "text": "自然的回应" if kind != "like" else "",
            **({"comment_id": "native-comment-20001", "comment_author_id": "wxid_comment"}
                if kind == "reply" else {})}

    def execute(self, kind="comment", **kwargs):
        return self.social.execute_interaction(self.action(kind), generation=7, **kwargs)

    def native_receipt_fixture(self, kind="comment"):
        """A committed Qt editor becomes unreadable; native records must prove it."""
        self.fixture()
        self.source.update(post_id="10001", interaction_identity_verified=True)
        self.source["comments"][0]["comment_id"] = "20001"
        action = self.action(kind)
        if kind == "reply":
            action["comment_id"] = "20001"
        fresh = copy.deepcopy(self.source)
        fresh["comments"].append({"comment_id": "30001", "author_id": "wxid_bot",
            "content": action["text"], "identity_verified": True,
            "ref_comment_id": "20001" if kind == "reply" else "",
            "ref_author_id": "wxid_comment" if kind == "reply" else ""})
        def stale_editor():
            self.auto.focused = self.auto.editor
            self.comment_editor.GetRuntimeId = lambda: []
            self.comment_editor.GetValuePattern = Mock(side_effect=OSError("old editor no longer exists"))
        self.send.click_action = stale_editor
        self.feed_client.read_post_details.return_value = fresh
        return action, fresh

    def execute_with_receipt_wait(self, action, **kwargs):
        # Keep the production predicate/cancellation checks; only bound failed
        # synthetic polling so an absent native record does not cost six seconds.
        original_wait = self.module.wait_until
        def bounded_wait(predicate, timeout=2.5, cancelled=None, interval=.06):
            return original_wait(predicate, min(timeout, .025), cancelled, min(interval, .005))
        with patch.object(self.module, "wait_until", side_effect=bounded_wait):
            return self.social.execute_interaction(action, generation=7, **kwargs)

    def wheel_fixture(self):
        """Inject all mouse primitives; no real cursor or wheel API is used."""
        self.fixture()
        self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="small+",
            SmallDecrement="small-", LargeIncrement="large+")
        mouse = {"cursor": (0, 0), "moves": [], "wheels": []}
        self.after_move = self.after_wheel = None
        def move(x, y, **options):
            self.assertEqual(options, {"moveSpeed": 0, "waitTime": 0})
            mouse["cursor"] = (x, y)
            mouse["moves"].append((x, y))
            if self.after_move:
                self.after_move()
        def wheel(direction, **options):
            count = options.pop("wheelTimes")
            self.assertIn(count, (1, 2, 3))
            self.assertEqual(options, {"interval": 0, "waitTime": 0})
            for _ in range(count):
                mouse["wheels"].append(direction)
                if self.after_wheel:
                    self.after_wheel()
        self.auto.MoveTo = move
        self.auto.GetCursorPos = lambda: mouse["cursor"]
        self.auto.WheelUp = lambda **options: wheel("up", **options)
        self.auto.WheelDown = lambda **options: wheel("down", **options)
        self.auto.ControlFromPoint = lambda x, y: self.row if (
            self.row.BoundingRectangle.left <= x < self.row.BoundingRectangle.right
            and self.row.BoundingRectangle.top <= y < self.row.BoundingRectangle.bottom) else self.auto.main
        self.auto.main.SetActive = self.feed.SetFocus = self.row.SetFocus = Mock(
            side_effect=AssertionError("wheel navigation must not focus or reactivate"))
        return mouse

    def two_phase_reply_fixture(self, *, reopen=True, after_first=None, after_second=None, caption="回复 Carol"):
        """The first leaf navigation closes Qt's empty generic editor."""
        self.fixture()
        initial_editor = self.comment_editor
        navigations = []
        def navigate():
            navigations.append(self.original_comment.Name)
            if len(navigations) == 1:
                self.holder.children.remove(self.view)
                initial_editor.IsOffscreen = True
                self.auto.focused = self.auto.editor
                if after_first:
                    after_first(initial_editor)
                return
            if not reopen:
                return
            self.view = self.holder.add(Control(self.auto, rect=Rect(20, 660, 980, 780),
                klass="mmui::CommentReplyTextView"))
            self.comment_editor = self.view.add(Control(self.auto, "", "EditControl",
                Rect(30, 675, 800, 725), klass="mmui::CommentReplyTextEdit"))
            self.comment_editor.HelpText = caption
            def set_value(text):
                self.writes.append(text)
                self.comment_editor.text = text
                if self.after_write:
                    self.after_write()
                return True
            self.comment_editor.GetValuePattern = lambda: types.SimpleNamespace(
                Value=self.comment_editor.text, SetValue=set_value, IsReadOnly=False)
            self.send = self.view.add(Control(self.auto, "发送", "ButtonControl", Rect(815, 675, 965, 725)))
            self.send.click_action = self.close_editor
            self.auto.focused = self.comment_editor
            if after_second:
                after_second()
        self.original_comment.click_action = navigate
        return navigations, initial_editor

    def collapsed_reply_fixture(self, *, after_collapse=None, after_reopen=None):
        """Qt destroys the old child provider; all pointer input stays fake."""
        original = {}
        def destroy(initial_editor):
            original["cell_rect"] = self.cell.BoundingRectangle
            self.cell.BoundingRectangle = Rect(10, 560, 990, 650)
            for control in (initial_editor, self.original_comment):
                control.GetRuntimeId = lambda: []
                control.ProcessId, control.ClassName, control.Name = 0, "", ""
            initial_editor.IsOffscreen = None
            initial_editor.GetValuePattern = lambda: None
            self.auto.focused = self.cell
            if after_collapse:
                after_collapse(initial_editor)
        def reopened():
            self.cell.BoundingRectangle = original["cell_rect"]
            if after_reopen:
                after_reopen()
        navigations, initial_editor = self.two_phase_reply_fixture(
            after_first=destroy, after_second=reopened)
        self.source["interaction_identity_verified"] = True
        geometry_clicks = []
        navigate = self.original_comment.click_action
        def cell_click(**options):
            geometry_clicks.append(options)
            self.auto.clicks.append("[UIA reply navigation]")
            navigate()
        self.cell.Click = cell_click
        original_hit = self.auto.ControlFromPoint
        self.auto.ControlFromPoint = lambda x, y: self.cell if (
            self.cell.BoundingRectangle.left <= x < self.cell.BoundingRectangle.right
            and self.cell.BoundingRectangle.top <= y < self.cell.BoundingRectangle.bottom) else original_hit(x, y)
        return navigations, geometry_clicks, initial_editor

    def ocr_reply_fixture(self, *, text="回复 Carol", during_read=None):
        """Caption recognition is mocked; neither screen capture nor OCR runs."""
        self.fixture()
        self.source["interaction_identity_verified"] = True
        self.original_comment.click_action = lambda: setattr(self.comment_editor, "HelpText", "")
        previous_hit = self.auto.ControlFromPoint
        self.auto.ControlFromPoint = lambda x, y: self.comment_editor if (
            self.comment_editor.BoundingRectangle.left <= x < self.comment_editor.BoundingRectangle.right
            and self.comment_editor.BoundingRectangle.top <= y < self.comment_editor.BoundingRectangle.bottom
            ) else previous_hit(x, y)
        def recognize(box):
            self.assertEqual(box, (30, 675, 800, 725))
            if during_read:
                during_read()
            return text
        self.social._read_reply_caption = Mock(side_effect=recognize)
        return self.social._read_reply_caption

    def assert_not_submitted(self, result):
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["submit_attempted"], result)
        self.assertFalse(result["outcome_uncertain"], result)
        self.assertNotIn("发送", self.auto.clicks)
        self.assertNotIn("赞", self.auto.clicks)

    def test_refresh_open_feed_keeps_original_main_and_never_toggles(self):
        self.fixture()
        result = self.social.refresh_feed(generation=7)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.auto.clicks, ["刷新"])
        self.assertEqual(self.auto.main.NativeWindowHandle, 100)
        self.assertIn(self.feed, self.auto.main.children)
        self.feed_client.fetch.assert_not_called()

    def test_refresh_trusted_original_main_pane_role_is_supported(self):
        self.fixture()
        self.auto.main.ControlTypeName = "PaneControl"
        self.assertIs(self.social._main_window(), self.auto.main)
        result = self.social.refresh_feed(generation=7)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.auto.clicks, ["刷新"])
        self.feed_client.fetch.assert_not_called()

    def test_nested_equal_navigation_wrappers_open_feed_but_siblings_remain_ambiguous(self):
        self.fixture()
        for control in (self.feed, self.toolbar, self.entry):
            self.auto.main.children.remove(control)
        discover = self.auto.main.add(Control(self.auto, "发现", "ButtonControl", Rect(0, 180, 60, 230)))
        button = discover.add(Control(self.auto, "发现", "ButtonControl", discover.BoundingRectangle))
        def show_moments():
            self.auto.main.add(self.entry)
            self.entry.click_action = lambda: (self.auto.main.add(self.feed), self.auto.main.add(self.toolbar))
        button.click_action = show_moments
        self.assertIs(self.social._open_feed(), self.feed)
        self.assertEqual(self.auto.clicks, ["发现", "朋友圈"])
        self.auto.main.add(Control(self.auto, "发现", "ButtonControl", discover.BoundingRectangle))
        self.assertIsNone(self.social._main_named({"发现"}))

    def test_profile_can_be_embedded_under_original_main_without_physical_popup(self):
        self.fixture()
        popup = self.open_profile("author_alias")
        self.auto.extra_windows.remove(popup)
        popup.NativeWindowHandle = 0
        popup.ControlTypeName = "PaneControl"
        self.auto.main.add(popup)
        self.assertIs(self.social._profile_popup(), popup)
        self.assertEqual(self.social._read_profile_actor(self.records), "wxid_author")
        popup.ProcessId = 20
        self.assertIsNone(self.social._profile_popup())

    def test_duplicate_root_wrappers_for_one_profile_hwnd_are_not_two_people(self):
        self.fixture()
        first = self.open_profile("author_alias")
        second = self.open_profile("author_alias")
        self.assertNotEqual(first.GetRuntimeId(), second.GetRuntimeId())
        self.assertEqual(self.social._read_profile_actor(self.records), "wxid_author")
        second.NativeWindowHandle += 1
        self.assertIsNone(self.social._profile_popup())

    def test_refresh_missing_exact_button_name_stops_before_click(self):
        self.fixture()
        self.refresh.Name = "Reload"
        result = self.social.refresh_feed(generation=7)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"], "朋友圈刷新前窗口或按钮已经改变")
        self.assertEqual(self.auto.clicks, [])

    def test_refresh_reacquires_rebuilt_provider_before_freezing_final_button(self):
        for wrapper in ("reused", "recreated", "same_native"):
            with self.subTest(wrapper=wrapper):
                self.fixture()
                original_unique, queries = self.social._unique, [0]
                def unique(root, predicate):
                    found = original_unique(root, predicate)
                    if root is self.toolbar and found is self.refresh:
                        queries[0] += 1
                        if queries[0] == 2:
                            if wrapper != "same_native":
                                self.refresh.rid += 100000
                            if wrapper != "reused":
                                found = Control(self.auto, "刷新", "ButtonControl")
                                found.rid = self.refresh.rid
                                self.toolbar.children = []
                                self.toolbar.add(found)
                    return found
                self.social._unique = unique
                result = self.social.refresh_feed(generation=7)
                self.assertTrue(result["ok"], result)
                self.assertEqual(self.auto.clicks, ["刷新"])

    def test_refresh_rebuilt_live_toolbar_uses_current_button_geometry(self):
        self.fixture()
        original, queries = self.social._unique, [0]
        def unique(root, predicate):
            found = original(root, predicate)
            if root is self.toolbar and found is self.refresh:
                queries[0] += 1
                if queries[0] == 2:
                    rebuilt = Control(self.auto, "刷新", "ButtonControl", Rect(900, 25, 940, 65))
                    self.toolbar.children = []
                    self.toolbar.add(rebuilt)
                    return rebuilt
            return found
        self.social._unique = unique
        result = self.social.refresh_feed(generation=7)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.auto.clicks, ["刷新"])

    def test_manual_refresh_and_account_stage_resume_main_from_chrome(self):
        self.fixture()
        chrome = Control(self.auto, "控制面板", "WindowControl", klass="Chrome_WidgetWin_1", pid=20, handle=200)
        self.auto.foreground = chrome
        result = self.social.refresh_feed(generation=7)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.auto.clicks, ["刷新"])
        # Native feed/contact reads can run while the user inspects the panel.
        self.auto.foreground = chrome
        self.social._verify_account("wxid_bot", self.records)
        self.assertIs(self.auto.foreground, self.auto.main)
        self.assertEqual(self.auto.extra_windows, [])
        self.assertNotIn("发送", self.auto.clicks)

    def test_refresh_reparented_toolbar_cannot_follow_same_pid_popup_foreground(self):
        for role in ("WindowControl", "PaneControl"):
            with self.subTest(role=role):
                self.fixture()
                original_unique, queries = self.social._unique, [0]
                def unique(root, predicate):
                    found = original_unique(root, predicate)
                    if root is self.toolbar and found is self.refresh:
                        queries[0] += 1
                        if queries[0] == 2:
                            popup = Control(self.auto, "synthetic popup", role,
                                klass="mmui::ChatWindow", handle=110)
                            self.auto.extra_windows.append(popup)
                            self.auto.main.children.remove(self.toolbar)
                            popup.add(self.toolbar)
                            self.auto.foreground = popup
                    return found
                self.social._unique = unique
                result = self.social.refresh_feed(generation=7)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["error"], "朋友圈刷新按钮无法确认")
                self.assertEqual(self.auto.clicks, [])

    def test_refresh_final_foreground_query_cannot_hide_token_drift_or_cancellation(self):
        for drift in ("token", "cancel"):
            with self.subTest(drift=drift):
                self.fixture()
                event = threading.Event()
                original_unique, queries = self.social._unique, [0]
                original_foreground = self.social._foreground
                def unique(root, predicate):
                    found = original_unique(root, predicate)
                    if root is self.toolbar and found is self.refresh:
                        queries[0] += 1
                    return found
                def foreground(root=None):
                    result = original_foreground(root)
                    if queries[0] == 2:
                        if drift == "token":
                            self.refresh.rid += 100000
                        else:
                            event.set()
                    return result
                self.social._unique, self.social._foreground = unique, foreground
                result = self.social.refresh_feed(generation=7, cancel_event=event)
                self.assertFalse(result["ok"], result)
                self.assertEqual(self.auto.clicks, [])

    def test_popout_feed_cannot_replace_original_main_feed(self):
        self.fixture()
        self.auto.main.children.remove(self.feed)
        popout = Control(self.auto, "Alice", "WindowControl", klass="mmui::ChatWindow", handle=110)
        popout.add(self.feed)
        self.auto.extra_windows.append(popout)
        self.assertIsNone(self.social._feed())
        self.assertIs(self.social._main_window(), self.auto.main)
        self.auto.main.NativeWindowHandle = 111
        self.assertIsNone(self.social._main_window())

    def test_refresh_window_replaced_after_control_lookup_never_clicks_stale_button(self):
        self.fixture()
        native_id = self.refresh.GetRuntimeId
        def replaced():
            self.auto.main.NativeWindowHandle = 111
            return native_id()
        self.refresh.GetRuntimeId = replaced
        result = self.social.refresh_feed(generation=7)
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.auto.clicks, [])

    def test_source_invalid_original_ids_or_identity_never_open_profiles(self):
        for field, value in (("post_id", "cmt_generated"), ("post_id", "0"),
                ("post_id", True), ("author_id", "显示昵称"), ("author_id", "123@chatroom"),
                ("identity_verified", False)):
            with self.subTest(field=field, value=value):
                self.fixture()
                action = self.action()
                action["source"][field] = value
                result = self.social.execute_interaction(action, generation=7)
                self.assert_not_submitted(result)
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])

    def test_fresh_cache_changed_actor_or_content_refuses_ui_write(self):
        for field, value in (("author_id", "wxid_other"), ("content", "已改变"), ("created_at", 1)):
            with self.subTest(field=field):
                self.fixture()
                cached = copy.deepcopy(self.source)
                cached[field] = value
                self.feed_client.fetch.side_effect = lambda **kw: [cached]
                self.assert_not_submitted(self.execute())
                self.assertEqual(self.writes, [])
                self.assertEqual(self.auto.clicks, [])

    def test_same_content_and_time_with_different_native_post_ids_refuses(self):
        self.fixture()
        other = copy.deepcopy(self.source)
        other["post_id"] = "native-post-10002"
        self.feed_client.fetch.side_effect = lambda **kw: [copy.deepcopy(self.source), other]
        result = self.execute()
        self.assert_not_submitted(result)
        self.assertIn("无法唯一", result["error"])
        self.assertEqual(self.auto.clicks, [])

    def test_post_collision_uses_same_whitespace_normalization_as_live_uia(self):
        self.fixture()
        self.source["content"] = "晴天  散步"
        self.row.Name = "Alice 晴天 散步 1分钟前"
        other = copy.deepcopy(self.source)
        other.update(post_id="native-post-10002", content="晴天 散步")
        self.feed_client.fetch.side_effect = lambda **kw: [copy.deepcopy(self.source), other]
        self.assert_not_submitted(self.execute())
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.writes, [])

    def test_live_duplicate_rows_are_ambiguous_even_with_verified_native_id(self):
        self.fixture()
        self.feed.add(Control(self.auto, self.row.Name, "ListItemControl", self.row.BoundingRectangle,
            klass="mmui::TimelineContentCell"))
        result = self.execute()
        self.assert_not_submitted(result)
        self.assertIn("重复正文", result["error"])
        self.assertEqual(self.writes, [])

    def test_relative_minute_can_lag_in_an_open_feed(self):
        self.fixture()
        self.assertTrue(self.social._time_matches("20分钟前", 0, now=2069))
        self.assertFalse(self.social._time_matches("20分钟前", 0, now=120))
        self.assertFalse(self.social._time_matches("20分钟前", 0, now=7200))

    def test_minute_label_across_midnight_cannot_disambiguate_repeated_post(self):
        self.fixture()
        current = dict(self.source, created_at=1717948860)
        other = dict(current, post_id="different-post", created_at=current["created_at"] - 120)
        self.feed_client.fetch.side_effect = lambda **kw: [current, other]
        with self.assertRaisesRegex(RuntimeError, "无法唯一"):
            self.social._fresh_source(current, {"type":"comment"}, "wxid_bot", self.records)

    def test_distinct_controls_need_no_parent_walk_to_coalesce(self):
        self.fixture()
        with patch.object(self.module, "_parent", side_effect=AssertionError("unnecessary UIA ancestor lookup")):
            self.assertEqual(self.social._coalesce_wrappers([self.row, self.cell]), [self.row, self.cell])

    def test_sidebar_avatar_verifies_account_without_scrolling_the_feed(self):
        self.fixture()
        sidebar = self.auto.main.add(Control(self.auto, "导航", "ToolBarControl", Rect(5,5,65,500),
                                     aid="MainView.main_tabbar", klass="mmui::MainTabBar"))
        self.auto.ControlFromPoint = lambda x,y: sidebar if (x,y)==(35,65) else self.auto.main
        sidebar.click_action = lambda: self.open_profile("bot_alias")
        with patch.object(self.social, "_account_feed_context", side_effect=AssertionError("unexpected return to cover")):
            self.social._verify_account("wxid_bot", self.records)
            with self.assertRaisesRegex(RuntimeError, "不一致"):
                self.social._verify_account("wxid_author", self.records)
        self.assertEqual(self.auto.clicks, ["导航", "导航"])

    def test_two_wrappers_for_same_native_post_and_cover_are_single_elements(self):
        self.fixture()
        duplicate = Control(self.auto, self.row.Name, self.row.ControlTypeName,
            self.row.BoundingRectangle, klass=self.row.ClassName)
        duplicate.rid = self.row.rid
        duplicate.parent = self.feed
        self.feed.children.insert(self.feed.children.index(self.row)+1, duplicate)
        self.assertEqual(len(self.social._post_rows()), 1)
        self.assertEqual(self.social._locate_post(self.source, self.records).GetRuntimeId(), self.row.GetRuntimeId())
        self.assertIs(self.social._comment_cell(self.row), self.cell)
        cover_proxy = Control(self.auto, "", rect=self.cover.BoundingRectangle, klass=self.cover.ClassName)
        cover_proxy.rid = self.cover.rid
        cover_proxy.click_action = self.cover.click_action
        self.feed.add(cover_proxy)
        self.social._verify_account("wxid_bot", self.records)
        self.assertEqual(self.auto.clicks, [""])

    def test_partial_post_scrolls_small_step_until_fully_visible_using_bounds_progress(self):
        for side in ("top", "bottom"):
            with self.subTest(side=side):
                self.fixture()
                self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="small+",
                    SmallDecrement="small-", LargeIncrement="large+", LargeDecrement="large-")
                before_id, before_label = self.row.GetRuntimeId(), self.row.Name
                self.row.BoundingRectangle = Rect(10, 40, 990, 260) if side == "top" else Rect(10, 610, 990, 920)
                expected = "small-" if side == "top" else "small+"
                calls = []
                def scroll(horizontal, vertical, **options):
                    calls.append((horizontal, vertical))
                    self.assertEqual((horizontal, vertical), ("none", expected))
                    self.row.BoundingRectangle = Rect(10, 125, 990, 345) if side == "top" else Rect(10, 400, 990, 710)
                pattern = types.SimpleNamespace(VerticallyScrollable=True, Scroll=scroll)
                self.feed.GetScrollPattern = lambda: pattern
                row = self.social._locate_post(self.source, self.records)
                self.assertIs(row, self.row)
                self.assertEqual(calls, [("none", expected)])
                self.assertEqual(row.GetRuntimeId(), before_id)
                self.assertEqual(row.Name, before_label)
                self.assertGreaterEqual(row.BoundingRectangle.top, self.feed.BoundingRectangle.top)
                self.assertLessEqual(row.BoundingRectangle.bottom, self.feed.BoundingRectangle.bottom)
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])

    def test_tall_post_verifies_author_then_scrolls_to_its_menu_for_like_and_comment(self):
        for kind in ("like", "comment"):
            with self.subTest(kind=kind):
                self.fixture()
                self.row.BoundingRectangle = Rect(10, 340, 990, 1300)
                self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="down")
                def scroll(horizontal, vertical, **options):
                    self.assertIn(self.row.Name, self.auto.clicks, "author profile must be opened first")
                    self.assertEqual(vertical, "down")
                    self.row.BoundingRectangle = Rect(10, -400, 990, 560)
                pattern = types.SimpleNamespace(VerticallyScrollable=True, Scroll=Mock(side_effect=scroll))
                self.feed.GetScrollPattern = lambda: pattern
                result = self.execute(kind)
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["submit_attempted"])
                self.assertEqual(self.auto.clicks.count("赞" if kind == "like" else "发送"), 1)
                pattern.Scroll.assert_called_once()

    def test_tall_post_rebuilt_as_another_target_does_not_submit(self):
        self.fixture()
        self.row.BoundingRectangle = Rect(10, 340, 990, 1300)
        self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="down", LargeIncrement="jump")
        def replace_row(*args, **kwargs):
            self.row.Name = "Alice 不同的动态 1分钟前"
            self.row.BoundingRectangle = Rect(10, -400, 990, 560)
        self.feed.GetScrollPattern = lambda: types.SimpleNamespace(VerticallyScrollable=True, Scroll=replace_row)
        result = self.execute("like")
        self.assert_not_submitted(result)
        self.assertEqual(result["stage"], "relocate_post")
        self.assertNotIn("赞", self.auto.clicks)

    def test_fully_visible_small_post_moves_above_bottom_notification_area(self):
        for wheel in (False, True):
            with self.subTest(wheel=wheel):
                mouse = self.wheel_fixture() if wheel else None
                if not wheel:
                    self.fixture()
                self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="small+",
                    SmallDecrement="small-", LargeIncrement="large+")
                self.row.BoundingRectangle = Rect(10, 610, 990, 760)
                token, label = self.row.GetRuntimeId(), self.row.Name
                calls = []
                def scroll(*args, **kwargs):
                    calls.append((args, kwargs))
                    self.row.BoundingRectangle = Rect(10, 340, 990, 490)
                if wheel:
                    self.after_wheel = scroll
                else:
                    self.feed.GetScrollPattern = lambda: types.SimpleNamespace(VerticallyScrollable=True, Scroll=scroll)
                self.assertIs(self.social._locate_post(self.source, self.records), self.row)
                self.assertEqual(self.row.GetRuntimeId(), token)
                self.assertEqual(self.row.Name, label)
                self.assertEqual(len(calls), 1)
                if wheel: self.assertEqual(mouse["wheels"], ["down"])
                self.assertLessEqual(self.row.BoundingRectangle.bottom, 600)
                self.assertEqual(self.auto.clicks, [])

    def test_small_post_centering_stops_at_feed_end_with_fresh_visible_row(self):
        for reason in ("no_progress", "not_scrollable"):
            with self.subTest(reason=reason):
                self.fixture()
                self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="small+")
                self.row.BoundingRectangle = Rect(10, 610, 990, 760)
                scroll = Mock()
                self.feed.GetScrollPattern = lambda: types.SimpleNamespace(
                    VerticallyScrollable=reason != "not_scrollable", Scroll=scroll)
                self.assertIs(self.social._locate_post(self.source, self.records), self.row)
                self.assertEqual(scroll.call_count, 1 if reason == "no_progress" else 0)
                self.assertEqual(self.auto.clicks, [])

    def test_small_post_centering_rejects_row_identity_cancel_or_foreground_drift(self):
        for reason in ("label", "cancel", "foreground"):
            with self.subTest(reason=reason):
                self.fixture()
                self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="small+")
                self.row.BoundingRectangle = Rect(10, 610, 990, 760)
                event = threading.Event()
                self.social._cancelled = event.is_set
                def scroll(*args, **kwargs):
                    self.row.BoundingRectangle = Rect(10, 340, 990, 490)
                    if reason == "label": self.row.Name = "Alice 另一条动态 1分钟前"
                    elif reason == "cancel": event.set()
                    else: self.auto.foreground = Control(self.auto, "Other", "WindowControl", pid=20, handle=200)
                self.feed.GetScrollPattern = lambda: types.SimpleNamespace(VerticallyScrollable=True, Scroll=scroll)
                with self.assertRaises(RuntimeError):
                    self.social._locate_post(self.source, self.records)
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])

    def test_partial_post_without_scroll_or_observable_progress_is_rejected(self):
        for reason in ("no_pattern", "not_scrollable", "no_progress"):
            with self.subTest(reason=reason):
                self.fixture()
                self.row.BoundingRectangle = Rect(10, 610, 990, 920)
                self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="small+",
                    SmallDecrement="small-", LargeIncrement="large+")
                scroll = Mock()
                if reason != "no_pattern":
                    pattern = types.SimpleNamespace(VerticallyScrollable=reason != "not_scrollable", Scroll=scroll)
                    self.feed.GetScrollPattern = lambda: pattern
                with self.assertRaises(RuntimeError):
                    self.social._locate_post(self.source, self.records)
                self.assertEqual(scroll.call_count, 1 if reason == "no_progress" else 0)
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])

    def test_scroll_reacquires_target_and_refuses_ambiguity_cancel_or_replaced_main(self):
        for reason in ("ambiguous", "cancel", "replaced_main"):
            with self.subTest(reason=reason):
                self.fixture()
                self.row.BoundingRectangle = Rect(10, 610, 990, 920)
                self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="small+",
                    SmallDecrement="small-", LargeIncrement="large+")
                event = threading.Event()
                self.social._cancelled = event.is_set
                calls = []
                def scroll(horizontal, vertical, **options):
                    calls.append((horizontal, vertical))
                    self.row.BoundingRectangle = Rect(10, 400, 990, 710)
                    if reason == "ambiguous":
                        self.feed.add(Control(self.auto, self.row.Name, "ListItemControl", Rect(10, 120, 990, 330),
                            klass="mmui::TimelineContentCell"))
                    elif reason == "cancel":
                        event.set()
                    else:
                        self.auto.main.NativeWindowHandle = 111
                pattern = types.SimpleNamespace(VerticallyScrollable=True, Scroll=scroll)
                self.feed.GetScrollPattern = lambda: pattern
                with self.assertRaises(RuntimeError):
                    self.social._locate_post(self.source, self.records)
                self.assertEqual(calls, [("none", "small+")])
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])

    def test_scroll_pattern_provider_drift_is_rechecked_before_any_navigation(self):
        for reason in ("foreground", "main_handle", "feed_pid"):
            with self.subTest(reason=reason):
                self.fixture()
                self.row.BoundingRectangle = Rect(10, 610, 990, 920)
                self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="small+",
                    SmallDecrement="small-", LargeIncrement="large+")
                scroll = Mock()
                pattern = types.SimpleNamespace(VerticallyScrollable=True, Scroll=scroll)
                def pattern_lookup():
                    # A COM provider call may complete after the user or Qt has
                    # replaced the foreground/window/virtual feed identity.
                    if reason == "foreground":
                        self.auto.foreground = Control(self.auto, "Other", "WindowControl", pid=20, handle=200)
                    elif reason == "main_handle":
                        self.auto.main.NativeWindowHandle = 111
                    elif reason == "feed_runtime":
                        self.feed.rid += 10000
                    else:
                        self.feed.ProcessId = 20
                    return pattern
                self.feed.GetScrollPattern = pattern_lookup
                with self.assertRaises(RuntimeError):
                    self.social._locate_post(self.source, self.records)
                scroll.assert_not_called()
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])

    def test_uia_bound_wheel_fallback_scrolls_partial_post_without_focus_or_activation(self):
        for side in ("top", "bottom"):
            with self.subTest(side=side):
                mouse = self.wheel_fixture()
                self.row.BoundingRectangle = Rect(10, 40, 990, 260) if side == "top" else Rect(10, 610, 990, 920)
                original, viewport = self.row.BoundingRectangle, self.feed.BoundingRectangle
                self.after_wheel = lambda: setattr(self.row, "BoundingRectangle", Rect(10, 340, 990, 560))
                row = self.social._locate_post(self.source, self.records)
                self.assertIs(row, self.row)
                self.assertEqual(mouse["wheels"], ["up" if side == "top" else "down"])
                self.assertEqual(len(mouse["moves"]), 1)
                x, y = mouse["moves"][0]
                self.assertTrue(max(original.left, viewport.left) < x < min(original.right, viewport.right))
                self.assertTrue(max(original.top, viewport.top) < y < min(original.bottom, viewport.bottom))
                self.auto.main.SetActive.assert_not_called()
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])

    def test_wheel_burst_reacquires_geometry_before_finding_absent_target(self):
        mouse = self.wheel_fixture()
        expected_name = self.row.Name
        self.row.Name = "Alice 另一条动态 1分钟前"
        def progress():
            tick = len(mouse["wheels"])
            self.row.BoundingRectangle = Rect(10, 340 - tick * 40, 990, 560 - tick * 40)
            if tick == 3:
                self.row.Name = expected_name
        self.after_wheel = progress
        self.assertIs(self.social._locate_post(self.source, self.records), self.row)
        self.assertEqual(mouse["wheels"], ["down"] * 3)
        self.assertEqual(mouse["moves"], [(500, 450)])
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.writes, [])

    def test_wheel_hit_on_real_post_descendant_is_anchored_to_its_row(self):
        mouse = self.wheel_fixture()
        child = self.row.add(Control(self.auto, "图片", "ImageControl", Rect(200, 350, 800, 550)))
        self.auto.ControlFromPoint = lambda x, y: child
        self.social._wheel_feed(self.feed)
        self.assertEqual(mouse["wheels"], ["down"])
        child.parent = self.auto.main
        with self.assertRaises(RuntimeError):
            self.social._wheel_feed(self.feed)
        self.assertEqual(mouse["wheels"], ["down"])

    def test_wheel_hit_on_verified_list_viewport_is_safe_navigation(self):
        mouse = self.wheel_fixture()
        self.auto.ControlFromPoint = lambda x, y: self.feed
        self.social._wheel_feed(self.feed)
        self.assertEqual(mouse["wheels"], ["down"])
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.writes, [])

    def test_comment_block_filling_viewport_can_scroll_without_visible_content_row(self):
        mouse = self.wheel_fixture()
        self.row.BoundingRectangle = Rect(10, -250, 990, -30)
        self.cell.BoundingRectangle = self.feed.BoundingRectangle
        self.auto.ControlFromPoint = lambda x, y: self.cell
        self.social._wheel_feed(self.feed)
        self.assertEqual(mouse["wheels"], ["down"])
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.writes, [])

    def test_empty_qt_hit_uses_physical_window_owner_only_for_wheel_navigation(self):
        for owner in ("main", "foreign_hwnd", "foreign_pid", "changed_after_move"):
            with self.subTest(owner=owner):
                mouse = self.wheel_fixture()
                self.row.BoundingRectangle = Rect(10, -250, 990, -30)
                self.auto.ControlFromPoint = lambda x, y: None
                native = types.SimpleNamespace(WindowFromPoint=Mock(return_value=100),
                    GetAncestor=Mock(return_value=200 if owner == "foreign_hwnd" else 100))
                self.social._native_user32 = Mock(return_value=native)
                self.social._native_owner = Mock(return_value=(1, 20 if owner == "foreign_pid" else 10))
                # Keep native foreground probing synthetic as well.
                self.social._foreground = lambda root=None: True
                if owner == "changed_after_move":
                    self.after_move = lambda: setattr(native.GetAncestor, "return_value", 200)
                if owner == "main":
                    self.social._wheel_feed(self.feed)
                    self.assertEqual(mouse["wheels"], ["down"])
                else:
                    with self.assertRaises(RuntimeError):
                        self.social._wheel_feed(self.feed)
                    self.assertEqual(mouse["wheels"], [])
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])

    def test_old_post_beyond_eight_large_pages_is_located_without_clicks(self):
        self.fixture()
        self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", LargeIncrement="large+")
        wanted = self.row.Name
        self.row.Name = "Alice 第零页 1分钟前"
        pages = []
        def scroll(horizontal, vertical, **options):
            pages.append((horizontal, vertical))
            self.row.Name = wanted if len(pages) == 12 else f"Alice 第{len(pages)}页 1分钟前"
        self.feed.GetScrollPattern = lambda: types.SimpleNamespace(VerticallyScrollable=True, Scroll=scroll)
        self.assertIs(self.social._locate_post(self.source, self.records), self.row)
        self.assertEqual(pages, [("none", "large+")] * 12)
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.writes, [])

    def test_partial_native_post_uses_scroll_into_view_before_incremental_wheels(self):
        mouse = self.wheel_fixture()
        self.row.BoundingRectangle = Rect(10, 610, 990, 920)
        scroll = Mock(side_effect=lambda **kwargs: setattr(self.row, "BoundingRectangle", Rect(10, 340, 990, 650)))
        self.row.GetScrollItemPattern = lambda: types.SimpleNamespace(ScrollIntoView=scroll)
        self.assertIs(self.social._locate_post(self.source, self.records), self.row)
        scroll.assert_called_once_with(waitTime=0)
        self.assertEqual(mouse["wheels"], [])
        self.assertEqual(self.auto.clicks, [])

    def test_wheel_burst_enumerates_post_rows_once(self):
        mouse = self.wheel_fixture()
        original = self.social._post_rows
        self.social._post_rows = Mock(side_effect=original)
        self.social._wheel_feed(self.feed, steps=3)
        self.assertEqual(self.social._post_rows.call_count, 1)
        self.assertEqual(mouse["wheels"], ["down"] * 3)
        self.assertEqual(len(mouse["moves"]), 1)

    def test_virtual_hit_parent_link_can_be_proven_from_current_row_enumeration(self):
        mouse = self.wheel_fixture()
        leaf = self.row.add(Control(self.auto, "图片", "ImageControl", Rect(100, 350, 900, 550)))
        leaf.parent = None
        self.auto.ControlFromPoint = lambda x, y: leaf
        self.social._wheel_feed(self.feed)
        self.assertEqual(mouse["wheels"], ["down"])
        self.row.children.remove(leaf)
        with self.assertRaises(RuntimeError):
            self.social._wheel_feed(self.feed)
        self.assertEqual(mouse["wheels"], ["down"])

    def test_missing_parent_hit_enumeration_never_crosses_foreign_hwnd_or_pid(self):
        for pid, hwnd in ((10, 888), (20, 0)):
            with self.subTest(pid=pid, hwnd=hwnd):
                mouse = self.wheel_fixture()
                popup = self.row.add(Control(self.auto, "其它窗口", "WindowControl", pid=pid, handle=hwnd))
                hit = popup.add(Control(self.auto, "图片", "ImageControl", Rect(100, 350, 900, 550)))
                hit.parent = None  # The row can enumerate it, but its real owner is not this row.
                self.auto.ControlFromPoint = lambda x, y: hit
                self.assertFalse(self.social._row_contains_hit(self.row, hit))
                with self.assertRaises(RuntimeError):
                    self.social._wheel_feed(self.feed)
                self.assertEqual(mouse["wheels"], [])
                self.assertEqual(self.auto.clicks, [])

    def test_semantic_list_and_post_rebuild_during_mouse_motion_do_not_change_physical_target(self):
        mouse = self.wheel_fixture()
        self.after_move = lambda: (setattr(self.feed, "rid", self.feed.rid+100),
            setattr(self.row, "rid", self.row.rid+100))
        self.social._wheel_feed(self.feed)
        self.assertEqual(mouse["wheels"], ["down"])
        self.assertEqual(self.writes, [])

    def test_scroll_point_uses_visible_intersection_of_horizontally_clipped_post(self):
        mouse = self.wheel_fixture()
        self.row.BoundingRectangle = Rect(-500, 340, 1400, 560)
        self.social._wheel_feed(self.feed)
        self.assertEqual(mouse["moves"], [(500, 450)])
        self.assertEqual(mouse["wheels"], ["down"])

    def test_wheel_failure_returns_precise_native_diagnostics_without_post_text(self):
        self.wheel_fixture()
        self.auto.ControlFromPoint = lambda x, y: self.auto.main
        with self.assertRaises(RuntimeError) as caught:
            self.social._wheel_feed(self.feed)
        detail = caught.exception.uia_diagnostics
        self.assertEqual(detail["substage"], "wheel_hit")
        self.assertEqual(detail["expected_main"], {"hwnd": 100, "pid": 10})
        self.assertEqual(detail["observed_hit"]["hwnd"], 100)
        self.assertEqual(detail["observed_foreground"]["hwnd"], 100)
        self.assertEqual(detail["observed_feed"]["runtime_id"], self.feed.GetRuntimeId())
        self.assertIn("[wheel_hit]", str(caught.exception))
        self.assertNotIn("晴天散步", str(caught.exception))
        self.assertNotIn("原评论正文", str(detail))
        result = self.social._social_result(False, caught.exception)
        self.assertEqual(result["uia_diagnostics"], detail)

    def test_class_named_pane_feed_and_layout_wrappers_keep_account_and_reply_identity(self):
        self.fixture()
        self.feed.AutomationId = ""
        self.feed.ClassName = "mmui::TimeLineListView"
        self.feed.ControlTypeName = "PaneControl"
        layout = Control(self.auto, klass="mmui::LayoutPane", handle=100)
        children, self.feed.children = list(self.feed.children), []
        self.feed.add(layout)
        for control in children:
            layout.add(control)
        self.assertIs(self.social._feed(), self.feed)
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.writes, ["自然的回应"])

    def test_exposed_but_empty_selected_feed_is_warmed_without_toggling(self):
        self.fixture()
        children, self.feed.children = list(self.feed.children), []
        def warm(**kwargs):
            self.assertFalse(kwargs["controls_ready"]())
            self.feed.children = children
            return True
        self.social._recover_current_window = Mock(side_effect=warm)
        self.assertIs(self.social._open_feed(), self.feed)
        self.social._recover_current_window.assert_called_once()
        self.assertEqual(self.auto.clicks, [])

    def test_navigation_probes_do_not_scan_inside_every_post_delegate(self):
        self.fixture()
        self.row.GetChildren = Mock(side_effect=AssertionError("navigation must not read post subtree"))
        self.cell.GetChildren = Mock(side_effect=AssertionError("navigation must not read comment subtree"))
        self.assertIs(self.social._feed(), self.feed)
        self.assertIs(self.social._sns_toolbar(), self.toolbar)
        self.assertIs(self.social._main_named({"朋友圈"}), self.entry)
        self.row.GetChildren.assert_not_called()
        self.cell.GetChildren.assert_not_called()

    def test_account_read_accepts_semantic_list_runtime_rebuild_and_reports_real_mismatch(self):
        self.fixture()
        previous = tuple(self.feed.GetRuntimeId())
        self.feed.rid += 100
        self.assertIs(self.social._account_feed_context(previous), self.feed)
        self.auto.foreground = Control(self.auto, "别的程序", "WindowControl", pid=20, handle=200)
        with self.assertRaises(RuntimeError) as caught:
            self.social._account_feed_context(previous)
        self.assertEqual(caught.exception.uia_diagnostics["substage"], "account_feed_context")
        self.assertEqual(caught.exception.uia_diagnostics["observed_foreground"]["pid"], 20)

    def test_account_context_draft_checks_do_not_walk_timeline_rows(self):
        self.fixture()
        self.feed.GetChildren = Mock(side_effect=AssertionError("account context must not scan the timeline"))
        self.assertIs(self.social._account_feed_context(), self.feed)
        self.feed.GetChildren.assert_not_called()

    def test_error_diagnostic_does_not_scan_feed_or_missing_sns_tree(self):
        self.fixture()
        self.feed.GetChildren = Mock(side_effect=AssertionError("error path must not read feed children"))
        self.auto.main.GetChildren = Mock(side_effect=AssertionError("error path must not scan SNS"))
        for feed in (self.feed, None):
            error = self.social._ui_error("synthetic", "synthetic failure", main=self.auto.main, feed=feed)
            self.assertEqual(error.uia_diagnostics["expected_main"], {"hwnd": 100, "pid": 10})
            self.assertNotIn("feed_children", error.uia_diagnostics)
            self.assertNotIn("sns_candidates", error.uia_diagnostics)
        self.feed.GetChildren.assert_not_called()
        self.auto.main.GetChildren.assert_not_called()

    def test_cancelled_moments_releases_desktop_without_fresh_diagnostic_reads(self):
        self.fixture()
        cancelled = threading.Event()
        with self.social._social_operation(cancelled, 7):
            self.social._update_snapshot = Mock(side_effect=AssertionError("cancelled exit must not scan"))
            self.auto.GetForegroundControl = Mock(side_effect=AssertionError("cancelled error must not read UI"))
            self.social._native_attributes = Mock(side_effect=AssertionError("cancelled error must not read properties"))
            cancelled.set()
            with self.assertRaises(uia_fixtures.DesktopCancelled):
                self.social._ui_error("synthetic", "cancelled", main=self.auto.main, feed=self.feed)
        self.social._update_snapshot.assert_not_called()
        self.auto.GetForegroundControl.assert_not_called()
        self.social._native_attributes.assert_not_called()
        self.assertEqual(self.social._snapshot["snapshot_source"], "operation_observation")

    def test_completed_wheel_burst_reacquires_rebuilt_feed_and_row(self):
        mouse = self.wheel_fixture()
        def rebuild():
            previous = self.feed
            replacement = Control(self.auto, previous.Name, previous.ControlTypeName,
                previous.BoundingRectangle, previous.AutomationId, previous.ClassName)
            replacement.GetScrollPattern = lambda: None
            self.auto.main.children[self.auto.main.children.index(previous)] = replacement
            replacement.parent = self.auto.main
            for control in previous.children:
                replacement.add(control)
            self.feed = replacement
            self.row.rid += 1000
            rect = self.row.BoundingRectangle
            self.row.BoundingRectangle = Rect(rect.left, rect.top-20, rect.right, rect.bottom-20)
        self.after_wheel = rebuild
        self.social._wheel_feed(self.feed, steps=3)
        self.assertEqual(mouse["wheels"], ["down"] * 3)
        self.assertEqual(mouse["moves"], [(500, 450)])
        self.assertEqual(self.auto.clicks, [])

    def test_rebuilt_provider_alone_is_not_scroll_progress(self):
        mouse = self.wheel_fixture()
        self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="small+",
            SmallDecrement="small-", LargeIncrement="large+")
        self.row.BoundingRectangle = Rect(10, 610, 990, 920)
        self.after_wheel = lambda: (setattr(self.feed, "rid", self.feed.rid+100),
            setattr(self.row, "rid", self.row.rid+100))
        with self.assertRaisesRegex(RuntimeError, "唯一定位"):
            self.social._locate_post(self.source, self.records)
        self.assertEqual(mouse["wheels"], ["down"])

    def test_centering_completed_scroll_can_rebuild_same_unique_source_row(self):
        self.fixture()
        self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="small+")
        self.row.BoundingRectangle = Rect(10, 610, 990, 760)
        def scroll(*args, **kwargs):
            self.row.rid += 1000
            self.feed.rid += 1000
            self.row.BoundingRectangle = Rect(10, 340, 990, 490)
        self.feed.GetScrollPattern = lambda: types.SimpleNamespace(VerticallyScrollable=True, Scroll=scroll)
        self.assertIs(self.social._locate_post(self.source, self.records), self.row)
        self.assertEqual(self.auto.clicks, [])

    def test_wheel_fallback_requires_exact_visible_post_hit_and_actual_progress(self):
        for reason in ("obscured", "no_intersection", "tiny_intersection", "no_progress", "explicit_not_scrollable"):
            with self.subTest(reason=reason):
                mouse = self.wheel_fixture()
                self.row.BoundingRectangle = Rect(10, 610, 990, 920)
                if reason == "obscured":
                    self.auto.ControlFromPoint = lambda x, y: self.auto.main
                elif reason == "no_intersection":
                    self.row.BoundingRectangle = Rect(10, 800, 990, 920)
                elif reason == "tiny_intersection":
                    self.row.BoundingRectangle = Rect(10, 785, 990, 920)
                elif reason == "explicit_not_scrollable":
                    self.feed.GetScrollPattern = lambda: types.SimpleNamespace(VerticallyScrollable=False)
                with self.assertRaises(RuntimeError):
                    self.social._locate_post(self.source, self.records)
                self.assertEqual(len(mouse["wheels"]), 1 if reason == "no_progress" else 0)
                self.assertEqual(len(mouse["moves"]), 1 if reason == "no_progress" else 0)
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])

    def test_wheel_fallback_rechecks_controls_geometry_cursor_and_cancel_after_move(self):
        for reason in ("foreground", "main_handle", "feed_pid",
                "hit_pid", "cursor", "cancel"):
            with self.subTest(reason=reason):
                mouse = self.wheel_fixture()
                event = threading.Event()
                self.social._cancelled = event.is_set
                def drift():
                    if reason == "foreground":
                        self.auto.foreground = Control(self.auto, "Other", "WindowControl", pid=20, handle=200)
                    elif reason == "main_handle": self.auto.main.NativeWindowHandle = 111
                    elif reason == "feed_runtime": self.feed.rid += 10000
                    elif reason == "feed_pid": self.feed.ProcessId = 20
                    elif reason == "row_runtime": self.row.rid += 10000
                    elif reason == "hit_pid": self.row.ProcessId = 20
                    elif reason == "row_bounds": self.row.BoundingRectangle = Rect(10, 340, 990, 570)
                    elif reason == "viewport": self.feed.BoundingRectangle = Rect(10, 100, 990, 780)
                    elif reason == "cursor": mouse["cursor"] = (0, 0)
                    else: event.set()
                self.after_move = drift
                with self.assertRaises(RuntimeError):
                    self.social._wheel_feed(self.feed)
                self.assertEqual(len(mouse["moves"]), 1)
                self.assertEqual(mouse["wheels"], [])
                self.assertEqual(self.auto.clicks, [])

    def test_navigation_accepts_rebuilt_list_shape_inside_same_live_viewport(self):
        mouse = self.wheel_fixture()
        def drift():
            self.feed.rid += 1000
            self.feed.AutomationId = "sns_list"
            self.feed.Name = ""
            self.feed.BoundingRectangle = Rect(10, 100, 990, 780)
            self.row.rid += 1000
            self.row.BoundingRectangle = Rect(10, 340, 990, 570)
        self.after_move = drift
        self.social._wheel_feed(self.feed)
        self.assertEqual(mouse["wheels"], ["down"])
        self.assertEqual(self.auto.clicks, [])

    def test_navigation_recovers_same_window_cold_feed_once_without_activating_foreign_window(self):
        self.fixture()
        original = self.social._feed
        self.social._feed = Mock(side_effect=[None, None, self.feed, self.feed, self.feed])
        self.social._wait_main = Mock(return_value=self.feed)
        shape = self.social._control_shape(self.feed)
        self.assertIs(self.social._feed_after_navigation(shape), self.feed)
        self.social._wait_main.assert_called_once_with(self.social._ready_feed, recover_missing=True)
        self.social._feed = Mock(return_value=None)
        self.auto.foreground = Control(self.auto, "Other", "WindowControl", pid=20, handle=200)
        self.social._wait_main.reset_mock()
        with self.assertRaises(RuntimeError):
            self.social._feed_after_navigation(shape)
        self.social._wait_main.assert_not_called()
        self.social._feed = original

    def test_wheel_burst_stops_following_navigation_when_foreground_or_cancellation_changes(self):
        for reason in ("foreground", "cancel"):
            with self.subTest(reason=reason):
                mouse = self.wheel_fixture()
                event = threading.Event()
                self.social._cancelled = event.is_set
                def changed():
                    if reason == "foreground":
                        self.auto.foreground = Control(self.auto, "Other", "WindowControl", pid=20, handle=200)
                    else:
                        event.set()
                self.after_wheel = changed
                with self.assertRaises(RuntimeError):
                    self.social._wheel_feed(self.feed, steps=3)
                self.assertEqual(len(mouse["moves"]), 1)
                self.assertEqual(mouse["wheels"], ["down"] * 3)
                self.assertEqual(self.auto.clicks, [])

    def test_stock_contacts_resolve_wechat_profile_when_extension_is_absent(self):
        self.fixture()
        contacts = list(self.records.values())
        refresh = Mock()
        refresh._get_snapshot.return_value = (None, "unavailable")
        refresh._get_list.return_value = (contacts, True)
        with patch.dict(sys.modules, {"name_refresh": refresh}):
            records = self.module.MomentsSocialSender._fresh_identities(self.social)
        refresh._get_list.assert_called_once_with("contacts")
        self.open_profile("author_alias")
        self.assertEqual(self.social._read_profile_actor(records), "wxid_author")

    def test_profile_card_with_several_lines_binds_alias_to_its_own_row(self):
        """Region and gender reuse the value class; the alias must stay unique.

        Requiring one ProfileTextView made every real card unverifiable and
        blocked likes, comments and replies before any click.
        """
        self.fixture()
        profile = Control(self.auto, "身份卡", "WindowControl",
            klass="mmui::ProfileUniquePop", handle=103)
        profile.add(Control(self.auto, "微信号：", "TextControl", rect=Rect(20, 100, 90, 120)))
        profile.add(Control(self.auto, "author_alias", "TextControl", aid="profile.basic_line_view",
            klass="mmui::ProfileTextView", rect=Rect(100, 100, 240, 120)))
        profile.add(Control(self.auto, "地区：", "TextControl", rect=Rect(20, 130, 90, 150)))
        profile.add(Control(self.auto, "山东 济南", "TextControl", aid="profile.basic_line_view",
            klass="mmui::ProfileTextView", rect=Rect(100, 130, 240, 150)))
        profile.add(Control(self.auto, "性别：", "TextControl", rect=Rect(20, 160, 90, 180)))
        profile.add(Control(self.auto, "男", "TextControl", aid="profile.basic_line_view",
            klass="mmui::ProfileTextView", rect=Rect(100, 160, 240, 180)))
        self.auto.extra_windows.append(profile)
        self.assertEqual(self.social._read_profile_actor(self.records), "wxid_author")

    def test_profile_card_without_alias_line_stops_instead_of_guessing(self):
        """No 微信号 line means no identity proof; never fall back to a name."""
        self.fixture()
        profile = Control(self.auto, "身份卡", "WindowControl",
            klass="mmui::ProfileUniquePop", handle=103)
        profile.add(Control(self.auto, "地区：", "TextControl", rect=Rect(20, 130, 90, 150)))
        profile.add(Control(self.auto, "山东 济南", "TextControl", aid="profile.basic_line_view",
            klass="mmui::ProfileTextView", rect=Rect(100, 130, 240, 150)))
        self.auto.extra_windows.append(profile)
        with self.assertRaisesRegex(RuntimeError, "没有可核验的微信号"):
            self.social._read_profile_actor(self.records)

    def test_profile_alias_must_map_uniquely_to_true_actor(self):
        self.fixture()
        self.open_profile("author_alias")
        self.assertEqual(self.social._read_profile_actor(self.records), "wxid_author")
        self.records["wxid_impostor"] = {"username": "wxid_impostor", "alias": "author_alias"}
        with self.assertRaisesRegex(RuntimeError, "唯一对应"):
            self.social._read_profile_actor(self.records)

    def account_scroll_fixture(self, *, wheel=False):
        mouse = self.wheel_fixture() if wheel else None
        if not wheel:
            self.fixture()
        self.auto.ScrollAmount = types.SimpleNamespace(NoAmount="none", SmallIncrement="small+",
            SmallDecrement="small-", LargeIncrement="large+", LargeDecrement="large-")
        self.cover.IsOffscreen = True
        self.cover.BoundingRectangle = Rect(10, -200, 990, 30)
        self.auto.ControlFromPoint = lambda x, y: next((c for c in (self.cover_avatar, self.cover, self.row)
            if not c.IsOffscreen and (c is self.row or not self.cover.IsOffscreen)
            and c.BoundingRectangle.left <= x < c.BoundingRectangle.right
            and c.BoundingRectangle.top <= y < c.BoundingRectangle.bottom), self.auto.main)
        def reveal_cover():
            self.cover.IsOffscreen = False
            self.cover.BoundingRectangle = Rect(10, 100, 990, 330)
        self.reveal_account_cover = reveal_cover
        self.account_scrolls = []
        def scroll(horizontal, vertical, **options):
            self.account_scrolls.append((horizontal, vertical, options))
            self.assertEqual((horizontal, vertical, options), ("none", "large-", {"waitTime": 0}))
            reveal_cover()
        self.account_pattern = types.SimpleNamespace(VerticallyScrollable=True, Scroll=scroll)
        if wheel:
            self.after_wheel = reveal_cover
        else:
            self.feed.GetScrollPattern = lambda: self.account_pattern
        return mouse

    def test_continuous_interactions_restore_cover_and_reverify_real_account(self):
        self.account_scroll_fixture()
        self.reveal_account_cover()
        self.row.BoundingRectangle = Rect(10, 610, 990, 920)
        profiles = []
        original_profile = self.open_profile
        def profile(alias):
            profiles.append(alias)
            return original_profile(alias)
        self.open_profile = profile
        def scroll(horizontal, vertical, **options):
            self.account_scrolls.append((horizontal, vertical))
            if vertical == "small+":
                self.cover.IsOffscreen = True
                self.cover.BoundingRectangle = Rect(10, -200, 990, 30)
            elif vertical == "large-":
                self.reveal_account_cover()
            else:
                self.fail(f"unexpected direction: {vertical}")
            self.row.BoundingRectangle = Rect(10, 340, 990, 560)
        self.account_pattern.Scroll = scroll
        first = self.execute("like")
        self.assertTrue(first["ok"], first)
        self.assertTrue(self.cover.IsOffscreen)
        second = self.execute("comment")
        self.assertTrue(second["ok"], second)
        self.assertEqual(self.account_scrolls, [("none", "small+"), ("none", "large-")])
        self.assertEqual(profiles, ["bot_alias", "author_alias", "bot_alias", "author_alias"])
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.main.NativeWindowHandle, 100)
        self.assertEqual(self.auto.extra_windows, [])

    def test_account_cover_completed_scroll_accepts_rebuilt_provider_then_checks_real_id(self):
        mouse = self.account_scroll_fixture(wheel=True)
        reveal = self.after_wheel
        def rebuild_after_scroll():
            reveal()
            self.feed.rid += 1000
            self.cover.rid += 1000
            self.row.rid += 1000
        self.after_wheel = rebuild_after_scroll
        self.social._verify_account("wxid_bot", self.records)
        self.assertEqual(mouse["wheels"], ["up"])
        self.assertIsNone(self.social._profile_popup())
        self.assertEqual(self.writes, [])

    def test_account_cover_wheel_recovery_keeps_hit_guards_and_reads_true_id(self):
        mouse = self.account_scroll_fixture(wheel=True)
        self.social._verify_account("wxid_bot", self.records)
        self.assertEqual(mouse["wheels"], ["up"])
        self.assertEqual(mouse["moves"], [(500, 450)])
        self.assertEqual(self.auto.keys, ["{Esc}"])
        self.auto.main.SetActive.assert_not_called()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.auto.extra_windows, [])

    def account_home_fixture(self, *, child=False, effective=True):
        mouse = self.account_scroll_fixture(wheel=True)
        self.feed.ClassName = "mmui::TimeLineListView"
        self.feed.IsKeyboardFocusable = True
        self.cell.ControlTypeName = "ListItemControl"
        self.cell.Name = "评论区"
        self.focus_for_home = self.cell if child else self.feed
        def focus():
            self.auto.focused = self.focus_for_home
        self.feed.SetFocus = Mock(side_effect=focus)
        original = self.auto.SendKeys
        def keys(key, **kwargs):
            if key == "{Ctrl}{Home}":
                self.auto.keys.append(key)
                if effective: self.reveal_account_cover()
            else: original(key, **kwargs)
        self.auto.SendKeys = keys
        return mouse

    def test_account_fast_home_owns_exact_feed_or_empty_virtual_direct_child(self):
        for child in (False, True):
            with self.subTest(child=child):
                mouse = self.account_home_fixture(child=child)
                if child: self.cell.BoundingRectangle = Rect(10, 760, 990, 940)
                self.social._verify_account("wxid_bot", self.records)
                self.assertEqual(self.auto.keys, ["{Ctrl}{Home}", "{Esc}"])
                self.feed.SetFocus.assert_called_once_with()
                self.assertEqual(mouse["moves"], [])
                self.assertEqual(mouse["wheels"], [])
                self.auto.main.SetActive.assert_not_called()
                self.assertEqual(self.auto.extra_windows, [])

    def test_account_fast_home_rejects_editor_foreign_nested_or_nonempty_focus(self):
        for reason in ("editor", "foreign", "nested", "children", "missing_runtime", "cancel"):
            with self.subTest(reason=reason):
                mouse = self.account_home_fixture(child=True)
                event = threading.Event()
                self.social._cancelled = event.is_set
                if reason == "editor": self.focus_for_home = self.comment_editor
                elif reason == "foreign": self.focus_for_home = Control(self.auto, pid=20)
                elif reason == "nested": self.focus_for_home = self.original_comment
                elif reason == "children":
                    self.cell.GetChildren = lambda: [self.comment_editor]
                elif reason == "missing_runtime": self.cell.GetRuntimeId = lambda: []
                else:
                    def focus():
                        self.auto.focused = self.cell
                        event.set()
                    self.feed.SetFocus.side_effect = focus
                with self.assertRaises(RuntimeError):
                    self.social._verify_account("wxid_bot", self.records)
                self.assertEqual(self.auto.keys, [])
                self.assertEqual(mouse["wheels"], [])
                self.assertEqual(self.auto.clicks, [])

    def test_account_fast_home_rechecks_new_draft_or_profile_after_setfocus(self):
        for reason in ("draft", "composer", "profile", "foreground"):
            with self.subTest(reason=reason):
                mouse = self.account_home_fixture(child=True)
                arrived = {"value": False}
                def focus():
                    arrived["value"] = True
                    self.auto.focused = self.cell
                    if reason == "draft":
                        self.comment_editor.text = "焦点变化时新建的草稿"
                        self.auto.focused = self.comment_editor
                    elif reason == "profile": self.open_profile("author_alias")
                    elif reason == "foreground":
                        self.auto.foreground = Control(self.auto, "Other", "WindowControl", pid=20, handle=200)
                self.feed.SetFocus.side_effect = focus
                with patch.object(self.social, "_has_composer", side_effect=lambda:
                        arrived["value"] and reason == "composer"):
                    with self.assertRaises(RuntimeError):
                        self.social._verify_account("wxid_bot", self.records)
                self.assertEqual(self.auto.keys, [])
                self.assertEqual(mouse["wheels"], [])
                self.assertEqual(self.auto.clicks, [])
                if reason == "draft": self.assertEqual(self.comment_editor.text, "焦点变化时新建的草稿")
                if reason == "profile": self.assertEqual(len(self.auto.extra_windows), 1)

    def test_ineffective_home_is_not_repeated_and_uses_guarded_wheel_fallback(self):
        mouse = self.account_home_fixture(child=True, effective=False)
        self.social._verify_account("wxid_bot", self.records)
        self.assertEqual(self.auto.keys, ["{Ctrl}{Home}", "{Esc}"])
        self.feed.SetFocus.assert_called_once_with()
        self.assertEqual(mouse["wheels"], ["up"])
        self.assertEqual(self.auto.extra_windows, [])

    def test_account_scroll_item_restores_offscreen_same_header_before_home(self):
        mouse = self.account_scroll_fixture(wheel=True)
        scroll = Mock(side_effect=lambda **kwargs: self.reveal_account_cover())
        self.cover.GetScrollItemPattern = lambda: types.SimpleNamespace(ScrollIntoView=scroll)
        self.social._verify_account("wxid_bot", self.records)
        scroll.assert_called_once_with(waitTime=0)
        self.assertEqual(self.auto.keys, ["{Esc}"])
        self.assertEqual(mouse["wheels"], [])
        self.feed.SetFocus.assert_not_called()

    def test_account_scroll_item_rejects_changed_header_token_before_navigation(self):
        self.account_scroll_fixture()
        scroll = Mock()
        def pattern():
            self.cover.rid += 1000
            return types.SimpleNamespace(ScrollIntoView=scroll)
        self.cover.GetScrollItemPattern = pattern
        with self.assertRaisesRegex(RuntimeError, "原封面元素已改变"):
            self.social._verify_account("wxid_bot", self.records)
        scroll.assert_not_called()
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.clicks, [])

    def test_account_cover_navigation_also_has_elapsed_time_limit(self):
        self.account_scroll_fixture()
        def progress(*args, **kwargs):
            self.account_scrolls.append("up")
            self.row.Name = f"Alice 另一条动态 {len(self.account_scrolls)}分钟前"
        self.account_pattern.Scroll = progress
        ticks = {"time": 0}
        def clock():
            ticks["time"] += 6
            return ticks["time"]
        with patch.object(self.module.time, "monotonic", side_effect=clock):
            with self.assertRaisesRegex(RuntimeError, "耗时超过界限"):
                self.social._verify_account("wxid_bot", self.records)
        self.assertLess(len(self.account_scrolls), 24)
        self.assertEqual(self.auto.clicks, [])

    def test_account_home_deadline_crossed_by_setfocus_or_final_focus_query_blocks_keys(self):
        for reason in ("setfocus", "final_focus_query"):
            with self.subTest(reason=reason):
                mouse = self.account_home_fixture(child=True)
                ticks = {"time": 0}
                if reason == "setfocus":
                    original_focus = self.feed.SetFocus.side_effect
                    def focus():
                        original_focus()
                        ticks["time"] = 31
                    self.feed.SetFocus.side_effect = focus
                else:
                    original_focus = self.social._account_home_focus
                    calls = []
                    def focus(token):
                        result = original_focus(token)
                        calls.append(token)
                        if len(calls) == 2: ticks["time"] = 31
                        return result
                    self.social._account_home_focus = focus
                with patch.object(self.module.time, "monotonic", side_effect=lambda: ticks["time"]):
                    with self.assertRaisesRegex(RuntimeError, "耗时超过界限"):
                        self.social._verify_account("wxid_bot", self.records)
                self.feed.SetFocus.assert_called_once_with()
                self.assertEqual(self.auto.keys, [])
                self.assertEqual(mouse["wheels"], [])
                self.assertEqual(mouse["moves"], [])
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])

    def test_account_deadline_expiry_after_slow_uia_read_blocks_each_pending_input(self):
        for reason in ("initial_context", "before_focus", "scroll_item", "scroll_pattern", "after_move", "avatar_hit"):
            with self.subTest(reason=reason):
                mouse = self.account_home_fixture(child=True) if reason == "before_focus" else (
                    self.account_scroll_fixture(wheel=reason != "scroll_pattern"))
                if not isinstance(self.feed.SetFocus, Mock):
                    self.feed.SetFocus = Mock(side_effect=AssertionError("expired navigation must not focus"))
                ticks = {"time": 0}
                navigation = Mock()
                if reason == "initial_context":
                    def composer():
                        ticks["time"] = 31
                        return False
                    self.social._has_composer = composer
                elif reason == "before_focus":
                    original_prop = self.module._prop
                    def prop(control, name, *args):
                        result = original_prop(control, name, *args)
                        if control is self.feed and name == "IsKeyboardFocusable": ticks["time"] = 31
                        return result
                    self.module._prop = prop
                elif reason == "scroll_item":
                    def item():
                        ticks["time"] = 31
                        return types.SimpleNamespace(ScrollIntoView=navigation)
                    self.cover.GetScrollItemPattern = item
                elif reason == "scroll_pattern":
                    self.account_pattern.Scroll = navigation
                    def pattern():
                        ticks["time"] = 31
                        return self.account_pattern
                    self.feed.GetScrollPattern = pattern
                elif reason == "after_move":
                    self.after_move = lambda: ticks.update(time=31)
                else:
                    self.reveal_account_cover()
                    original_hit = self.auto.ControlFromPoint
                    def hit(x, y):
                        result = original_hit(x, y)
                        ticks["time"] = 31
                        return result
                    self.auto.ControlFromPoint = hit
                with patch.object(self.module.time, "monotonic", side_effect=lambda: ticks["time"]):
                    with self.assertRaisesRegex(RuntimeError, "耗时超过界限"):
                        self.social._verify_account("wxid_bot", self.records)
                navigation.assert_not_called()
                self.feed.SetFocus.assert_not_called()
                if mouse:
                    self.assertEqual(mouse["wheels"], [])
                    self.assertEqual(mouse["moves"], [(500, 450)] if reason == "after_move" else [])
                self.assertEqual(self.auto.keys, [])
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])
                self.assertEqual(self.auto.extra_windows, [])

    def test_account_progress_wait_uses_remaining_deadline_and_never_inputs_after_expiry(self):
        mouse = self.account_home_fixture(child=True, effective=False)
        self.social._timeout = lambda: 2
        ticks, waits = {"time": 0}, []
        original_keys = self.auto.SendKeys
        def keys(key, **kwargs):
            original_keys(key, **kwargs)
            if key == "{Ctrl}{Home}": ticks["time"] = 29.8
        def wait(predicate, timeout, cancelled):
            waits.append(timeout)
            ticks["time"] = 30.1
            return None
        self.auto.SendKeys = keys
        with patch.object(self.module.time, "monotonic", side_effect=lambda: ticks["time"]), \
                patch.object(self.module, "wait_until", side_effect=wait):
            with self.assertRaisesRegex(RuntimeError, "耗时超过界限"):
                self.social._verify_account("wxid_bot", self.records)
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], .2)
        self.assertEqual(self.auto.keys, ["{Ctrl}{Home}"])
        self.assertEqual(mouse["wheels"], [])
        self.assertEqual(mouse["moves"], [])
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.writes, [])

    def test_partial_or_clipped_cover_is_recovered_before_avatar_navigation(self):
        for rect in (Rect(10, 40, 990, 270), Rect(10, 100, 990, 220)):
            with self.subTest(rect=rect):
                self.account_scroll_fixture()
                self.cover.IsOffscreen = False
                self.cover.BoundingRectangle = rect
                self.social._verify_account("wxid_bot", self.records)
                self.assertEqual(len(self.account_scrolls), 1)
                self.assertEqual(self.auto.keys, ["{Esc}"])

    def test_account_recovery_without_progress_or_scrollability_stops_before_profiles(self):
        for reason in ("no_progress", "not_scrollable"):
            with self.subTest(reason=reason):
                self.account_scroll_fixture()
                scroll = Mock()
                self.account_pattern.Scroll = scroll
                self.account_pattern.VerticallyScrollable = reason != "not_scrollable"
                with self.assertRaises(RuntimeError):
                    self.social._verify_account("wxid_bot", self.records)
                self.assertEqual(scroll.call_count, 1 if reason == "no_progress" else 0)
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.auto.extra_windows, [])

    def test_account_recovery_has_finite_limit_even_when_virtual_rows_keep_changing(self):
        self.account_scroll_fixture()
        def progress_without_cover(*args, **kwargs):
            self.account_scrolls.append("up")
            self.row.Name = f"Alice 另一条动态 {len(self.account_scrolls)}分钟前"
        self.account_pattern.Scroll = progress_without_cover
        with self.assertRaisesRegex(RuntimeError, "回顶界限"):
            self.social._verify_account("wxid_bot", self.records)
        self.assertEqual(self.account_scrolls, ["up"] * 24)
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.writes, [])

    def test_account_scroll_pattern_rechecks_provider_drift_before_navigation(self):
        for reason in ("foreground", "main_handle", "feed_pid", "draft", "profile", "composer"):
            with self.subTest(reason=reason):
                self.account_scroll_fixture()
                scroll = Mock()
                self.account_pattern.Scroll = scroll
                composer = {"open": False}
                def lookup():
                    if reason == "foreground":
                        self.auto.foreground = Control(self.auto, "Other", "WindowControl", pid=20, handle=200)
                    elif reason == "main_handle": self.auto.main.NativeWindowHandle = 111
                    elif reason == "feed_runtime": self.feed.rid += 10000
                    elif reason == "feed_pid": self.feed.ProcessId = 20
                    elif reason == "draft":
                        self.comment_editor.text = "用户的新草稿"
                        self.auto.focused = self.comment_editor
                    elif reason == "profile": self.open_profile("author_alias")
                    else: composer["open"] = True
                    return self.account_pattern
                self.feed.GetScrollPattern = lookup
                with patch.object(self.social, "_has_composer", side_effect=lambda: composer["open"]):
                    with self.assertRaises(RuntimeError):
                        self.social._verify_account("wxid_bot", self.records)
                scroll.assert_not_called()
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])
                if reason == "draft":
                    self.assertEqual(self.comment_editor.text, "用户的新草稿")
                if reason == "profile":
                    self.assertEqual(len(self.auto.extra_windows), 1)
                    self.assertEqual(self.auto.keys, [])

    def test_account_recovery_rejects_obscured_scroll_point_with_or_without_pattern(self):
        for wheel in (False, True):
            with self.subTest(wheel=wheel):
                mouse = self.account_scroll_fixture(wheel=wheel)
                scroll = Mock()
                self.account_pattern.Scroll = scroll
                self.auto.ControlFromPoint = lambda x, y: self.auto.main
                with self.assertRaises(RuntimeError):
                    self.social._verify_account("wxid_bot", self.records)
                scroll.assert_not_called()
                if mouse:
                    self.assertEqual(mouse["moves"], [])
                    self.assertEqual(mouse["wheels"], [])
                self.assertEqual(self.auto.clicks, [])

    def test_account_wheel_rechecks_new_drafts_and_popups_after_cursor_motion(self):
        for reason in ("comment_draft", "composer", "profile", "file_dialog", "menu"):
            with self.subTest(reason=reason):
                mouse = self.account_scroll_fixture(wheel=True)
                arrived = {"value": False}
                def after_move():
                    arrived["value"] = True
                    if reason == "comment_draft":
                        self.comment_editor.text = "鼠标移动时到达的用户草稿"
                        self.auto.focused = self.comment_editor
                    elif reason == "profile": self.open_profile("author_alias")
                    elif reason == "menu": self.auto.main.add(self.popup)
                self.after_move = after_move
                with patch.object(self.social, "_has_composer", side_effect=lambda:
                        arrived["value"] and reason == "composer"), patch.object(
                        self.social, "_file_dialog", side_effect=lambda:
                        arrived["value"] and reason == "file_dialog"):
                    with self.assertRaisesRegex(RuntimeError, "已有草稿或弹窗"):
                        self.social._verify_account("wxid_bot", self.records)
                self.assertEqual(mouse["moves"], [(500, 450)])
                self.assertEqual(mouse["wheels"], [])
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.auto.keys, [])
                self.assertEqual(self.writes, [])
                if reason == "comment_draft":
                    self.assertEqual(self.comment_editor.text, "鼠标移动时到达的用户草稿")
                if reason == "profile": self.assertEqual(len(self.auto.extra_windows), 1)
                if reason == "menu": self.assertIn(self.popup, self.auto.main.children)

    def test_account_recovery_cancel_or_new_generation_after_scroll_never_opens_profile(self):
        for reason in ("cancel", "generation", "draft", "foreground"):
            with self.subTest(reason=reason):
                self.account_scroll_fixture()
                event = threading.Event()
                self.social._cancelled = self.social._cancel_test(event, 7)
                original_scroll = self.account_pattern.Scroll
                def scroll(*args, **kwargs):
                    original_scroll(*args, **kwargs)
                    if reason == "cancel": event.set()
                    elif reason == "generation": self.state.bridge_generation += 1
                    elif reason == "draft":
                        self.comment_editor.text = "用户刚输入的草稿"
                        self.auto.focused = self.comment_editor
                    else:
                        self.auto.foreground = Control(self.auto, "Other", "WindowControl", pid=20, handle=200)
                self.account_pattern.Scroll = scroll
                with self.assertRaises(RuntimeError):
                    self.social._verify_account("wxid_bot", self.records)
                self.assertEqual(len(self.account_scrolls), 1)
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.auto.extra_windows, [])
                self.assertEqual(self.writes, [])
                if reason == "draft":
                    self.assertEqual(self.comment_editor.text, "用户刚输入的草稿")

    def test_account_recovery_does_not_accept_a_different_real_account(self):
        self.account_scroll_fixture()
        with self.assertRaisesRegex(RuntimeError, "登录账号"):
            self.social._verify_account("wxid_author", self.records)
        self.assertEqual(len(self.account_scrolls), 1)
        self.assertNotIn("评论", self.auto.clicks)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.auto.extra_windows, [])

    def test_duplicate_account_covers_stop_without_scrolling_or_choosing_one(self):
        self.account_scroll_fixture()
        self.reveal_account_cover()
        self.feed.add(Control(self.auto, rect=Rect(10, 340, 990, 570), klass="mmui::TimelineCell"))
        with self.assertRaisesRegex(RuntimeError, "唯一确认"):
            self.social._verify_account("wxid_bot", self.records)
        self.assertEqual(self.account_scrolls, [])
        self.assertEqual(self.auto.clicks, [])

    def test_wrong_author_profile_is_rejected_before_menu_or_input(self):
        self.fixture()
        self.records["wxid_author"]["alias"] = "other_alias"
        result = self.execute()
        self.assert_not_submitted(result)
        self.assertEqual(self.writes, [])
        self.assertNotIn("评论", self.auto.clicks)

    def test_other_logged_in_account_is_rejected_before_opening_post_menu(self):
        self.fixture()
        self.cfg.BOT_WXID = "wxid_other"
        result = self.execute()
        self.assert_not_submitted(result)
        self.assertIn("登录账号", result["error"])
        self.assertNotIn("评论", self.auto.clicks)
        self.assertEqual(self.writes, [])

    def test_raw_empty_comment_cell_focused_virtual_ancestry_binds_to_adjacent_post(self):
        self.fixture()
        self.auto.focused = self.comment_editor
        self.assertEqual(self.cell.GetChildren(), [])
        self.assertEqual(self.social._comment_context(self.row),
            (self.comment_editor, self.view, self.holder))
        replacement = Control(self.auto, self.row.Name, "ListItemControl", self.row.BoundingRectangle,
            klass="mmui::TimelineContentCell")
        replacement.rid = self.row.rid  # Fresh wrapper, same native element.
        self.assertIsNotNone(self.social._comment_context(replacement))

    def test_virtual_editor_wrong_adjacent_cell_pid_focus_or_runtime_is_rejected(self):
        for drift in ("cell", "pid", "focus", "empty_runtime", "old_runtime", "foreground", "bounds"):
            with self.subTest(drift=drift):
                self.fixture()
                self.auto.focused = self.comment_editor
                token = tuple(self.comment_editor.GetRuntimeId())
                if drift == "cell":
                    other = Control(self.auto, klass="mmui::TimelineCommentCell", rect=self.cell.BoundingRectangle)
                    self.holder.parent = other
                elif drift == "pid":
                    self.holder.ProcessId = 20
                elif drift == "focus":
                    self.auto.focused = self.auto.editor
                elif drift == "empty_runtime":
                    self.comment_editor.GetRuntimeId = lambda: []
                elif drift == "old_runtime":
                    self.comment_editor.rid += 10000
                elif drift == "foreground":
                    self.auto.foreground = Control(self.auto, "Other", "WindowControl", pid=20, handle=200)
                else:
                    self.comment_editor.BoundingRectangle = Rect(0, 0, 100, 50)
                self.assertIsNone(self.social._comment_context(self.row, token))
                self.assertEqual(self.writes, [])

    def test_existing_focused_comment_draft_is_preserved(self):
        self.fixture()
        self.comment_editor.text = "用户尚未发出的原草稿"
        self.auto.focused = self.comment_editor
        self.assert_not_submitted(self.execute())
        self.assertEqual(self.comment_editor.text, "用户尚未发出的原草稿")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.auto.clicks, [])

    def test_draft_appearing_when_target_editor_opens_is_preserved(self):
        self.fixture()
        self.comment_editor.text = "原草稿"
        self.assert_not_submitted(self.execute())
        self.assertEqual(self.comment_editor.text, "原草稿")
        self.assertEqual(self.writes, [])

    def test_verified_comment_submission_uses_value_pattern_and_closes_virtual_editor(self):
        self.fixture()
        result = self.execute()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["submit_attempted"])
        self.assertFalse(result["outcome_uncertain"])
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)
        self.assertEqual(self.clip.writes, 0)
        self.assertNotIn("{Enter}", self.auto.keys)
        self.assertFalse(result["delivery_verified"])

    def test_verified_reply_requires_original_comment_actor_and_live_binding(self):
        self.fixture()
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertIn("Carol：原评论正文", self.auto.clicks)
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_native_comment_collect_wrapper_and_its_same_label_leaf_are_one_reply_target(self):
        self.fixture()
        self.holder.children.remove(self.original_comment)
        wrapper = self.holder.add(Control(self.auto, self.original_comment.Name, "TextControl",
            self.original_comment.BoundingRectangle, klass="mmui::TimelineCommentCollect"))
        wrapper.add(self.original_comment)
        wrapper.Click = Mock(side_effect=AssertionError("collect wrapper must not receive reply navigation"))
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(tuple(wrapper.GetRuntimeId()), tuple(self.original_comment.GetRuntimeId()))
        wrapper.Click.assert_not_called()
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.clicks.count(self.original_comment.Name), 1)
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_duplicate_rendered_leaves_reply_once_to_the_same_person(self):
        """Two renderings of one author's comment are interchangeable."""
        self.fixture()
        self.holder.children.remove(self.original_comment)
        first = self.holder.add(Control(self.auto, self.original_comment.Name, "TextControl",
            klass="mmui::TimelineCommentCollect"))
        first.add(self.original_comment)
        second = self.holder.add(Control(self.auto, self.original_comment.Name, "TextControl",
            klass="mmui::TimelineCommentCollect"))
        second.add(Control(self.auto, self.original_comment.Name, "TextControl",
            klass="mmui::CommentTextView"))
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_leaf_without_runtime_id_stops_reply(self):
        """A control that cannot be addressed is not an ambiguity, it is unusable."""
        self.fixture()
        self.holder.children.remove(self.original_comment)
        first = self.holder.add(Control(self.auto, self.original_comment.Name, "TextControl",
            klass="mmui::TimelineCommentCollect"))
        first.add(self.original_comment)
        second = self.holder.add(Control(self.auto, self.original_comment.Name, "TextControl",
            klass="mmui::TimelineCommentCollect"))
        leaf = second.add(Control(self.auto, self.original_comment.Name, "TextControl",
            klass="mmui::CommentTextView"))
        leaf.GetRuntimeId = lambda: []
        self.assertNotEqual(tuple(leaf.GetRuntimeId()), tuple(self.original_comment.GetRuntimeId()))
        self.assert_not_submitted(self.execute("reply"))
        self.assertEqual(self.writes, [])

    def test_identical_parent_child_comment_proxies_select_only_the_real_leaf(self):
        self.fixture()
        leaf = self.original_comment.add(Control(self.auto, self.original_comment.Name, "TextControl",
            self.original_comment.BoundingRectangle, klass="mmui::CommentTextView"))
        leaf.click_action = lambda: setattr(self.comment_editor, "HelpText", "回复 Carol")
        self.original_comment.Click = Mock(side_effect=AssertionError("parent proxy must not receive click"))
        original_hit = self.auto.ControlFromPoint
        self.auto.ControlFromPoint = lambda x, y: leaf if original_hit(x, y) is self.original_comment else original_hit(x, y)
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.original_comment.Click.assert_not_called()
        self.assertEqual(self.writes, ["自然的回应"])

    def test_unknown_comment_leaf_class_never_receives_reply_navigation(self):
        for klass in ("", "mmui::UnknownCommentText", "mmui::TimelineCommentCollect"):
            with self.subTest(klass=klass):
                self.fixture()
                self.original_comment.ClassName = klass
                self.assert_not_submitted(self.execute("reply"))
                self.assertNotIn(self.original_comment.Name, self.auto.clicks)
                self.assertEqual(self.writes, [])

    def test_reply_empty_generic_editor_closes_then_same_leaf_opens_exact_new_reply(self):
        navigations, initial_editor = self.two_phase_reply_fixture()
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(navigations), 2)
        self.assertNotEqual(tuple(initial_editor.GetRuntimeId()), tuple(self.comment_editor.GetRuntimeId()))
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_reply_second_navigation_is_bounded_and_still_requires_exact_recipient(self):
        for mode in ("never_reopens", "wrong_caption"):
            with self.subTest(mode=mode):
                navigations, _ = self.two_phase_reply_fixture(reopen=mode != "never_reopens",
                    caption="回复 Carol2" if mode == "wrong_caption" else "回复 Carol")
                self.assert_not_submitted(self.execute("reply"))
                self.assertEqual(len(navigations), 2)
                self.assertEqual(self.writes, [])

    def test_reply_navigation_drift_never_reclicks_original_leaf(self):
        cases = ("old_draft", "old_still_visible", "old_editor_runtime", "old_editor_pid", "foreground", "main_handle", "main_pid",
            "profile", "post_label", "post_runtime", "post_pid", "cell_runtime", "leaf_runtime", "leaf_label", "leaf_parent", "leaf_pid",
            "leaf_hidden", "focused_comment_draft", "cancel", "generation")
        for drift in cases:
            with self.subTest(drift=drift):
                cancelled = threading.Event()
                def mutate(initial_editor):
                    if drift == "old_draft":
                        initial_editor.text = "用户尚未发送的草稿"
                    elif drift == "old_still_visible":
                        initial_editor.IsOffscreen = False
                    elif drift == "old_editor_runtime":
                        initial_editor.rid += 1000
                    elif drift == "old_editor_pid":
                        initial_editor.ProcessId = 11
                    elif drift == "foreground":
                        self.auto.foreground = Control(self.auto, "其他软件", "WindowControl", pid=20, handle=200)
                    elif drift == "main_handle":
                        self.auto.main.NativeWindowHandle = 111
                    elif drift == "main_pid":
                        self.auto.main.ProcessId = 11
                    elif drift == "profile":
                        self.open_profile("comment_alias")
                    elif drift == "post_label":
                        self.row.Name = "Mallory unrelated post 1分钟前"
                    elif drift == "post_runtime":
                        self.row.rid += 1000
                    elif drift == "post_pid":
                        self.row.ProcessId = 11
                    elif drift == "cell_runtime":
                        self.cell.rid += 1000
                    elif drift == "leaf_runtime":
                        self.original_comment.rid += 1000
                    elif drift == "leaf_label":
                        self.original_comment.Name = "Carol：原评论已修改"
                    elif drift == "leaf_parent":
                        self.original_comment.parent = self.feed
                    elif drift == "leaf_pid":
                        self.original_comment.ProcessId = 11
                    elif drift == "leaf_hidden":
                        self.original_comment.IsOffscreen = True
                    elif drift == "focused_comment_draft":
                        draft = Control(self.auto, "", "EditControl", klass="mmui::CommentReplyTextEdit")
                        draft.text = "用户新建的评论草稿"
                        self.auto.focused = draft
                    elif drift == "cancel":
                        cancelled.set()
                    elif drift == "generation":
                        self.state.bridge_generation += 1
                navigations, _ = self.two_phase_reply_fixture(after_first=mutate)
                self.assert_not_submitted(self.execute("reply", cancel_event=cancelled))
                self.assertEqual(len(navigations), 1)
                self.assertEqual(self.writes, [])

    def test_reply_post_drift_before_write_or_submit_never_sends(self):
        for stage in ("before_write", "before_submit"):
            for drift in ("label", "runtime", "pid"):
                with self.subTest(stage=stage, drift=drift):
                    def mutate():
                        if drift == "label":
                            self.row.Name = "Mallory unrelated post 1分钟前"
                        elif drift == "runtime":
                            self.row.rid += 1000
                        else:
                            self.row.ProcessId = 11
                    navigations, _ = self.two_phase_reply_fixture(
                        after_second=mutate if stage == "before_write" else None)
                    if stage == "before_submit":
                        self.after_write = mutate
                    self.assert_not_submitted(self.execute("reply"))
                    self.assertEqual(len(navigations), 2)
                    self.assertEqual(self.writes, [] if stage == "before_write" else ["自然的回应"])

    def test_reply_user_draft_arriving_before_first_leaf_navigation_is_preserved(self):
        navigations, initial_editor = self.two_phase_reply_fixture()
        open_comment = self.social._open_comment
        def draft_arrives(row):
            context = open_comment(row)
            context[0].text = "用户尚未发送的草稿"
            return context
        with patch.object(self.social, "_open_comment", side_effect=draft_arrives):
            self.assert_not_submitted(self.execute("reply"))
        self.assertEqual(navigations, [])
        self.assertFalse(initial_editor.IsOffscreen)
        self.assertEqual(initial_editor.text, "用户尚未发送的草稿")
        self.assertEqual(self.writes, [])

    def test_short_comment_uses_body_endpoint_for_both_navigations_avoiding_author_link(self):
        navigations, _ = self.two_phase_reply_fixture()
        self.source["comments"][0]["content"] = "？"
        self.original_comment.Name = "Carol：？"
        self.original_comment.BoundingRectangle = Rect(30, 590, 100, 616)
        body_clicks = []
        navigate = self.original_comment.click_action
        def rich_text_click(**options):
            self.auto.clicks.append(self.original_comment.Name)
            body_clicks.append(options)
            if options.get("x", 35) < 55:
                self.open_profile("comment_alias")
            else:
                navigate()
        self.original_comment.Click = rich_text_click
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(navigations), 2)
        self.assertEqual(body_clicks, [{"x": 66, "y": 13, "simulateMove": False, "waitTime": 0}] * 2)
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.extra_windows, [])

    def test_short_comment_collapsed_provider_reuses_proven_body_endpoint(self):
        navigations, geometry_clicks, _ = self.collapsed_reply_fixture()
        self.source["comments"][0]["content"] = "？"
        self.original_comment.Name = "Carol：？"
        self.original_comment.BoundingRectangle = Rect(30, 590, 100, 616)
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(navigations), 2)
        self.assertEqual(geometry_clicks, [{"x": 86, "y": 43, "simulateMove": False, "waitTime": 0}])
        self.assertEqual(self.writes, ["自然的回应"])

    def test_short_comment_real_dpi_geometry_has_body_point_beyond_author_center(self):
        self.fixture()
        comment = self.source["comments"][0]
        comment.update(content="？", author_name="Yeah", author_names=["Yeah"])
        self.original_comment.Name = "Yeah：？"
        self.original_comment.BoundingRectangle = Rect(1440, 1462, 1560, 1514)
        self.social._dpi_scale = lambda: 2
        self.assertEqual(self.social._reply_body_point(self.original_comment, comment, self.records), (1552, 1488))
        self.assertEqual(self.auto.clicks, [])

    def test_reply_body_text_pattern_uses_body_range_for_wrapped_comment(self):
        self.fixture()
        comment = self.source["comments"][0]
        self.original_comment.BoundingRectangle = Rect(30, 590, 960, 650)
        body = types.SimpleNamespace(GetText=lambda count: comment["content"],
            GetBoundingRectangles=lambda: [Rect(150, 620, 300, 645)])
        find = Mock(return_value=body)
        document = types.SimpleNamespace(GetText=lambda count: self.original_comment.Name, FindText=find)
        self.original_comment.GetTextPattern = lambda: types.SimpleNamespace(DocumentRange=document)
        self.assertEqual(self.social._reply_body_point(self.original_comment, comment, self.records), (225, 632))
        find.assert_called_once_with("原评论正文", True, False)
        self.assertEqual(self.auto.clicks, [])

    def test_reply_body_missing_or_mismatched_text_range_never_falls_back_to_guess(self):
        for reason in ("body_mismatch", "document_mismatch", "missing_range", "no_bounds", "outside_bounds"):
            with self.subTest(reason=reason):
                self.fixture()
                comment = self.source["comments"][0]
                body = types.SimpleNamespace(GetText=lambda count:
                    "另一条正文" if reason == "body_mismatch" else comment["content"],
                    GetBoundingRectangles=lambda: [] if reason == "no_bounds" else [
                        Rect(950, 590, 999, 620) if reason == "outside_bounds" else Rect(150, 590, 300, 620)])
                document = types.SimpleNamespace(GetText=lambda count:
                    "Carol：其他正文" if reason == "document_mismatch" else self.original_comment.Name,
                    FindText=lambda *args: None if reason == "missing_range" else body)
                self.original_comment.GetTextPattern = lambda: types.SimpleNamespace(DocumentRange=document)
                with self.assertRaises(RuntimeError):
                    self.social._reply_body_point(self.original_comment, comment, self.records)
                self.assertEqual(self.auto.clicks, [])

    def test_reply_body_without_pattern_rejects_multiline_or_inconsistent_leaf(self):
        for reason in ("wrapped", "newline", "suffix", "tiny"):
            with self.subTest(reason=reason):
                self.fixture()
                comment = self.source["comments"][0]
                if reason == "wrapped": self.original_comment.BoundingRectangle = Rect(30, 590, 960, 655)
                elif reason == "newline":
                    comment["content"] = "原评论\n正文"
                    self.original_comment.Name = "Carol：原评论\n正文"
                elif reason == "suffix": self.original_comment.Name += " extra"
                else: self.original_comment.BoundingRectangle = Rect(30, 590, 35, 620)
                with self.assertRaises(RuntimeError):
                    self.social._reply_body_point(self.original_comment, comment, self.records)
                self.assertEqual(self.auto.clicks, [])

    def test_reply_body_obscured_first_point_never_navigates_or_writes(self):
        self.fixture()
        original = self.auto.ControlFromPoint
        self.auto.ControlFromPoint = lambda x, y: self.auto.main if (x, y) == (956, 605) else original(x, y)
        self.assert_not_submitted(self.execute("reply"))
        self.assertNotIn(self.original_comment.Name, self.auto.clicks)
        self.assertEqual(self.writes, [])

    def test_reply_body_virtual_cell_rechecks_draft_point_and_ids_after_native_read(self):
        for reason in ("draft", "leaf_bounds", "cell_runtime", "point", "profile", "cancel"):
            with self.subTest(reason=reason):
                self.fixture()
                self.source["interaction_identity_verified"] = True
                event = threading.Event()
                original = self.auto.ControlFromPoint
                covered = {"value": False}
                self.auto.ControlFromPoint = lambda x, y: (
                    self.auto.main if covered["value"] else self.cell) if (x, y) == (956, 605) else original(x, y)
                def fresh(source, **kwargs):
                    if reason == "draft": self.comment_editor.text = "原生读取期间出现的新草稿"
                    elif reason == "leaf_bounds": self.original_comment.BoundingRectangle = Rect(30, 591, 960, 620)
                    elif reason == "cell_runtime": self.cell.rid += 1000
                    elif reason == "point": covered["value"] = True
                    elif reason == "profile": self.open_profile("comment_alias")
                    else:
                        event.set()
                        self.assertTrue(kwargs["cancel_event"].is_set())
                    return copy.deepcopy(self.source)
                self.feed_client.read_post_details.side_effect = fresh
                self.assert_not_submitted(self.execute("reply", cancel_event=event))
                self.feed_client.fetch.assert_called_once()
                self.feed_client.read_post_details.assert_called_once()
                self.assertNotIn(self.original_comment.Name, self.auto.clicks)
                self.assertEqual(self.writes, [])
                if reason == "draft": self.assertEqual(self.comment_editor.text, "原生读取期间出现的新草稿")

    def test_destroyed_comment_provider_uses_one_verified_geometry_navigation(self):
        navigations, geometry_clicks, initial_editor = self.collapsed_reply_fixture()
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.feed_client.fetch.assert_called_once()
        self.assertGreater(self.feed_client.read_post_details.call_count, 0)
        self.assertIsNone(self.social._moment_text(initial_editor))
        self.assertEqual(len(navigations), 2)
        self.assertEqual(geometry_clicks, [{"x": 946, "y": 45, "simulateMove": False, "waitTime": 0}])
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_geometry_reply_requires_complete_ids_and_unique_target_actor_caption(self):
        for invalid in ("same_actor_identical_comment", "different_actor_same_name", "unverified_comment",
                "incomplete_list", "unknown_context"):
            with self.subTest(invalid=invalid):
                _, geometry_clicks, _ = self.collapsed_reply_fixture()
                if invalid == "same_actor_identical_comment":
                    other = copy.deepcopy(self.source["comments"][0])
                    other.update(comment_id="native-comment-20002")
                    self.source["comments"].append(other)
                elif invalid == "different_actor_same_name":
                    self.records["wxid_other"] = {"username": "wxid_other", "nickname": "Carol"}
                    self.source["comments"].append({"comment_id": "native-comment-20002",
                        "author_id": "wxid_other", "author_name": "Carol", "author_names": ["Carol"],
                        "content": "另一个人的不同正文", "identity_verified": True})
                elif invalid == "unverified_comment":
                    self.source["comments"].append({"comment_id": "native-comment-20002",
                        "author_id": "wxid_other", "author_name": "Dan", "content": "不同正文",
                        "identity_verified": False})
                elif invalid == "incomplete_list":
                    self.source["interaction_identity_verified"] = False
                else:
                    self.source["comments"].append({"content": "身份未知的评论", "identity_verified": False})
                self.assert_not_submitted(self.execute("reply"))
                self.assertEqual(geometry_clicks, [])
                self.assertEqual(self.writes, [])

    def test_geometry_reply_never_treats_partially_available_old_properties_as_destroyed(self):
        for remaining in ("editor_runtime", "editor_pid", "editor_class", "editor_offscreen", "editor_value",
                "leaf_runtime", "leaf_pid", "leaf_class", "leaf_name"):
            with self.subTest(remaining=remaining):
                def partially_destroyed(initial_editor):
                    if remaining == "editor_runtime":
                        initial_editor.GetRuntimeId = lambda: [901]
                    elif remaining == "editor_pid":
                        initial_editor.ProcessId = 10
                    elif remaining == "editor_class":
                        initial_editor.ClassName = "mmui::CommentReplyTextEdit"
                    elif remaining == "editor_offscreen":
                        initial_editor.IsOffscreen = True
                    elif remaining == "editor_value":
                        initial_editor.GetValuePattern = lambda: types.SimpleNamespace(Value="")
                    elif remaining == "leaf_runtime":
                        self.original_comment.GetRuntimeId = lambda: [902]
                    elif remaining == "leaf_pid":
                        self.original_comment.ProcessId = 10
                    elif remaining == "leaf_class":
                        self.original_comment.ClassName = "mmui::CommentTextView"
                    else:
                        self.original_comment.Name = "Carol：原评论正文"
                _, geometry_clicks, _ = self.collapsed_reply_fixture(after_collapse=partially_destroyed)
                self.assert_not_submitted(self.execute("reply"))
                self.assertEqual(geometry_clicks, [])
                self.assertEqual(self.writes, [])

    def test_geometry_reply_shape_point_or_fresh_comments_drift_never_clicks_cell(self):
        cases = ("cell_left", "cell_top", "cell_right", "cell_bottom", "row_bounds", "wrong_focus",
            "new_editor", "covered_point", "fresh_comments", "feed_runtime", "feed_viewport")
        for drift in cases:
            with self.subTest(drift=drift):
                def mutate(initial_editor):
                    if drift.startswith("cell_"):
                        r = self.cell.BoundingRectangle
                        values = [r.left, r.top, r.right, r.bottom]
                        values[{"cell_left": 0, "cell_top": 1, "cell_right": 2, "cell_bottom": 3}[drift]] += 1
                        self.cell.BoundingRectangle = Rect(*values)
                    elif drift == "row_bounds":
                        self.row.BoundingRectangle = Rect(10, 341, 990, 560)
                    elif drift == "wrong_focus":
                        self.auto.focused = self.auto.editor
                    elif drift == "new_editor":
                        draft = Control(self.auto, "", "EditControl", klass="mmui::CommentReplyTextEdit")
                        draft.text = "新的用户草稿"
                        self.cell.GetChildren = lambda: [draft]
                    elif drift == "covered_point":
                        previous = self.auto.ControlFromPoint
                        self.auto.ControlFromPoint = lambda x, y: self.auto.main if (x, y) == (956, 605) else previous(x, y)
                    elif drift == "fresh_comments":
                        fresh = copy.deepcopy(self.source)
                        fresh["comments"].append({"comment_id": "native-comment-20002", "author_id": "wxid_other",
                            "author_name": "Dan", "author_names": ["Dan"], "content": "新增评论", "identity_verified": True})
                        self.feed_client.read_post_details.return_value = fresh
                    elif drift == "feed_runtime":
                        self.feed.rid += 1000
                    elif drift == "feed_viewport":
                        self.feed.BoundingRectangle = Rect(10, 100, 990, 580)
                _, geometry_clicks, _ = self.collapsed_reply_fixture(after_collapse=mutate)
                self.assert_not_submitted(self.execute("reply"))
                self.assertEqual(geometry_clicks, [])
                self.assertEqual(self.writes, [])

    def test_rebuilt_reply_cell_reads_only_the_original_post(self):
        for field in ("post_id", "author_id", "content", "created_at"):
            with self.subTest(field=field):
                self.fixture()
                self.source["interaction_identity_verified"] = True
                changed = dict(self.source, **{field: "other"})
                self.feed_client.read_post_details.return_value = changed
                with self.assertRaisesRegex(RuntimeError, "原生评论列表"):
                    self.social._verify_collapsed_reply_comments(
                        self.source, self.source["comments"][0], self.records)
                self.feed_client.fetch.assert_not_called()
                self.feed_client.read_post_details.assert_called_once()

    def test_geometry_reply_added_same_actor_comment_before_write_or_submit_stops(self):
        for stage in ("before_write", "before_submit"):
            with self.subTest(stage=stage):
                def changed_native_list():
                    fresh = copy.deepcopy(self.source)
                    second = copy.deepcopy(fresh["comments"][0])
                    second.update(comment_id="native-comment-20002", content="刚新增的同人评论")
                    fresh["comments"].append(second)
                    self.feed_client.read_post_details.return_value = fresh
                _, geometry_clicks, _ = self.collapsed_reply_fixture(
                    after_reopen=changed_native_list if stage == "before_write" else None)
                if stage == "before_submit":
                    self.after_write = changed_native_list
                self.assert_not_submitted(self.execute("reply"))
                self.assertEqual(len(geometry_clicks), 1)
                self.assertEqual(self.writes, [] if stage == "before_write" else ["自然的回应"])

    def test_geometry_reply_native_order_body_reference_and_names_must_stay_identical(self):
        for change in ("order", "body", "reference", "names"):
            with self.subTest(change=change):
                def changed_list(initial_editor):
                    fresh = copy.deepcopy(self.source)
                    if change == "order":
                        fresh["comments"].reverse()
                    elif change == "body":
                        fresh["comments"][1]["content"] = "另一条正文改变"
                    elif change == "reference":
                        fresh["comments"][1].update(ref_comment_id="native-comment-20001", ref_author_id="wxid_comment")
                    else:
                        fresh["comments"][1]["author_names"] = ["Dan changed"]
                    self.feed_client.read_post_details.return_value = fresh
                _, geometry_clicks, _ = self.collapsed_reply_fixture(after_collapse=changed_list)
                self.records["wxid_other"] = {"username": "wxid_other", "nickname": "Dan"}
                self.source["comments"].append({"comment_id": "native-comment-20002", "author_id": "wxid_other",
                    "author_name": "Dan", "author_names": ["Dan"], "content": "另一个人的不同正文",
                    "identity_verified": True})
                self.assert_not_submitted(self.execute("reply"))
                self.assertEqual(geometry_clicks, [])
                self.assertEqual(self.writes, [])

    def test_geometry_reply_rechecks_window_feed_cell_and_focus_after_native_read(self):
        for drift in ("main_handle", "foreground", "feed_runtime", "feed_bounds", "cell_runtime",
                "row_runtime", "focus", "new_editor"):
            with self.subTest(drift=drift):
                def arm_read(initial_editor):
                    def read(**kwargs):
                        if drift == "main_handle":
                            self.auto.main.NativeWindowHandle = 111
                        elif drift == "foreground":
                            self.auto.foreground = Control(self.auto, "其他软件", "WindowControl", pid=20, handle=200)
                        elif drift == "feed_runtime":
                            self.feed.rid += 1000
                        elif drift == "feed_bounds":
                            self.feed.BoundingRectangle = Rect(10, 100, 990, 580)
                        elif drift == "cell_runtime":
                            self.cell.rid += 1000
                        elif drift == "row_runtime":
                            self.row.rid += 1000
                        elif drift == "focus":
                            self.auto.focused = self.auto.editor
                        else:
                            draft = Control(self.auto, "", "EditControl", klass="mmui::CommentReplyTextEdit")
                            draft.text = "新到达的用户草稿"
                            self.cell.GetChildren = lambda: [draft]
                        return copy.deepcopy(self.source)
                    self.feed_client.read_post_details.side_effect = lambda source, **kw: read(**kw)
                _, geometry_clicks, _ = self.collapsed_reply_fixture(after_collapse=arm_read)
                self.assert_not_submitted(self.execute("reply"))
                self.assertEqual(geometry_clicks, [])
                self.assertEqual(self.writes, [])

    def test_absent_uia_caption_local_ocr_whitespace_exact_match_uses_single_operation_proof(self):
        reader = self.ocr_reply_fixture(text="回 复\t C a r o l")
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        reader.assert_called_once_with((30, 675, 800, 725))
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)
        self.assertIsNone(self.social._reply_caption_proof)
        self.assertIsNone(self.social._reply_caption_source)
        self.assertFalse(self.social._reply_caption_ocr_used)

    def test_delayed_recipient_caption_is_checked_before_ocr(self):
        for caption, expected in (("回复 Carol", True), ("回复 Bob", False)):
            with self.subTest(caption=caption):
                reader = self.ocr_reply_fixture()
                original = self.social._uia_reply_target
                def probe(*args):
                    result = original(*args)
                    self.comment_editor.HelpText = caption
                    return result
                self.social._uia_reply_target = probe
                result = self.execute("reply")
                self.assertEqual(result["ok"], expected, result)
                reader.assert_not_called()
                self.assertEqual(self.auto.clicks.count("发送"), int(expected))

    def test_generic_editor_help_still_requires_recipient_proof(self):
        for caption, expected in (("回复 Carol", True), ("回复 Bob", False)):
            with self.subTest(caption=caption):
                reader = self.ocr_reply_fixture(text=caption)
                self.original_comment.click_action = lambda: setattr(self.comment_editor, "HelpText", "输入评论")
                result = self.execute("reply")
                self.assertEqual(result["ok"], expected, result)
                reader.assert_called_once()
                self.assertEqual(self.auto.clicks.count("发送"), int(expected))

    def test_wrong_or_conflicting_uia_caption_never_uses_ocr_to_override(self):
        for case in ("wrong_help", "wrong_live", "correct_help_wrong_live"):
            with self.subTest(case=case):
                reader = self.ocr_reply_fixture()
                if case in ("wrong_help", "correct_help_wrong_live"):
                    caption = "回复 Carol" if case == "correct_help_wrong_live" else "回复 Bob"
                    self.original_comment.click_action = lambda: setattr(self.comment_editor, "HelpText", caption)
                if case in ("wrong_live", "correct_help_wrong_live"):
                    self.view.add(Control(self.auto, "回复 Bob", "TextControl"))
                self.assert_not_submitted(self.execute("reply"))
                reader.assert_not_called()
                self.assertEqual(self.writes, [])

    def test_unreadable_caption_falls_back_to_native_identity(self):
        """No OCR text means "unreadable", not "wrong person".

        WeChat paints "回复 X" as an in-editor placeholder; neither UIA nor OCR
        can read it. The native comment ID was already verified, so the reply
        proceeds instead of stalling on every attempt.
        """
        for text in ("", None, b"reply Carol", "reader_error"):
            with self.subTest(text=text):
                reader = self.ocr_reply_fixture(text=text)
                if text == "reader_error":
                    reader.side_effect = RuntimeError("local OCR unavailable")
                result = self.execute("reply")
                self.assertTrue(result["ok"], result)
                reader.assert_called_once()
                self.assertEqual(self.writes, ["自然的回应"])
                self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_wrong_or_partial_caption_never_writes(self):
        """A readable caption that names someone else must still stop the reply."""
        for text in ("回复 Bob", "回复 Carol2", "回复 Car", "回复", "Carol",
                "回复 Carol\n回复 Bob", "原评论 回复 Carol"):
            with self.subTest(text=text):
                reader = self.ocr_reply_fixture(text=text)
                self.assert_not_submitted(self.execute("reply"))
                reader.assert_called_once()
                self.assertEqual(self.writes, [])
                self.assertIsNone(self.social._reply_caption_proof)

    def test_ocr_requires_complete_unique_actor_and_noncolliding_full_caption_sets(self):
        for invalid in ("incomplete", "same_actor_second", "space_name", "colon_name"):
            with self.subTest(invalid=invalid):
                reader = self.ocr_reply_fixture()
                if invalid == "incomplete":
                    self.source["interaction_identity_verified"] = False
                elif invalid == "same_actor_second":
                    other = copy.deepcopy(self.source["comments"][0])
                    other.update(comment_id="native-comment-20002")
                    self.source["comments"].append(other)
                else:
                    name = "C a r o l" if invalid == "space_name" else "Carol:"
                    self.records["wxid_other"] = {"username": "wxid_other", "nickname": name}
                    self.source["comments"].append({"comment_id": "native-comment-20002", "author_id": "wxid_other",
                        "author_name": name, "author_names": [name], "content": "不同人的另一条正文",
                        "identity_verified": True})
                self.assert_not_submitted(self.execute("reply"))
                reader.assert_not_called()
                self.assertEqual(self.writes, [])

    def test_ocr_capture_window_context_point_or_draft_drift_never_establishes_proof(self):
        cases = ("main_handle", "foreground", "feed_runtime", "row_runtime", "cell_runtime", "editor_runtime",
            "view_runtime", "editor_rect", "focus", "covered_point", "draft", "profile", "generation", "cancel")
        for drift in cases:
            with self.subTest(drift=drift):
                cancelled = threading.Event()
                def mutate():
                    if drift == "main_handle":
                        self.auto.main.NativeWindowHandle = 111
                    elif drift == "foreground":
                        self.auto.foreground = Control(self.auto, "其他软件", "WindowControl", pid=20, handle=200)
                    elif drift.endswith("_runtime"):
                        controls = {"feed_runtime": self.feed, "row_runtime": self.row, "cell_runtime": self.cell,
                            "editor_runtime": self.comment_editor, "view_runtime": self.view}
                        controls[drift].rid += 1000
                    elif drift == "editor_rect":
                        self.comment_editor.BoundingRectangle = Rect(31, 675, 801, 725)
                    elif drift == "focus":
                        self.auto.focused = self.auto.editor
                    elif drift == "covered_point":
                        self.auto.ControlFromPoint = lambda x, y: self.auto.main
                    elif drift == "draft":
                        self.comment_editor.text = "用户刚输入的草稿"
                    elif drift == "profile":
                        self.open_profile("comment_alias")
                    elif drift == "generation":
                        self.state.bridge_generation += 1
                    else:
                        cancelled.set()
                reader = self.ocr_reply_fixture(during_read=mutate)
                self.assert_not_submitted(self.execute("reply", cancel_event=cancelled))
                reader.assert_called_once()
                self.assertEqual(self.writes, [])
                self.assertIsNone(self.social._reply_caption_proof)
                if drift == "draft":
                    self.assertEqual(self.comment_editor.text, "用户刚输入的草稿")

    def test_ocr_verified_empty_editor_proof_expires_or_changes_after_write_without_sending(self):
        for drift in ("editor_runtime", "cell_runtime", "editor_rect", "expired", "wrong_binding", "generation"):
            with self.subTest(drift=drift):
                reader = self.ocr_reply_fixture()
                def mutate():
                    if drift == "editor_runtime":
                        self.comment_editor.rid += 1000
                    elif drift == "cell_runtime":
                        self.cell.rid += 1000
                    elif drift == "editor_rect":
                        self.comment_editor.BoundingRectangle = Rect(31, 675, 801, 725)
                    elif drift == "expired":
                        proof = self.social._reply_caption_proof
                        self.social._reply_caption_proof = (proof[0], proof[1], time.monotonic() - 31)
                    elif drift == "wrong_binding":
                        proof = self.social._reply_caption_proof
                        binding = (proof[1][0], "native-comment-other", *proof[1][2:])
                        self.social._reply_caption_proof = (proof[0], binding, proof[2])
                    else:
                        self.state.bridge_generation += 1
                self.after_write = mutate
                self.assert_not_submitted(self.execute("reply"))
                reader.assert_called_once()
                self.assertEqual(self.writes, ["自然的回应"])
                self.assertEqual(self.comment_editor.text, "自然的回应")
                self.assertIsNone(self.social._reply_caption_proof)

    def test_ocr_caption_proof_cannot_be_reused_by_a_later_run_with_same_controls(self):
        reader = self.ocr_reply_fixture()
        reader.side_effect = ["回复 Carol", "回复 Bob"]
        self.assertTrue(self.execute("reply")["ok"])
        self.assertIsNone(self.social._reply_caption_proof)
        self.holder.add(self.view)
        self.comment_editor.IsOffscreen = False
        second = self.execute("reply")
        self.assertFalse(second["ok"], second)
        self.assertFalse(second["submit_attempted"], second)
        self.assertFalse(second["outcome_uncertain"], second)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_ocr_proof_does_not_bypass_fresh_native_comment_checks_at_write_or_submit(self):
        for stage in ("before_write", "before_submit"):
            with self.subTest(stage=stage):
                def mutate():
                    fresh = copy.deepcopy(self.source)
                    another = copy.deepcopy(fresh["comments"][0])
                    another.update(comment_id="native-comment-20002", content="新加入的同人评论")
                    fresh["comments"].append(another)
                    self.feed_client.read_post_details.return_value = fresh
                reader = self.ocr_reply_fixture(during_read=mutate if stage == "before_write" else None)
                if stage == "before_submit":
                    self.after_write = mutate
                self.assert_not_submitted(self.execute("reply"))
                reader.assert_called_once()
                self.assertEqual(self.writes, [] if stage == "before_write" else ["自然的回应"])

    def test_ocr_crop_must_be_bounded_visible_and_both_points_bound_to_same_editor(self):
        for invalid in ("outside_viewport", "too_narrow", "left_point_covered", "right_point_covered"):
            with self.subTest(invalid=invalid):
                reader = self.ocr_reply_fixture()
                if invalid == "outside_viewport":
                    self.feed.BoundingRectangle = Rect(10, 100, 990, 700)
                elif invalid == "too_narrow":
                    self.comment_editor.BoundingRectangle = Rect(30, 675, 45, 725)
                else:
                    previous = self.auto.ControlFromPoint
                    blocked = (38, 699) if invalid == "left_point_covered" else (792, 699)
                    self.auto.ControlFromPoint = lambda x, y: self.auto.main if (x, y) == blocked else previous(x, y)
                self.assert_not_submitted(self.execute("reply"))
                reader.assert_not_called()
                self.assertEqual(self.writes, [])

    def test_same_editor_reparented_to_new_reply_view_or_holder_invalidates_ocr_proof(self):
        for change in ("view_wrong_caption", "holder", "holder_runtime"):
            with self.subTest(change=change):
                reader = self.ocr_reply_fixture()
                self.records["wxid_other"] = {"username": "wxid_other", "nickname": "Eve"}
                self.source["comments"].append({"comment_id": "native-comment-20002", "author_id": "wxid_other",
                    "author_name": "Eve", "author_names": ["Eve"], "content": "不同作者的评论",
                    "identity_verified": True})
                captured = (tuple(self.comment_editor.GetRuntimeId()), tuple(self.view.GetRuntimeId()),
                    tuple(self.holder.GetRuntimeId()))
                def reparent():
                    if change == "view_wrong_caption":
                        self.view.children.remove(self.comment_editor)
                        current = self.holder.add(Control(self.auto, rect=self.view.BoundingRectangle,
                            klass="mmui::CommentReplyTextView"))
                        current.add(self.comment_editor)
                        current.add(Control(self.auto, "回复 Eve", "TextControl"))
                    elif change == "holder":
                        self.holder.children.remove(self.view)
                        current = self.cell.add(Control(self.auto, rect=self.holder.BoundingRectangle))
                        current.add(self.view)
                    else:
                        self.holder.rid += 1000
                    self.assertEqual(tuple(self.comment_editor.GetRuntimeId()), captured[0])
                self.after_write = reparent
                self.assert_not_submitted(self.execute("reply"))
                reader.assert_called_once()
                self.assertEqual(self.writes, ["自然的回应"])
                self.assertEqual(self.comment_editor.text, "自然的回应")
                self.assertIsNone(self.social._reply_caption_proof)

    def test_exact_virtual_comment_cell_hit_can_verify_absent_caption_without_editor_child_hit(self):
        reader = self.ocr_reply_fixture()
        previous = self.auto.ControlFromPoint
        def hit(x, y):
            rect = self.comment_editor.BoundingRectangle
            return self.cell if rect.left <= x < rect.right and rect.top <= y < rect.bottom else previous(x, y)
        self.auto.ControlFromPoint = hit
        self.assertTrue(self.execute("reply")["ok"])
        reader.assert_called_once_with((30, 675, 800, 725))
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_virtual_cell_point_guard_rejects_wrong_identity_ancestry_focus_bounds_or_one_overlay(self):
        cases = ("sibling", "pid", "class", "offscreen", "feed", "main", "overlay_child", "no_reply_view",
            "focus", "box_left", "box_bottom", "second_point")
        for invalid in cases:
            with self.subTest(invalid=invalid):
                self.ocr_reply_fixture()
                self.auto.focused = self.comment_editor
                box = (30, 675, 800, 725)
                current = self.cell
                if invalid in ("sibling", "pid", "class", "offscreen"):
                    current = self.feed.add(Control(self.auto, rect=self.cell.BoundingRectangle,
                        klass="mmui::TimelineCommentCell"))
                    if invalid != "sibling":
                        current.GetRuntimeId = self.cell.GetRuntimeId
                    if invalid == "pid":
                        current.ProcessId = 20
                    elif invalid == "class":
                        current.ClassName = "mmui::UnknownCommentCell"
                    elif invalid == "offscreen":
                        current.IsOffscreen = True
                elif invalid == "feed":
                    current = self.feed
                elif invalid == "main":
                    current = self.auto.main
                elif invalid == "overlay_child":
                    current = self.cell.add(Control(self.auto, "遮挡层", "PaneControl"))
                elif invalid == "no_reply_view":
                    self.comment_editor.parent = self.holder
                elif invalid == "focus":
                    self.auto.focused = Control(self.auto, "", "EditControl", klass="mmui::CommentReplyTextEdit")
                elif invalid == "box_left":
                    box = (0, 675, 800, 725)
                elif invalid == "box_bottom":
                    box = (30, 675, 800, 791)
                self.auto.ControlFromPoint = lambda x, y: self.auto.main if invalid == "second_point" and x == 792 else current
                self.assertFalse(self.social._caption_point_matches(self.comment_editor, box))
                self.social._read_reply_caption.assert_not_called()
                self.assertEqual(self.auto.clicks, [])

    def test_virtual_cell_hit_foreground_operation_stops_when_focus_changes_during_caption_read(self):
        reader = self.ocr_reply_fixture(during_read=lambda: setattr(self.auto, "focused", self.auto.editor))
        previous = self.auto.ControlFromPoint
        self.auto.ControlFromPoint = lambda x, y: self.cell if 30 <= x < 800 and 675 <= y < 725 else previous(x, y)
        self.assert_not_submitted(self.execute("reply"))
        reader.assert_called_once()
        self.assertEqual(self.writes, [])

    def test_reply_author_display_name_and_body_may_contain_both_colons(self):
        self.fixture()
        name, body = "Ca:rol：朋友", "段落：说明:内容"
        self.source["comments"][0].update(author_name=name, author_names=[name], content=body)
        self.records["wxid_comment"]["nickname"] = name
        self.original_comment.Name = name + "：" + body
        self.original_comment.click_action = lambda: setattr(self.comment_editor, "HelpText", "回复 " + name)
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_reply_comment_author_new_remark_is_matched_by_fresh_true_actor_record(self):
        self.fixture()
        self.records["wxid_comment"]["remark"] = "评论者新备注"
        self.original_comment.Name = "评论者新备注：原评论正文"
        self.original_comment.click_action = lambda: setattr(self.comment_editor, "HelpText", "回复 评论者新备注")
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_fresh_name_collision_with_different_true_comment_actor_refuses_reply(self):
        self.fixture()
        self.records["wxid_comment"]["remark"] = "共同的新备注"
        self.records["wxid_other"] = {"username": "wxid_other", "alias": "other_alias", "remark": "共同的新备注"}
        other = copy.deepcopy(self.source["comments"][0])
        other.update(comment_id="native-comment-20002", author_id="wxid_other",
            author_name="另一个旧名字", author_names=["另一个旧名字"])
        self.source["comments"].append(other)
        self.original_comment.Name = "共同的新备注：原评论正文"
        self.original_comment.click_action = lambda: setattr(self.comment_editor, "HelpText", "回复 共同的新备注")
        self.assert_not_submitted(self.execute("reply"))
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.writes, [])

    def test_reply_label_recipient_name_with_colons_uses_known_reference_actor(self):
        self.fixture()
        recipient = "Al:ice：老友"
        self.records["wxid_author"]["nickname"] = recipient
        self.source["comments"][0].update(ref_author_id="wxid_author", ref_author_name=recipient)
        self.original_comment.Name = "Carol 回复 " + recipient + "：原评论正文"
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_reply_draft_starting_with_reply_is_not_a_recipient_caption(self):
        for conflicting_caption in (False, True):
            with self.subTest(conflicting_caption=conflicting_caption):
                self.fixture()
                action = self.action("reply")
                action["text"] = "回复功能测试"
                def expose_draft():
                    self.comment_editor.Name = self.comment_editor.text
                    self.comment_editor.add(Control(self.auto, self.comment_editor.text, "TextControl"))
                    if conflicting_caption:
                        self.view.add(Control(self.auto, "回复 Dave", "TextControl"))
                self.after_write = expose_draft
                result = self.social.execute_interaction(action, generation=7)
                self.assertEqual(result["ok"], not conflicting_caption, result)
                self.assertEqual(self.auto.clicks.count("发送"), 0 if conflicting_caption else 1)

    def test_reply_caption_drift_or_name_substring_never_commits_wrong_recipient(self):
        for drift in ("substring", "changed_after_write"):
            with self.subTest(drift=drift):
                self.fixture()
                if drift == "substring":
                    self.original_comment.click_action = lambda: setattr(self.comment_editor, "HelpText", "回复 Carol2")
                else:
                    self.after_write = lambda: setattr(self.comment_editor, "HelpText", "回复 Dave")
                self.assert_not_submitted(self.execute("reply"))
                if drift == "substring":
                    self.assertEqual(self.writes, [])
                else:
                    self.assertEqual(self.comment_editor.text, "自然的回应")
                self.assertEqual(self.auto.clicks.count("发送"), 0)

    def test_reply_mismatched_native_comment_id_or_actor_refuses_write(self):
        for field, value in (("comment_id", "cmt_generated"), ("comment_author_id", "wxid_other"),
                ("comment_author_id", "wxid_bot")):
            with self.subTest(field=field):
                self.fixture()
                action = self.action("reply")
                action[field] = value
                self.assert_not_submitted(self.social.execute_interaction(action, generation=7))
                self.assertEqual(self.auto.clicks, [])
                self.assertEqual(self.writes, [])

    def test_reply_original_comment_changed_since_decision_refuses_write(self):
        self.fixture()
        changed = copy.deepcopy(self.source)
        changed["comments"][0]["content"] = "已改变的原评论"
        self.feed_client.fetch.side_effect = lambda **kw: [changed]
        self.original_comment.Name = "Carol：已改变的原评论"
        result = self.execute("reply")
        self.assert_not_submitted(result)
        self.assertEqual(self.writes, [])

    def test_duplicate_live_comment_text_and_names_replies_to_the_same_person(self):
        """One author's repeated identical comment is interchangeable.

        A Moments reply addresses a person, so a duplicate rendering must not
        block a reply that would read the same to everyone.
        """
        self.fixture()
        self.holder.add(Control(self.auto, self.original_comment.Name, "TextControl",
            klass="mmui::CommentTextView"))
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_cached_same_name_same_comment_cannot_bind_to_only_one_visible_comment(self):
        self.fixture()
        other = copy.deepcopy(self.source["comments"][0])
        other.update(comment_id="native-comment-20002", author_id="wxid_other")
        self.source["comments"].append(other)
        result = self.execute("reply")
        self.assert_not_submitted(result)
        self.assertEqual(result["error_code"], "comment_identity_ambiguous")
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.writes, [])

    def test_comment_collision_uses_same_whitespace_normalization_as_live_uia(self):
        """Two renderings of one author's text collapse to the same person."""
        self.fixture()
        self.source["comments"][0]["content"] = "原  评论 正文"
        other = copy.deepcopy(self.source["comments"][0])
        other.update(comment_id="native-comment-20002", content="原 评论 正文")
        self.source["comments"].append(other)
        self.original_comment.Name = "Carol：原 评论 正文"
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_comment_suffix_match_cannot_replace_exact_original_comment_body(self):
        self.fixture()
        self.original_comment.Name = "Carol：前缀原评论正文"
        self.assert_not_submitted(self.execute("reply"))
        self.assertEqual(self.writes, [])

    def test_distinct_reference_and_body_cannot_share_identical_rendered_comment_label(self):
        """Different quoted replies from the same author are still one person."""
        self.fixture()
        original = self.source["comments"][0]
        original.update(content="片段：末尾", ref_author_id="wxid_foo", ref_author_name="Foo")
        other = copy.deepcopy(original)
        other.update(comment_id="native-comment-20002", content="末尾",
            ref_author_id="wxid_bar", ref_author_name="Foo：片段")
        self.source["comments"].append(other)
        self.original_comment.Name = "Carol 回复 Foo：片段：末尾"
        result = self.execute("reply")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.writes, ["自然的回应"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_already_liked_post_is_never_unliked(self):
        self.fixture()
        self.like.Name = "取消赞"
        result = self.execute("like")
        self.assert_not_submitted(result)
        self.assertEqual(result["status"], "already_liked")
        self.assertNotIn("取消赞", self.auto.clicks)
        self.assertEqual(self.writes, [])
        self.assertNotIn(self.popup, self.auto.main.children)
        self.assertTrue(self.execute("comment")["ok"])

    def test_like_menu_rebuilt_before_click_is_a_pre_submit_failure(self):
        self.fixture()
        opening = self.social._open_post_menu
        def replaced(row):
            popup = opening(row)
            self.auto.main.children.remove(popup)
            return popup
        self.social._open_post_menu = replaced
        result = self.execute("like")
        self.assert_not_submitted(result)
        self.assertEqual(result["stage"], "submit_like")
        self.assertNotIn("赞", self.auto.clicks)

    def test_like_click_with_lost_acknowledgement_remains_uncertain(self):
        self.fixture()
        def lost(**kwargs):
            raise RuntimeError("provider disconnected during click")
        self.like.Click = lost
        result = self.execute("like")
        self.assertTrue(result["submit_attempted"])
        self.assertTrue(result["outcome_uncertain"])

    def test_like_proves_cancel_label_after_submission_without_clicking_it(self):
        self.fixture()
        result = self.execute("like")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["submit_attempted"])
        self.assertEqual(self.auto.clicks.count("赞"), 1)
        self.assertNotIn("取消赞", self.auto.clicks)

    def test_nested_menu_items_and_localized_actions_support_like_and_comment(self):
        for kind in ("like", "comment"):
            with self.subTest(kind=kind):
                self.fixture()
                self.popup.ControlTypeName = "PaneControl"
                self.popup.children = []
                wrapper = self.popup.add(Control(self.auto))
                wrapper.add(self.like)
                wrapper.add(self.comment_button)
                self.like.ControlTypeName = self.comment_button.ControlTypeName = "MenuItemControl"
                self.like.Name = "点赞"
                self.comment_button.Name = "Comment"
                result = self.execute(kind)
                self.assertTrue(result["ok"], result)
                self.assertEqual(self.auto.clicks.count("点赞" if kind == "like" else "发送"), 1)

    def test_accessible_more_button_uses_actual_position_on_changed_layout(self):
        self.fixture()
        more = self.row.add(Control(self.auto, "更多", "ButtonControl", Rect(835, 500, 900, 540)))
        more.click_action = lambda: self.auto.main.add(self.popup)
        old_hit = self.auto.ControlFromPoint
        self.auto.ControlFromPoint = lambda x, y: more if (x,y) == (867,520) else old_hit(x,y)
        result = self.execute("comment")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.auto.clicks.count("更多"), 1)
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_incomplete_menu_reports_missing_actions_without_click(self):
        self.fixture()
        self.comment_button.Name = "non-action"
        result = self.execute("like")
        self.assert_not_submitted(result)
        self.assertIn("已找到菜单", result["error"])
        self.assertNotIn("赞", self.auto.clicks)

    def test_owned_top_level_social_menu_requires_native_owner_of_current_main(self):
        self.fixture()
        self.social._social_menu_row = self.row
        self.popup.NativeWindowHandle = 950
        self.social._roots = lambda: [self.auto.main, self.popup]
        owner = Mock(return_value=self.social._main_handle)
        self.social._native_user32 = lambda: types.SimpleNamespace(GetAncestor=owner)
        self.assertIs(self.social._social_popup(), self.popup)
        owner.assert_called_once_with(950, 3)
        owner.return_value = 99
        self.assertIsNone(self.social._social_popup())

    def test_same_labels_in_different_native_popup_class_cannot_be_submitted(self):
        self.fixture()
        self.popup.ClassName = "mmui::UnrelatedPopup"
        self.assert_not_submitted(self.execute("like"))
        self.assertNotIn("赞", self.auto.clicks)

    def test_obscured_target_or_menu_in_wrong_post_region_refuses_like(self):
        for drift in ("hit", "popup_region", "popup_pid", "empty_runtime"):
            with self.subTest(drift=drift):
                self.fixture()
                if drift == "hit":
                    original = self.auto.ControlFromPoint
                    self.auto.ControlFromPoint = lambda x, y: self.auto.main if y > 500 else original(x, y)
                elif drift == "popup_region":
                    self.popup.BoundingRectangle = Rect(600, 100, 940, 130)
                elif drift == "popup_pid":
                    self.popup.ProcessId = 20
                else:
                    self.popup.GetRuntimeId = lambda: []
                self.assert_not_submitted(self.execute("like"))

    def test_submit_provider_error_is_uncertain_and_never_retried(self):
        self.fixture()
        def provider_error():
            raise OSError("synthetic UI provider failed after click")
        self.send.click_action = provider_error
        result = self.execute()
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["submit_attempted"])
        self.assertTrue(result["outcome_uncertain"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_submit_focus_loss_without_editor_close_is_uncertain(self):
        self.fixture()
        self.send.click_action = lambda: setattr(self.auto, "focused", self.send)
        result = self.execute()
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["submit_attempted"])
        self.assertTrue(result["outcome_uncertain"])
        self.assertEqual(self.comment_editor.text, "自然的回应")
        self.assertIn(self.view, self.holder.children)
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_new_native_self_comment_confirms_submission_when_original_editor_is_stale(self):
        for kind in ("comment", "reply"):
            with self.subTest(kind=kind):
                action, fresh = self.native_receipt_fixture(kind)
                result = self.execute_with_receipt_wait(action)
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["submit_attempted"])
                self.assertFalse(result["outcome_uncertain"])
                self.assertEqual(result["acknowledgement"], "native_comment_record")
                self.assertEqual(fresh["comments"][-1]["comment_id"], "30001")
                self.assertEqual(self.auto.clicks.count("发送"), 1)
                self.feed_client.read_post_details.assert_called_once()
                args, options = self.feed_client.read_post_details.call_args
                self.assertEqual(args[0]["post_id"], "10001")
                self.assertEqual({c["comment_id"] for c in args[0]["comments"]}, {"20001"})
                self.assertEqual(options["bot_id"], "wxid_bot")
                self.assertEqual(options["timeout"], 3.0)
                self.assertFalse(options["cancel_event"].is_set())

    def test_existing_identical_native_self_comment_cannot_confirm_a_new_submission(self):
        action, fresh = self.native_receipt_fixture()
        self.source["comments"].append(copy.deepcopy(fresh["comments"][-1]))
        action["source"] = copy.deepcopy(self.source)
        result = self.execute_with_receipt_wait(action)
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["submit_attempted"])
        self.assertTrue(result["outcome_uncertain"])
        self.assertNotIn("acknowledgement", result)
        self.assertEqual(self.auto.clicks.count("发送"), 1)
        self.assertGreater(self.feed_client.read_post_details.call_count, 0)

    def test_missing_native_id_wrong_reply_reference_or_cancelled_read_cannot_acknowledge(self):
        for reason in ("no_id", "incomplete", "wrong_author", "wrong_reply_reference", "cancel", "generation"):
            with self.subTest(reason=reason):
                action, fresh = self.native_receipt_fixture("reply")
                event = threading.Event()
                if reason == "no_id":
                    fresh["comments"][-1]["comment_id"] = ""
                elif reason == "incomplete":
                    fresh["interaction_identity_verified"] = False
                elif reason == "wrong_author":
                    fresh["comments"][-1]["author_id"] = "wxid_other"
                elif reason == "wrong_reply_reference":
                    fresh["comments"][-1]["ref_comment_id"] = "20002"
                else:
                    def cancelled_read(*args, **options):
                        self.assertFalse(options["cancel_event"].is_set())
                        if reason == "cancel":
                            event.set()
                        else:
                            self.state.bridge_generation = 8
                        self.assertTrue(options["cancel_event"].is_set())
                        return fresh
                    self.feed_client.read_post_details.side_effect = cancelled_read
                result = self.execute_with_receipt_wait(action, cancel_event=event)
                self.assertFalse(result["ok"], result)
                self.assertTrue(result["submit_attempted"])
                self.assertTrue(result["outcome_uncertain"])
                self.assertNotIn("acknowledgement", result)
                self.assertEqual(self.auto.clicks.count("发送"), 1)
                self.assertGreater(self.feed_client.read_post_details.call_count, 0)

    def test_editor_hidden_after_submit_in_other_application_is_still_uncertain(self):
        self.fixture()
        def hidden_in_other_app():
            self.comment_editor.IsOffscreen = True
            self.auto.foreground = Control(self.auto, "Other", "WindowControl", pid=20, handle=200)
            self.auto.focused = self.auto.foreground
        self.send.click_action = hidden_in_other_app
        result = self.execute()
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["submit_attempted"])
        self.assertTrue(result["outcome_uncertain"])
        self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_cache_refresh_receives_cancellation_and_cannot_commit_when_cancelled(self):
        self.fixture()
        event = threading.Event()
        def fetch(**kwargs):
            self.assertFalse(kwargs["cancel_event"].is_set())
            event.set()
            self.assertTrue(kwargs["cancel_event"].is_set())
            return [copy.deepcopy(self.source)]
        self.feed_client.fetch.side_effect = fetch
        self.assert_not_submitted(self.execute(cancel_event=event))
        self.assertEqual(self.auto.clicks, [])
        self.assertEqual(self.writes, [])

    def test_cancellation_or_new_generation_never_commits_existing_written_draft(self):
        for reason in ("event", "generation", "closed", "wrong_focus", "new_runtime"):
            with self.subTest(reason=reason):
                self.fixture()
                event = threading.Event()
                def changed():
                    if reason == "event":
                        event.set()
                    elif reason == "generation":
                        self.state.bridge_generation = 8
                    elif reason == "closed":
                        self.social._closed = True
                    elif reason == "wrong_focus":
                        self.auto.focused = self.auto.editor
                    else:
                        self.comment_editor.rid += 10000
                self.after_write = changed
                self.state.bridge_generation = 7
                self.assert_not_submitted(self.execute(cancel_event=event))
                self.assertEqual(self.comment_editor.text, "自然的回应")
                self.assertEqual(self.auto.clicks.count("发送"), 0)
        self.state.bridge_generation = 7

    def test_cancelled_or_stale_generation_never_refreshes_or_fetches(self):
        self.fixture()
        event = threading.Event(); event.set()
        result = self.social.refresh_feed(cancel_event=event, generation=7)
        self.assertFalse(result["ok"])
        self.state.bridge_generation = 8
        self.assert_not_submitted(self.execute())
        self.assertEqual(self.auto.clicks, [])
        self.feed_client.fetch.assert_not_called()

    def test_distinct_comments_by_same_actor_keep_native_target_when_provider_rebuilds(self):
        for kind in ("geometry", "ocr"):
            with self.subTest(kind=kind):
                if kind == "geometry":
                    self.collapsed_reply_fixture()
                else:
                    self.ocr_reply_fixture()
                other = copy.deepcopy(self.source["comments"][0])
                other.update(comment_id="native-comment-20002", content="同一人的另一条不同正文")
                self.source["comments"].append(other)
                result = self.execute("reply")
                self.assertTrue(result["ok"], result)
                self.assertEqual(self.writes, ["自然的回应"])
                self.assertEqual(self.auto.clicks.count("发送"), 1)

    def test_failed_reply_closes_only_its_empty_editor_and_next_comment_can_run(self):
        self.fixture()
        self.social._reply_target_matches = Mock(return_value=False)
        closed = []
        keys = self.auto.SendKeys
        def observe(key, **kwargs):
            if key == "{Esc}" and self.auto.focused is self.comment_editor:
                closed.append(self.comment_editor.text)
            return keys(key, **kwargs)
        self.auto.SendKeys = observe
        self.assert_not_submitted(self.execute("reply"))
        self.assertEqual(closed, [""])
        self.assertIs(self.auto.focused, self.auto.editor)
        self.assertTrue(self.execute("comment")["ok"])

    def test_failed_reply_preserves_text_entered_into_its_editor(self):
        self.fixture()
        def changed(*unused, **kwargs):
            self.comment_editor.text = "用户刚输入的草稿"
            return False
        self.social._reply_target_matches = changed
        self.assert_not_submitted(self.execute("reply"))
        self.assertEqual(self.comment_editor.text, "用户刚输入的草稿")
        self.assertIs(self.auto.focused, self.comment_editor)

    def test_possibly_submitted_empty_comment_editor_is_not_closed_as_an_unsent_draft(self):
        self.fixture()
        def accepted_then_provider_failed():
            self.comment_editor.text = ""
            raise RuntimeError("provider lost after accepting click")
        self.send.click_action = accepted_then_provider_failed
        result = self.execute("comment")
        self.assertFalse(result["ok"])
        self.assertTrue(result["submit_attempted"])
        self.assertIs(self.auto.focused, self.comment_editor)
        self.assertIsNone(self.social._owned_empty_comment)

    def test_cancelled_operation_can_close_its_exact_empty_editor_before_releasing_desktop(self):
        self.fixture()
        event = threading.Event()
        event.set()
        cancelled = event.is_set
        self.social._cancelled = cancelled
        self.auto.focused = self.comment_editor
        self.social._owned_empty_comment = (self.row, (self.comment_editor, self.view, self.holder))
        self.social._close_owned_empty_comment()
        self.assertIs(self.auto.focused, self.auto.editor)
        self.assertIs(self.social._cancelled, cancelled)
        self.assertIsNone(self.social._owned_empty_comment)
        self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()
