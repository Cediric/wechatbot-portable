"""One-off Moments announcement through the real task entrypoint, without desktop or network."""
from datetime import datetime
import json
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

import test_autopilot as fixtures


class FirstOnlineTests(unittest.TestCase):
    def setUp(self):
        paths = patch.object(sys, "path", [str(fixtures.SOURCE), *sys.path])
        paths.start()
        self.addCleanup(paths.stop)
        self.fx = fixtures.AutopilotImageTests()
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.bot = self.fx.bot
        self.fx.config.BOT_WXID = "wxid_original"
        self.bot.set_enabled(False)
        self.bot._compose_post.return_value = {"content": "我来啦。", "image_prompt": "月光下的窗台"}
        self.image = self.fx.image()
        self.fx.tools.generate_image.return_value = self.image

    def finish(self):
        self.assertTrue(self.bot._decision_lock.acquire(timeout=5), "announcement worker did not finish")
        self.bot._decision_lock.release()

    def publish(self):
        self.bot.request_first_online()
        self.finish()

    def test_manual_with_schedule_off_uses_main_brief_and_only_publishes_once(self):
        self.bot.moments_sender = None
        factory = Mock(return_value=self.fx.sender)
        with patch.dict(sys.modules, {"senders": types.SimpleNamespace(create_moments_sender=factory)}):
            self.publish()
        factory.assert_called_once_with()
        self.bot._ask_llm.assert_not_called()
        context, plan = self.bot._compose_post.call_args.args
        self.assertEqual(json.loads(context)["occasion"], "first_online")
        self.assertEqual(plan, {"form": "image", "occasion": "first_online"})
        self.assertEqual(self.fx.tools.generate_image.call_args.args, ("月光下的窗台",))
        self.assertEqual(self.fx.sender.send_moment.call_args.args, ("我来啦。", self.image))
        self.fx.sender.send_moment.assert_called_once()
        self.assertFalse(self.bot.enabled)
        self.assertEqual(self.bot.first_online_status()["state"], "published")
        reopened = self.fx.module.Autopilot(moments_sender=self.fx.sender)
        self.assertEqual(reopened.first_online_status()["state"], "published")
        with self.assertRaises(ValueError):
            reopened.request_first_online()
        self.fx.config.BOT_WXID = "wxid_second"
        self.assertEqual(reopened.first_online_status()["state"], "ready")
        self.fx.config.BOT_WXID = "wxid_original"
        self.assertEqual(reopened.first_online_status()["state"], "published")
        self.fx.sender.send_moment.assert_called_once()

    def test_duplicate_click_while_generating_does_not_make_a_second_paid_call(self):
        entered, release = threading.Event(), threading.Event()
        def generate(*args, **kwargs):
            entered.set()
            release.wait(5)
            return self.image
        self.fx.tools.generate_image.side_effect = generate
        self.bot.request_first_online()
        try:
            self.assertTrue(entered.wait(5))
            snapshot = self.bot.first_online_status()
            self.assertEqual((snapshot["state"], snapshot["stage"]), ("running", "generating_image"))
            self.assertIn("生图", snapshot["message"])
            with self.assertRaises(ValueError):
                self.bot.request_first_online()
        finally:
            release.set()
            self.finish()
        self.fx.tools.generate_image.assert_called_once()
        self.fx.sender.send_moment.assert_called_once()

    def test_missing_brief_or_image_never_submits_text_only_and_allows_retry(self):
        self.bot._compose_post.return_value = {"content": "我来啦。", "image_path": self.image}
        self.publish()
        self.fx.tools.generate_image.assert_not_called()
        self.fx.sender.send_moment.assert_not_called()
        self.bot._compose_post.return_value["image_prompt"] = "月光"
        self.fx.tools.generate_image.return_value = None
        self.publish()
        self.fx.sender.send_moment.assert_not_called()
        self.assertIsNone(self.bot.pending_snapshot())
        self.assertEqual(self.bot.first_online_status()["state"], "ready")
        self.fx.tools.generate_image.return_value = self.image
        self.publish()
        self.fx.sender.send_moment.assert_called_once()
        self.assertEqual(self.bot.first_online_status()["state"], "published")

    def test_switching_accounts_during_generation_cancels_without_submitting(self):
        def change_account(*args, **kwargs):
            self.fx.config.BOT_WXID = "wxid_second"
            return self.image
        self.fx.tools.generate_image.side_effect = change_account
        self.publish()
        self.fx.sender.send_moment.assert_not_called()
        self.assertIsNone(self.bot.pending_snapshot())
        self.assertEqual(self.bot.first_online_status()["state"], "ready")

    def test_display_name_is_not_a_publishable_account(self):
        self.fx.config.BOT_WXID = "机器人昵称"
        self.assertEqual(self.bot.first_online_status()["state"], "unavailable")
        with self.assertRaises(ValueError):
            self.bot.request_first_online()
        self.bot._compose_post.assert_not_called()

    def test_image_service_failure_is_visible_and_safe_to_retry(self):
        class ImageGenerationError(ValueError):
            pass
        self.fx.tools.ImageGenerationError = ImageGenerationError
        self.fx.tools.generate_image.side_effect = ImageGenerationError("千问生图失败，HTTP 403")
        self.publish()
        self.assertTrue(self.fx.tools.generate_image.call_args.kwargs["raise_on_error"])
        result = self.bot.first_online_status()
        self.assertEqual(result["state"], "ready")
        self.assertIn("HTTP 403", result["message"])
        self.fx.sender.send_moment.assert_not_called()

    def test_manual_announcement_acknowledged_before_sender_initialization(self):
        self.bot.moments_sender = None
        entered, release, acknowledged = threading.Event(), threading.Event(), threading.Event()
        def factory():
            entered.set()
            release.wait(5)
            return self.fx.sender
        def click():
            self.bot.request_first_online()
            acknowledged.set()
        with patch.dict(sys.modules, {"senders": types.SimpleNamespace(create_moments_sender=factory)}):
            caller = threading.Thread(target=click)
            caller.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(acknowledged.wait(1))
                self.assertEqual(self.bot.first_online_status()["stage"], "queued")
            finally:
                release.set()
                caller.join(2)
                self.finish()
        self.fx.sender.send_moment.assert_called_once()

    def test_manual_work_runs_despite_quiet_hours_and_private_priority(self):
        self.fx.state.social_should_yield = lambda: True
        self.fx.config.MOMENTS_ACTIVE_START_HOUR = 8
        self.fx.config.MOMENTS_ACTIVE_END_HOUR = 23
        with patch.object(self.fx.module.time, "time", return_value=datetime(2026, 11, 3, 0, 2).timestamp()):
            self.publish()
        self.fx.sender.send_moment.assert_called_once()

    def test_model_error_or_pre_submit_uia_error_reaches_the_manual_card(self):
        del self.bot._compose_post
        class ModelError(RuntimeError):
            pass
        client = types.SimpleNamespace(request=Mock(side_effect=ModelError("主模型 HTTP 401")), ModelError=ModelError)
        with patch.dict(sys.modules, {"model_client": client}):
            self.publish()
        self.assertIn("主模型 HTTP 401", self.bot.first_online_status()["message"])
        self.fx.sender.send_moment.assert_not_called()
        self.bot._compose_post = Mock(return_value={"content":"公开文案","image_prompt":"画面"})
        self.fx.sender.send_moment.return_value = False
        self.fx.sender.last_send_result.return_value = {"submit_attempted": False, "error": "微信主窗口不可用"}
        self.publish()
        result = self.bot.first_online_status()
        self.assertEqual(result["state"], "ready")
        self.assertIn("微信主窗口不可用", result["message"])
        self.assertNotIn("公开文案", result["message"])
        self.assertIsNone(self.bot.pending_snapshot())

    def test_public_text_and_image_intent_are_both_authored_by_main(self):
        del self.bot._compose_post
        model = Mock(return_value=json.dumps({"content": "我来啦。", "image_prompt": "合成月色"}))
        with patch.dict(sys.modules, {"model_client": types.SimpleNamespace(request=model)}):
            self.publish()
        role, messages = model.call_args.args
        self.assertEqual(role, "main")
        self.assertIn("首次上线", messages[0]["content"])
        self.assertEqual(json.loads(messages[1]["content"])["publish_plan"]["occasion"], "first_online")
        self.assertEqual(self.fx.tools.generate_image.call_args.args, ("合成月色",))
        self.assertEqual(self.fx.sender.send_moment.call_args.args, ("我来啦。", self.image))

    def test_unconfirmed_result_requires_manual_review_then_persists_original_account(self):
        self.fx.sender.send_moment.return_value = False
        self.fx.sender.last_send_result.return_value = {"submit_attempted": True, "outcome_uncertain": True}
        self.publish()
        pending = self.bot.pending_snapshot()
        self.assertEqual(pending["first_online_account"], "wxid_original")
        with self.assertRaises(ValueError):
            self.bot.request_first_online()
        self.fx.config.BOT_WXID = "wxid_second"
        reopened = self.fx.module.Autopilot(moments_sender=self.fx.sender)
        self.assertTrue(reopened.resolve_pending("published", intent_id=pending["intent_id"]))
        self.assertEqual(reopened.first_online_status()["state"], "ready")
        self.fx.config.BOT_WXID = "wxid_original"
        self.assertEqual(reopened.first_online_status()["state"], "published")
        self.fx.sender.send_moment.assert_called_once()

    def test_review_as_not_published_only_allows_an_explicit_retry(self):
        self.fx.sender.send_moment.return_value = False
        self.fx.sender.last_send_result.return_value = {"submit_attempted": True, "outcome_uncertain": True}
        self.publish()
        intent = self.bot.pending_snapshot()["intent_id"]
        self.assertTrue(self.bot.resolve_pending("not_published", intent_id=intent))
        self.fx.sender.send_moment.assert_called_once()
        self.assertEqual(self.bot.first_online_status()["state"], "ready")
        self.fx.sender.send_moment.return_value = True
        self.fx.sender.last_send_result.return_value = {}
        self.publish()
        self.assertEqual(self.fx.sender.send_moment.call_count, 2)
        self.assertEqual(self.bot.first_online_status()["state"], "published")
