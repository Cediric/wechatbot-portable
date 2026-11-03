"""Synthetic images and mocked model/sender; no paid API, desktop or HTTP."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AutopilotImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".autopilot-test-", dir=SOURCE.parent / "tests")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = types.SimpleNamespace(AUTOPILOT_ENABLED=True, MEDIA_MAX_MB=30,
            AUTOPILOT_LLM_API_KEY="synthetic-key", AUTOPILOT_LLM_API_BASE="https://example.invalid/v1",
            AUTOPILOT_LLM_MODEL="synthetic-model", BOT_PERSONA="", AUTOPILOT_PROMPT="Return JSON",
            MOMENTS_HISTORY_PATH=str(self.root / "moments_history.json"), DATA_DIR=str(self.root / "data"))
        self.state = types.SimpleNamespace(running=True, paused=threading.Event(), bridge_generation=7)
        self.http = types.SimpleNamespace(session=Mock(side_effect=AssertionError("No HTTP allowed")))
        self.tools = types.SimpleNamespace(generate_image=Mock())
        modules = patch.dict(sys.modules, {"config": self.config, "state": self.state,
            "http_util": self.http, "tools": self.tools})
        modules.start(); self.addCleanup(modules.stop)
        self.media = load("autopilot_test_media", "media_util.py")
        media_patch = patch.dict(sys.modules, {"media_util": self.media})
        media_patch.start(); self.addCleanup(media_patch.stop)
        self.module = load("autopilot_test_module", "autopilot.py")
        self.sender = Mock()
        self.sender.send_moment.return_value = True
        self.sender.last_send_result.return_value = {"submit_attempted": False, "outcome_uncertain": False}
        self.bot = self.module.Autopilot(moments_sender=self.sender)
        self.bot._build_context = Mock(return_value=json.dumps({"task": "moments_publish",
            "related_actor_ids": ["wxid_friend"], "related_chats": [{"session_id": "wxid_friend",
                "actor_id": "wxid_friend", "content": "合成聊天记录", "provenance": "same_private_conversation"}],
            "related_interactions": [{"post_id": "10001", "target_actor_id": "wxid_friend", "text": "合成互动"}],
            "persona_state": {"humanlike_enabled": True}}))
        self.bot._ask_llm = Mock()
        self.bot._compose_post = Mock(return_value={"content": "today"})
        self.bot._save_history = Mock(return_value=True)

    def image(self, name="image.png", *, truncated=False):
        from PIL import Image
        path = self.root / name
        target = io.BytesIO()
        Image.new("RGB", (64, 64), "white").save(target, format="JPEG" if name.endswith(".jpg") else "PNG")
        path.write_bytes(target.getvalue()[:-8] if truncated else target.getvalue())
        return str(path)

    def decide(self, **fields):
        self.bot._ask_llm.return_value = dict(should_post=True, topic="近况", form="image" if fields.get("image_prompt") or fields.get("image_path") else "text")
        self.bot._compose_post.return_value = dict(content="today", **fields)
        self.bot.think_and_post()

    def test_text_only_never_calls_image_api(self):
        self.decide(image_prompt=None, image_path=None)
        self.tools.generate_image.assert_not_called()
        self.sender.send_moment.assert_called_once()
        self.assertEqual(self.sender.send_moment.call_args.args, ("today", ""))
        self.assertIn("微信界面已接受", self.bot.last_result)

    def test_main_published_caption_passes_unchanged_and_image_prompt_stays_private(self):
        caption = "API & C++？🎉 " * 30 + "收工"
        self.bot._ask_llm.return_value = {"should_post":True, "form":"image"}
        self.bot._compose_post.return_value = {"content":caption,"image_prompt":"Cinematic 4K & soft light"}
        self.tools.generate_image.return_value = self.image()
        self.bot.think_and_post()
        self.assertEqual(self.tools.generate_image.call_args.args, ("Cinematic 4K & soft light",))
        self.assertEqual(self.sender.send_moment.call_args.args, (caption, self.tools.generate_image.return_value))

    def test_invalid_publish_context_stops_before_paid_image_api(self):
        self.bot._build_context.return_value = "invalid context"
        self.decide(image_prompt="garden")
        self.tools.generate_image.assert_not_called()
        self.sender.send_moment.assert_not_called()
        self.assertIn("配图生成失败", self.bot.last_result)

    def test_empty_post_stops_before_journal(self):
        self.bot._submit_pending("  ", "", lambda: False, 7)
        self.assertIsNone(self.bot.pending_snapshot())
        self.sender.send_moment.assert_not_called()

    def test_generated_image_validates_and_reaches_sender_with_generation(self):
        path = self.image()
        self.tools.generate_image.return_value = path
        self.decide(image_prompt="paint a quiet garden")
        self.tools.generate_image.assert_called_once()
        image_call = self.tools.generate_image.call_args
        self.assertEqual(image_call.args, ("paint a quiet garden",))
        image_context = image_call.kwargs["context"]
        self.assertEqual(image_context["related_actor_ids"], ["wxid_friend"])
        self.assertEqual(image_context["related_chats"][0]["actor_id"], "wxid_friend")
        self.assertEqual(image_context["related_interactions"][0]["post_id"], "10001")
        self.assertEqual(image_context["publish_decision"]["form"], "image")
        self.assertEqual(image_context["main_creation"]["image_prompt"], "paint a quiet garden")
        self.assertEqual(image_context["publish_decision_source"], "autopilot_publish_decision")
        self.assertFalse(image_context["humanlike_plan"]["shared_humanlike_decision_available"])
        self.assertFalse(image_call.kwargs["cancelled"]())
        call = self.sender.send_moment.call_args
        self.assertEqual(call.args, ("today", path))
        self.assertEqual(call.kwargs["generation"], 7)
        self.assertFalse(call.kwargs["cancel_event"].is_set())
        self.state.bridge_generation = 8
        self.assertTrue(call.kwargs["cancel_event"].is_set())
        saved = self.bot._save_history.call_args
        self.assertEqual(saved.args, ("today", path, True))
        self.assertEqual(saved.kwargs["history_path"], self.config.MOMENTS_HISTORY_PATH)
        self.assertTrue(saved.kwargs["intent_id"])

    def test_existing_image_keeps_compatibility_without_paid_generation(self):
        path = self.image()
        self.decide(image_path=path, image_prompt="optional prompt")
        self.tools.generate_image.assert_not_called()
        self.assertEqual(self.sender.send_moment.call_args.args, ("today", path))
        self.assertTrue(Path(path).exists())

    def test_missing_existing_image_never_falls_back_to_text_or_api(self):
        self.decide(image_path=str(self.root / "missing.png"), image_prompt="do not silently replace")
        self.sender.send_moment.assert_not_called()
        self.tools.generate_image.assert_not_called()
        self.assertIn("配图", self.bot.last_result)
        self.bot._save_history.assert_not_called()

    def test_failed_generation_never_posts_text(self):
        for result in (None, ""):
            with self.subTest(result=result):
                self.tools.generate_image.return_value = result
                self.decide(image_prompt="garden")
                self.sender.send_moment.assert_not_called()
                self.assertIn("配图生成失败", self.bot.last_result)

    def test_generation_exception_never_posts_text(self):
        self.tools.generate_image.side_effect = RuntimeError("synthetic API failure")
        self.decide(image_prompt="garden")
        self.sender.send_moment.assert_not_called()
        self.assertIn("配图生成失败", self.bot.last_result)

    def test_invalid_decision_fields_fail_before_paid_api(self):
        for fields in ({"image_prompt": True}, {"image_path": []}, {"image_path": "bad\x00path"}):
            with self.subTest(fields=fields):
                self.decide(**fields)
                self.sender.send_moment.assert_not_called()
                self.tools.generate_image.assert_not_called()

    def test_generated_truncated_image_never_reaches_desktop(self):
        self.tools.generate_image.return_value = self.image("broken.jpg", truncated=True)
        self.decide(image_prompt="garden")
        self.sender.send_moment.assert_not_called()
        self.assertIn("损坏", self.bot.last_result)
        self.assertFalse(self.media._LEASED_FILES)

    def test_explicit_false_decision_never_pays_or_sends(self):
        for value in (False, "false", 1):
            with self.subTest(value=value):
                self.bot._ask_llm.return_value = {"should_post": value, "content": "today", "image_prompt": "garden"}
                self.bot.think_and_post()
                self.tools.generate_image.assert_not_called()
                self.sender.send_moment.assert_not_called()

    def test_generation_change_during_api_cancels_after_return(self):
        path = self.image()
        def generate(_, **kwargs):
            self.state.bridge_generation += 1
            self.assertTrue(kwargs["cancelled"]())
            return path
        self.tools.generate_image.side_effect = generate
        self.decide(image_prompt="garden")
        self.sender.send_moment.assert_not_called()
        self.assertIn("取消", self.bot.last_result)
        self.assertTrue(Path(path).exists())

    def test_pause_during_image_validation_cancels_before_sender(self):
        path = self.image()
        validate = self.media.validate_image_file
        def pause(p):
            validate(p)
            self.state.paused.set()
        with patch.object(self.media, "validate_image_file", side_effect=pause):
            self.decide(image_path=path)
        self.sender.send_moment.assert_not_called()
        self.assertIn("取消", self.bot.last_result)
        self.assertFalse(self.media._LEASED_FILES)

    def test_disable_then_reenable_during_blocked_api_keeps_old_cancel_token(self):
        path = self.image()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def generate(_, **kwargs):
            started.set()
            if not release.wait(2):
                raise AssertionError("mock API was not released")
            return path
        self.tools.generate_image.side_effect = generate
        self.bot._ask_llm.return_value = {"should_post": True, "content": "today", "image_prompt": "garden"}
        self.bot._ask_llm.return_value["form"] = "image"
        self.bot._compose_post.return_value = {"content": "today", "image_prompt": "garden"}
        thread = threading.Thread(target=self.bot.think_and_post, daemon=True)
        thread.start()
        self.assertTrue(started.wait(1))
        self.bot.set_enabled(False); self.bot.set_enabled(True)
        release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.sender.send_moment.assert_not_called()
        self.assertIn("取消", self.bot.last_result)

    def test_cache_cleanup_preserves_image_for_waiting_sender_then_releases(self):
        path = self.image()
        age = time.time() - 8 * 86400
        os.utime(path, (age, age))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def send(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("mock desktop queue was not released")
            return not kwargs["cancel_event"].is_set()
        self.sender.send_moment.side_effect = send
        self.bot._ask_llm.return_value = {"should_post": True, "content": "today", "image_path": path}
        self.bot._ask_llm.return_value["form"] = "image"
        self.bot._compose_post.return_value = {"content": "today", "image_path": path}
        thread = threading.Thread(target=self.bot.think_and_post, daemon=True)
        thread.start(); self.assertTrue(entered.wait(1))
        self.media.prune_cache(self.root, 7)
        self.assertTrue(Path(path).exists())
        self.state.paused.set(); release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.media._LEASED_FILES)
        self.assertIn("取消", self.bot.last_result)
        self.media.prune_cache(self.root, 7)
        self.assertFalse(Path(path).exists())

    def test_sender_exception_always_releases_image_lease(self):
        path = self.image()
        self.sender.send_moment.side_effect = RuntimeError("mock desktop failure")
        self.decide(image_path=path)
        self.assertFalse(self.media._LEASED_FILES)
        self.assertTrue(Path(path).exists())
        self.assertIn("待核对", self.bot.last_result)
        self.assertIsNotNone(self.bot.pending_snapshot())

    def test_uncertain_submit_blocks_all_later_model_image_and_sender_calls(self):
        self.sender.send_moment.return_value = False
        self.sender.last_send_result.return_value = {"submit_attempted": True, "outcome_uncertain": True}
        self.decide()
        pending = self.bot.pending_snapshot()
        self.assertEqual(pending["content"], "today")
        self.assertIn("待核对", self.bot.last_result)
        self.bot._save_history.assert_not_called()
        self.bot._ask_llm.reset_mock(); self.sender.send_moment.reset_mock()
        self.bot.set_enabled(False); self.bot.set_enabled(True)
        self.bot._ask_llm.return_value = {"should_post": True, "content": "different", "image_prompt": "garden"}
        self.bot.think_and_post()
        self.bot._ask_llm.assert_not_called()
        self.tools.generate_image.assert_not_called()
        self.sender.send_moment.assert_not_called()
        self.assertEqual(self.bot.pending_snapshot()["intent_id"], pending["intent_id"])

    def test_pending_submit_survives_new_autopilot_instance(self):
        self.sender.send_moment.return_value = False
        self.sender.last_send_result.return_value = {"submit_attempted": True, "outcome_uncertain": True}
        self.decide()
        restarted = self.module.Autopilot(moments_sender=self.sender)
        restarted._ask_llm = Mock(side_effect=AssertionError("Restart must not make a paid decision"))
        self.sender.send_moment.reset_mock()
        restarted.think_and_post()
        self.assertIn("待核对", restarted.last_result)
        self.sender.send_moment.assert_not_called()

    def test_journal_is_durable_before_sender_and_preserved_on_abrupt_exit(self):
        path = self.image()
        def crash(*args, **kwargs):
            pending = json.loads(Path(self.bot._pending_path()).read_text(encoding="utf-8"))
            self.assertEqual(pending["content"], "today")
            self.assertEqual(pending["image_path"], path)
            raise KeyboardInterrupt("synthetic abrupt exit")
        self.sender.send_moment.side_effect = crash
        with self.assertRaises(KeyboardInterrupt):
            self.decide(image_path=path)
        self.assertIsNotNone(self.bot.pending_snapshot())
        self.assertFalse(self.media._LEASED_FILES)

    def test_definite_pre_submit_failure_clears_journal_and_can_retry(self):
        self.sender.send_moment.return_value = False
        self.decide()
        self.assertIsNone(self.bot.pending_snapshot())
        self.assertIn("发表前失败", self.bot.last_result)
        self.decide()
        self.assertEqual(self.sender.send_moment.call_count, 2)

    def test_failed_legacy_result_or_post_click_timeout_is_conservatively_pending(self):
        for result in ({}, {"submit_attempted": True, "outcome_uncertain": False}):
            with self.subTest(result=result):
                self.sender.send_moment.return_value = False
                self.sender.last_send_result.return_value = result
                self.decide()
                pending = self.bot.pending_snapshot()
                self.assertIsNotNone(pending)
                self.assertIn("待核对", self.bot.last_result)
                self.assertTrue(self.bot.resolve_pending("not_published", intent_id=pending["intent_id"]))

    def test_cancel_after_possible_submit_retains_pending_instead_of_safe_failure(self):
        def send(*args, **kwargs):
            self.state.paused.set()
            return False
        self.sender.send_moment.side_effect = send
        self.sender.last_send_result.return_value = {"submit_attempted": True, "outcome_uncertain": True}
        self.decide()
        self.assertIn("待核对", self.bot.last_result)
        self.assertIsNotNone(self.bot.pending_snapshot())

    def test_explicit_resolution_rejects_stale_or_invalid_requests_without_sending(self):
        pending = self.bot._begin_pending("previous", "")
        for outcome, identifier in (("published", "stale"), ("retry", pending["intent_id"])):
            self.assertFalse(self.bot.resolve_pending(outcome, intent_id=identifier))
        self.assertIsNotNone(self.bot.pending_snapshot())
        self.bot._save_history.assert_not_called()
        self.sender.send_moment.assert_not_called()

    def test_explicit_resolution_writes_history_then_allows_next_decision(self):
        pending = self.bot._begin_pending("previous", "")
        self.assertTrue(self.bot.resolve_pending("published", intent_id=pending["intent_id"]))
        self.bot._save_history.assert_called_once_with("previous", "", True,
            history_path=pending["history_path"], intent_id=pending["intent_id"])
        self.assertIsNone(self.bot.pending_snapshot())
        self.sender.send_moment.assert_not_called()
        self.decide()
        self.sender.send_moment.assert_called_once()

    def test_history_failure_after_acceptance_preserves_pending_and_restart_block(self):
        self.bot._save_history.return_value = False
        self.decide()
        self.assertIn("待核对", self.bot.last_result)
        pending = self.bot.pending_snapshot()
        self.assertIsNotNone(pending)
        self.assertFalse(self.bot.resolve_pending("published", intent_id=pending["intent_id"]))
        self.assertIsNotNone(self.bot.pending_snapshot())

    def test_corrupt_pending_record_blocks_without_model_or_sender(self):
        Path(self.bot._pending_path()).parent.mkdir(parents=True, exist_ok=True)
        Path(self.bot._pending_path()).write_text("{broken", encoding="utf-8")
        restarted = self.module.Autopilot(moments_sender=self.sender)
        restarted._ask_llm = Mock()
        restarted.think_and_post()
        self.assertEqual(restarted.pending_snapshot()["status"], "record_error")
        self.assertIn("待核对", restarted.last_result)
        restarted._ask_llm.assert_not_called()
        self.sender.send_moment.assert_not_called()

    def test_unwritable_journal_never_calls_sender(self):
        with patch("builtins.open", side_effect=PermissionError("synthetic denied")):
            self.decide()
        self.assertIn("待核对", self.bot.last_result)
        self.sender.send_moment.assert_not_called()
        self.tools.generate_image.assert_not_called()

    def test_existing_pending_is_never_overwritten_by_second_instance(self):
        pending = self.bot._begin_pending("previous", "")
        other = self.module.Autopilot(moments_sender=self.sender)
        self.assertIsNone(other._begin_pending("another", ""))
        self.assertEqual(other.pending_snapshot()["intent_id"], pending["intent_id"])

    def test_history_is_atomically_persisted_before_pending_is_removed(self):
        self.bot._save_history = types.MethodType(self.module.Autopilot._save_history, self.bot)
        self.decide()
        history = json.loads(Path(self.config.MOMENTS_HISTORY_PATH).read_text(encoding="utf-8"))
        self.assertEqual(history[-1]["content"], "today")
        self.assertTrue(history[-1]["ok"])
        self.assertIsNone(self.bot.pending_snapshot())

    def test_existing_corrupt_history_is_preserved_and_keeps_accepted_intent_pending(self):
        history_path = Path(self.config.MOMENTS_HISTORY_PATH)
        history_path.write_text("{private broken history", encoding="utf-8")
        self.bot._save_history = types.MethodType(self.module.Autopilot._save_history, self.bot)
        self.decide()
        self.assertEqual(history_path.read_text(encoding="utf-8"), "{private broken history")
        self.assertIsNotNone(self.bot.pending_snapshot())
        self.assertIn("待核对", self.bot.last_result)

    def test_pending_unlink_failure_keeps_block_across_restart(self):
        with patch.object(self.module.os, "unlink", side_effect=PermissionError("synthetic remove denied")):
            self.decide()
        restarted = self.module.Autopilot(moments_sender=self.sender)
        restarted._ask_llm = Mock()
        self.sender.send_moment.reset_mock()
        restarted.think_and_post()
        self.assertIn("待核对", restarted.last_result)
        restarted._ask_llm.assert_not_called()
        self.sender.send_moment.assert_not_called()

    def test_pending_fsync_failure_stops_before_sender(self):
        with patch.object(self.module.os, "fsync", side_effect=OSError("synthetic disk failure")):
            self.decide()
        self.assertIn("待核对", self.bot.last_result)
        self.sender.send_moment.assert_not_called()

    def test_resolution_while_sender_transaction_is_active_is_refused(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def send(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("mock sender not released")
            return True
        self.sender.send_moment.side_effect = send
        self.bot._ask_llm.return_value = {"should_post": True, "content": "today"}
        thread = threading.Thread(target=self.bot.think_and_post, daemon=True)
        thread.start(); self.assertTrue(entered.wait(1))
        pending = self.bot.pending_snapshot()
        self.assertFalse(self.bot.resolve_pending("published", intent_id=pending["intent_id"]))
        self.assertIsNotNone(self.bot.pending_snapshot())
        release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.bot.pending_snapshot())

    def test_history_path_change_cannot_evade_pending_even_after_restart(self):
        pending = self.bot._begin_pending("previous", "")
        original_journal = self.bot._pending_path()
        self.config.MOMENTS_HISTORY_PATH = str(self.root / "other-history.json")
        restarted = self.module.Autopilot(moments_sender=self.sender)
        restarted._ask_llm = Mock()
        self.assertEqual(restarted._pending_path(), original_journal)
        self.assertEqual(restarted.pending_snapshot()["intent_id"], pending["intent_id"])
        restarted.think_and_post()
        restarted._ask_llm.assert_not_called()
        self.sender.send_moment.assert_not_called()

    def test_history_path_change_during_sender_writes_original_history_and_clears_fixed_journal(self):
        original_history = self.config.MOMENTS_HISTORY_PATH
        replacement_history = str(self.root / "changed-history.json")
        self.bot._save_history = types.MethodType(self.module.Autopilot._save_history, self.bot)
        def send(*args, **kwargs):
            self.config.MOMENTS_HISTORY_PATH = replacement_history
            return True
        self.sender.send_moment.side_effect = send
        self.decide()
        self.assertTrue(Path(original_history).is_file())
        self.assertFalse(Path(replacement_history).exists())
        self.assertIsNone(self.bot.pending_snapshot())

    def test_reconciliation_after_config_change_updates_original_history_without_duplicate(self):
        pending = self.bot._begin_pending("previous", "")
        self.bot._save_history = types.MethodType(self.module.Autopilot._save_history, self.bot)
        self.assertTrue(self.bot._save_history("previous", "", True,
            history_path=pending["history_path"], intent_id=pending["intent_id"]))
        original_history = Path(pending["history_path"])
        self.config.MOMENTS_HISTORY_PATH = str(self.root / "new-history.json")
        restarted = self.module.Autopilot(moments_sender=self.sender)
        self.assertTrue(restarted.resolve_pending("published", intent_id=pending["intent_id"]))
        history = json.loads(original_history.read_text(encoding="utf-8"))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["intent_id"], pending["intent_id"])
        self.assertFalse(Path(self.config.MOMENTS_HISTORY_PATH).exists())
        self.assertIsNone(restarted.pending_snapshot())

    def test_other_instance_cannot_resolve_inflight_sender(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        other = self.module.Autopilot(moments_sender=self.sender)
        def send(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("mock sender not released")
            return True
        self.sender.send_moment.side_effect = send
        self.bot._ask_llm.return_value = {"should_post": True, "content": "today"}
        thread = threading.Thread(target=self.bot.think_and_post, daemon=True)
        thread.start(); self.assertTrue(entered.wait(1))
        pending = other.pending_snapshot()
        self.assertFalse(other.resolve_pending("published", intent_id=pending["intent_id"]))
        self.assertEqual(other.pending_snapshot()["intent_id"], pending["intent_id"])
        release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_cross_instance_check_and_unlink_cannot_remove_a_replacement_intent(self):
        pending = self.bot._begin_pending("previous", "")
        other = self.module.Autopilot(moments_sender=self.sender)
        original_snapshot = self.bot.pending_snapshot
        checked, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def hold_snapshot():
            value = original_snapshot()
            checked.set()
            if not release.wait(2):
                raise AssertionError("mock clear not released")
            return value
        self.bot.pending_snapshot = hold_snapshot
        results = []
        thread = threading.Thread(target=lambda: results.append(self.bot._clear_pending(pending["intent_id"])), daemon=True)
        thread.start(); self.assertTrue(checked.wait(1))
        self.assertFalse(other._clear_pending(pending["intent_id"]))
        self.assertIsNone(other._begin_pending("replacement", ""))
        release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [True])
        replacement = other._begin_pending("replacement", "")
        self.assertIsNotNone(replacement)
        self.assertFalse(self.bot._clear_pending(pending["intent_id"]))
        self.assertEqual(other.pending_snapshot()["intent_id"], replacement["intent_id"])

    def test_os_file_lock_blocks_another_process_then_releases(self):
        script = '\n'.join([
            'import importlib.util, json, sys, types',
            'cfg = types.SimpleNamespace(AUTOPILOT_ENABLED=True, DATA_DIR=' + repr(self.config.DATA_DIR) +
                ', MOMENTS_HISTORY_PATH=' + repr(self.config.MOMENTS_HISTORY_PATH) + ')',
            'sys.modules["config"] = cfg',
            'for name in ("http_util", "media_util", "state", "tools"): sys.modules[name] = types.ModuleType(name)',
            'spec = importlib.util.spec_from_file_location("child_autopilot", ' + repr(str(SOURCE / 'autopilot.py')) + ')',
            'module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)',
            'bot = module.Autopilot()',
            'print(json.dumps(bot._begin_pending("child", "")))',
        ])
        with self.module._journal_operation(self.bot._pending_path()):
            blocked = subprocess.run([sys.executable, '-X', 'utf8', '-c', script],
                capture_output=True, text=True, encoding='utf-8', timeout=5, check=True)
        self.assertIsNone(json.loads(blocked.stdout))
        self.assertIsNone(self.bot.pending_snapshot())
        allowed = subprocess.run([sys.executable, '-X', 'utf8', '-c', script],
            capture_output=True, text=True, encoding='utf-8', timeout=5, check=True)
        created = json.loads(allowed.stdout)
        self.assertEqual(created['content'], 'child')
        self.assertEqual(self.bot.pending_snapshot()['intent_id'], created['intent_id'])

    def test_decision_contract_selects_posting_and_form_without_writing(self):
        model = types.SimpleNamespace(request=Mock(return_value=json.dumps({"should_post": False, "image_prompt": None})))
        with patch.dict(sys.modules, {"model_client": model}):
            self.assertFalse(type(self.bot)._ask_llm(self.bot, "context")["should_post"])
        system = model.request.call_args.args[1][0]["content"]
        self.assertEqual(model.request.call_args.args[0], "autopilot")
        self.assertIn("不撰写文案或生图提示词", system)
        self.assertIn('"form"', system)
        self.assertIn("不要返回 topic、content", system)

    def test_main_creation_owns_content_and_image_prompt_and_excludes_decision_drafts(self):
        path = self.image()
        self.tools.generate_image.return_value = path
        self.bot._ask_llm.return_value = {"should_post": True, "form": "image",
            "topic": "DECISION_TOPIC_CANARY", "content": "DECISION_TEXT_CANARY",
            "image_prompt": "DECISION_IMAGE_CANARY", "image_path": "DECISION_PATH_CANARY"}
        self.bot._compose_post = types.MethodType(self.module.Autopilot._compose_post, self.bot)
        model = types.SimpleNamespace(request=Mock(return_value=json.dumps({"content": "今儿心情不错~",
            "image_prompt": "MAIN_IMAGE_CANARY", "reason": "INTERNAL_REASON_CANARY",
            "chat_reply": "CHAT_ROUTE_CANARY", "voice_text": "VOICE_ROUTE_CANARY"}, ensure_ascii=False)))
        with patch.dict(sys.modules, {"model_client": model}):
            self.bot.think_and_post()
        self.assertEqual(model.request.call_args.args[0], "main")
        self.assertTrue(model.request.call_args.kwargs["json_mode"])
        payload = json.loads(model.request.call_args.args[1][1]["content"])
        self.assertEqual(payload["publish_plan"], {"form": "image"})
        self.assertNotIn("DECISION_", json.dumps(payload))
        self.assertEqual(self.bot._build_context.call_args.kwargs["profile"], "main")
        self.assertEqual(self.tools.generate_image.call_args.args, ("MAIN_IMAGE_CANARY",))
        self.assertEqual(self.sender.send_moment.call_args.args, ("今儿心情不错~", path))
        self.assertNotIn("CANARY", self.sender.send_moment.call_args.args[0])

    def test_main_failure_never_falls_back_to_decision_text_or_image(self):
        self.bot._ask_llm.return_value = {"should_post": True, "form": "image", "content": "旧决策草稿",
            "image_prompt": "old image", "image_path": self.image()}
        self.bot._compose_post = types.MethodType(self.module.Autopilot._compose_post, self.bot)
        model = types.SimpleNamespace(request=Mock())
        with patch.dict(sys.modules, {"model_client": model}):
            for value in ("invalid json", '{}', '{"image_prompt":"only image"}'):
                with self.subTest(value=value):
                    model.request.return_value = value
                    self.bot.think_and_post()
                    self.assertIsNone(self.bot.pending_snapshot())
                    self.sender.send_moment.assert_not_called()
                    self.tools.generate_image.assert_not_called()
            model.request.side_effect = RuntimeError("synthetic main failure")
            self.bot.think_and_post()
        self.assertIn("主模型", self.bot.last_result)
        self.sender.send_moment.assert_not_called()
        self.tools.generate_image.assert_not_called()

    def test_skip_never_requests_creation_and_text_plan_does_not_generate_an_image(self):
        self.bot._ask_llm.return_value = {"should_post": False, "content": "旧草稿"}
        self.bot.think_and_post()
        self.bot._compose_post.assert_not_called()
        self.bot._ask_llm.return_value = {"should_post": True, "form": "text"}
        self.bot._compose_post.return_value = {"content": "today", "image_prompt": "misrouted image"}
        self.bot.think_and_post()
        self.tools.generate_image.assert_not_called()
        self.assertEqual(self.sender.send_moment.call_args.args, ("today", ""))

    def test_cancellation_after_main_creation_precedes_image_and_send(self):
        self.bot._ask_llm.return_value = {"should_post": True, "form": "image"}
        def create(*args):
            self.state.bridge_generation += 1
            return {"content": "today", "image_prompt": "garden"}
        self.bot._compose_post.side_effect = create
        self.bot.think_and_post()
        self.assertIn("取消", self.bot.last_result)
        self.tools.generate_image.assert_not_called()
        self.sender.send_moment.assert_not_called()

    def test_keyless_loopback_decision_reaches_shared_http_client(self):
        self.config.AUTOPILOT_LLM_API_KEY = ""
        self.config.AUTOPILOT_LLM_API_BASE = "http://127.0.0.1:11434/v1"
        catalog = load("autopilot_test_catalog", "model_catalog.py")
        session = Mock()
        response = Mock(status_code=200)
        response.iter_content.return_value = [json.dumps({"choices": [{"message": {
            "content": json.dumps({"should_post": False, "image_prompt": None})}}]}).encode()]
        session.post.return_value = response
        with patch.dict(sys.modules, {"model_catalog": catalog,
                "http_util": types.SimpleNamespace(session=lambda: session)}):
            client = load("autopilot_actual_local_client", "model_client.py")
            with patch.dict(sys.modules, {"model_client": client}):
                self.assertFalse(type(self.bot)._ask_llm(self.bot, "isolated local context")["should_post"])
        session.post.assert_called_once()
        self.assertEqual(session.post.call_args.args[0], "http://127.0.0.1:11434/v1/chat/completions")
        self.assertNotIn("Authorization", session.post.call_args.kwargs["headers"])
        self.assertEqual(session.post.call_args.kwargs["json"]["messages"][-1]["content"], "isolated local context")
        response.close.assert_called_once()
        self.sender.send_moment.assert_not_called()

    def test_keyless_remote_decision_is_rejected_before_opening_http(self):
        self.config.AUTOPILOT_LLM_API_KEY = ""
        self.config.AUTOPILOT_LLM_API_BASE = "https://example.invalid/v1"
        catalog = load("autopilot_test_remote_catalog", "model_catalog.py")
        session = Mock()
        http = types.SimpleNamespace(session=Mock(return_value=session))
        with patch.dict(sys.modules, {"model_catalog": catalog, "http_util": http}):
            client = load("autopilot_actual_remote_client", "model_client.py")
            with patch.dict(sys.modules, {"model_client": client}), self.assertLogs("ob11-bridge", level="WARNING") as logs:
                self.assertIsNone(type(self.bot)._ask_llm(self.bot, "isolated remote context"))
        self.assertIn("密钥未配置", "\n".join(logs.output))
        http.session.assert_not_called()
        session.post.assert_not_called()
        self.sender.send_moment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
