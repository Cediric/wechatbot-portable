"""Synthetic feed/model/UIA only; no paid APIs or actual desktop interactions."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
NOW = 1791720000.0  # 2026-10-11 20:00 China standard time.


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def post(*, own=False, **kwargs):
    result = {"post_id": "10001", "author_id": "wxid_self" if own else "wxid_friend",
        "author_name": "本人" if own else "朋友", "author_names": ["本人" if own else "朋友"],
        "content": "今天散步很舒服", "created_at": int(NOW - 3600), "comments": [],
        "like_user_ids": [], "likes_verified": True, "liked_by_self": False,
        "has_media": False, "identity_verified": True, "interaction_identity_verified": True}
    result.update(kwargs)
    return result


def comment(**kwargs):
    result = {"comment_id": "20001", "author_id": "wxid_friend", "author_name": "朋友", "author_names": ["朋友"],
        "content": "这天气适合散步！", "created_at": int(NOW - 60), "identity_verified": True,
        "ref_author_id": "", "ref_comment_id": ""}
    result.update(kwargs)
    return result


class MomentsInteractionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".social-test-", dir=SOURCE.parent / "tests")
        self.addCleanup(self.temp.cleanup)
        self.config = types.SimpleNamespace(MOMENTS_INTERACTIONS_ENABLED=True, DATA_DIR=self.temp.name,
            BOT_WXID="wxid_self", AUTOPILOT_LLM_API_BASE="https://example.invalid/v1",
            AUTOPILOT_LLM_API_KEY="synthetic-key", AUTOPILOT_LLM_MODEL="synthetic-model",
            MOMENTS_DECISION_LLM_API_BASE="https://example.invalid/v1", MOMENTS_DECISION_LLM_API_KEY="synthetic-key", MOMENTS_DECISION_LLM_MODEL="decision-model",
            MAIN_LLM_API_BASE="https://example.invalid/v1", MAIN_LLM_API_KEY="synthetic-key", MAIN_LLM_MODEL="main-model", MOMENTS_REACTION_MIN_SECONDS=0.5,
            BOT_PERSONA="谨慎自然", MOMENTS_ACTIVE_START_HOUR=8, MOMENTS_ACTIVE_END_HOUR=23,
            MOMENTS_INTERACTION_COOLDOWN_MIN=10, MOMENTS_INTERACTION_CONTACTS=[], MEMORY_SCOPE="session")
        self.state = types.SimpleNamespace(running=True, paused=threading.Event(), bridge_generation=7)
        self.http = types.SimpleNamespace(session=Mock(side_effect=AssertionError("No real HTTP allowed")))
        modules = patch.dict(sys.modules, {"config": self.config, "state": self.state, "http_util": self.http})
        modules.start(); self.addCleanup(modules.stop)
        self.stable = load("social_stable", "stable_id.py")
        stable_patch = patch.dict(sys.modules, {"stable_id": self.stable})
        stable_patch.start(); self.addCleanup(stable_patch.stop)
        self.feed_module = load("social_feed", "moments_feed.py")
        feed_patch = patch.dict(sys.modules, {"moments_feed": self.feed_module})
        feed_patch.start(); self.addCleanup(feed_patch.stop)
        self.module = load("social_interactions", "moments_interactions.py")
        self.original_observe = self.module.MomentsInteractions._observe_post
        observer = patch.object(self.module.MomentsInteractions, "_observe_post", return_value="本条已阅读")
        observer.start(); self.addCleanup(observer.stop)
        clock = patch.object(self.module, "_now", return_value=NOW)
        clock.start(); self.addCleanup(clock.stop)
        self.sender = Mock()
        self.sender.refresh_feed.return_value = {"ok": True, "status": "refreshed"}
        self.sender.execute_interaction.return_value = {"status": "accepted_by_ui", "submit_attempted": True, "outcome_uncertain": False}
        self.feed = Mock()
        self.feed.fetch.return_value = [post()]
        self.bot = self.module.MomentsInteractions(sender=self.sender, feed_client=self.feed)
        self.bot._ask_llm = Mock(side_effect=self.select_like)
        self.bot._observe_post = Mock(return_value="本条已阅读")
        self.model = types.SimpleNamespace(request=Mock(return_value=json.dumps({"text": "一句自然回应"})),
            profile_settings=load("social_model_profile", "model_client.py").profile_settings)
        model_patch = patch.dict(sys.modules, {"model_client": self.model})
        model_patch.start(); self.addCleanup(model_patch.stop)
        wait = patch.object(self.module.MomentsInteractions, "_wait_reaction", side_effect=lambda started, cancelled: not cancelled())
        wait.start(); self.addCleanup(wait.stop)

    @staticmethod
    def select_like(context):
        return next(({"candidate_id": item["candidate_id"], "text": "", "reason": "朋友近况轻松"}
            for item in json.loads(context)["candidates"] if item["type"] == "like"), {"candidate_id": "", "text": "", "reason": "略过"})

    def select(self, kind, text="一句自然回应"):
        def choose(context):
            return next(({"candidate_id": item["candidate_id"], "reason": "适合回应", "guidance": "简短回应"}
                for item in json.loads(context)["candidates"] if item["type"] == kind), {"candidate_id": "", "text": ""})
        self.bot._ask_llm.side_effect = choose
        self.model.request.return_value = json.dumps({"text": text}, ensure_ascii=False)

    def run_good(self):
        result = self.bot.run_once()
        self.assertEqual(result["status"], "accepted_by_ui", result)
        self.assertTrue(result["ok"])
        return result

    def test_refresh_precedes_cache_read_and_sender_receives_exact_identity(self):
        sequence = []
        self.sender.refresh_feed.side_effect = lambda **kw: sequence.append("refresh") or {"ok": True}
        self.feed.fetch.side_effect = lambda **kw: sequence.append("feed") or [post()]
        self.run_good()
        self.assertEqual(sequence, ["refresh", "feed", "feed"])
        self.assertEqual(self.feed.fetch.call_args_list[0].kwargs["usernames"], ["wxid_self"])
        self.assertEqual(self.feed.fetch.call_args_list[0].kwargs["limit"], 10)
        action = self.sender.execute_interaction.call_args.args[0]
        self.assertEqual(action["source"]["post_id"], "10001")
        self.assertEqual(action["source"]["author_id"], "wxid_friend")
        self.assertEqual(self.sender.execute_interaction.call_args.kwargs["generation"], 7)
        snapshot = self.bot.status_snapshot()
        self.assertEqual(snapshot["daily"]["like"], 1)
        self.assertIsNone(snapshot["pending"])
        self.assertEqual(snapshot["recent"][0]["status"], "completed")

    def test_explicit_manual_browse_runs_outside_scheduled_hours(self):
        with patch.object(self.bot, "_active", return_value=False):
            self.assertEqual(self.bot.run_once()["status"], "outside_hours")
            self.sender.refresh_feed.assert_not_called()
            result = self.bot.run_once(manual=True)
        self.assertEqual(result["status"], "accepted_by_ui", result)
        self.sender.execute_interaction.assert_called_once()
        self.assertEqual(self.sender.execute_interaction.call_args.args[0]["source"]["post_id"], "10001")

    def test_explicit_manual_browse_still_yields_to_private_chat(self):
        self.state.social_should_yield = lambda: True
        result = self.bot.run_once(manual=True)
        self.assertEqual(result["status"], "private_priority")
        self.sender.refresh_feed.assert_not_called()
        self.bot._ask_llm.assert_not_called()

    def test_default_disabled_manual_run_cannot_bypass(self):
        self.bot.set_enabled(False)
        self.assertEqual(self.bot.run_once()["status"], "disabled")
        self.sender.refresh_feed.assert_not_called()
        self.bot._ask_llm.assert_not_called()

    def test_stopped_and_paused_never_refresh(self):
        self.state.running = False
        self.assertEqual(self.bot.run_once()["status"], "disabled")
        self.state.running = True; self.state.paused.set()
        self.assertEqual(self.bot.run_once()["status"], "disabled")
        self.sender.refresh_feed.assert_not_called()

    def test_missing_model_key_and_missing_self_identity_are_clear(self):
        self.config.MOMENTS_DECISION_LLM_API_KEY = ""
        self.assertEqual(self.bot.run_once()["status"], "unavailable")
        self.assertIn("密钥", self.bot.last_result)
        self.config.MOMENTS_DECISION_LLM_API_KEY = "synthetic"
        self.config.BOT_WXID = "我是本人"
        self.assertEqual(self.bot.run_once()["status"], "unavailable")
        self.assertIn("真实微信 ID", self.bot.last_result)
        self.sender.refresh_feed.assert_not_called()

    def test_refresh_failure_does_not_read_stale_feed_or_call_model(self):
        self.sender.refresh_feed.return_value = {"ok": False, "error": "UIA 被遮挡"}
        self.assertEqual(self.bot.run_once()["status"], "refresh_failed")
        self.feed.fetch.assert_not_called()
        self.bot._ask_llm.assert_not_called()

    def test_private_input_cancels_feed_read_without_reporting_feed_failure(self):
        def interrupted(**kwargs):
            self.bot.cancel_pending()
            raise self.feed_module.FeedError("朋友圈读取已取消")
        self.feed.fetch.side_effect = interrupted
        self.assertEqual(self.bot.run_once()["status"], "cancelled")
        self.assertNotIn("读取失败", self.bot.last_result)
        self.sender.execute_interaction.assert_not_called()

    def test_cancel_before_final_click_is_not_reported_as_failed_interaction(self):
        def interrupted(action, **kwargs):
            self.bot.cancel_pending()
            return {"status": "failed", "submit_attempted": False, "outcome_uncertain": False, "error": "新私聊已到达"}
        self.sender.execute_interaction.side_effect = interrupted
        self.assertEqual(self.bot.run_once()["status"], "cancelled")
        self.assertNotIn("未提交", self.bot.last_result)
        self.assertIsNone(self.bot.pending_snapshot())
        self.assertEqual(self.bot.status_snapshot()["recent"][0]["status"], "cancelled")

    def test_cancellation_after_confirmed_click_still_records_delivery(self):
        def accepted(action, **kwargs):
            self.bot.cancel_pending()
            return {"status": "accepted_by_ui", "submit_attempted": True, "outcome_uncertain": False}
        self.sender.execute_interaction.side_effect = accepted
        self.run_good()
        self.assertEqual(self.bot.status_snapshot()["daily"]["like"], 1)

    def test_one_ambiguous_comment_does_not_block_another_independent_post(self):
        self.feed.fetch.side_effect = [[post(own=True, comments=[comment()])], [post(post_id="10002")]]
        def choose(context):
            items = json.loads(context)["candidates"]
            item = next((item for item in items if item["type"] == "reply"), items[0])
            return {"candidate_id": item["candidate_id"], "reason": "合适"}
        self.bot._ask_llm.side_effect = choose
        self.sender.execute_interaction.side_effect = [
            {"status": "failed", "submit_attempted": False, "outcome_uncertain": False,
             "error_code": "comment_identity_ambiguous", "error": "同名同正文原评论无法唯一确认"},
            {"status": "accepted_by_ui", "submit_attempted": True, "outcome_uncertain": False}]
        result = self.bot.run_once()
        self.assertEqual(result["status"], "failed")
        self.assertIn("其余动态已继续独立处理", result["error"])
        self.assertEqual([call.args[0]["source"]["post_id"] for call in self.sender.execute_interaction.call_args_list], ["10001", "10002"])
        self.assertEqual(self.bot.status_snapshot()["daily"]["like"], 1)
        self.assertIsNone(self.bot.pending_snapshot())

    def test_ambiguous_error_with_uncertain_submit_still_stops_the_batch(self):
        self.feed.fetch.side_effect = [[post(own=True, comments=[comment()])], [post(post_id="10002")]]
        self.select("reply")
        self.sender.execute_interaction.return_value = {"status": "failed", "submit_attempted": True,
            "outcome_uncertain": True, "error_code": "comment_identity_ambiguous"}
        self.assertEqual(self.bot.run_once()["status"], "pending_review")
        self.sender.execute_interaction.assert_called_once()

    def test_refresh_diagnostics_reach_execution_result_and_log_without_rescan(self):
        details = {"substage": "account_feed_context", "expected_main": {"hwnd": 1234, "pid": 77}}
        self.sender.refresh_feed.return_value = {"ok": False, "error": "[account_feed_context] 主窗口无法确认", "uia_diagnostics": details}
        with self.assertLogs("ob11-bridge", level="INFO") as logs:
            result = self.bot.run_once()
        self.assertEqual(result["uia_diagnostics"], details)
        self.assertTrue(any("uia_diagnostics=" in line and '"hwnd": 1234' in line for line in logs.output))
        self.sender.refresh_feed.assert_called_once()

    def test_cancel_and_generation_change_during_model_cannot_send(self):
        for mutate in (self.bot.cancel_pending, lambda: setattr(self.state, "bridge_generation", 8)):
            self.bot.set_enabled(True); self.state.bridge_generation = 7
            self.bot._ask_llm.side_effect = lambda context: mutate() or self.select_like(context)
            self.assertEqual(self.bot.run_once()["status"], "cancelled")
        self.sender.execute_interaction.assert_not_called()

    def test_cancel_token_survives_toggle_during_model(self):
        def choose(context):
            self.bot.set_enabled(False); self.bot.set_enabled(True)
            return self.select_like(context)
        self.bot._ask_llm.side_effect = choose
        self.assertEqual(self.bot.run_once()["status"], "cancelled")
        self.sender.execute_interaction.assert_not_called()

    def test_delayed_manual_request_uses_captured_sender_generation_and_cancel(self):
        token, generation, sender = self.bot._cancel, self.state.bridge_generation, self.bot.sender
        self.bot.set_enabled(False); self.bot.set_enabled(True)
        self.assertEqual(self.bot.run_once(expected_cancel=token, expected_generation=generation, expected_sender=sender)["status"], "disabled")
        token = self.bot._cancel
        self.state.bridge_generation = 8
        self.assertEqual(self.bot.run_once(expected_cancel=token, expected_generation=generation, expected_sender=sender)["status"], "disabled")
        self.state.bridge_generation = generation
        self.bot.sender = Mock()
        self.assertEqual(self.bot.run_once(expected_cancel=token, expected_generation=generation, expected_sender=sender)["status"], "disabled")
        self.sender.refresh_feed.assert_not_called(); self.bot.sender.refresh_feed.assert_not_called()

    def test_sender_replacement_during_model_cannot_retarget_send(self):
        replacement = Mock()
        def change(context):
            self.bot.sender = replacement
            return self.select_like(context)
        self.bot._ask_llm.side_effect = change
        self.assertEqual(self.bot.run_once()["status"], "cancelled")
        self.sender.execute_interaction.assert_not_called(); replacement.execute_interaction.assert_not_called()

    def test_real_reply_id_and_actor_reach_sender(self):
        self.feed.fetch.return_value = [post(own=True, comments=[comment()])]
        self.select("reply", "你那边也好天气吗？")
        self.run_good()
        action = self.sender.execute_interaction.call_args.args[0]
        self.assertEqual(action["comment_id"], "20001")
        self.assertEqual(action["comment_author_id"], "wxid_friend")
        self.assertEqual(action["source"]["author_id"], "wxid_self")

    def test_synthetic_comment_id_never_becomes_reply_candidate(self):
        self.feed.fetch.return_value = [post(own=True, comments=[comment(comment_id="cmt_1791720000_random")])]
        self.select("reply")
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.sender.execute_interaction.assert_not_called()
        self.bot._ask_llm.assert_not_called()

    def test_unknown_sender_and_own_comment_cannot_reply(self):
        self.feed.fetch.return_value = [post(own=True, comments=[comment(author_id=""), comment(comment_id="20002", author_id="wxid_self")])]
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.sender.execute_interaction.assert_not_called()

    def test_other_post_reply_requires_explicit_ref_to_bot(self):
        self.feed.fetch.return_value = [post(comments=[comment()])]
        self.select("reply")
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.feed.fetch.return_value = [post(comments=[comment(ref_author_id="wxid_self")])]
        self.run_good()

    def test_reply_already_answered_by_bot_is_not_repeated(self):
        self.feed.fetch.return_value = [post(own=True, comments=[comment(), comment(comment_id="20002", author_id="wxid_self", ref_comment_id="20001")])]
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.bot._ask_llm.assert_not_called()

    def test_already_liked_never_reaches_toggle(self):
        self.feed.fetch.return_value = [post(liked_by_self=True)]
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.sender.execute_interaction.assert_not_called()

    def test_live_already_liked_does_not_spend_quota_or_stop_the_next_post(self):
        self.feed.fetch.return_value = [post(post_id="10001"), post(post_id="10002")]
        self.sender.execute_interaction.side_effect = [
            {"status": "already_liked", "submit_attempted": False, "outcome_uncertain": False},
            {"status": "accepted_by_ui", "submit_attempted": True, "outcome_uncertain": False}]
        result = self.bot.run_once(manual=True)
        self.assertEqual(result["status"], "accepted_by_ui")
        self.assertEqual(self.sender.execute_interaction.call_count, 2)
        status = self.bot.status_snapshot()
        self.assertEqual(status["daily"]["like"], 1)
        self.assertEqual({record["status"] for record in status["recent"]}, {"already_liked", "completed"})
        self.assertIsNone(status["pending"])

    def test_manual_failed_action_log_contains_stage_without_post_or_comment_body(self):
        self.select("comment", "private-comment-must-not-be-logged")
        self.feed.fetch.return_value = [post(content="private-post-must-not-be-logged")]
        self.sender.execute_interaction.return_value = {"status": "failed", "stage": "comment_editor",
            "submit_attempted": False, "outcome_uncertain": False, "error": "朋友圈编辑器不可用"}
        with self.assertLogs("ob11-bridge", "INFO") as logged:
            self.assertEqual(self.bot.run_once(manual=True)["status"], "failed")
        output = "\n".join(logged.output)
        self.assertIn("action=comment stage=comment_editor status=failed submit_attempted=False", output)
        self.assertIn("mode=manual_browse", output)
        self.assertNotIn("must-not-be-logged", output)

    def test_unknown_cached_like_state_is_deferred_to_explicit_ui_menu_guard(self):
        self.feed.fetch.return_value = [post(likes_verified=False)]
        self.sender.execute_interaction.return_value = {"status": "failed", "submit_attempted": False,
            "outcome_uncertain": False, "error": "当前菜单已为取消赞，未点击"}
        self.assertEqual(self.bot.run_once()["status"], "failed")
        self.assertFalse(self.sender.execute_interaction.call_args.args[0]["source"]["likes_verified"])
        self.assertIsNone(self.bot.pending_snapshot())
        self.assertEqual(self.bot.status_snapshot()["daily"]["like"], 0)

    def test_model_cannot_inject_target_or_change_type(self):
        for decision in ({"candidate_id": "a"*32, "text": ""}, {"candidate_id": "10001", "text": ""},
                {"candidate_id": "", "post_id": "10001", "text": ""}, {"actions": [{"type": "like", "post_id": "10001"}]}):
            self.bot._ask_llm.side_effect = None
            self.bot._ask_llm.return_value = decision
            self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.sender.execute_interaction.assert_not_called()

    def test_like_uses_no_writer_and_comment_retains_long_authored_text(self):
        self.select("like", "文字不能被误当作点赞")
        self.assertEqual(self.bot.run_once()["status"], "accepted_by_ui")
        self.model.request.assert_not_called()
        self.sender.execute_interaction.reset_mock()
        self.config.MOMENTS_INTERACTION_COOLDOWN_MIN = 0
        text = "C++ & 我的想法："*40
        self.select("comment", text)
        self.assertEqual(self.bot.run_once()["status"], "accepted_by_ui")
        self.assertEqual(self.sender.execute_interaction.call_args.args[0]["text"], text)

    def test_image_only_post_does_not_allow_guessed_top_level_comment(self):
        self.bot._observe_post.return_value = None
        self.feed.fetch.return_value = [post(content="", has_media=True)]
        self.select("comment")
        self.assertEqual(self.bot.run_once()["status"], "observation_failed")
        self.sender.execute_interaction.assert_not_called()

    def test_old_post_future_post_and_old_comment_are_skipped(self):
        for value in [post(post_id="10001", created_at=NOW-8*86400), post(post_id="10002", created_at=NOW+3600),
                post(post_id="10003", own=True, comments=[comment(created_at=NOW-4*86400)])]:
            self.feed.fetch.return_value = [value]
            self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.bot._ask_llm.assert_not_called()

    def test_contact_allowlist_includes_own_posts_but_filters_reply_author(self):
        self.config.MOMENTS_INTERACTION_CONTACTS = ["wxid_allowed"]
        self.feed.fetch.return_value = [post(own=True, comments=[comment()])]
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.assertEqual(self.feed.fetch.call_args.kwargs["usernames"], ["wxid_allowed", "wxid_self"])
        self.feed.fetch.return_value = [post(own=True, comments=[comment(comment_id="20002", author_id="wxid_allowed")])]
        self.select("reply")
        self.run_good()

    def test_own_posts_never_like_or_top_level_comment(self):
        self.feed.fetch.return_value = [post(own=True)]
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.bot._ask_llm.assert_not_called()

    def test_session_memories_never_leak_to_feed_model(self):
        memory = Mock()
        memory.recent.return_value = [{"session_id": "private:1", "content": "私人密码 synthetic"}, {"session_id": "", "content": "公共偏好 synthetic"}]
        memory.recent_chats.return_value = [{"content": "shared chat synthetic"}]
        self.bot.memory_store = memory
        self.run_good()
        context = self.bot._ask_llm.call_args.args[0]
        self.assertIn("公共偏好", context); self.assertNotIn("私人密码", context)
        self.assertTrue(all(call.kwargs.get("session_id") == "wxid_friend" or call.kwargs.get("actor_id") == "wxid_friend"
                            for call in memory.recent_chats.call_args_list))
        self.config.MEMORY_SCOPE = "shared"
        with self.bot._connection() as db:
            candidates = self.bot._candidates([post(post_id="10002")], "wxid_self", [], db, NOW+3600)
        self.assertNotIn("shared chat", self.bot._context(candidates))

    def test_budget_is_durable_and_resets_on_beijing_midnight(self):
        self.config.MOMENTS_LIKE_DAILY_LIMIT = 1
        self.config.MOMENTS_INTERACTION_COOLDOWN_MIN = 0
        self.run_good()
        self.feed.fetch.return_value = [post(post_id="10002")]
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        replacement = self.module.MomentsInteractions(sender=self.sender, feed_client=self.feed)
        self.assertEqual(replacement.status_snapshot()["daily"]["like"], 1)
        with patch.object(self.module, "_now", return_value=NOW+86400):
            self.run_good()
            self.assertEqual(self.bot.status_snapshot()["daily"]["like"], 1)

    def test_dedup_persists_across_restart_even_after_cooldown(self):
        self.config.MOMENTS_INTERACTION_COOLDOWN_MIN = 0
        self.run_good()
        replacement = self.module.MomentsInteractions(sender=self.sender, feed_client=self.feed)
        replacement._ask_llm = Mock(side_effect=self.select_like)
        with patch.object(self.module, "_now", return_value=NOW+86400):
            self.assertEqual(replacement.run_once()["status"], "skipped")
        self.assertEqual(self.sender.execute_interaction.call_count, 1)

    def test_cooldown_prevents_all_other_interactions(self):
        self.run_good()
        self.feed.fetch.return_value = [post(post_id="10002")]
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.assertEqual(self.sender.execute_interaction.call_count, 1)
        with patch.object(self.module, "_now", return_value=NOW+601):
            self.run_good()

    def test_fractional_cooldown_is_not_truncated(self):
        self.config.MOMENTS_INTERACTION_COOLDOWN_MIN = 0.5
        self.run_good()
        self.feed.fetch.return_value = [post(post_id="10002")]
        with patch.object(self.module, "_now", return_value=NOW+29):
            self.assertEqual(self.bot.run_once()["status"], "skipped")
        with patch.object(self.module, "_now", return_value=NOW+31):
            self.run_good()
        self.config.MOMENTS_BROWSE_INTERVAL_MIN = 15.5
        self.assertEqual(self.module._minutes("MOMENTS_BROWSE_INTERVAL_MIN", 15, minimum=1), 930)

    def test_hours_use_beijing_and_support_overnight(self):
        self.assertTrue(self.bot._active(NOW))
        self.config.MOMENTS_ACTIVE_START_HOUR = 22; self.config.MOMENTS_ACTIVE_END_HOUR = 6
        self.assertFalse(self.bot._active(NOW))
        self.assertEqual(self.bot.run_once()["status"], "outside_hours")
        self.sender.refresh_feed.assert_not_called()
        self.assertTrue(self.bot._active(NOW+4*3600))

    def test_intent_committed_before_entering_sender(self):
        def inspect_intent(action, **kwargs):
            pending = self.bot.pending_snapshot()
            self.assertEqual(pending["status"], "pending_review")
            self.assertEqual(pending["post_id"], action["source"]["post_id"])
            self.assertRegex(pending["intent_id"], r"^[0-9a-f]{32}$")
            return {"status": "accepted_by_ui", "submit_attempted": True, "outcome_uncertain": False}
        self.sender.execute_interaction.side_effect = inspect_intent
        self.run_good()

    def test_uncertain_result_blocks_model_and_refresh_after_restart(self):
        self.sender.execute_interaction.return_value = {"status": "failed", "submit_attempted": True, "outcome_uncertain": True, "error": "提交后无法读取"}
        result = self.bot.run_once()
        self.assertEqual(result["status"], "pending_review")
        replacement = self.module.MomentsInteractions(sender=self.sender, feed_client=self.feed)
        replacement._ask_llm = Mock()
        self.sender.refresh_feed.reset_mock()
        self.assertEqual(replacement.run_once()["status"], "pending_review")
        replacement._ask_llm.assert_not_called(); self.sender.refresh_feed.assert_not_called()
        self.assertEqual(replacement.status_snapshot()["daily"]["like"], 1)

    def test_exception_after_driver_entry_is_uncertain(self):
        self.sender.execute_interaction.side_effect = OSError("synthetic interruption")
        self.assertEqual(self.bot.run_once()["status"], "pending_review")
        self.assertIsNotNone(self.bot.pending_snapshot())

    def test_unreported_submit_state_is_uncertain(self):
        self.sender.execute_interaction.return_value = {"status": "failed", "error": "unknown"}
        self.assertEqual(self.bot.run_once()["status"], "pending_review")

    def test_accepted_result_without_explicit_submit_proof_is_uncertain(self):
        self.sender.execute_interaction.return_value = {"status": "accepted_by_ui"}
        self.assertEqual(self.bot.run_once()["status"], "pending_review")
        self.assertIsNotNone(self.bot.pending_snapshot())

    def test_pre_submit_failure_is_logged_without_pending_or_quota(self):
        self.sender.execute_interaction.return_value = {"status": "failed", "submit_attempted": False, "outcome_uncertain": False, "error": "目标无法核验"}
        self.assertEqual(self.bot.run_once()["status"], "failed")
        self.assertIsNone(self.bot.pending_snapshot())
        self.assertEqual(self.bot.status_snapshot()["daily"]["like"], 0)
        self.assertEqual(self.bot.status_snapshot()["recent"][0]["status"], "failed")

    def test_known_pre_submit_failure_can_be_redecided_only_after_normal_interval(self):
        self.sender.execute_interaction.return_value = {"status": "failed", "submit_attempted": False,
            "outcome_uncertain": False, "error": "临时被窗口遮挡"}
        self.assertEqual(self.bot.run_once()["status"], "failed")
        self.sender.execute_interaction.return_value = {"status": "accepted_by_ui", "submit_attempted": True, "outcome_uncertain": False}
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.assertEqual(self.sender.execute_interaction.call_count, 1)
        with patch.object(self.module, "_now", return_value=NOW+901):
            self.run_good()
        self.assertEqual(self.sender.execute_interaction.call_count, 2)
        self.assertEqual(len(self.bot.status_snapshot()["recent"]), 2)
        self.assertEqual({item["status"] for item in self.bot.status_snapshot()["recent"]}, {"failed", "completed"})

    def test_first_schema_upgrade_preserves_history_and_removes_failed_unique_key(self):
        legacy = Path(self.temp.name) / "legacy"; legacy.mkdir()
        path = legacy / "moments_interactions.sqlite3"
        db = sqlite3.connect(path)
        try:
            with db:
                db.execute(self.module._TABLE_SQL.replace("dedup_key TEXT NOT NULL,", "dedup_key TEXT NOT NULL UNIQUE,").format(name="interactions"))
                db.execute("INSERT INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("a"*32, "like:10001:", "like", "10001", "", "wxid_friend", "朋友", "", "旧记录", "{}", NOW-1000, self.module._day(NOW), "failed", "临时遮挡", NOW-1000))
        finally:
            db.close()
        upgraded = self.module.MomentsInteractions(sender=self.sender, feed_client=self.feed, data_dir=legacy)
        upgraded._ask_llm = Mock(side_effect=self.select_like)
        self.assertEqual(upgraded.run_once()["status"], "accepted_by_ui")
        self.assertEqual(len(upgraded.status_snapshot()["recent"]), 2)
        self.assertIsNone(upgraded.pending_snapshot())

    def test_old_unknown_time_comments_form_baseline_new_ids_are_replyable_after_restart(self):
        age = int(NOW-30*86400)
        old = comment(comment_id="20001", created_at=age, time_source="post")
        self.feed.fetch.return_value = [post(own=True, created_at=age, comments=[old])]
        self.select("reply")
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.bot._ask_llm.assert_not_called()
        replacement = self.module.MomentsInteractions(sender=self.sender, feed_client=self.feed)
        replacement._ask_llm = Mock(side_effect=lambda context: next((
            {"candidate_id": item["candidate_id"], "text": "谢谢你来看我的动态！"}
            for item in json.loads(context)["candidates"] if item["type"] == "reply"), {"candidate_id": "", "text": ""}))
        self.feed.fetch.return_value = [post(own=True, created_at=age, comments=[old, comment(comment_id="20002", content="刚看到这条", created_at=age, time_source="post")])]
        self.assertEqual(replacement.run_once()["status"], "accepted_by_ui")
        action = self.sender.execute_interaction.call_args.args[0]
        self.assertEqual(action["comment_id"], "20002")
        self.assertTrue(action["comment"]["observed_new"])
        self.assertEqual(action["comment"]["observed_at"], NOW)
        self.assertEqual(self.sender.execute_interaction.call_count, 1)

    def test_known_recent_comment_on_old_post_is_not_rejected_by_post_age(self):
        self.feed.fetch.return_value = [post(own=True, created_at=NOW-60*86400, comments=[comment()])]
        self.select("reply")
        self.run_good()

    def test_emoji_only_comment_is_not_replied_and_identity_skips_are_visible(self):
        emoji = comment(content='')
        def fetch(**kwargs):
            self.feed.last_skipped = 1 if kwargs.get('usernames') == ['wxid_self'] else 2
            return [post(own=True, comments=[emoji])]
        self.feed.fetch.side_effect = fetch
        self.select('reply')
        self.assertEqual(self.bot.run_once()['status'], 'skipped')
        self.sender.execute_interaction.assert_not_called()
        self.assertEqual(self.bot.status_snapshot()['feed']['skipped_post_count'], 3)

    def test_native_32_to_64_upgrade_preserves_old_comment_observation(self):
        age = int(NOW - 30*86400)
        old = {'comment_id': '', 'source_comment_id': '20001', 'author_id': '', 'author_name': '朋友',
            'content': '这天气适合散步！', 'identity_verified': False, 'created_at': 0}
        self.feed.fetch.return_value = [post(own=True, created_at=age, comments=[old])]
        self.select('reply')
        self.assertEqual(self.bot.run_once()['status'], 'skipped')
        canonical = comment(comment_id='18446744073709550001', source_comment_id='20001',
            native_alias_verified=True, created_at=age, time_source='post')
        self.feed.fetch.return_value = [post(own=True, created_at=age, comments=[canonical])]
        self.assertEqual(self.bot.run_once()['status'], 'skipped')
        self.sender.execute_interaction.assert_not_called()
        with self.bot._connection() as db:
            old_rows = db.execute("SELECT COUNT(*) FROM observed_comments WHERE comment_id='20001'").fetchone()[0]
            new = db.execute("SELECT * FROM observed_comments WHERE comment_id='18446744073709550001'").fetchone()
        self.assertEqual(old_rows, 0)
        self.assertEqual(new['observed_new'], 0)
        self.assertEqual(new['first_seen'], NOW)
        self.assertEqual(new['author_id'], 'wxid_friend')

    def test_native_32_to_64_upgrade_does_not_reply_again_to_completed_target(self):
        self.feed.fetch.return_value = [post(own=True, comments=[comment(comment_id='20001')])]
        self.select('reply')
        self.run_good()
        self.config.MOMENTS_INTERACTION_COOLDOWN_MIN = 0
        canonical = comment(comment_id='18446744073709550001', source_comment_id='20001', native_alias_verified=True)
        self.feed.fetch.return_value = [post(own=True, comments=[canonical])]
        self.assertEqual(self.bot.run_once()['status'], 'skipped')
        self.assertEqual(self.sender.execute_interaction.call_count, 1)

    def test_generated_cache_upgrade_baselines_old_list_then_new_real_id_after_restart(self):
        age = int(NOW - 7*86400)
        cached = {'comment_id': '', 'source_comment_id': '', 'author_id': '', 'author_name': '朋友',
            'content': '这天气适合散步！', 'identity_verified': False, 'created_at': 0}
        self.feed.fetch.return_value = [post(own=True, created_at=age, comments=[cached], interaction_identity_verified=False)]
        self.select('reply')
        self.assertEqual(self.bot.run_once()['status'], 'skipped')
        with self.bot._connection() as db:
            self.assertEqual(db.execute("SELECT comments_complete FROM observed_posts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observed_comments").fetchone()[0], 0)
        old = comment(created_at=age, time_source='post')
        self.feed.fetch.return_value = [post(own=True, created_at=age, comments=[old])]
        self.assertEqual(self.bot.run_once()['status'], 'skipped')
        self.sender.execute_interaction.assert_not_called()
        replacement = self.module.MomentsInteractions(sender=self.sender, feed_client=self.feed)
        replacement._ask_llm = Mock(side_effect=lambda context: next((
            {'candidate_id': item['candidate_id'], 'text': '谢谢你来看我的动态！'}
            for item in json.loads(context)['candidates'] if item['type']=='reply'), {'candidate_id':'','text':''}))
        with replacement._connection() as db:
            self.assertEqual(db.execute("SELECT comments_complete FROM observed_posts").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT observed_new FROM observed_comments WHERE comment_id='20001'").fetchone()[0], 0)
        self.feed.fetch.return_value = [post(own=True, created_at=age, comments=[old,
            comment(comment_id='20002', content='后来新加的评论', created_at=age, time_source='post')])]
        self.assertEqual(replacement.run_once()['status'], 'accepted_by_ui')
        self.assertEqual(self.sender.execute_interaction.call_args.args[0]['comment_id'], '20002')

    def test_first_complete_old_post_keeps_reliable_recent_comment_time_replyable(self):
        age = int(NOW - 7*86400)
        self.feed.fetch.return_value = [post(own=True, created_at=age, interaction_identity_verified=False)]
        self.select('reply')
        self.assertEqual(self.bot.run_once()['status'], 'skipped')
        self.feed.fetch.return_value = [post(own=True, created_at=age, comments=[comment(time_source='comment')])]
        self.run_good()

    def test_first_complete_empty_old_post_baselines_future_real_comment_ids(self):
        age = int(NOW - 7*86400)
        self.feed.fetch.return_value = [post(own=True, created_at=age, comments=[])]
        self.select('reply')
        self.assertEqual(self.bot.run_once()['status'], 'skipped')
        self.feed.fetch.return_value = [post(own=True, created_at=age,
            comments=[comment(created_at=age, time_source='post')])]
        self.run_good()

    def test_old_observation_schema_migrates_unproven_native_baseline_conservatively(self):
        legacy = Path(self.temp.name)/'observations'; legacy.mkdir()
        path = legacy/'moments_interactions.sqlite3'
        age = int(NOW - 7*86400)
        db = sqlite3.connect(path)
        try:
            with db:
                db.execute('CREATE TABLE observed_posts (post_id TEXT PRIMARY KEY,author_id TEXT NOT NULL,first_seen REAL NOT NULL)')
                db.execute('INSERT INTO observed_posts VALUES (?,?,?)', ('10001','wxid_self',NOW-3600))
                db.execute('CREATE TABLE observed_comments (post_id TEXT NOT NULL,comment_id TEXT NOT NULL,author_id TEXT NOT NULL,content_hash TEXT NOT NULL,first_seen REAL NOT NULL,observed_new INTEGER NOT NULL,PRIMARY KEY(post_id,comment_id))')
                digest = self.module.hashlib.sha256(comment()['content'].encode('utf-8')).hexdigest()
                db.execute('INSERT INTO observed_comments VALUES (?,?,?,?,?,?)', ('10001','20001','wxid_friend',digest,NOW-3600,1))
        finally:
            db.close()
        migrated = self.module.MomentsInteractions(sender=self.sender, feed_client=self.feed, data_dir=legacy)
        self.feed.fetch.return_value = [post(own=True, created_at=age, comments=[comment(created_at=age,time_source='post')])]
        migrated._ask_llm = Mock(side_effect=AssertionError('Old comments must not reach the model'))
        self.assertEqual(migrated.run_once()['status'],'skipped')
        self.sender.execute_interaction.assert_not_called()
        with migrated._connection() as db:
            self.assertEqual(db.execute('SELECT comments_complete FROM observed_posts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT observed_new FROM observed_comments').fetchone()[0],0)

    def test_missing_comment_actor_is_explicit_and_api_enrichment_preserves_old_baseline(self):
        age = int(NOW-30*86400)
        context_comment = {"comment_id": "", "source_comment_id": "20001", "author_id": "", "author_name": "朋友",
            "content": "这天气适合散步！", "identity_verified": False, "created_at": 0}
        self.feed.fetch.return_value = [post(own=True, created_at=age, comments=[context_comment])]
        self.select("reply")
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        snapshot = self.bot.status_snapshot()
        self.assertEqual(snapshot["feed"]["unverified_comment_count"], 1)
        self.assertEqual(snapshot["feed"]["reply_identity"], "missing")
        self.assertIn("缺少发送者微信 ID", snapshot["last_result"])
        self.assertNotIn("退出 WeFlow", snapshot["last_result"])
        self.feed.details_endpoint_missing = True
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.assertIn("缺少评论身份接口", self.bot.status_snapshot()["last_result"])
        self.feed.fetch.return_value = [post(own=True, created_at=age, comments=[comment(created_at=age, time_source="post")])]
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.sender.execute_interaction.assert_not_called()
        with self.bot._connection() as db:
            observed = db.execute("SELECT * FROM observed_comments WHERE comment_id='20001'").fetchone()
        self.assertEqual(observed["author_id"], "wxid_friend")
        self.assertEqual(observed["observed_new"], 0)

    def test_separate_own_post_query_merges_old_comments_and_caps_total_scan(self):
        self.config.MOMENTS_BROWSE_LIMIT = 100
        own_post = post(post_id="old-own-real-id", own=True, created_at=NOW-60*86400, comments=[comment()])
        friends = [post(post_id=str(30000+i)) for i in range(150)]
        def fetch(**kwargs):
            return [own_post] if kwargs["usernames"] == ["wxid_self"] else friends
        self.feed.fetch.side_effect = fetch
        self.select("reply")
        self.run_good()
        self.assertEqual(self.feed.fetch.call_args_list[1].kwargs["limit"], 99)
        self.assertEqual(self.bot.status_snapshot()["feed"]["scanned"], 100)
        self.assertEqual(self.sender.execute_interaction.call_args.args[0]["source"]["post_id"], "old-own-real-id")

    def test_reply_pending_display_contains_original_comment_and_real_commenter(self):
        self.feed.fetch.return_value = [post(own=True, comments=[comment()])]
        self.select("reply")
        self.sender.execute_interaction.return_value = {"status": "failed", "submit_attempted": True, "outcome_uncertain": True}
        result = self.bot.run_once()
        self.assertEqual(result["status"], "pending_review")
        pending = self.bot.pending_snapshot()
        self.assertEqual(pending["author_id"], "wxid_self")
        self.assertEqual(pending["comment_author_id"], "wxid_friend")
        self.assertEqual(pending["comment_author_name"], "朋友")
        self.assertEqual(pending["comment_content"], "这天气适合散步！")
        with self.bot._connection() as db:
            db.execute("UPDATE interactions SET source_json=? WHERE intent_id=?", (json.dumps({"post_id": "10001", "author_id": "wxid_self", "comments": []}), result["intent_id"]))
        self.assertEqual(self.bot.pending_snapshot()["status"], "record_error")
        self.assertFalse(self.bot.resolve_pending("published", intent_id=result["intent_id"]))

    def test_history_failure_after_click_leaves_pending(self):
        with patch.object(self.bot, "_finish", side_effect=OSError("disk full")):
            self.assertEqual(self.bot.run_once()["status"], "pending_review")
        self.assertIsNotNone(self.bot.pending_snapshot())
        self.assertEqual(self.bot.run_once()["status"], "pending_review")
        self.assertEqual(self.sender.execute_interaction.call_count, 1)

    def test_intent_write_failure_cannot_click(self):
        with patch.object(self.bot, "_write_intent", side_effect=OSError("disk full")):
            self.assertEqual(self.bot.run_once()["status"], "failed")
        self.sender.execute_interaction.assert_not_called()

    def test_cancel_between_intent_and_sender_does_not_click(self):
        original = self.bot._write_intent
        def write(*args):
            value = original(*args); self.bot.cancel_pending(); return value
        with patch.object(self.bot, "_write_intent", side_effect=write):
            self.assertEqual(self.bot.run_once()["status"], "cancelled")
        self.sender.execute_interaction.assert_not_called()
        self.assertIsNone(self.bot.pending_snapshot())

    def test_reconcile_exact_opaque_id_no_automatic_retry(self):
        self.sender.execute_interaction.return_value = {"status": "failed", "submit_attempted": True, "outcome_uncertain": True}
        result = self.bot.run_once()
        self.assertFalse(self.bot.resolve_pending("published", intent_id="f"*32))
        self.assertFalse(self.bot.resolve_pending("other", intent_id=result["intent_id"]))
        self.assertFalse(self.bot.resolve_pending({}, intent_id=result["intent_id"]))
        self.assertTrue(self.bot.resolve_pending("published", intent_id=result["intent_id"]))
        self.assertIsNone(self.bot.pending_snapshot())
        self.assertEqual(self.bot.status_snapshot()["daily"]["like"], 1)
        self.assertEqual(self.sender.execute_interaction.call_count, 1)

    def test_reconcile_not_published_preserves_dedup_and_failed_record(self):
        self.sender.execute_interaction.return_value = {"status": "failed", "submit_attempted": True, "outcome_uncertain": True}
        result = self.bot.run_once()
        self.assertTrue(self.bot.resolve_pending("not_published", intent_id=result["intent_id"]))
        self.assertEqual(self.bot.status_snapshot()["daily"]["like"], 0)
        self.assertEqual(self.bot.status_snapshot()["recent"][0]["status"], "not_published")
        self.assertEqual(self.bot.run_once()["status"], "skipped")

    def test_two_instances_cannot_decide_or_reconcile_in_flight(self):
        other = self.module.MomentsInteractions(sender=self.sender, feed_client=self.feed)
        other._ask_llm = Mock()
        def overlap(action, **kwargs):
            pending = self.bot.pending_snapshot()
            self.assertFalse(other.resolve_pending("published", intent_id=pending["intent_id"]))
            self.assertEqual(other.run_once()["status"], "failed")
            return {"status": "accepted_by_ui", "submit_attempted": True, "outcome_uncertain": False}
        self.sender.execute_interaction.side_effect = overlap
        self.run_good()
        other._ask_llm.assert_not_called()

    def test_os_lock_blocks_a_separate_process(self):
        script = """import sys,types
