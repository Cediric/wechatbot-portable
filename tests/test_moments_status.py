"""Retained execution errors with synthetic feed/model/UIA fixtures only."""
import unittest
from unittest.mock import patch

import test_moments_interactions as fixtures

NOW, comment, post = fixtures.NOW, fixtures.comment, fixtures.post


class MomentsStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.MomentsInteractionsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bot,self.sender,self.feed=self.fixture.bot,self.fixture.sender,self.fixture.feed

    def refresh_failure(self):
        self.sender.refresh_feed.return_value={"ok":False,"error":"synthetic refresh blocked"}
        self.assertEqual(self.bot.run_once()["status"],"refresh_failed")
        error=self.bot.status_snapshot()["last_action_error"]
        self.sender.refresh_feed.return_value={"ok":True}
        return error

    def test_automatic_refresh_failure_survives_twenty_post_empty_reply_poll(self):
        error=self.refresh_failure()
        self.feed.fetch.return_value=[post(post_id=str(10001+index)) for index in range(20)]
        result=self.bot.run_once(replies_only=True)
        self.assertEqual(result["status"],"skipped")
        self.assertEqual(result["scanned"],20)
        snapshot=self.bot.status_snapshot()
        self.assertIn("已检查 20",snapshot["last_result"])
        self.assertEqual(snapshot["last_action_error"],error)
        self.assertEqual(error["mode"],"browse")
        self.assertEqual(error["time"],NOW)
        self.bot._ask_llm.assert_not_called()
        self.sender.execute_interaction.assert_not_called()

    def test_same_mode_empty_browse_cannot_clear_a_refresh_failure(self):
        error=self.refresh_failure()
        self.feed.fetch.return_value=[]
        self.assertEqual(self.bot.run_once()["status"],"skipped")
        self.assertEqual(self.bot.status_snapshot()["last_action_error"],error)

    def test_completed_same_mode_read_and_decision_recovers_a_read_failure(self):
        self.refresh_failure()
        self.bot._ask_llm.side_effect=lambda context:{"candidate_id":"","text":""}
        result=self.bot.run_once()
        self.assertEqual(result["status"],"skipped")
        self.assertEqual(result["processed"],1)
        self.assertEqual(self.bot.status_snapshot()["last_action_error"],{})
        self.sender.execute_interaction.assert_not_called()

    def test_ui_identity_failure_survives_model_skip_until_verified_acceptance(self):
        self.sender.execute_interaction.return_value={"status":"failed","submit_attempted":False,
            "outcome_uncertain":False,"error":"synthetic account verification failed"}
        self.assertEqual(self.bot.run_once()["status"],"failed")
        error=self.bot.status_snapshot()["last_action_error"]
        self.bot._ask_llm.side_effect=lambda context:{"candidate_id":"","text":""}
        result=self.bot.run_once()
        self.assertEqual(result["status"],"skipped")
        self.assertEqual(result["processed"],1)
        self.assertEqual(self.bot.status_snapshot()["last_action_error"],error)
        self.fixture.select("comment")
        self.sender.execute_interaction.return_value={"status":"accepted_by_ui","submit_attempted":True,"outcome_uncertain":False}
        self.assertEqual(self.bot.run_once()["status"],"accepted_by_ui")
        self.assertEqual(self.bot.status_snapshot()["last_action_error"],{})

    def test_other_mode_progress_cannot_clear_prior_model_failure(self):
        self.bot._ask_llm.side_effect=lambda context:None
        self.assertEqual(self.bot.run_once()["status"],"model_failed")
        error=self.bot.status_snapshot()["last_action_error"]
        self.feed.fetch.return_value=[post(own=True,post_id="10002",comments=[comment()])]
        self.bot._ask_llm.side_effect=lambda context:{"candidate_id":"","text":""}
        result=self.bot.run_once(replies_only=True)
        self.assertEqual(result["processed"],1)
        self.assertEqual(self.bot.status_snapshot()["last_action_error"],error)
        self.assertEqual(self.bot.run_once()["processed"],1)
        self.assertEqual(self.bot.status_snapshot()["last_action_error"],{})

    def test_pause_cancel_and_outside_hours_preserve_prior_error(self):
        error=self.refresh_failure()
        for status in ("disabled","cancelled","busy","private_priority","outside_hours"):
            with self.subTest(status=status),patch.object(self.bot,"_run_locked",return_value={
                    "ok":status in {"private_priority","outside_hours"},"status":status}):
                self.bot.run_once()
                self.assertEqual(self.bot.status_snapshot()["last_action_error"],error)

    def test_status_error_is_a_copy_and_hides_signed_url(self):
        failure={"ok":False,"status":"feed_failed","error":"synthetic read https://example.invalid/a?token=synthetic-secret"}
        with patch.object(self.bot,"_run_locked",return_value=failure):
            self.bot.run_once()
        snapshot=self.bot.status_snapshot()
        self.assertNotIn("synthetic-secret",snapshot["last_action_error"]["message"])
        snapshot["last_action_error"]["message"]="client mutation"
        self.assertNotEqual(self.bot.status_snapshot()["last_action_error"]["message"],"client mutation")

    def test_only_successful_exact_pending_resolution_clears_uncertain_error(self):
        self.sender.execute_interaction.return_value={"status":"failed","submit_attempted":True,"outcome_uncertain":True}
        self.assertEqual(self.bot.run_once()["status"],"pending_review")
        error=self.bot.status_snapshot()["last_action_error"]
        pending=self.bot.pending_snapshot()
        self.assertFalse(self.bot.resolve_pending("not_published",intent_id="f"*32))
        self.assertEqual(self.bot.status_snapshot()["last_action_error"],error)
        self.assertTrue(self.bot.resolve_pending("not_published",intent_id=pending["intent_id"]))
        snapshot=self.bot.status_snapshot()
        self.assertEqual(snapshot["last_action_error"],{})
        self.assertEqual(snapshot["recent"][0]["status"],"not_published")

    def test_failure_is_published_before_lock_release_hands_off_to_success(self):
        original_lock=self.bot._decision_lock
        handed_off=[]
        testcase=self
        class HandoffLock:
            def acquire(self,blocking=False):
                return original_lock.acquire(blocking=blocking)
            def release(self):
                original_lock.release()
                if not handed_off:
                    handed_off.append(True)
                    snapshot=testcase.bot.status_snapshot()
                    testcase.assertFalse(snapshot["running"])
                    testcase.assertEqual(snapshot["last_action_error"]["status"],"failed")
                    testcase.assertEqual(note_check.call_count,1)
                    testcase.assertEqual(testcase.bot.run_once()["status"],"accepted_by_ui")
        with patch.object(self.bot,"_decision_lock",HandoffLock()),patch.object(self.bot,"_run_locked",side_effect=[
                {"ok":False,"status":"failed","error":"synthetic prior failure"},
                {"ok":True,"status":"accepted_by_ui"}]),patch.object(self.bot,"_note_check") as note_check:
            self.assertEqual(self.bot.run_once()["status"],"failed")
            self.assertEqual(note_check.call_count,2)
        self.assertEqual(self.bot.status_snapshot()["last_action_error"],{})
        self.assertFalse(self.bot.status_snapshot()["running"])
        self.assertTrue(original_lock.acquire(blocking=False))
        original_lock.release()


if __name__=="__main__":unittest.main()
