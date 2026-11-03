"""Fake clocks and existing isolated fixtures, never actual social actions."""
import unittest
from unittest.mock import Mock, patch

import test_autopilot as publish
import test_moments_interactions as interact
import test_lifecycle as lifecycle


class PublishScheduleTests(unittest.TestCase):
    def setUp(self):
        publish.AutopilotImageTests.setUp(self)
        self.now = 100000.0
        clock = patch.object(self.module.time, "time", side_effect=lambda: self.now)
        clock.start(); self.addCleanup(clock.stop)
        self.config.AUTOPILOT_INTERVAL_MIN = 150
        self.bot._schedule_at = self.now
        self.bot.next_run = 0.0
        self.bot._sync_schedule()
        self.bot._ask_llm.return_value = {"should_post": False}

    def test_first_publish_check_waits_full_user_interval(self):
        due = self.bot.next_run
        self.bot._scheduled_check()
        self.now = due - 1
        self.bot._scheduled_check()
        self.bot._ask_llm.assert_not_called()
        self.assertEqual(due, 109000.0)
        self.now = due
        self.bot._scheduled_check()
        self.bot._ask_llm.assert_called_once()
        self.sender.send_moment.assert_not_called()
        self.assertEqual(self.bot.next_run, self.now + 9000)

    def test_hot_enable_clears_disabled_result_and_starts_new_interval(self):
        self.bot.set_enabled(False)
        self.now += 300
        self.bot.set_enabled(True)
        self.assertEqual(self.bot.next_run, self.now + 9000)
        self.assertEqual(self.bot.last_result, "等待定时发布")
        self.assertTrue(self.bot._wake.is_set())

    def test_hot_interval_changes_without_postponing_for_unrelated_save(self):
        self.now += 60
        self.bot.set_enabled(True)
        self.assertEqual(self.bot.next_run, 109000.0)
        self.config.AUTOPILOT_INTERVAL_MIN = 60
        self.bot.set_enabled(True)
        self.assertEqual(self.bot.next_run, 103600.0)

    def test_private_priority_keeps_due_for_next_quiet_check(self):
        priority = [True]
        self.state.social_should_yield = lambda: priority[0]
        self.now = self.bot.next_run
        due = self.now
        self.bot._scheduled_check()
        self.bot._ask_llm.assert_not_called()
        self.assertEqual(self.bot.next_run, due)
        self.assertEqual(self.bot.status_snapshot()["blocked_reason"], "private_priority")
        self.now += 60
        priority[0] = False
        self.bot._scheduled_check()
        self.bot._ask_llm.assert_called_once()

    def test_schedule_notices_are_deduplicated_and_model_failure_is_visible(self):
        with self.assertLogs("ob11-bridge", level="INFO") as logged:
            self.bot._scheduled_check()
            self.bot._scheduled_check()
        self.assertEqual(sum("schedule enabled" in line for line in logged.output), 1)
        self.now = self.bot.next_run
        self.bot._ask_llm.return_value = None
        self.bot._scheduled_check()
        snapshot = self.bot.status_snapshot()
        self.assertEqual(snapshot["action_error"], "model_failed")
        self.assertEqual(snapshot["last_check"], self.now)

    def test_private_arrival_after_confirmed_post_does_not_repeat_completed_period(self):
        priority = [False]
        self.state.social_should_yield = lambda: priority[0]
        self.now = self.bot.next_run
        def complete():
            self.bot.last_result = "微信界面已接受：合成文案"
            priority[0] = True
        self.bot.think_and_post = complete
        self.bot._scheduled_check()
        self.assertEqual(self.bot.next_run, self.now + 9000)

    def test_pending_reconciliation_starts_a_full_normal_period_without_retry(self):
        intent = self.bot._begin_pending("合成文案", "")
        self.now = self.bot.next_run + 300
        self.assertTrue(self.bot.resolve_pending("not_published", intent_id=intent["intent_id"]))
        self.assertEqual(self.bot.next_run, self.now + 9000)
        self.bot._scheduled_check()
        self.bot._ask_llm.assert_not_called()
        self.sender.send_moment.assert_not_called()


