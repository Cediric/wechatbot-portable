"""Legacy persona settings must preserve model text in chat and voice delivery."""
from __future__ import annotations

from pathlib import Path
import asyncio
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_stream_runtime as streams
import test_protocol_validation as protocol_tests

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "wechat-weflow-bridge-ob11"
STYLE = {"reply_style_max_chars": 25, "reply_style_no_letters": True,
         "reply_style_allowed_punctuation": "！，。？（）”：；~《》"}


class StreamReplyStyleTests(unittest.IsolatedAsyncioTestCase):
    def media_event(self, restricted=True):
        event = streams.StreamEvent()
        async def convert(chain):
            return [{"type": "text", "data": {"text": component.text}} if component.type == "Plain"
                    else {"type": component.type.lower(), "data": component.data}
                    for component in chain.chain]
        event._parse_onebot_json = convert
        profile = {**STYLE, "streaming": {"enabled": True}} if restricted else {"streaming": {"enabled": True}}
        streams.streaming.bind_streaming(event, profile)
        return event

    @staticmethod
    def media(kind="Image", **data):
        chain = streams.Chain()
        chain.chain = [types.SimpleNamespace(type=kind, data=data)]
        return chain

    async def run_stream(self, body, width=1):
        event = streams.StreamEvent()
        streams.streaming.bind_streaming(event, {**STYLE, "streaming": {"enabled": True, "max_chars": 160}})
        async def provider():
            yield streams.Chain("推理不可发A", "reasoning")
            for offset in range(0, len(body), width):
                yield streams.Chain(body[offset:offset + width])
        await event.send_streaming(provider())
        parts = [segment["data"]["text"] for _, params in event.calls for segment in params["message"]]
        return event, "".join(parts)

    async def test_legacy_character_settings_preserve_every_answer_character(self):
        body = "“等一下”…… A&B？ " + "好" * 40 + " 🙂。"
        for width in (1, 6, 200):
            event, text = await self.run_stream(body, width)
            self.assertEqual(text, body)
            self.assertTrue(event.calls[-1][1]["wechat_stream"]["final"])
        event = streams.StreamEvent()
        streams.streaming.bind_streaming(event, {**STYLE, "streaming": {"enabled": False}})
        await event.send(streams.Chain(body))
        self.assertEqual(event.calls[0][1]["message"][0]["data"]["text"], body)
        self.assertEqual(len(event.calls), 1)

    async def test_media_waits_for_accepted_sentence_and_preserves_order(self):
        event = self.media_event()
        sent = asyncio.Event()
        original_send = event.bot.call_action
        async def send(*args, **kwargs):
            result = await original_send(*args, **kwargs)
            sent.set()
            return result
        event.bot.call_action = send
        async def provider():
            yield self.media(file="synthetic.png")
            self.assertEqual(event.calls, [])
            yield streams.Chain("给你看看。")
            await asyncio.wait_for(sent.wait(), 1)
            self.assertEqual([segment["type"] for segment in event.calls[0][1]["message"]], ["text", "image"])
        await event.send_streaming(provider())
        self.assertEqual(event.calls[0][1]["message"][0]["data"]["text"], "给你看看。")
        self.assertEqual(event.calls[-1][1]["message"], [])

    async def test_image_only_sends_media_without_program_written_caption(self):
        event = self.media_event()
        async def provider():
            yield self.media(file="synthetic.png")
        await event.send_streaming(provider())
        self.assertEqual(len(event.calls), 1)
        self.assertEqual([segment["type"] for segment in event.calls[0][1]["message"]], ["image"])
        self.assertTrue(event.calls[0][1]["wechat_stream"]["final"])

    async def test_self_reported_audio_transcript_is_not_trusted(self):
        for kind, data in (("Record", {"file": "synthetic.wav", "text": "你好。"}),
                           ("Voice", {"text": "你好。"})):
            event = self.media_event()
            async def provider():
                yield self.media(kind, **data)
            await event.send_streaming(provider())
            self.assertEqual(event.calls[0][1]["message"], [])
            self.assertTrue(event.calls[0][1]["wechat_stream"]["final"])

    async def test_sdk_error_before_generator_closes_without_canned_character_line(self):
        event = self.media_event()
        await event.send(streams.Chain("Error occurred while processing agent request: synthetic"))
        await event.send(streams.Chain("Error occurred again!"))
        self.assertEqual(len(event.calls), 1)
        self.assertEqual(event.calls[0][1]["message"], [])
        self.assertTrue(event.calls[0][1]["wechat_stream"]["final"])

    async def test_sdk_generator_error_closes_safe_without_replaying_partial_answer(self):
        for first in ("", "第一句。"):
            event = self.media_event()
            async def provider():
                if first:
                    yield streams.Chain(first)
                raise RuntimeError("synthetic provider error")
            with self.assertRaises(RuntimeError):
                await event.send_streaming(provider())
            await event.send(streams.Chain("Error occurred while processing agent request: synthetic"))
            text = "".join(segment["data"]["text"] for _, params in event.calls for segment in params["message"])
            self.assertEqual(text, first)
            self.assertTrue(event.calls[-1][1]["wechat_stream"]["final"])
            self.assertEqual(len(event.calls), 2 if first else 1)

    async def test_generic_sdk_error_after_generator_is_final(self):
        event = self.media_event(restricted=False)
        async def provider():
            raise RuntimeError("synthetic provider error")
            yield streams.Chain()
        with self.assertRaises(RuntimeError):
            await event.send_streaming(provider())
        await event.send(streams.Chain("Synthetic error!"))
        self.assertTrue(event.calls[0][1]["wechat_stream"]["final"])
        self.assertEqual(event.calls[0][1]["message"], [])

    async def test_ambiguous_transport_failure_is_not_retried_for_error_chain(self):
        event = self.media_event()
        event.bot.call_action = AsyncMock(side_effect=TimeoutError("synthetic ambiguous send"))
        async def provider():
            yield streams.Chain("第一句。")
        with self.assertRaises(TimeoutError):
            await event.send_streaming(provider())
        await event.send(streams.Chain("Error occurred while processing agent request: synthetic"))
        event.bot.call_action.assert_awaited_once()


