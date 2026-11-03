"""Per-post media failures and selected SNS cold trees; no real I/O or UI."""
import json
import sys
import types
import threading
import unittest
from unittest.mock import Mock, patch

import test_moments_interactions as interact
import test_moments_social_sender as social
from test_uia_sender import Control, FakeGate


class ReaderRecoveryTests(unittest.TestCase):
    setUp = interact.MomentsInteractionsTests.setUp
    select_like = staticmethod(interact.MomentsInteractionsTests.select_like)

    def bind_reader(self):
        self.bot._observe_post = types.MethodType(self.original_observe, self.bot)
        self.feed.fetch.side_effect = [[], [interact.post(post_id="10001", content="不可读甲"),
                                         interact.post(post_id="10002", content="可读乙")]]
        self.model.request.return_value = "本条正常摘要"

    def test_unreadable_media_skips_only_its_post_then_reads_decides_and_sends_next(self):
        self.bind_reader()
        def parts(post, **kwargs):
            if post["post_id"] == "10001":
                raise ValueError("朋友圈媒体域名未获准")
            return []
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=Mock(side_effect=parts))}):
            result = self.bot.run_once()
        self.assertEqual((result["status"], result["scanned"], result["processed"], result["media_skipped"]),
                         ("accepted_by_ui", 2, 1, 1))
        self.model.request.assert_called_once()
        request = self.model.request.call_args.args[1]
        self.assertEqual(json.loads(request[1]["content"][0]["text"])["post_id"], "10002")
        self.bot._ask_llm.assert_called_once()
        context = json.loads(self.bot._ask_llm.call_args.args[0])
        self.assertEqual([post["post_id"] for post in context["posts"]], ["10002"])
        self.assertNotIn("不可读甲", json.dumps(context, ensure_ascii=False))
        self.assertEqual(self.sender.execute_interaction.call_args.args[0]["source"]["post_id"], "10002")
        self.bot._set_result("朋友圈刷新失败：模拟后续错误")
        self.assertIn("域名未获准", self.bot.status_snapshot()["last_media_error"]["message"])

    def test_all_media_failures_complete_scan_without_model_decision_or_send(self):
        self.bind_reader()
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=Mock(side_effect=ValueError("缺少原媒体地址")))}):
            result = self.bot.run_once()
        self.assertEqual((result["status"], result["scanned"], result["processed"], result["media_skipped"]),
                         ("skipped", 2, 0, 2))
        self.model.request.assert_not_called()
        self.bot._ask_llm.assert_not_called()
        self.sender.execute_interaction.assert_not_called()
        self.assertIn("跳过", self.bot.last_result)

    def test_model_request_failure_stops_before_second_post(self):
        self.bind_reader()
        self.model.request.side_effect = RuntimeError("provider unavailable")
        parts = Mock(return_value=[])
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=parts)}):
            result = self.bot.run_once()
        self.assertEqual((result["status"], result["scanned"], result["media_skipped"]), ("observation_failed", 2, 0))
        self.model.request.assert_called_once()
        parts.assert_called_once()
        self.bot._ask_llm.assert_not_called()
        self.sender.execute_interaction.assert_not_called()

    def test_invalid_model_summary_is_not_classified_as_unreadable_media(self):
        self.bind_reader()
        self.model.request.return_value = "字" * 4001
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=Mock(return_value=[]))}):
            result = self.bot.run_once()
        self.assertEqual(result["status"], "observation_failed")
        self.assertEqual(result["media_skipped"], 0)
        self.model.request.assert_called_once()
        self.assertEqual(self.bot.last_media_error, {})

    def test_local_image_encoding_model_error_is_still_a_media_failure(self):
        self.bind_reader()
        local_error = type("ModelError", (RuntimeError,), {})
        parts = Mock(side_effect=[local_error("图像编码失败"), []])
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=parts)}):
            result = self.bot.run_once()
        self.assertEqual(result["status"], "accepted_by_ui")
        self.assertEqual(result["media_skipped"], 1)
        self.model.request.assert_called_once()

    def test_request_value_error_is_not_mistaken_for_media_failure(self):
        self.bind_reader()
        self.model.request.side_effect = ValueError("请求参数无效")
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=Mock(return_value=[]))}):
            result = self.bot.run_once()
        self.assertEqual(result["status"], "observation_failed")
        self.assertEqual(result["media_skipped"], 0)
        self.model.request.assert_called_once()

    def test_media_cancellation_stops_without_counting_a_failure(self):
        self.bind_reader()
        def cancelled(*args, **kwargs):
            self.bot.cancel_pending()
            raise ValueError("媒体读取已取消")
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=Mock(side_effect=cancelled))}):
            result = self.bot.run_once()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.bot.media_skipped, 0)
        self.assertEqual(self.bot.last_media_error, {})
        self.model.request.assert_not_called()
        self.sender.execute_interaction.assert_not_called()

    def test_media_error_logs_have_stage_and_reason_but_no_signed_url(self):
        self.bind_reader()
        error = ValueError("下载失败 HTTPS://cdn.invalid/img?enc_key=synthetic-secret")
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=Mock(side_effect=error))}), \
                self.assertLogs("ob11-bridge", level="INFO") as recorded:
            self.bot.run_once()
        text = "\n".join(recorded.output)
        self.assertIn("stage=media", text)
        self.assertIn("reason=下载失败", text)
        self.assertIn("media_skipped=2", text)
        self.assertNotIn("cdn.invalid", text)
        self.assertNotIn("synthetic-secret", text)

    def test_submit_failure_keeps_actual_scan_and_model_counts(self):
        self.sender.execute_interaction.return_value = {"status": "failed", "error": "模拟定位失败",
                                                       "submit_attempted": False, "outcome_uncertain": False}
        result = self.bot.run_once()
        self.assertEqual((result["status"], result["scanned"], result["processed"]), ("failed", 1, 1))
        self.assertIn("定位失败", result["error"])

    def test_media_skip_round_keeps_normal_reply_poll_interval(self):
        self.bot._next_browse = interact.NOW
        self.config.MOMENTS_REPLY_POLL_SECONDS = 3
        self.bot.run_once = Mock(return_value={"ok": True, "status": "skipped", "media_skipped": 1})
        self.bot._scheduled_check()
        self.assertGreater(self.bot.next_run, interact.NOW)
        self.assertEqual(self.bot.next_reply_poll, interact.NOW + 3)
        self.assertEqual(self.bot._retry_at, 0)

    def test_unchanged_reply_post_reuses_reading_but_decides_again(self):
        self.bot._observe_post = types.MethodType(self.original_observe, self.bot)
        own = interact.post(own=True, comments=[interact.comment()])
        self.feed.fetch.side_effect = lambda **kw: [own] if kw.get("usernames") == ["wxid_self"] else []
        self.model.request.return_value = "这一条动态的摘要"
        parts = Mock(return_value=[])
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=parts)}):
            self.bot.run_once(replies_only=True)
            self.bot.run_once(replies_only=True)
        self.model.request.assert_called_once()
        parts.assert_called_once()
        self.assertEqual(self.bot._ask_llm.call_count, 2)
        self.sender.refresh_feed.assert_not_called()
        self.sender.execute_interaction.assert_not_called()

    def test_reply_refresh_happens_only_after_read_decision_and_writing(self):
        own = interact.post(own=True, comments=[interact.comment()])
        self.feed.fetch.side_effect = lambda **kw: [own] if kw.get("usernames") == ["wxid_self"] else []
        sequence = []
        self.bot._observe_post.side_effect = lambda source: sequence.append("read") or "本条摘要"
        def decide(context):
            sequence.append("decide")
            return {"candidate_id": json.loads(context)["candidates"][0]["candidate_id"]}
        self.bot._ask_llm.side_effect = decide
        self.model.request.side_effect = lambda *args, **kw: sequence.append("write") or "天气挺好。"
        self.sender.refresh_feed.side_effect = lambda **kw: sequence.append("refresh") or {"ok": True}
        self.sender.execute_interaction.side_effect = lambda *args, **kw: sequence.append("send") or {
            "status": "accepted_by_ui", "submit_attempted": True, "outcome_uncertain": False}
        self.assertEqual(self.bot.run_once(replies_only=True)["status"], "accepted_by_ui")
        self.assertEqual(sequence, ["read", "decide", "write", "refresh", "send"])

    def test_private_arrival_during_reply_writing_never_refreshes_or_submits(self):
        own = interact.post(own=True, comments=[interact.comment()])
        self.feed.fetch.side_effect = lambda **kw: [own] if kw.get("usernames") == ["wxid_self"] else []
        self.bot._ask_llm.side_effect = lambda context: {
            "candidate_id": json.loads(context)["candidates"][0]["candidate_id"]}
        def write(*args, **kwargs):
            self.state.social_should_yield = lambda: True
            return "天气挺好。"
        self.model.request.side_effect = write
        self.assertEqual(self.bot.run_once(replies_only=True)["status"], "cancelled")
        self.sender.refresh_feed.assert_not_called()
        self.sender.execute_interaction.assert_not_called()

    def test_reading_cache_is_invalidated_by_comment_media_or_identity_change(self):
        self.bot._observe_post = types.MethodType(self.original_observe, self.bot)
        source = interact.post(has_media=True, media=[{"url": "https://example.invalid/image", "key": "123"}])
        self.model.request.return_value = "本条摘要"
        parts = Mock(return_value=[])
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=parts)}):
            self.bot._observe_post(source)
            source["comments"].append(interact.comment())
            self.bot._observe_post(source)
            source["media"][0]["key"] = "456"
            self.bot._observe_post(source)
            source["author_id"] = "wxid_another"
            self.bot._observe_post(source)
        self.assertEqual(self.model.request.call_count, 4)
        self.assertEqual(parts.call_count, 4)

    def test_failed_or_cancelled_reading_is_not_cached(self):
        self.bot._observe_post = types.MethodType(self.original_observe, self.bot)
        source = interact.post()
        token = threading.Event()
        self.bot._model_cancel = token
        self.model.request.side_effect = [RuntimeError("synthetic failure"), "成功摘要"]
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=Mock(return_value=[]))}):
            self.assertIsNone(self.bot._observe_post(source))
            self.assertEqual(self.bot._observation_cache, {})
            token.set()
            self.assertIsNone(self.bot._observe_post(source))
            token.clear()
            self.assertEqual(self.bot._observe_post(source), "成功摘要")
        self.assertEqual(self.model.request.call_count, 2)

    def test_reading_cache_expires_and_hot_settings_clear_it(self):
        self.bot._observe_post = types.MethodType(self.original_observe, self.bot)
        source = interact.post()
        self.model.request.return_value = "本条摘要"
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=Mock(return_value=[]))}), \
                patch.object(self.module.time, "monotonic", return_value=100) as clock:
            self.bot._observe_post(source)
            clock.return_value = 399
            self.bot._observe_post(source)
            self.assertEqual(self.model.request.call_count, 1)
            clock.return_value = 401
            self.bot._observe_post(source)
            self.bot.set_enabled(True)
            self.bot._observe_post(source)
        self.assertEqual(self.model.request.call_count, 3)

    def test_reading_cache_tracks_model_persona_prompt_and_sampling_parameters(self):
        self.bot._observe_post = types.MethodType(self.original_observe, self.bot)
        source = interact.post()
        self.model.request.return_value = "本条摘要"
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=Mock(return_value=[]))}):
            self.bot._observe_post(source)
            self.config.MAIN_LLM_MODEL = "changed-main"
            self.bot._observe_post(source)
            self.config.BOT_PERSONA = "改后的人设"
            self.bot._observe_post(source)
            self.config.MAIN_LLM_PROMPT = "改后的主模型提示词"
            self.bot._observe_post(source)
            self.config.CHAT_VIDEO_MAX_FRAMES = 8
            self.bot._observe_post(source)
        self.assertEqual(self.model.request.call_count, 5)

    def test_reading_cache_does_not_grow_with_entire_history(self):
        self.bot._observe_post = types.MethodType(self.original_observe, self.bot)
        self.model.request.return_value = "本条摘要"
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=Mock(return_value=[]))}):
            for index in range(70):
                self.bot._observe_post(interact.post(post_id=str(10000 + index)))
        self.assertEqual(len(self.bot._observation_cache), 64)

    def test_settings_change_during_read_does_not_repopulate_old_cache(self):
        self.bot._observe_post = types.MethodType(self.original_observe, self.bot)
        self.model.request.side_effect = lambda *args, **kwargs: self.bot.set_enabled(True) or "旧设置摘要"
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=Mock(return_value=[]))}):
            self.bot._observe_post(interact.post())
        self.assertEqual(self.bot._observation_cache, {})

    def test_rejected_media_poll_does_not_redownload_or_reopen_ui_until_url_changes(self):
        self.bot._observe_post = types.MethodType(self.original_observe, self.bot)
        own = interact.post(own=True, has_media=True, media=[{"url": "https://example.invalid/old", "type": "image"}],
                            comments=[interact.comment()])
        self.feed.fetch.side_effect = lambda **kw: [own] if kw.get("usernames") == ["wxid_self"] else []
        error = ValueError("朋友圈媒体下载 HTTP 400")
        error.http_status = 400
        parts = Mock(side_effect=error)
        self.model.request.return_value = "这一条动态的摘要"
        with patch.dict(sys.modules, {"moments_media": types.SimpleNamespace(post_parts=parts)}):
            first = self.bot.run_once(replies_only=True)
            second = self.bot.run_once(replies_only=True)
            self.assertEqual((first["media_skipped"], second["media_skipped"]), (1, 1))
            self.assertEqual(parts.call_count, 1)
            self.sender.refresh_feed.assert_not_called()
            self.model.request.assert_not_called()
            own["media"][0]["url"] = "https://example.invalid/new"
            parts.side_effect = None
            parts.return_value = []
            self.bot.run_once(replies_only=True)
        self.assertEqual(parts.call_count, 2)
        self.sender.refresh_feed.assert_not_called()
        self.model.request.assert_called_once()

    def test_rejected_media_retry_expires_without_changing_unrelated_posts(self):
        failed = interact.post(has_media=True, media=[{"url": "https://example.invalid/a"}])
        error = ValueError("HTTP 400")
        error.http_status = 400
        self.bot._defer_failed_media(failed, error)
        self.assertEqual(self.bot._media_retry_after(failed), interact.NOW + 600)
        self.assertEqual(self.bot._media_retry_after(interact.post(post_id="10002")), 0)
        with patch.object(self.module, "_now", return_value=interact.NOW + 601):
            self.assertEqual(self.bot._media_retry_after(failed), 0)

    def test_failed_media_url_change_unblocks_that_post_but_new_comments_do_not(self):
        failed = interact.post(media=[{"url": "https://example.invalid/a", "key": "123"}])
        self.bot._defer_failed_media(failed, ValueError("transport error"))
        failed["comments"] = [interact.comment()]
        self.assertEqual(self.bot._media_retry_after(failed), interact.NOW + 60)
        failed["media"][0]["key"] = "456"
        self.assertEqual(self.bot._media_retry_after(failed), 0)