class InteractionScheduleTests(unittest.TestCase):
    select_like = staticmethod(interact.MomentsInteractionsTests.select_like)

    def setUp(self):
        interact.MomentsInteractionsTests.setUp(self)
        self.now = interact.NOW
        self.module._now.side_effect = lambda: self.now
        self.config.MOMENTS_BROWSE_INTERVAL_MIN = 240
        self.bot._schedule_at = self.now
        self.bot._next_browse = self.now + 14400
        self.bot._browse_interval = 14400

    def test_initial_reply_poll_does_not_trigger_proactive_browse(self):
        self.feed.fetch.return_value = []
        self.bot._scheduled_check()
        self.sender.refresh_feed.assert_not_called()
        self.bot._ask_llm.assert_not_called()
        self.assertEqual(self.bot.next_run, self.now + 14400)
        self.assertEqual(self.bot.next_reply_poll, self.now + 5)

    def test_failed_or_outside_hours_round_does_not_consume_active_interval(self):
        self.now = self.bot._next_browse
        due = self.now
        original = self.bot.run_once
        for status in ("outside_hours", "unavailable", "feed_failed"):
            with self.subTest(status=status):
                self.bot._retry_at = 0
                self.bot.run_once = Mock(return_value={"ok": False, "status": status})
                self.bot._scheduled_check()
                self.assertEqual(self.bot.next_run, due)
                self.assertGreater(self.bot.next_reply_poll, self.now)
                self.assertLessEqual(self.bot.next_reply_poll - self.now, 60)
                calls = self.bot.run_once.call_count
                self.bot._scheduled_check()
                self.assertEqual(self.bot.run_once.call_count, calls)
        self.bot.run_once = original

    def test_recovered_due_round_advances_only_after_success(self):
        self.now = self.bot._next_browse
        self.bot.run_once = Mock(side_effect=[{"ok": False, "status": "feed_failed"}, {"ok": True, "status": "skipped"}])
        self.bot._scheduled_check()
        self.now = self.bot.next_reply_poll
        self.bot._scheduled_check()
        self.assertEqual(self.bot.next_run, self.now + 14400)

    def test_hot_enable_and_interval_change_are_immediate_but_not_immediate_actions(self):
        self.bot.set_enabled(False)
        self.now += 60
        self.bot.set_enabled(True)
        self.assertEqual(self.bot.next_run, self.now + 14400)
        self.config.MOMENTS_BROWSE_INTERVAL_MIN = 60
        self.bot.set_enabled(True)
        self.assertEqual(self.bot.next_run, self.now + 3600)
        self.sender.execute_interaction.assert_not_called()

    def test_missing_account_identity_is_visible_as_exact_manual_block(self):
        self.config.BOT_WXID = ""
        result = self.bot.run_once()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["blocked_reason"], "account_identity_missing")
        snapshot = self.bot.status_snapshot()
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["blocked_reason"], "account_identity_missing")
        self.assertEqual(snapshot["action_error"], "unavailable")
        self.sender.refresh_feed.assert_not_called()
        self.feed.fetch.assert_not_called()


class ManualBrowseCompletionTests(unittest.TestCase):
    def setUp(self):
        lifecycle.LifecycleTests.setUp(self)

    def test_background_result_and_fixed_log_survive_without_exception(self):
        controller = lifecycle.LifecycleTests.enable_interactions(self)
        controller.run_once = Mock(return_value={"ok": False, "status": "unavailable",
            "blocked_reason": "account_identity_missing", "error": "未运行：请配置当前账号的真实微信 ID"})
        with self.assertLogs("ob11-bridge", level="INFO") as logged:
            self.main.run_moments_interactions()
            worker = lifecycle.LifecycleTests.manual_worker(self)
            worker.target(*worker.args)
        snapshot = self.main.get_status()["moments_manual_request"]
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertEqual(snapshot["blocked_reason"], "account_identity_missing")
        self.assertGreater(snapshot["finished_at"], 0)
        self.assertTrue(any("manual finished status=unavailable" in line for line in logged.output))
        self.assertIs(controller.run_once.call_args.kwargs["manual"], True)

    def test_old_settings_bound_request_has_visible_cancelled_terminal_status(self):
        controller = lifecycle.LifecycleTests.enable_interactions(self)
        self.main.run_moments_interactions()
        worker = lifecycle.LifecycleTests.manual_worker(self)
        controller.set_enabled(True)
        worker.target(*worker.args)
        self.assertEqual(self.main.get_status()["moments_manual_request"]["status"], "cancelled")
        self.assertIsNone(self.main._moments_manual_request)


if __name__ == "__main__":
    unittest.main()