class ProtocolReplyStyleTests(unittest.TestCase):
    setUpClass = classmethod(protocol_tests.ProtocolValidationTests.setUpClass.__func__)
    tearDownClass = classmethod(protocol_tests.ProtocolValidationTests.tearDownClass.__func__)

    def setUp(self):
        protocol_tests.ProtocolValidationTests.setUp(self)
        for key, value in STYLE.items():
            patcher = patch.object(self.config, key.upper(), value, create=True)
            patcher.start(); self.addCleanup(patcher.stop)
        self.state.reply_request_active = lambda _: True
        tail = patch.object(self.protocol, "_decide_tail_media", return_value={"reply_type": "text", "reason": "text", "sticker_keyword": ""})
        tail.start(); self.addCleanup(tail.stop)

    def request(self):
        return {"request_id": "native", "texts": [], "mode": {"reply_type": "text"},
                "session_id": "wxid_native", "started": time.monotonic() - 2}

    def perform(self, message, request=None, final=False):
        params = {"wechat_stream": {"final": final}} if request else {}
        return self.protocol._perform_send(params, 1, False, message, 123, {}, request)

    def test_reply_text_is_delivered_verbatim_with_legacy_rules(self):
        request = self.request()
        parts = ["“OK”， ", "A&B……🙂 " + "长" * 40 + "。"]
        self.perform([protocol_tests.text(part) for part in parts], request, final=True)
        self.assertEqual(self.state.dispatch_send.call_args.args, ("send_text", "friend", "".join(parts)))
        self.assertEqual(request["texts"], parts)
        self.tts.synthesize_voice.assert_not_called()

    def test_at_and_face_keep_their_onebot_meaning(self):
        for segment, expected in (({"type": "at", "data": {"name": "abc"}}, "@abc"),
                                  ({"type": "face", "data": {"id": 1}}, "[face 1]")):
            self.state.dispatch_send.reset_mock()
            self.perform([segment], self.request(), final=True)
            self.assertEqual(self.state.dispatch_send.call_args.args, ("send_text", "friend", expected))

    def test_bridge_accepts_complete_stream_and_tail_despite_legacy_length_budget(self):
        request = self.request()
        self.perform([protocol_tests.text("我" * 20 + "。")], request)
        self.perform([protocol_tests.text("你" * 5 + "。")], request)
        with patch.object(self.protocol, "_decide_tail_media", return_value={"reply_type": "text", "reason": "text", "sticker_keyword": ""}) as decide:
            self.perform([], request, final=True)
        self.assertEqual(request["texts"], ["我" * 20 + "。", "你" * 5 + "。"])
        self.assertEqual(self.state.dispatch_send.call_count, 2)
        self.assertEqual(decide.call_args.args[1], "我" * 20 + "。" + "你" * 5 + "。")

    def test_unticketed_private_packets_are_rejected_without_new_turn_guessing(self):
        require_ticket = patch.object(self.config, "REQUIRE_REPLY_TICKET", True, create=True)
        require_ticket.start(); self.addCleanup(require_ticket.stop)
        for body in ("我" * 20 + "。", "你" * 20 + "。"):
            with self.assertRaisesRegex(ValueError, "会话票据"):
                self.perform([protocol_tests.text(body)])
            response = asyncio.run(self.protocol._handle_ob_api({"action": "send_private_msg",
                "params": {"user_id": 1, "message": [protocol_tests.text(body)]}}))
            self.assertEqual(response["status"], "failed")
            self.assertEqual(response["retcode"], 1400)
        self.state.dispatch_send.assert_not_called()
        self.tts.synthesize_voice.assert_not_called()

    def test_bridge_does_not_delay_media_to_inspect_future_characters(self):
        request = self.request()
        image = {"type": "image", "data": {"file": protocol_tests.ProtocolValidationTests.image(self)}}
        self.perform([image], request)
        self.assertEqual(self.state.dispatch_send.call_args.args[0], "send_image")
        self.perform([protocol_tests.text("hello!")], request, final=True)
        self.assertEqual([call.args[0] for call in self.state.dispatch_send.call_args_list], ["send_image", "send_text"])
        self.assertEqual(request["texts"], ["hello!"])

    def test_bridge_image_only_final_sends_image_without_invented_caption(self):
        request = self.request()
        self.perform([{"type": "image", "data": {"file": protocol_tests.ProtocolValidationTests.image(self)}}], request, final=True)
        self.assertEqual([call.args[0] for call in self.state.dispatch_send.call_args_list], ["send_image"])
        self.assertEqual(request["texts"], [])

    def test_bridge_unverified_audio_rejected_even_with_legal_claimed_text(self):
        for kind, data in (("record", {"file": "synthetic.wav", "text": "你好。"}),
                           ("voice", {"text": "你好。"})):
            with self.assertRaisesRegex(ValueError, "外部语音"):
                self.perform([protocol_tests.text("你好。"), {"type": kind, "data": data}], self.request(), final=True)
        self.state.dispatch_send.assert_not_called()
        self.tts.synthesize_voice.assert_not_called()
        self.media.download.assert_not_called()

    def test_complete_main_model_script_stays_on_voice_channel_with_or_without_image(self):
        script = "“OK”，A&B…… " + "长" * 35 + "🙂。"
        for with_image in (False, True):
            with self.subTest(with_image=with_image):
                request = self.request()
                request["mode"] = {"reply_type": "voice", "explicit_voice": True}
                packet = [protocol_tests.text(script)]
                if with_image:
                    packet.append({"type": "image", "data": {"file": protocol_tests.ProtocolValidationTests.image(self)}})
                inline_worker = lambda target, **_: types.SimpleNamespace(start=target)
                with patch.object(self.tts, "synthesize_reply_voice", return_value="synthetic.wav", create=True) as synthesize, \
                     patch.object(self.protocol.threading, "Thread", side_effect=inline_worker), \
                     patch.object(self.protocol, "_decide_tail_media", return_value={"reply_type": "voice", "reason": "requested_voice"}):
                    self.assertTrue(self.perform(packet, request, final=True))
                self.assertEqual(synthesize.call_count, 1)
                self.assertEqual(synthesize.call_args.args, (script,))
                self.assertEqual([call.args[0] for call in self.state.dispatch_send.call_args_list],
                                 ["send_image", "send_voice"] if with_image else ["send_voice"])
                self.assertEqual(self.state.dispatch_send.call_args.kwargs["reply_request_id"], "native")
                self.state.dispatch_send.reset_mock()


if __name__ == "__main__":
    unittest.main()
