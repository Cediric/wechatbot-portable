"""Media choices and final voice safety without models, paid APIs or WeChat."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ReplyMediaPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = load("isolated_reply_media_policy", "media_policy.py")

    def test_unsuitable_replies_never_replace_copyable_or_private_text(self):
        cases = {
            "代码如下：```python\nprint(1)\n```": "code",
            "打开 https://example.invalid/a?private=keep-copyable 查看。": "copyable_url",
            "密钥：sk-synthetic_private_012345678901234567890": "private_data",
            "手机号是 13800138000，记一下。": "private_data",
            "<think>先分析用户性格</think>好的": "reasoning",
            "一、说明\n1. 安装环境\n2. 调整参数": "structured_steps",
            "这是一段很长的技术解释。" * 80: "too_long",
        }
        for text, reason in cases.items():
            with self.subTest(reason=reason):
                plan = self.policy.prepare_voice(text)
                self.assertFalse(plan.allowed)
                self.assertEqual(plan.reason, reason)
                self.assertEqual(plan.text, "")

    def test_short_natural_reply_keeps_content_and_removes_formatting(self):
        plan = self.policy.prepare_voice(" **我在呢**，慢慢说，我听着。 ")
        self.assertTrue(plan.allowed)
        self.assertEqual(plan.text, "我在呢，慢慢说，我听着。")

    def test_explicit_voice_cannot_bypass_content_checks(self):
        policy = self.policy.ReplyMediaPolicy()
        for mode in ("voice", "text_voice"):
            result = policy.decide("发语音", "代码：```x```", requested_mode=mode,
                                   explicit_voice=True, session_id="wxid_001")
            self.assertEqual(result["reply_type"], "text")
            self.assertEqual(result["voice_text"], "")

    def test_serious_and_technical_context_never_add_random_happy_stickers(self):
        policy = self.policy.ReplyMediaPolicy()
        for user in ("刚接到亲人去世的消息", "这个 API 报错怎么办", "被诈骗了怎么办", "我今天好难过"):
            result = policy.decide(user, "哈哈，我在呢", session_id=user)
            self.assertEqual(result["reply_type"], "text")
            self.assertEqual(result["sticker_keyword"], "")
            forced = policy.decide(user, "我在呢", requested_mode="sticker", sticker_keyword="开心", session_id=user)
            self.assertEqual(forced["reply_type"], "text")

    def test_explicit_comfort_sticker_is_allowed_without_random_joke(self):
        result = self.policy.ReplyMediaPolicy().decide("我很难过，给我一个抱抱表情", "抱抱你。",
                  requested_mode="text_sticker", sticker_keyword="抱抱", explicit_sticker=True, session_id="wxid_a")
        self.assertEqual(result["reply_type"], "text_sticker")
        self.assertEqual(result["sticker_keyword"], "抱抱")

    def test_cooldown_and_turn_spacing_are_both_required_and_session_local(self):
        now = [0.0]
        policy = self.policy.ReplyMediaPolicy(clock=lambda: now[0], sticker_cooldown=120, sticker_min_turns=4)
        first = policy.decide("真不错", "哈哈，太好了", session_id="wxid_a", request_id="a1")
        self.assertEqual(first["reply_type"], "text_sticker")
        self.assertEqual(policy.decide("same", "same", session_id="wxid_a", request_id="a1"), first)
        # A same-name different native account is not throttled by account A.
        self.assertEqual(policy.decide("真不错", "哈哈，太好了", session_id="wxid_b")["reply_type"], "text_sticker")
        now[0] = 121
        for turn in range(3):
            self.assertEqual(policy.decide("真不错", "哈哈，太好了", session_id="wxid_a")["reply_type"], "text")
        self.assertEqual(policy.decide("真不错", "哈哈，太好了", session_id="wxid_a")["reply_type"], "text_sticker")
        self.assertEqual(policy.decide("真不错", "哈哈，太好了", session_id="wxid_a")["reply_type"], "text")

    def test_unknown_identity_or_unmatched_intent_never_selects_automatic_media(self):
        policy = self.policy.ReplyMediaPolicy(auto_voice=True)
        self.assertEqual(policy.decide("你好", "哈哈，太好了", session_id="")["reply_type"], "text")
        policy = self.policy.ReplyMediaPolicy()
        self.assertEqual(policy.decide("你好", "今天整理了一下房间。", session_id="wxid_a")["reply_type"], "text")

    def test_automatic_voice_is_opt_in_and_does_not_override_sticker_intent(self):
        text = "我刚收拾完桌子，正想歇一会儿。你今天过得怎么样，有没有什么想说的？"
        self.assertEqual(self.policy.ReplyMediaPolicy().decide("聊一会儿", text, session_id="a")["reply_type"], "text")
        result = self.policy.ReplyMediaPolicy(auto_voice=True).decide("聊一会儿", text, session_id="a")
        self.assertEqual(result["reply_type"], "voice")
        self.assertEqual(result["voice_text"], text)

    def test_model_voice_plan_obeys_auto_switch_and_verified_session(self):
        for mode in ("voice", "text_voice"):
            with self.subTest(mode=mode):
                policy = self.policy.ReplyMediaPolicy(auto_voice=False)
                disabled = policy.decide("聊一会儿", "我在这儿陪你聊聊", requested_mode=mode, session_id="wxid_a")
                self.assertEqual((disabled["reply_type"], disabled["voice_text"], disabled["reason"]),
                                 ("text", "", "voice_auto_disabled"))
                policy = self.policy.ReplyMediaPolicy(auto_voice=True)
                unknown = policy.decide("聊一会儿", "我在这儿陪你聊聊", requested_mode=mode, session_id="")
                self.assertEqual((unknown["reply_type"], unknown["voice_text"], unknown["reason"]),
                                 ("text", "", "voice_identity_unverified"))

    def test_repeated_model_voice_plan_obeys_both_cooldown_and_turn_spacing(self):
        now = [0.0]
        policy = self.policy.ReplyMediaPolicy(auto_voice=True, voice_cooldown=180,
                                            voice_min_turns=3, clock=lambda: now[0])
        def decide(session="wxid_a"):
            return policy.decide("聊一会儿", "我在这儿陪你聊聊", requested_mode="voice", session_id=session)
        self.assertEqual(decide()["reply_type"], "voice")
        self.assertEqual(decide()["reason"], "voice_frequency_limited")
        now[0] = 181
        self.assertEqual(decide()["reply_type"], "text")  # Cooldown alone is insufficient.
        self.assertEqual(decide()["reply_type"], "voice")
        for _ in range(3):
            self.assertEqual(decide()["reply_type"], "text")  # Turns alone are insufficient.
        self.assertEqual(decide("wxid_b")["reply_type"], "voice")

    def test_explicit_user_voice_bypasses_auto_and_cooldown_but_not_content_or_disable(self):
        policy = self.policy.ReplyMediaPolicy(auto_voice=False, voice_cooldown=180, voice_min_turns=6)
        for _ in range(2):
            result = policy.decide("用语音说", "我在", requested_mode="voice", explicit_voice=True,
                                   session_id="wxid_a")
            self.assertEqual((result["reply_type"], result["voice_text"]), ("voice", "我在"))
        result = policy.decide("用语音说", "我在", requested_mode="voice", explicit_voice=True, session_id="")
        self.assertEqual(result["reply_type"], "voice")
        for text, enabled in (("```x```", True), ("我在", False)):
            result = policy.decide("用语音说", text, requested_mode="voice", explicit_voice=True,
                                   session_id="wxid_a", voice_enabled=enabled)
            self.assertEqual((result["reply_type"], result["voice_text"]), ("text", ""))

    def test_automatic_voice_minimum_is_configurable_and_old_default_is_retained(self):
        eight_chars = "我在这儿陪你聊聊"
        self.assertEqual(len(eight_chars), 8)
        generic = self.policy.ReplyMediaPolicy(auto_voice=True, auto_sticker=False)
        self.assertEqual(generic.decide("聊聊", eight_chars, session_id="wxid_a")["reply_type"], "text")
        personal = self.policy.ReplyMediaPolicy(auto_voice=True, auto_sticker=False, voice_min_chars=8)
        eligible = personal.decide("聊聊", eight_chars, session_id="wxid_a")
        self.assertEqual((eligible["reply_type"], eligible["voice_text"]), ("voice", eight_chars))
        too_short = personal.decide("聊聊", "我在", session_id="wxid_b")
        self.assertEqual((too_short["reply_type"], too_short["voice_text"]), ("text", ""))

    def test_retired_mixed_mode_is_voice_only_even_when_explicit(self):
        result = self.policy.ReplyMediaPolicy().decide("用语音说", "我在呢。", requested_mode="text_voice",
                                                     explicit_voice=True, session_id="wxid_a")
        self.assertEqual((result["reply_type"], result["voice_text"]), ("voice", "我在呢。"))

    def test_final_voice_accumulator_deduplicates_and_speaks_complete_stream_once(self):
        accumulator = self.policy.ReplyVoiceAccumulator()
        self.assertTrue(accumulator.add(1, "慢慢说，我听着。"))
        self.assertTrue(accumulator.add(0, "我在呢。"))
        self.assertFalse(accumulator.add(0, "我在呢。"))
        result = accumulator.finish()
        self.assertTrue(result.allowed)
        self.assertEqual(result.text, "我在呢。慢慢说，我听着。")
        self.assertEqual(accumulator.finish().reason, "already_finished")
        self.assertFalse(accumulator.add(2, "多余重复尾段"))

    def test_voice_accumulator_refuses_cancel_missing_sequence_and_overflow(self):
        for action, expected in (("cancel", "cancelled"), ("gap", "missing_segment"), ("overflow", "too_long")):
            with self.subTest(action=action):
                accumulator = self.policy.ReplyVoiceAccumulator()
                accumulator.add(0 if action != "gap" else 1, "你好")
                if action == "overflow":
                    self.assertFalse(accumulator.add(1, "字" * 20001))
                result = accumulator.finish(cancelled=action == "cancel")
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason, expected)


class StickerIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.happy = self.root / "happy.gif"
        self.sad = self.root / "sad.gif"
        self.happy.write_bytes(b"synthetic")
        self.sad.write_bytes(b"synthetic")
        (self.root / "manifest.json").write_text(json.dumps({"开心": ["happy.gif"], "不开心": ["sad.gif"], "happy": ["happy.gif"]}), encoding="utf-8")
        self.cfg = types.SimpleNamespace(STICKER_ENABLED=True, STICKER_DIR=str(self.root))
        with patch.dict(sys.modules, {"config": self.cfg}):
            self.selector = load("isolated_intent_stickers", "sticker_selector.py")

    def select(self, keyword):
        with patch.dict(sys.modules, {"config": self.cfg}):
            return self.selector.select_sticker(keyword)

    def test_unknown_empty_and_generic_intents_do_not_pick_random_root_file(self):
        for keyword in ("", "震惊", "表情包", "sticker", "随便"):
            with self.subTest(keyword=keyword):
                self.assertIsNone(self.select(keyword))

    def test_specific_matches_and_negation_do_not_confuse_happy_and_sad(self):
        self.assertEqual(self.select("开心"), str(self.happy))
        self.assertEqual(self.select("我不开心"), str(self.sad))
        self.assertEqual(self.select("我很开心"), str(self.happy))
        self.assertIsNone(self.select("unhappy"))
        self.assertIsNone(self.select("not happy"))

    def test_manifest_escape_and_deleted_files_remain_unselectable(self):
        (self.root / "manifest.json").write_text(json.dumps({"开心": ["../outside.gif"]}), encoding="utf-8")
        self.happy.unlink()
        self.assertIsNone(self.select("开心"))


class SafeReplySynthesisTests(unittest.TestCase):
    def setUp(self):
        self.policy = load("isolated_safe_voice_policy", "media_policy.py")
        self.cfg = types.SimpleNamespace(VOICE_MAX_CHARS=600)
        modules = patch.dict(sys.modules, {"config":self.cfg, "http_util":types.SimpleNamespace(),
                                         "media_util":types.SimpleNamespace(), "media_policy":self.policy})
        modules.start()
        self.addCleanup(modules.stop)
        self.tts = load("isolated_safe_reply_tts", "tts.py")

    def test_rejected_or_cancelled_reply_never_calls_paid_synthesis(self):
        with patch.object(self.tts, "synthesize_voice") as synth:
            self.assertIsNone(self.tts.synthesize_reply_voice("```python\nprint(1)\n```"))
            self.assertIsNone(self.tts.synthesize_reply_voice("你好", cancelled=lambda: True))
            synth.assert_not_called()

    def test_finished_voice_calls_once_and_post_call_cancellation_blocks_dispatch(self):
        cancelled = [False]
        def finish(*args, **kwargs):
            cancelled[0] = True
            return "synthetic.wav"
        with patch.object(self.tts, "synthesize_voice", side_effect=finish) as synth:
            is_cancelled = lambda: cancelled[0]
            self.assertIsNone(self.tts.synthesize_reply_voice("我在呢，慢慢说。", cancelled=is_cancelled))
            synth.assert_called_once_with("我在呢，慢慢说。", "", cancelled=is_cancelled)

    def test_safe_synthesis_retains_full_text_and_options(self):
        with patch.object(self.tts, "synthesize_voice", return_value="synthetic.wav") as synth:
            self.assertEqual(self.tts.synthesize_reply_voice("**我在呢**，慢慢说。", voice="configured-voice"), "synthetic.wav")
            self.assertEqual(synth.call_args.args, ("我在呢，慢慢说。", ""))
            self.assertEqual(synth.call_args.kwargs["voice"], "configured-voice")
            self.assertFalse(synth.call_args.kwargs["cancelled"]())


if __name__ == "__main__":
    unittest.main()
