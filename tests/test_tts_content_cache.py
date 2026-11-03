"""Completed speech reuse; mocked synthesis only, no external API."""
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import struct
import threading
import time
import unittest
from unittest.mock import Mock, patch
import wave

import test_voice_bindings as fixtures
import tts_cache


def pcm_wav(samples, rate=24000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buffer.getvalue()


class ContentCacheTests(unittest.TestCase):
    def setUp(self):
        fixtures.VoiceBindingTests.setUp(self)
        self.cache = tts_cache.SpeechCache()
        active = patch.object(tts_cache, "cache", self.cache)
        active.start(); self.addCleanup(active.stop)

    sdk = fixtures.VoiceBindingTests.sdk

    def test_long_quiet_lead_shortened_once_preserving_soft_consonant_and_tail(self):
        speech = [-40, 40] * 720 + [800] * 4800 + [0] * 7200 + [200] * 2400 + [0] * 1200
        original = pcm_wav([0] * 12000 + speech)
        expected = pcm_wav([0] * 1680 + speech)  # 70 ms before the quiet consonant
        sdk, _, paid = self.sdk(audio=original)
        with sdk, patch.object(self.tts, "_trim_leading_quiet", wraps=self.tts._trim_leading_quiet) as trim:
            result = self.tts.synthesize_voice("轻声起句")
            self.assertEqual(Path(result).read_bytes(), expected)
            self.assertEqual(self.tts.synthesize_voice("轻声起句"), result)
        trim.assert_called_once()
        paid.assert_called_once()

    def test_short_lead_very_soft_opening_all_quiet_and_other_formats_remain_intact(self):
        for name, audio in [
            ("short", pcm_wav([0] * 3600 + [500] * 2400)),
            ("quiet voice", pcm_wav([50] * 12000 + [900] * 2400)),
            ("all quiet", pcm_wav([0] * 24000)),
            ("different format", pcm_wav([0] * 12000 + [900] * 2400, 16000)),
        ]:
            with self.subTest(name=name):
                self.assertEqual(self.tts._trim_leading_quiet(audio), audio)

    def test_valid_legacy_audio_is_upgraded_locally_once(self):
        original = pcm_wav([0] * 12000 + [500] * 2400)
        sdk, _, paid = self.sdk(audio=original)
        with sdk, patch.object(self.cache, "get_or_create", wraps=self.cache.get_or_create) as lookup:
            current = Path(self.tts.synthesize_voice("之前合成过的台词"))
        identity = lookup.call_args.args[0]
        old = Path(tts_cache._cache_path({**identity, "version": 1}, self.cfg.TTS_OUTPUT_DIR))
        current.unlink()
        old.write_bytes(original)
        with patch.object(self.tts, "_trim_leading_quiet", wraps=self.tts._trim_leading_quiet) as trim:
            for _ in range(2):
                self.assertEqual(Path(self.tts.synthesize_voice("之前合成过的台词")), current)
        trim.assert_called_once()
        paid.assert_called_once()
        self.assertEqual(current.read_bytes(), pcm_wav([0] * 1680 + [500] * 2400))

    def test_completed_audio_reused_and_remains_valid_preview_id(self):
        sdk, _, paid = self.sdk()
        with sdk:
            first = self.tts.synthesize_voice("同一句台词")
            second = self.tts.synthesize_voice("同一句台词")
        self.assertEqual(first, second)
        paid.assert_called_once()
        self.assertEqual(self.client.read_preview(Path(first).name), fixtures.wav_bytes())
        stats = tts_cache.stats()
        self.assertEqual((stats["hits"], stats["misses"], stats["synthesis_count"]), (1, 1, 1))
        self.assertNotIn("同一句台词", repr(stats))
        self.assertNotIn(self.cfg.DASHSCOPE_API_KEY, repr(stats))

    def test_complete_request_identity_and_account_do_not_share_audio(self):
        sdk, _, paid = self.sdk()
        cases = [("DASHSCOPE_BASE_URL", "https://second.invalid/api/v1"),
                 ("DASHSCOPE_API_KEY", "another-synthetic-key"), ("BOT_WXID", "wxid_other"),
                 ("TTS_MODEL", "cosyvoice-v3.5-flash"), ("TTS_VOICE", "another-voice"),
                 ("TTS_SPEECH_RATE", 1.1), ("TTS_PITCH_RATE", 1.2), ("TTS_VOLUME", 60)]
        with sdk, patch.object(self.module, "verify_for_synthesis"):
            original = self.tts.synthesize_voice("完整台词")
            for field, value in cases:
                previous = getattr(self.cfg, field, None)
                setattr(self.cfg, field, value)
                self.assertNotEqual(self.tts.synthesize_voice("完整台词"), original, field)
                if previous is None:
                    delattr(self.cfg, field)
                else:
                    setattr(self.cfg, field, previous)
                self.assertEqual(self.tts.synthesize_voice("完整台词"), original)
            self.assertNotEqual(self.tts.synthesize_voice("另一个台词"), original)
        self.assertEqual(paid.call_count, len(cases)+2)

    def test_concurrent_same_key_synthesizes_once_and_cancelled_waiter_returns_no_audio(self):
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        sdk, _, paid = self.sdk()
        def render(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return fixtures.wav_bytes()
        paid.side_effect = render
        with sdk, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.tts.synthesize_voice, "你好呀")
            self.assertTrue(entered.wait(2))
            second = pool.submit(self.tts.synthesize_reply_voice, "你好呀", cancelled=cancelled.is_set)
            deadline = time.monotonic()+2
            while self.cache.stats()["coalesced_waits"] < 1 and time.monotonic() < deadline:
                cancelled.wait(.005)
            self.assertEqual(self.cache.stats()["coalesced_waits"], 1)
            try:
                cancelled.set()
                self.assertIsNone(second.result(1), "cancelled waiter must not wait for paid synthesis")
                self.assertFalse(first.done(), "shared synthesis remains owned by its original caller")
                self.assertEqual(self.cache.stats()["inflight"], 1)
            finally:
                release.set()
            self.assertTrue(Path(first.result(3)).is_file())
            self.assertEqual(self.tts.synthesize_voice("你好呀"), first.result())
        paid.assert_called_once()

    def test_cache_cleanup_is_amortized_and_tracks_output_directory(self):
        sdk, _, paid = self.sdk()
        now = [100.0]
        with sdk, patch.object(self.tts.media_util, "prune_cache") as prune, \
                patch.object(self.tts.time, "monotonic", side_effect=lambda: now[0]):
            for text in ("第一句", "第二句", "第三句"):
                self.assertTrue(self.tts.synthesize_voice(text))
            self.assertEqual(prune.call_count, 1)
            now[0] += 601
            self.assertTrue(self.tts.synthesize_voice("第四句"))
            self.assertEqual(prune.call_count, 2)
            self.cfg.TTS_OUTPUT_DIR = str(Path(self.temp.name) / "other-tts")
            self.assertTrue(self.tts.synthesize_voice("第五句"))
            self.assertEqual(prune.call_count, 3)
            self.assertEqual(prune.call_args.args[0], self.cfg.TTS_OUTPUT_DIR)
        self.assertEqual(paid.call_count, 5)

    def test_unfinished_or_invalid_audio_never_enters_cache_and_has_no_paid_fallback(self):
        sdk, _, paid = self.sdk(response={"header": {"event": "task-failed"}})
        with sdk, patch.object(self.tts, "_http_audio") as fallback:
            self.assertIsNone(self.tts.synthesize_voice("失败台词"))
            self.assertIsNone(self.tts.synthesize_voice("失败台词"))
        self.assertEqual(paid.call_count, 2)
        fallback.assert_not_called()
        self.assertEqual(list(Path(self.cfg.TTS_OUTPUT_DIR).glob("*.wav")), [])
        self.assertEqual(self.cache.stats()["failures"], 2)

    def test_concurrent_failed_synthesis_has_one_paid_attempt_and_no_negative_cache(self):
        entered, release = threading.Event(), threading.Event()
        sdk, _, paid = self.sdk()
        def failed(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            raise RuntimeError("synthetic vendor failure")
        paid.side_effect = failed
        with sdk, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.tts.synthesize_voice, "失败的并发台词")
            self.assertTrue(entered.wait(2))
            second = pool.submit(self.tts.synthesize_voice, "失败的并发台词")
            deadline = time.monotonic()+2
            while self.cache.stats()["coalesced_waits"] < 1 and time.monotonic() < deadline:
                release.wait(.005)
            self.assertEqual(self.cache.stats()["coalesced_waits"], 1)
            release.set()
            self.assertIsNone(first.result(3)); self.assertIsNone(second.result(3))
            paid.assert_called_once()
            paid.side_effect = None
            paid.return_value = fixtures.wav_bytes()
            self.assertTrue(self.tts.synthesize_voice("失败的并发台词"))
        self.assertEqual(paid.call_count, 2)

    def test_expired_or_damaged_cached_file_is_not_reused(self):
        sdk, _, paid = self.sdk()
        with sdk:
            first = self.tts.synthesize_voice("保留策略")
            old = time.time()-8*86400
            os.utime(first, (old, old))
            self.assertEqual(self.tts.synthesize_voice("保留策略"), first)
            Path(first).write_bytes(b"incomplete")
            self.assertEqual(self.tts.synthesize_voice("保留策略"), first)
        self.assertEqual(paid.call_count, 3)
        self.assertEqual(Path(first).read_bytes(), fixtures.wav_bytes())

    def test_explicit_output_path_uses_completed_content_without_resynthesis(self):
        sdk, _, paid = self.sdk()
        custom = str(Path(self.temp.name)/"chosen.wav")
        with sdk:
            self.assertTrue(self.tts.synthesize_voice("指定文件"))
            self.assertEqual(self.tts.synthesize_voice("指定文件", custom), custom)
        paid.assert_called_once()
        self.assertEqual(Path(custom).read_bytes(), fixtures.wav_bytes())

    def test_stats_bound_success_samples_and_key_includes_format(self):
        writer = Mock(return_value="synthetic.wav")
        for index in range(205):
            self.cache.get_or_create({"text": "same", "format": str(index)}, self.temp.name,
                86400, lambda: b"audio", lambda data: data, writer, 100)
        snapshot = self.cache.stats()
        self.assertEqual((snapshot["misses"], snapshot["synthesis_count"], snapshot["synthesis_samples"]), (205, 205, 200))
        self.assertEqual(snapshot["inflight"], 0)


if __name__ == "__main__":
    unittest.main()