sys.path.insert(0,sys.argv[1])
sys.modules['config']=types.SimpleNamespace()
sys.modules['state']=types.SimpleNamespace()
sys.modules['http_util']=types.SimpleNamespace()
from moments_interactions import _process_operation
try:
    with _process_operation(sys.argv[2]):
        print('acquired')
except OSError:
    print('blocked')
"""
        with self.module._process_operation(self.bot._db_path):
            child = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", script, str(SOURCE), self.bot._db_path],
                capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "blocked")

    def test_database_corruption_blocks_all_external_operations(self):
        Path(self.bot._db_path).write_bytes(b"invalid database synthetic")
        self.assertEqual(self.bot.pending_snapshot()["status"], "record_error")
        self.assertEqual(self.bot.run_once()["status"], "pending_review")
        self.sender.refresh_feed.assert_not_called(); self.bot._ask_llm.assert_not_called()

    def test_model_error_is_reported_as_failure_instead_of_normal_skip(self):
        self.bot._ask_llm.side_effect = lambda context: self.bot._set_result("朋友圈决策模型 HTTP 401")
        result = self.bot.run_once()
        self.assertEqual(result["status"], "model_failed")
        self.assertEqual(result["error"], "朋友圈决策模型 HTTP 401")
        self.sender.execute_interaction.assert_not_called()

    def test_model_response_requires_real_json_and_preserves_system_guard(self):
        self.bot._ask_llm = types.MethodType(self.module.MomentsInteractions._ask_llm, self.bot)
        self.model.request.return_value = '```json\n{"candidate_id":"","text":"","reason":"略过"}\n```'
        result = self.bot._ask_llm("synthetic context")
        self.assertEqual(result["candidate_id"], "")
        call = self.model.request.call_args
        self.assertEqual(call.args[0], "moments_decision")
        self.assertIn("不可信", call.args[1][0]["content"])
        self.assertTrue(call.kwargs["json_mode"])
        self.model.request.return_value = 'explanation {"candidate_id":""}'
        self.assertIsNone(self.bot._ask_llm("context"))

    def test_model_failure_and_cancel_are_delegated_without_retry(self):
        self.bot._ask_llm = types.MethodType(self.module.MomentsInteractions._ask_llm, self.bot)
        self.model.request.side_effect = RuntimeError("bounded request failed")
        self.assertIsNone(self.bot._ask_llm("synthetic"))
        self.assertIn("调用失败", self.bot.last_result)
        self.model.request.assert_called_once()
        cancel = threading.Event(); cancel.set()
        self.bot._model_cancel = cancel
        self.assertIsNone(self.bot._ask_llm("synthetic"))
        self.model.request.assert_called_once()

    def test_each_post_read_decide_send_precedes_next_post_with_isolated_context(self):
        self.config.MOMENTS_INTERACTION_COOLDOWN_MIN = 0
        self.feed.fetch.side_effect = [[], [post(post_id="10001", content="甲动态"), post(post_id="10002", content="乙动态")]]
        sequence, contexts = [], []
        self.bot._observe_post.side_effect = lambda p: sequence.append("read:"+p["post_id"]) or "主模型看见 "+p["content"]
        def choose(context):
            data = json.loads(context); contexts.append(data)
            label = data["posts"][0]["content"]
            sequence.append("decide:"+label)
            return self.select_like(context)
        self.bot._ask_llm.side_effect = choose
        self.sender.execute_interaction.side_effect = lambda action, **kw: sequence.append("send:"+action["source"]["post_id"]) or {"status":"accepted_by_ui", "submit_attempted":True,"outcome_uncertain":False}
        result = self.bot.run_once()
        self.assertEqual(result["processed"], 2)
        self.assertEqual(sequence, ["read:10001", "decide:甲动态", "send:10001", "read:10002", "decide:乙动态", "send:10002"])
        self.assertTrue(all(len(c["posts"]) == 1 for c in contexts))
        self.assertNotIn("甲动态", json.dumps(contexts[1]["posts"], ensure_ascii=False))
        self.assertEqual(contexts[1]["related_interactions"][0]["post_id"], "10001")
        self.assertEqual(contexts[1]["related_interactions"][0]["target_actor_id"], "wxid_friend")

    def test_uncertain_first_post_stops_before_reading_the_next(self):
        self.config.MOMENTS_INTERACTION_COOLDOWN_MIN = 0
        self.feed.fetch.side_effect = [[], [post(), post(post_id="10002")]]
        self.sender.execute_interaction.return_value = {"submit_attempted":True,"outcome_uncertain":True}
        self.assertEqual(self.bot.run_once()["status"], "pending_review")
        self.bot._observe_post.assert_called_once()
        self.bot._ask_llm.assert_called_once()

    def test_skip_one_post_then_immediately_decide_next_without_shared_history(self):
        self.feed.fetch.side_effect = [[], [post(content="略过甲"), post(post_id="10002",content="回应乙")]]
        self.bot._ask_llm.side_effect = lambda c: {"candidate_id":"", "text":""} if "略过甲" in c else self.select_like(c)
        self.assertEqual(self.bot.run_once()["status"], "accepted_by_ui")
        self.assertEqual(self.bot._ask_llm.call_count, 2)
        self.sender.execute_interaction.assert_called_once()

    def test_main_read_failure_and_cancellation_do_not_reach_decision_or_sender(self):
        self.bot._observe_post.side_effect = lambda p: self.bot.cancel_pending() or "读取摘要"
        self.assertEqual(self.bot.run_once()["status"], "cancelled")
        self.bot._ask_llm.assert_not_called()
        self.sender.execute_interaction.assert_not_called()

    def test_main_model_receives_real_parts_and_decision_only_receives_one_summary(self):
        self.model.request.return_value = "主模型看到真实图像"
        media = types.SimpleNamespace(post_parts=Mock(return_value=[{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,synthetic"}}]))
        with patch.dict(sys.modules, {"moments_media": media}):
            result = self.original_observe(self.bot, post(has_media=True))
        self.assertEqual(result, "主模型看到真实图像")
        self.assertEqual(self.model.request.call_args.args[0], "main")
        parts = self.model.request.call_args.args[1][1]["content"]
        self.assertEqual(parts[-1]["type"], "image_url")
        self.assertEqual(json.loads(parts[0]["text"])["post_id"], "10001")
        self.assertEqual(json.loads(parts[0]["text"])["author_id"], "wxid_friend")
        self.assertIn("供决策使用的内部阅读摘要", self.model.request.call_args.args[1][0]["content"])

    def test_reply_keeps_main_model_words_symbols_and_emoji(self):
        self.feed.fetch.return_value = [post(own=True, comments=[comment()])]
        text = "谢谢！C++ & API 😊"
        self.select("reply", text)
        self.assertEqual(self.bot.run_once()["status"], "accepted_by_ui")
        self.assertEqual(self.sender.execute_interaction.call_args.args[0]["text"], text)

    def test_reply_only_poll_without_reply_candidate_does_not_touch_uia(self):
        self.feed.fetch.return_value = [post()]
        result = self.bot.run_once(replies_only=True)
        self.assertEqual(result["status"], "skipped")
        self.sender.refresh_feed.assert_not_called()
        self.bot._ask_llm.assert_not_called()

    def test_private_priority_snapshot_is_visible_to_panel(self):
        self.state.private_priority_snapshot = lambda: {"private_inflight": True, "seconds_since_private_activity": 20}
        self.assertEqual(self.bot.status_snapshot()["private_priority"]["seconds_since_private_activity"], 20)

    def test_single_post_context_rejects_accidentally_mixed_candidates(self):
        with self.bot._connection() as db:
            actions = self.bot._candidates([post(), post(post_id="10002")],"wxid_self",[],db,NOW)
        with self.assertRaises(ValueError):
            self.bot._context(actions)

    def test_main_writer_exclusively_authors_selected_comment_and_ignores_decision_draft(self):
        def decide(context):
            return next({"candidate_id": item["candidate_id"], "reason": "关系自然", "guidance": "轻松简短",
                "text": "DECISION_DRAFT_CANARY"} for item in json.loads(context)["candidates"] if item["type"] == "comment")
        self.bot._ask_llm.side_effect = decide
        self.model.request.return_value = json.dumps({"text": "看着就挺舒服~", "image_prompt": "IMAGE_ROUTE_CANARY",
            "voice_text": "VOICE_ROUTE_CANARY", "content": "CHAT_ROUTE_CANARY", "reason": "INTERNAL_REASON_CANARY"}, ensure_ascii=False)
        self.run_good()
        self.assertEqual(self.model.request.call_args.args[0], "main")
        self.assertFalse(self.model.request.call_args.kwargs["json_mode"])
        payload = json.loads(self.model.request.call_args.args[1][1]["content"])
        self.assertEqual(payload["task"], "moments_interaction_creation")
        self.assertEqual(payload["selected_action"]["type"], "comment")
        self.assertEqual(payload["context"]["posts"][0]["post_id"], "10001")
        self.assertNotIn("DECISION_DRAFT_CANARY", json.dumps(payload))
        action = self.sender.execute_interaction.call_args.args[0]
        self.assertEqual(action["text"], "看着就挺舒服~")
        self.assertNotIn("CANARY", action["text"])

    def test_main_writer_failure_never_sends_decision_draft(self):
        self.select("comment")
        choose = self.bot._ask_llm.side_effect
        self.bot._ask_llm.side_effect = lambda context: dict(choose(context), text="旧决策直接发送草稿")
        for value in ("", '{}', '{"content":"wrong route"}', '{"text":""}'):
            with self.subTest(value=value):
                self.model.request.return_value = value
                result = self.bot.run_once()
                self.assertEqual(result["status"], "model_failed")
                self.sender.execute_interaction.assert_not_called()
                self.assertIsNone(self.bot.pending_snapshot())
        self.model.request.side_effect = RuntimeError("synthetic main failure")
        self.assertEqual(self.bot.run_once()["status"], "model_failed")
        self.sender.execute_interaction.assert_not_called()

    def test_single_comment_writer_uses_plain_final_text_without_json_constraint(self):
        self.select("comment")
        self.model.request.return_value = "[Live] 这个看着挺香~"
        self.run_good()
        self.assertEqual(self.sender.execute_interaction.call_args.args[0]["text"], "[Live] 这个看着挺香~")
        self.assertFalse(self.model.request.call_args.kwargs["json_mode"])
        self.assertIn("本次只有评论或回评一个文本出口", self.model.request.call_args.args[1][0]["content"])

    def test_browse_deadline_is_restored_after_restart_not_reset(self):
        """A restart must not postpone proactive browsing by a whole interval.

        The deadline belongs to the account, so a bot that restarts every few
        hours still reaches its browse window instead of waiting forever.
        """
        stored = NOW + 600
        self.bot._persist_next_browse(stored)
        restarted = self.module.MomentsInteractions(sender=self.sender, feed_client=self.feed)
        self.assertAlmostEqual(restarted._next_browse, stored)
        self.assertGreater(restarted._next_browse, NOW)

    def test_elapsed_browse_deadline_becomes_due_after_restart(self):
        """Downtime past the deadline fires on the next check, not later."""
        self.bot._persist_next_browse(NOW - 60)
        restarted = self.module.MomentsInteractions(sender=self.sender, feed_client=self.feed)
        self.assertFalse(restarted._next_browse > NOW)

    def test_first_schedule_still_waits_one_full_interval(self):
        """No stored deadline keeps the documented first-run wait."""
        self.assertAlmostEqual(self.bot._next_browse, NOW + 240 * 60)

    def test_skip_and_like_never_call_main_writer(self):
        self.bot._ask_llm.side_effect = lambda context: {"candidate_id": "", "text": "旧草稿"}
        self.assertEqual(self.bot.run_once()["status"], "skipped")
        self.model.request.assert_not_called()
        self.bot._ask_llm.side_effect = lambda context: dict(self.select_like(context), text="旧草稿不能变成评论")
        self.run_good()
        self.model.request.assert_not_called()
        self.assertEqual(self.sender.execute_interaction.call_args.args[0]["text"], "")

    def test_each_comment_read_decide_write_execute_completes_before_next_post(self):
        self.config.MOMENTS_INTERACTION_COOLDOWN_MIN = 0
        self.feed.fetch.side_effect = [[], [post(), post(post_id="10002")]]
        sequence = []
        self.bot._observe_post.side_effect = lambda item: sequence.append("read:"+item["post_id"]) or "阅读摘要"
        def decide(context):
            item = next(item for item in json.loads(context)["candidates"] if item["type"] == "comment")
            sequence.append("decide:"+item["post_id"])
            return {"candidate_id": item["candidate_id"], "guidance": "自然"}
        def write(profile, messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            sequence.append("write:"+payload["context"]["posts"][0]["post_id"])
            return json.dumps({"text": "看起来不错~"}, ensure_ascii=False)
        self.bot._ask_llm.side_effect = decide
        self.model.request.side_effect = write
        self.sender.execute_interaction.side_effect = lambda action, **kwargs: sequence.append("send:"+action["source"]["post_id"]) or {
            "status": "accepted_by_ui", "submit_attempted": True, "outcome_uncertain": False}
        self.run_good()
        self.assertEqual(sequence, ["read:10001", "decide:10001", "write:10001", "send:10001",
            "read:10002", "decide:10002", "write:10002", "send:10002"])

    def test_private_priority_or_generation_change_during_writing_stops_before_intent(self):
        self.select("comment")
        def write(*args, **kwargs):
            self.state.bridge_generation += 1
            return json.dumps({"text": "准备好的文案"})
        self.model.request.side_effect = write
        self.assertEqual(self.bot.run_once()["status"], "cancelled")
        self.sender.execute_interaction.assert_not_called()
        self.assertIsNone(self.bot.pending_snapshot())


if __name__ == "__main__":
    unittest.main()
