"""Natural image requests and truthful results; synthetic generation/desktop only."""
import sys
import types
import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch

import test_core as core_cases


class PictureRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core_cases.CoreTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        core_cases.CoreTests.tearDownClass.__func__(cls)

    def setUp(self):
        core_cases.CoreTests.setUp(self)
        self.config.HUMANLIKE_DECISION_ENABLED = False
        self.stop_scheduler()
        self.generate = Mock(return_value="synthetic-generated.png")
        self.tools.generate_image = self.generate
        self.context = types.SimpleNamespace(chat_context=lambda store, **values: dict(values))
        modules = patch.dict(sys.modules, {"interaction_context": self.context})
        modules.start(); self.addCleanup(modules.stop)

    message = staticmethod(core_cases.CoreTests.message)
    stop_scheduler = core_cases.CoreTests.stop_scheduler
    entry = core_cases.CoreTests.entry
    process_all = core_cases.CoreTests.process_all

    def run_request(self, text="发张照片看看", *, segments=None, session="friend"):
        data = self.message(session, content=text)
        if segments:
            self.bridge._append_message(self.bridge._extract_identities(data), text)
        else:
            self.bridge.add_to_buffer(data)
        if segments:
            self.entry()["messages"][0]["segments"] = segments
        self.process_all()

    def test_direct_natural_requests_trigger_generation(self):
        requests = ("发张照片看看", "发个图片", "你发张照片看看", "给我发张照片", "来张照片",
                    "给我看看你的照片", "想看你的照片", "我想看看你的照片", "给我一张自拍",
                    "想看你的全身照片", "给我看看你的新照片",
                    "能不能发张你的照片", "发张照片给我看看", "按照这张图片创作一张照片",
                    "请生成一张猫的图片")
        for text in requests:
            with self.subTest(text=text):
                self.assertEqual(self.core._explicit_tool(text), "generate_image")

    def test_additive_prefixes_and_newline_preserve_explicit_picture_intent(self):
        for text in ("然后发个图片", "也发个图片", "顺便发张图", "接着发张照片", "另外发张图",
                     "发个语音\n然后发个图片"):
            with self.subTest(text=text):
                self.assertEqual(self.core._explicit_tool(text), "generate_image")
        for text in ("然后不要发图片", "顺便他说发张照片", "接着不用生成图片"):
            with self.subTest(text=text):
                self.assertEqual(self.core._explicit_tool(text), "none")

    def test_negated_quoted_reported_existing_media_and_other_recipients_do_not_authorize_generation(self):
        ordinary = ("不要发张照片", "别给我发张自拍", "不发照片", "不用生成照片", "不需要发图片",
                    "发张照片，不用生成", "发张照片但不要生成",
                    "他说发张照片看看", "她说：发张照片看看", "他说“发张照片看看”", "引用：发张照片看看",
                    "发张照片给我朋友", "给小李发张照片", "发张照片，给他看看", "发张照片给别人",
                    "刚发张照片给我朋友", "我昨天生成的图片效果不错", "这个照片里的头像挺可爱",
                    "[图片]", "已有附件", "给我看看这张图片", "转发这张照片", "这是你的照片吗")
        for text in ordinary:
            with self.subTest(text=text):
                self.assertEqual(self.core._explicit_tool(text), "none")

    def test_success_sends_exact_generated_file_once_to_bound_private_target_before_main_event(self):
        self.run_request()
        self.generate.assert_called_once()
        self.assertEqual(len(self.sent), 1)
        method, contact, args, kwargs = self.sent[0]
        self.assertEqual(method, "send_image")
        self.assertEqual(contact, "alice")  # UI display alias; routing below stays bound to the real session.
        self.assertEqual(args, ("synthetic-generated.png",))
        self.assertEqual(kwargs["target_id"], self.stable.user_id("friend", "friend"))
        self.assertFalse(kwargs["is_group"])
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["wechat_reply_plan"], {
            "reply_type": "text", "decision_source": "explicit_user_request_default", "image_status": "sent"})
        self.assertIn("工具图片已经成功发送", self.events[0]["message"][0]["data"]["text"])
        context = self.generate.call_args.kwargs["context"]
        self.assertEqual(context["session_id"], "friend")
        self.assertEqual(context["humanlike_plan"]["source"], "explicit_user_request_default")

    def test_generation_failure_has_no_image_dispatch_and_main_receives_truthful_failure(self):
        self.generate.return_value = None
        self.run_request()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.events[0]["wechat_reply_plan"]["image_status"], "generation_failed")
        self.assertIn("没有发送图片", self.events[0]["message"][0]["data"]["text"])
        self.assertNotIn("工具图片已经成功发送", self.events[0]["message"][0]["data"]["text"])

    def test_unconfirmed_send_is_not_retried_or_claimed_success(self):
        dispatch = Mock(return_value=False)
        self.state.dispatch_send = dispatch
        self.run_request()
        dispatch.assert_called_once()
        self.assertEqual(self.events[0]["wechat_reply_plan"]["image_status"], "send_unconfirmed")
        self.assertIn("不要自动重发", self.events[0]["message"][0]["data"]["text"])
        self.assertNotIn("工具图片已经成功发送", self.events[0]["message"][0]["data"]["text"])

    def test_attachment_alone_is_forwarded_to_main_without_paid_generation(self):
        attachment = {"type": "image", "data": {"file": "original-received.png"}}
        self.run_request("[图片]", segments=[attachment])
        self.generate.assert_not_called()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.events[0]["message"][-1], attachment)
        self.assertEqual(self.events[0]["wechat_reply_plan"]["image_status"], "none")

    def test_explicit_photo_request_with_attachment_still_generates_and_preserves_inbound_media(self):
        attachment = {"type": "image", "data": {"file": "original-received.png"}}
        self.run_request("发张照片看看", segments=[attachment])
        self.generate.assert_called_once()
        self.assertEqual(self.sent[0][0], "send_image")
        self.assertEqual(self.events[0]["message"][-1], attachment)

    def test_explicit_private_picture_skips_judge_and_sends_once(self):
        self.config.HUMANLIKE_DECISION_ENABLED = True
        choose = Mock(return_value=(False, "text", "结束语", "", ""))
        with patch.dict(sys.modules, {"humanlike_decision": types.SimpleNamespace(choose=choose)}):
            self.run_request()
        choose.assert_not_called()
        self.generate.assert_called_once()
        self.assertEqual([(method, args) for method, _, args, _ in self.sent],
                         [("send_image", ("synthetic-generated.png",))])
        plan = self.events[0]["wechat_reply_plan"]
        self.assertEqual((plan["decision_source"], plan["image_status"]), ("explicit_user_request_default", "sent"))
        self.assertTrue(self.generate.call_args.kwargs["context"]["humanlike_plan"]["should_reply"])

    def test_cancelled_generated_result_does_not_send_or_publish(self):
        def late_result(*args, **kwargs):
            self.state.paused.set()
            self.state.send_cancel.set()
            return "synthetic-generated.png"
        self.generate.side_effect = late_result
        self.run_request()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.events, [])

    def test_picture_request_carries_bound_policy_without_incidental_voice(self):
        self.config.HUMANLIKE_DECISION_ENABLED = True
        choose = Mock(return_value=(True, "voice", "轻松闲聊", "", ""))
        policies = []
        register = lambda event, generation, mode, revision: policies.append(dict(mode))
        with patch.dict(sys.modules, {"humanlike_decision": types.SimpleNamespace(choose=choose)}), \
                patch.object(self.state, "register_reply_request", register, create=True):
            self.run_request()
        self.assertEqual(self.events[0]["wechat_reply_plan"]["reply_type"], "text")
        self.assertTrue(policies[0]["image_requested"])
        self.assertFalse(policies[0]["explicit_voice"])

    def test_failed_picture_request_still_disables_incidental_voice(self):
        self.generate.return_value = None
        policies = []
        register = lambda event, generation, mode, revision: policies.append(dict(mode))
        with patch.object(self.state, "register_reply_request", register, create=True):
            self.run_request()
        self.assertEqual(self.events[0]["wechat_reply_plan"]["image_status"], "generation_failed")
        self.assertTrue(policies[0]["image_requested"])

    def test_user_can_explicitly_request_both_picture_and_voice(self):
        policies = []
        register = lambda event, generation, mode, revision: policies.append(dict(mode))
        with patch.object(self.state, "register_reply_request", register, create=True):
            self.run_request("你发个语音我听听，再发张照片看看")
        self.generate.assert_called_once()
        self.assertEqual(self.events[0]["wechat_reply_plan"]["reply_type"], "voice")
        self.assertTrue(policies[0]["image_requested"])
        self.assertTrue(policies[0]["explicit_voice"])
        self.assertEqual(policies[0]["image_after_voice"], "synthetic-generated.png")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.events[0]["wechat_reply_plan"]["image_status"], "prepared")

    def test_picture_before_voice_keeps_original_picture_first_order(self):
        self.run_request("先发张照片，然后发个语音")
        self.assertEqual(self.sent[0][0], "send_image")
        self.assertEqual(self.events[0]["wechat_reply_plan"]["image_status"], "sent")

    def test_short_picture_word_keeps_voice_before_picture_order(self):
        self.run_request("发个语音，再来张图")
        self.generate.assert_called_once()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.events[0]["wechat_reply_plan"]["image_status"], "prepared")
        self.assertEqual(self.state.pending_reply_modes[self.stable.user_id("friend", "friend")]["image_after_voice"],
                         "synthetic-generated.png")

    def queued_voice(self):
        import test_stream_runtime
        tickets = test_stream_runtime.fresh_state()
        tickets.bridge_generation = self.bridge.generation
        target = self.stable.user_id("friend", "friend")
        event = {"message_type": "private", "user_id": target, "message_id": 1,
                 "wechat_identity": {"session_id": "friend"}}
        revision = tickets.advance_reply_revision(target, False, self.bridge.generation)
        ticket = tickets.register_reply_request(event, self.bridge.generation, {"reply_type": "voice"}, revision)
        record = tickets._reply_requests[ticket["request_id"]]
        future = Future(); future.set_result(True)
        record.update(closed=True, segments={0: {"future": future, "delivery": {"voice_state": "queued", "voice_pending": True}}})
        patches = [patch.object(self.state, name, getattr(tickets, name), create=True)
                   for name in ("advance_reply_revision", "reply_revision_active", "reply_target_busy")]
        for item in patches:
            item.start(); self.addCleanup(item.stop)
        return tickets, record

    def test_voice_then_additional_picture_waits_for_previous_voice_receipt(self):
        tickets, record = self.queued_voice()
        self.bridge.add_to_buffer(self.message("friend", content="然后发个图片"))
        self.assertTrue(tickets.reply_request_active(record["request_id"]))
        self.process_all()
        self.generate.assert_not_called(); self.assertEqual(self.events, [])
        self.assertEqual(len(self.entry()["messages"]), 1)
        record["segments"][0]["delivery"].update(voice_state="sent", voice_pending=False)
        self.process_all()
        self.generate.assert_called_once()
        self.assertEqual(self.sent[0][0], "send_image")

    def test_explicit_stop_interrupts_queued_voice_and_does_not_wait(self):
        tickets, record = self.queued_voice()
        self.bridge.add_to_buffer(self.message("friend", content="别发语音了"))
        self.assertFalse(tickets.reply_request_active(record["request_id"]))
        self.process_all()
        self.assertTrue(self.events[0]["wechat_interrupt_previous"])
        self.generate.assert_not_called()

    def test_stop_detection_does_not_treat_quotes_or_power_outage_as_cancellation(self):
        for text in ("停电了", "他说停一下", "请读这句“别发语音了”", "我还想补一句"):
            with self.subTest(text=text):
                self.assertFalse(self.core._explicit_cancel(text))
        for text in ("取消", "停一下", "别发语音了", "先别画了，换个话题"):
            with self.subTest(text=text):
                self.assertTrue(self.core._explicit_cancel(text))

    def test_same_burst_cancel_supersedes_media_intent_without_erasing_main_context(self):
        self.run_request("发个语音\n别发语音了\n然后发个图片")
        self.generate.assert_called_once()
        self.assertEqual(self.events[0]["wechat_reply_plan"]["reply_type"], "text")
        self.assertIn("发个语音\n别发语音了\n然后发个图片", self.events[0]["message"][0]["data"]["text"])
        self.assertEqual(self.sent[0][0], "send_image")
        self.assertEqual(self.core._explicit_tool("发个图片\n别发图片了"), "none")

    def test_natural_voice_request_overrides_private_skip_decision(self):
        self.config.HUMANLIKE_DECISION_ENABLED = True
        choose = Mock(return_value=(False, "text", "结束语", "", ""))
        policies = []
        register = lambda event, generation, mode, revision: policies.append(dict(mode))
        with patch.dict(sys.modules, {"humanlike_decision": types.SimpleNamespace(choose=choose)}), \
                patch.object(self.state, "register_reply_request", register, create=True):
            self.run_request("你发个语音我听听")
        self.generate.assert_not_called()
        self.assertEqual(self.events[0]["wechat_reply_plan"]["reply_type"], "voice")
        self.assertTrue(policies[0]["explicit_voice"])
        self.assertFalse(policies[0]["image_requested"])

    def test_quoted_reported_or_negated_voice_is_not_an_explicit_voice_request(self):
        for text in ("不要给我发语音", "别发语音", "你不用发语音", "他说你发个语音我听听",
                     "他说“给我发语音”", "请读这句“你发个语音”", "我跟同事说一句话"):
            with self.subTest(text=text):
                self.assertFalse(self.core._explicit_voice(text))
        for text in ("你发个语音我听听", "发个语音", "给我发语音", "发张照片，发个语音", "你说一句"):
            with self.subTest(text=text):
                self.assertTrue(self.core._explicit_voice(text))

    def test_cancel_during_image_dispatch_does_not_publish_a_stale_reply(self):
        def dispatch(*args, **kwargs):
            self.state.paused.set()
            self.state.send_cancel.set()
            return True
        self.state.dispatch_send = dispatch
        self.run_request()
        self.generate.assert_called_once()
        self.assertEqual(self.events, [])

    def test_group_plan_has_only_fixed_decision_fields_and_correct_recipient(self):
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        self.judge.should_reply_group = lambda *args, **kwargs: (True, "text_sticker", "自由原因", "开心", "none")
        self.run_request(session="one@chatroom")
        self.assertTrue(self.sent[0][3]["is_group"])
        self.assertEqual(self.sent[0][3]["target_id"], self.stable.group_id("one@chatroom"))
        self.assertEqual(self.events[0]["wechat_reply_plan"], {
            "reply_type": "text_sticker", "decision_source": "group_reply_judge", "image_status": "sent"})


if __name__ == '__main__':
    unittest.main()
