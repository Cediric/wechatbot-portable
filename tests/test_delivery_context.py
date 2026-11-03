"""Actual-send feedback, using synthetic boundaries; no WeChat or paid APIs."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sys
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


class DeliveryContextTests(unittest.TestCase):
    def setUp(self):
        self.module = load("isolated_delivery_context", "delivery_context.py")
        self.store = self.module.DeliveryContext()

    def test_native_session_and_actor_filter_are_isolated(self):
        self.store.record("room_a@chatroom", "voice", "confirmed", "甲的语音", actor_id="wxid_a", turn_id="private-ticket-a", message_id=42)
        self.store.record("room_a@chatroom", "image", "unconfirmed", actor_id="wxid_b")
        self.store.record("room_b@chatroom", "text", "confirmed", "另一同名群的文字")
        rows = self.store.snapshot("room_a@chatroom", {"wxid_a"})
        self.assertEqual([(row["kind"], row["status"]) for row in rows], [("voice", "confirmed")])
        self.assertEqual(len(rows[0]["message_id"]), 12)
        self.assertEqual(self.store.snapshot("room_unknown@chatroom"), [])
        context = self.store.format_context("room_a@chatroom")
        self.assertNotIn("另一同名群", context)
        self.assertNotIn("private-ticket-a", context)
        self.assertIn("语音，发送器已确认", context)
        self.assertIn("图片，发送未确认", context)

    def test_unconfirmed_generated_and_failed_are_not_claimed_as_sent(self):
        for kind, status in (("voice", "generated"), ("image", "unconfirmed"), ("sticker", "failed")):
            self.store.record("wxid_a", kind, status, "虚构成功文字")
        self.assertTrue(all(row["status"] != "confirmed" and not row["text"] for row in self.store.snapshot("wxid_a")))
        self.assertNotIn("虚构成功文字", self.store.format_context("wxid_a"))
        self.assertIn("仅已生成", self.store.format_context("wxid_a"))

    def test_operation_updates_are_idempotent_and_cannot_erase_confirmed_voice(self):
        self.store.record("a", "voice", "generated", operation_id="voice")
        self.store.record("a", "voice", "confirmed", "我在", operation_id="voice")
        self.assertFalse(self.store.record("a", "voice", "cancelled", operation_id="voice"))
        self.store.record("a", "image", "unconfirmed", operation_id="image")
        self.assertEqual([(row["kind"], row["status"]) for row in self.store.snapshot("a")],
                         [("voice", "confirmed"), ("image", "unconfirmed")])
        snapshot = self.store.snapshot("a")
        snapshot[0]["status"] = "failed"
        self.assertEqual(self.store.snapshot("a")[0]["status"], "confirmed")

    def test_content_hides_credentials_paths_urls_and_capability_tokens(self):
        body = r'正文 C:\Users\private\audio.wav /tmp/private/key.json https://provider.invalid/v1?token=secret sk-synthetic_private_123456 api_key=hidden-key Bearer secret-token'
        self.store.record("a", "text", "confirmed", body, turn_id="secret-capability")
        context = self.store.format_context("a")
        for private in ("Users", "key.json", "provider.invalid", "synthetic_private", "hidden-key", "secret-token", "secret-capability"):
            self.assertNotIn(private, context)
        self.assertIn("正文", context)
        for body in (r'C:\Users\Private Person\secret file.wav', '{"api_key": "synthetic-private-value"}'):
            self.store.record("b", "text", "confirmed", body)
        self.assertNotIn("Person", self.store.format_context("b"))
        self.assertNotIn("synthetic-private-value", self.store.format_context("b"))

    def test_bounds_expiry_and_concurrent_writes(self):
        now = [10.0]
        store = self.module.DeliveryContext(max_sessions=2, max_rows=8, ttl_seconds=5, clock=lambda: now[0])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda index: store.record("a", "text", "confirmed", str(index)), range(100)))
        self.assertEqual(len(store.snapshot("a")), 8)
        store.record("b", "text", "confirmed", "b")
        store.record("c", "text", "confirmed", "c")
        self.assertEqual(store.snapshot("a"), [])
        now[0] += 5
        self.assertEqual(store.snapshot("b"), [])
        self.assertEqual(store.snapshot("c"), [])

    def test_prompt_budget_and_unknown_scope_are_bounded(self):
        for index in range(40):
            self.store.record("a", "text", "confirmed", '\\"' * 300, turn_id=index)
        self.assertLessEqual(len(self.store.format_context("a")), 1200)
        self.assertEqual(len(self.store.snapshot("a")), 32)
        self.assertFalse(self.store.record("", "text", "confirmed", "no native identity"))
        self.assertFalse(self.store.record("a", "unknown", "confirmed"))
        self.assertFalse(self.store.record("a", "voice", "invented"))


import test_protocol_validation as validation


class InlineThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class DeliveryBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        validation.ProtocolValidationTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        validation.ProtocolValidationTests.tearDownClass.__func__(cls)

    def setUp(self):
        validation.ProtocolValidationTests.setUp(self)
        self.feedback = self.protocol._delivery_context_module()
        self.feedback.clear()
        self.state.identity_registry = types.SimpleNamespace(
            get_private_record=lambda target: {"session_id": "wxid_a" if target == 1 else "wxid_b", "name": "同名联系人"},
            get_group_record=lambda target: {"session_id": str(target) + "@chatroom", "name": "同名群"},
            get_member_name=str)
        self.state.memory_store = Mock()
        self.state.reply_request_active = lambda request_id: True
        self.protocol._reply_media_policy = None
        self.config.AUTO_STICKER_ENABLED = False
        self.config.AUTO_VOICE_ENABLED = False
        modules = patch.dict(sys.modules, {"media_policy": load("isolated_feedback_media", "media_policy.py")})
        modules.start()
        self.addCleanup(modules.stop)

    def send(self, message, target=1):
        return self.protocol._perform_send({}, target, False, message, 123)

    def statuses(self, session="wxid_a"):
        return [(row["kind"], row["status"]) for row in self.feedback.snapshot(session)]

    def test_dispatch_success_failure_and_cross_session_no_generated_memory(self):
        self.state.dispatch_send.side_effect = [True, False]
        self.assertTrue(self.send([validation.text("甲已发送")]))
        self.assertFalse(self.send([validation.text("乙未确认")], target=2))
        self.assertEqual(self.statuses(), [("text", "confirmed")])
        self.assertEqual(self.statuses("wxid_b"), [("text", "unconfirmed")])
        self.state.memory_store.add_chat.assert_called_once_with("甲已发送", "wxid_a", actor_id="self", role="assistant")
        self.assertNotIn("乙未确认", self.feedback.format_context("wxid_b"))

    def test_partial_bubbles_preserve_only_sender_confirmed_words(self):
        def partial(*args, **kwargs):
            self.state.sender_instance._last_send = {"status": "partial", "result_sequence": 1,
                                                     "sent_parts": 1, "confirmed_texts": ["已发第一句。"]}
            return False
        self.state.dispatch_send.side_effect = partial
        with self.assertRaises(self.protocol.SendFailure):
            self.send([validation.text("已发第一句。未发第二句。")])
        self.assertEqual(self.statuses(), [("text", "unconfirmed"), ("text", "confirmed")])
        self.state.memory_store.add_chat.assert_called_once_with("已发第一句。", "wxid_a", actor_id="self", role="assistant")
        self.assertNotIn("未发第二句", self.feedback.format_context("wxid_a"))

    def test_stale_partial_metadata_does_not_confirm_a_new_failed_send(self):
        self.state.sender_instance._last_send = {"status": "partial", "result_sequence": 1,
                                                 "sent_parts": 1, "confirmed_texts": ["旧发送正文"]}
        self.state.dispatch_send.side_effect = lambda *args, **kwargs: False
        self.assertFalse(self.send([validation.text("新正文")]))
        self.assertEqual(self.statuses(), [("text", "unconfirmed")])
        self.state.memory_store.add_chat.assert_not_called()

    def test_preflight_failure_records_no_success_or_generated_body(self):
        message = [validation.text("整条还未发送"), {"type": "image", "data": {"file": "missing-private.png"}}]
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.send(message)
        self.assertEqual(self.statuses(), [("text", "failed"), ("image", "failed")])
        self.state.dispatch_send.assert_not_called()
        self.state.memory_store.add_chat.assert_not_called()
        self.assertNotIn("missing-private.png", self.feedback.format_context("wxid_a"))
        self.assertNotIn("整条还未发送", self.feedback.format_context("wxid_a"))

    def run_voice(self, *, audio="synthetic.wav", image=False, dispatch=(True,), through_final=False):
        self.state.dispatch_send.side_effect = dispatch if callable(dispatch) else list(dispatch)
        self.tts.synthesize_reply_voice = Mock(return_value=audio)
        request = {"request_id": "secret-turn-ticket", "request_message_id": "message-42", "session_id": "wxid_a",
                   "texts": ["我在呢，慢慢说，我听着。"], "started": time.monotonic(),
                   "mode": {"reply_type": "voice", "explicit_voice": True}}
        if image:
            request["mode"]["image_after_voice"] = "private-generated.png"
        progress = {}
        with patch.object(self.protocol.threading, "Thread", InlineThread):
            if through_final:
                self.protocol._perform_send({"wechat_stream": {"final": True}}, 1, False, [], 123, progress, request)
            else:
                self.protocol._finish_stream_voice(request, 1, False, 123, progress, lambda: False)
        return request, progress

    def test_voice_confirmed_image_unconfirmed_never_claims_duplicate_text(self):
        _, progress = self.run_voice(image=True, dispatch=(True, False))
        self.assertEqual(self.statuses(), [("voice", "confirmed"), ("image", "unconfirmed")])
        self.assertEqual((progress["voice_state"], progress["image_state"]), ("sent", "unconfirmed"))
        self.assertEqual([call.args[0] for call in self.state.dispatch_send.call_args_list], ["send_voice", "send_image"])
        self.state.memory_store.add_chat.assert_called_once()
        context = self.feedback.format_context("wxid_a")
        self.assertNotIn("private-generated.png", context)
        self.assertNotIn("secret-turn-ticket", context)
        self.assertNotIn("文字，发送器已确认", context)

    def test_unconfirmed_voice_never_becomes_success_or_memory(self):
        self.run_voice(image=True, dispatch=(False,))
        self.assertEqual(self.statuses(), [("voice", "unconfirmed")])
        self.state.memory_store.add_chat.assert_not_called()
        self.state.dispatch_send.assert_called_once()

    def test_synthesis_failure_fallback_is_text_only(self):
        self.run_voice(audio=None)
        self.assertEqual(self.statuses(), [("voice", "failed"), ("text", "confirmed")])
        self.assertEqual(self.state.dispatch_send.call_args.args[0], "send_text")
        self.state.memory_store.add_chat.assert_called_once()

    def partial_fallback(self, method, *args, **kwargs):
        self.assertEqual(method, "send_text")
        self.state.sender_instance._last_send = {"status": "partial", "result_sequence": 1,
                                                 "sent_parts": 1, "confirmed_texts": ["我在呢，"]}
        return False

    def assert_partial_fallback(self, request, progress):
        self.assertTrue(progress["partial"])
        self.assertEqual(progress["confirmed_parts"], 1)
        self.assertEqual(request["confirmed_parts"], 1)
        self.assertTrue(request["text_delivered"])
        self.assertEqual(request.get("confirmed_operations", 0), 0)
        self.assertEqual(self.statuses()[-2:], [("text", "unconfirmed"), ("text", "confirmed")])
        self.assertNotIn("慢慢说", self.feedback.format_context("wxid_a"))
        self.assertEqual(self.feedback.snapshot("wxid_b"), [])
        self.state.memory_store.add_chat.assert_called_once_with("我在呢，", "wxid_a", actor_id="self", role="assistant")
        self.state.dispatch_send.assert_called_once()
        # A repeat tail cannot retransmit either the text or its prepared image.
        self.protocol._finish_stream_voice(request, 1, False, 123, progress, lambda: False)
        self.state.dispatch_send.assert_called_once()

    def test_partial_tts_failure_text_fallback_preserves_first_bubble_and_stops(self):
        request, progress = self.run_voice(audio=None, image=True, dispatch=self.partial_fallback, through_final=True)
        self.assertEqual(progress["voice_state"], "unconfirmed")
        self.assertEqual(self.statuses()[0], ("voice", "failed"))
        self.assert_partial_fallback(request, progress)

    def test_partial_busy_voice_text_fallback_preserves_first_bubble_and_stops(self):
        with patch.object(self.protocol, "_voice_slots", Mock(acquire=Mock(return_value=False))):
            request, progress = self.run_voice(image=True, dispatch=self.partial_fallback, through_final=True)
        self.assertEqual(progress["voice_state"], "busy_text_fallback")
        self.tts.synthesize_reply_voice.assert_not_called()
        self.assert_partial_fallback(request, progress)

    def test_generated_voice_queue_rejection_does_not_create_success(self):
        self.state.send_queue = Mock()
        self.state.send_queue.submit.side_effect = RuntimeError("synthetic rejected queue")
        self.run_voice()
        self.assertEqual(self.statuses(), [("voice", "failed")])
        self.state.dispatch_send.assert_not_called()
        self.state.memory_store.add_chat.assert_not_called()

    def test_sticker_and_image_are_receipted_by_actual_dispatch_kind(self):
        plan = [{"kind": "face", "data": {}, "path": "private-sticker.png"},
                {"kind": "image", "data": {}, "path": "private-picture.png"}]
        with patch.object(self.protocol, "_preflight_send", return_value=(plan, set())):
            self.assertTrue(self.send([validation.text("ignored synthetic input")]))
        self.assertEqual(self.statuses(), [("sticker", "confirmed"), ("image", "confirmed")])
        self.state.memory_store.add_chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