class SelectedFeedRecoveryTests(unittest.TestCase):
    setUp = social.SocialSenderTests.setUp
    tearDown = social.SocialSenderTests.tearDown
    fixture = social.SocialSenderTests.fixture
    pin_main = social.SocialSenderTests.pin_main
    open_editor = social.SocialSenderTests.open_editor
    close_editor = social.SocialSenderTests.close_editor
    open_profile = social.SocialSenderTests.open_profile

    def test_account_stage_recovers_bound_stub_to_full_tree_once(self):
        self.fixture()
        main = self.auto.main
        stub = Control(self.auto, "", "WindowControl", klass="Qt51514QWindowIcon", handle=main.NativeWindowHandle)
        current = [stub]
        self.auto.ControlFromHandle = lambda hwnd: current[0] if hwnd == main.NativeWindowHandle else None
        self.social._gate_manager = FakeGate(allowed=True)
        original = self.auto.SendKeys
        def restore(keys, **kwargs):
            if keys == "{Tab}":
                current[0] = main
                self.auto.foreground = main
            original(keys, **kwargs)
        self.auto.SendKeys = restore
        self.social._verify_account("wxid_bot", self.records)
        self.assertEqual(self.auto.keys.count("{Tab}"), 1)
        self.assertEqual(self.auto.keys[-1], "{Esc}")
        self.assertEqual(self.auto.clicks, [""])
        self.assertEqual(self.auto.extra_windows, [])

    def test_missing_discover_entry_recovers_same_main_before_navigation(self):
        self.fixture()
        self.auto.main.children.remove(self.feed)
        self.auto.main.children.remove(self.toolbar)
        self.auto.main.children.remove(self.entry)
        discover = Control(self.auto, "发现", "ButtonControl", aid="MainView.discover")
        def reveal(**kwargs):
            self.auto.main.add(discover)
            return True
        self.social._recover_current_window = Mock(side_effect=reveal)
        discover.click_action = lambda: self.auto.main.add(self.entry)
        self.entry.click_action = lambda: (self.auto.main.add(self.feed), self.auto.main.add(self.toolbar))
        self.assertIs(self.social._open_feed(), self.feed)
        self.social._recover_current_window.assert_called_once()
        self.assertEqual(self.auto.clicks, ["发现", "朋友圈"])

    def test_selected_page_cold_feed_recovers_even_when_chat_list_is_available(self):
        self.fixture()
        self.feed.IsOffscreen = True
        self.social._gate_manager = FakeGate(allowed=True)
        original = self.auto.SendKeys
        def restore(keys, **kwargs):
            if keys == "{Tab}":
                self.feed.IsOffscreen = False
            original(keys, **kwargs)
        self.auto.SendKeys = restore
        self.assertTrue(self.social._controls_available())
        result = self.social.refresh_feed(generation=7)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.auto.keys, ["{Tab}"])
        self.assertNotIn("朋友圈", self.auto.clicks)

    def test_native_selected_entry_with_missing_toolbar_is_not_toggled(self):
        self.fixture()
        self.feed.IsOffscreen = True
        self.toolbar.IsOffscreen = True
        self.entry.GetSelectionItemPattern = lambda: types.SimpleNamespace(IsSelected=True)
        def restore(**kwargs):
            self.assertFalse(kwargs["controls_ready"]())
            self.feed.IsOffscreen = False
            return True
        self.social._recover_current_window = Mock(side_effect=restore)
        self.assertIs(self.social._open_feed(), self.feed)
        self.social._recover_current_window.assert_called_once()
        self.assertNotIn("朋友圈", self.auto.clicks)

    def test_selected_page_without_exposed_feed_stops_without_clicking_entry(self):
        self.fixture()
        self.feed.IsOffscreen = True
        self.social._recover_current_window = Mock(return_value=False)
        result = self.social.refresh_feed(generation=7)
        self.assertFalse(result["ok"])
        self.assertIn("页面已打开", result["error"])
        self.assertEqual(self.auto.clicks, [])

    def test_main_feed_discovery_does_not_scan_chat_history_children(self):
        self.fixture()
        history = Control(self.auto, "消息", "ListControl", aid="MainView.chat_message_list")
        history.GetChildren = Mock(side_effect=AssertionError("chat history must not be walked"))
        self.auto.main.add(history)
        self.assertIs(self.social._feed(), self.feed)
        history.GetChildren.assert_not_called()

    def test_publish_navigation_uses_selected_page_recovery_without_toggling(self):
        self.fixture()
        self.feed.IsOffscreen = True
        self.toolbar.add(Control(self.auto, "发表", "ButtonControl"))
        self.social._find_control_global = Mock(return_value=Control(self.auto, "相机", "ButtonControl"))
        editor = Control(self.auto, "这一刻的想法", "EditControl")
        self.social._find_moment_editor = Mock(return_value=editor)
        self.social._choose_image = Mock()
        def restore(**kwargs):
            self.feed.IsOffscreen = False
            return True
        self.social._recover_current_window = Mock(side_effect=restore)
        self.assertEqual(self.social._open_composer("synthetic.png"), (editor, True))
        self.social._choose_image.assert_called_once()
        self.assertNotIn("朋友圈", self.auto.clicks)

    def test_feed_going_cold_after_refresh_gets_same_window_recovery(self):
        self.fixture()
        self.refresh.click_action = lambda: setattr(self.feed, "IsOffscreen", True)
        def restore(**kwargs):
            self.assertFalse(kwargs["controls_ready"]())
            self.feed.IsOffscreen = False
            return True
        self.social._recover_current_window = Mock(side_effect=restore)
        self.assertTrue(self.social.refresh_feed(generation=7)["ok"])
        self.social._recover_current_window.assert_called_once()


if __name__ == "__main__":
    unittest.main()
