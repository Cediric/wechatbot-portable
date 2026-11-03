"""Tool and media regressions use synthetic files and mocked APIs only."""
import base64
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch
import wave

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wav_bytes():
    target = io.BytesIO()
    with wave.open(target, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * 120)
    return target.getvalue()


class Response:
    def __init__(self, data=b"", json_data=None, status=200, headers=None):
        self.data = json.dumps(json_data).encode() if json_data is not None else data
        self.status_code = status
        self.headers = headers or {}
        self.closed = False
        self.text = self.data.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.data)

    def iter_content(self, _):
        for offset in range(0, len(self.data), 13):
            yield self.data[offset:offset + 13]

    def raise_for_status(self):
        if self.status_code != 200:
            raise ValueError("mock HTTP failure")

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix=".tools-test-", dir=str(SOURCE.parent / "tests"))
        cls.config = types.ModuleType("config")
        cls.config.__dict__.update(BASE_DIR=cls.temp.name, DATA_DIR=cls.temp.name)
        cls.http = types.ModuleType("http_util")
        cls.module_patch = patch.dict("sys.modules", {"config": cls.config, "http_util": cls.http})
        cls.module_patch.start()
        cls.media = load("test_media_util", "media_util.py")
        cls.media_patch = patch.dict("sys.modules", {"media_util": cls.media})
        cls.media_patch.start()
        cls.tts = load("test_tts", "tts.py")
        cls.tools = load("test_tools_module", "tools.py")
        cls.sticker = load("test_sticker", "sticker_selector.py")

    @classmethod
    def tearDownClass(cls):
        cls.media_patch.stop()
        cls.module_patch.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(dir=self.temp.name)
        self.addCleanup(self.case.cleanup)
        self.root = Path(self.case.name)
        self.config.__dict__.update(MEDIA_MAX_MB=30, CACHE_RETENTION_DAYS=7,
            TOOLS_TAVILY_KEY="synthetic-tavily", TOOLS_SEARCH_MAX_RESULTS=7,
            TOOLS_IMAGE_MODEL="wan2.7-image-pro", TOOLS_IMAGE_SIZE="1K",
            BOT_IMAGE_MODEL="wan2.7-image-pro", BOT_IMAGE_REFS=[],
            IMAGE_PROMPT_ENABLED=False,
            DASHSCOPE_API_KEY="synthetic-dashscope", DASHSCOPE_BASE_URL="https://example.invalid/api/v1",
            TTS_PROVIDER="dashscope", TTS_MODEL="cosyvoice-v2", TTS_VOICE="",
            TTS_OUTPUT_DIR=str(self.root / "voice"), STICKER_ENABLED=True,
            STICKER_DIR=str(self.root / "stickers"))
        self.session = Mock()
        self.http.session = lambda: self.session
        main = types.SimpleNamespace(request=Mock(side_effect=lambda profile, messages, **kwargs:
            json.loads(messages[-1]["content"])["request"]))
        main_patch = patch.dict("sys.modules", {"model_client": main})
        main_patch.start(); self.addCleanup(main_patch.stop)

    def image(self, path, size=(300, 300), mode="RGB"):
        from PIL import Image
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new(mode, size).save(path)
        return str(path.resolve())

    def sdk(self, result=None, error=None):
        dashscope = types.ModuleType("dashscope")
        synthesis = Mock(return_value=types.SimpleNamespace(status_code=200, output={"results": [{"url": "https://example.invalid/old.png"}]}))
        dashscope.ImageSynthesis = types.SimpleNamespace(call=synthesis)
        image_call = Mock(return_value=types.SimpleNamespace(status_code=200, output={"choices": [{"message": {"content": [{"image": "https://example.invalid/new.png"}]}}]}))
        image_generation = types.ModuleType("dashscope.aigc.image_generation")
        image_generation.ImageGeneration = types.SimpleNamespace(call=image_call)
        speech = Mock(return_value=result if result is not None else wav_bytes(), side_effect=error)
        constructor = Mock(return_value=types.SimpleNamespace(call=speech))
        audio = types.ModuleType("dashscope.audio.tts_v2")
        audio.AudioFormat = types.SimpleNamespace(WAV_24000HZ_MONO_16BIT="wav24k")
        audio.SpeechSynthesizer = constructor
        modules = {"dashscope": dashscope, "dashscope.aigc": types.ModuleType("dashscope.aigc"),
                   "dashscope.aigc.image_generation": image_generation,
                   "dashscope.audio": types.ModuleType("dashscope.audio"), "dashscope.audio.tts_v2": audio}
        return patch.dict("sys.modules", modules), image_call, speech, constructor

    def test_web_search_uses_configured_limit_and_explicit_override(self):
        self.session.post.return_value = Response(json_data={"results": [{"title": str(i), "url": "https://example.invalid", "content": "x"} for i in range(9)]})
        self.assertIn("7.", self.tools.web_search("query"))
        self.assertNotIn("8.", self.tools.web_search("query"))
        self.assertEqual(self.session.post.call_args.kwargs["json"]["max_results"], 7)
        self.tools.web_search("query", 3)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["max_results"], 3)

    def test_official_deepseek_search_reuses_only_its_own_credentials(self):
        self.config.MAIN_LLM_API_KEY = "synthetic-main-key"
        self.config.TOOLS_TAVILY_KEY = ""
        for base in ("https://api.deepseek.com.other.invalid", "https://custom.invalid/v1",
                     "https://api.deepseek.com@custom.invalid", "http://api.deepseek.com"):
            self.config.MAIN_LLM_API_BASE = base
            self.assertIn("未配置", self.tools.web_search("query"))
        self.session.post.assert_not_called()
        self.config.MAIN_LLM_API_BASE = "https://api.deepseek.com/v1/"
        self.session.post.return_value = Response(json_data={"results": [
            {"title": "合成标题", "summary": "合成摘要", "url": "https://example.invalid"}]})
        self.assertIn("合成摘要", self.tools.web_search("搜" * 150))
        self.session.post.assert_called_once_with("https://api.deepseek.com/v1/tools/search",
            headers={"Authorization": "Bearer synthetic-main-key"}, json={"query": "搜"*128, "count": 7},
            allow_redirects=False, timeout=15)
        self.config.TOOLS_TAVILY_KEY = "own-tavily"
        self.tools.web_search("query")
        call = self.session.post.call_args
        self.assertEqual(call.args[0], "https://api.tavily.com/search")
        self.assertNotIn("Authorization", call.kwargs["headers"])

    def test_search_failure_or_empty_results_never_echo_secret_error_bodies(self):
        self.session.post.return_value = Response(status=402, data=b"synthetic-echoed-key")
        self.assertEqual(self.tools.web_search("query"), "[web_search] Tavily 搜索接口返回 HTTP 402。")
        self.session.post.return_value = Response(json_data={"results": []})
        self.assertIn("没有搜索结果", self.tools.web_search("query"))

    def test_image_search_requests_images_and_tries_next_download(self):
        self.session.post.return_value = Response(json_data={"images": [{"url": "https://example.invalid/bad"}, {"url": "https://example.invalid/good"}]})
        with patch.object(self.tools, "_download_generated_image", side_effect=[None, "second.png"]) as download:
            self.assertEqual(self.tools.search_image("query"), "second.png")
        payload = self.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["max_results"], 7)
        self.assertTrue(payload["include_images"])
        self.assertNotIn("search_type", payload)
        self.assertEqual(download.call_count, 2)

    def test_image_search_candidate_count_is_bounded(self):
        self.session.post.return_value = Response(json_data={"images": ["https://example.invalid/" + str(i) for i in range(5)]})
        with patch.object(self.tools, "_download_generated_image", return_value=None) as download:
            self.tools.search_image("query", 2)
        self.assertEqual(download.call_count, 2)

    def test_reference_inputs_use_sdk_file_upload_protocol(self):
        local = self.image(self.root / "reference.png")
        expected = "file://" + local.replace("\\", "/")
        self.assertEqual(self.tools._reference_input(local), expected)
        self.assertEqual(self.tools._reference_input(expected), expected)
        self.assertEqual(self.tools._reference_input("https://example.invalid/reference.png"), "https://example.invalid/reference.png")
        with self.assertRaises(ValueError):
            self.tools._reference_input("https://user:pass@example.invalid/image.png")

    def test_invalid_local_references_fail_before_paid_call(self):
        bad = self.image(self.root / "transparent.png", mode="RGBA")
        self.config.BOT_IMAGE_REFS = [bad]
        sdk, call, _, _ = self.sdk()
        with sdk:
            self.assertIsNone(self.tools.generate_image("draw"))
        call.assert_not_called()
        with self.assertRaises(ValueError):
            self.tools._reference_input(self.image(self.root / "tiny.png", (100, 100)))

    def test_wan_refs_and_aspect_ratio_are_forwarded(self):
        local = self.image(self.root / "reference.png")
        self.config.BOT_IMAGE_REFS = [local, "https://example.invalid/ref.png"]
        self.config.TOOLS_IMAGE_SIZE = "1024*1536"
        sdk, call, _, _ = self.sdk()
        with sdk, patch.object(self.tools, "_download_generated_image", return_value="generated.png"), self.assertLogs("ob11-bridge", "INFO") as logs:
            self.assertEqual(self.tools.generate_image("draw"), "generated.png")
        payload = call.call_args.kwargs
        self.assertEqual(payload["size"], "1024*1536")
        self.assertEqual(len(payload["messages"][0]["content"]), 3)
        self.assertEqual(payload["api_key"], "synthetic-dashscope")
        self.assertTrue(any("bot_image_ref_index/strength" in message for message in logs.output))

    def test_wan_model_bounds_and_no_truncated_refs(self):
        self.assertEqual(self.tools._wan27_size("4K", True), "2K")
        self.assertEqual(self.tools._wan27_size("4K", False), "4K")
        with self.assertRaises(ValueError):
            self.tools._wan27_size("1024*9000", True)
        self.config.BOT_IMAGE_REFS = ["https://example.invalid/ref.png"] * 10
        sdk, call, _, _ = self.sdk()
        with sdk:
            self.assertIsNone(self.tools.generate_image("draw"))
        call.assert_not_called()

    def test_wan26_edit_mode_correct_route_and_ref_limit(self):
        self.config.BOT_IMAGE_MODEL = "wan2.6-image"
        self.config.BOT_IMAGE_REFS = ["https://example.invalid/ref.png"]
        sdk, call, _, _ = self.sdk()
        with sdk, patch.object(self.tools, "_download_generated_image", return_value="generated.png"):
            self.assertEqual(self.tools.generate_image("draw"), "generated.png")
        self.assertFalse(call.call_args.kwargs["enable_interleave"])
        self.config.BOT_IMAGE_REFS *= 5
        with sdk:
            self.assertIsNone(self.tools.generate_image("draw"))
        self.assertEqual(call.call_count, 1)

    def test_sdk_wav_atomic_output_and_matching_voice(self):
        sdk, _, call, constructor = self.sdk()
        with sdk:
            result = self.tts.synthesize_voice("hello")
        self.assertEqual(Path(result).read_bytes(), wav_bytes())
        self.assertEqual(constructor.call_args.kwargs["voice"], "longxiaochun_v2")
        self.assertEqual(constructor.call_args.kwargs["speech_rate"], 1.0)
        self.assertEqual(constructor.call_args.kwargs["pitch_rate"], 1.0)
        self.assertEqual(constructor.call_args.kwargs["volume"], 50)
        call.assert_called_once_with("hello", timeout_millis=60000)
        self.assertFalse(list(Path(self.config.TTS_OUTPUT_DIR).glob("*.part")))
        self.session.post.assert_not_called()

    def test_sdk_voice_tuning_overrides_and_invalid_values_never_synthesize(self):
        sdk, _, speech, constructor = self.sdk()
        with sdk:
            self.assertTrue(self.tts.synthesize_voice("试听", speech_rate=1.25, pitch_rate=.8, volume=65))
            self.assertEqual(constructor.call_args.kwargs["speech_rate"],1.25)
            self.assertEqual(constructor.call_args.kwargs["pitch_rate"],.8)
            self.assertEqual(constructor.call_args.kwargs["volume"],65)
            constructor.reset_mock(); speech.reset_mock()
            for fields in [{"speech_rate":.49},{"pitch_rate":float("nan")},{"volume":101},{"volume":True}]:
                self.assertIsNone(self.tts.synthesize_voice("试听", **fields))
            constructor.assert_not_called(); speech.assert_not_called()

    def test_failed_sdk_never_bills_second_request_or_overwrites_destination(self):
        destination = self.root / "existing.wav"
        destination.write_bytes(b"existing")
        sdk, _, _, _ = self.sdk(result=b"invalid audio")
        with sdk:
            self.assertIsNone(self.tts.synthesize_voice("hello", str(destination)))
        self.assertEqual(destination.read_bytes(), b"existing")
        self.session.post.assert_not_called()
        with patch.object(self.tts, "_audio_limit", return_value=100):
            with self.assertRaises(ValueError):
                self.tts._validate_wav(wav_bytes())

    def test_cosyvoice_http_tuning_uses_http_rate_pitch_fields(self):
        self.session.post.return_value=Response(data=wav_bytes())
        value=self.tts._http_audio("试听","cosyvoice-v2","longxiaochun_v2", {"speech_rate":1.25,"pitch_rate":.75,"volume":60})
        self.assertEqual(value,wav_bytes())
        body=self.session.post.call_args.kwargs["json"]
        self.assertEqual((body["input"]["rate"],body["input"]["pitch"],body["input"]["volume"]),(1.25,.75,60))
        self.assertNotIn("parameters",body)

    def test_qwen_audio_url_uses_distinct_endpoint_without_leaking_auth(self):
        self.config.TTS_MODEL = "qwen3-tts-flash"
        post = Response(json_data={"output": {"audio": {"url": "https://example.invalid/audio.wav"}}})
        get = Response(wav_bytes())
        self.session.post.return_value, self.session.get.return_value = post, get
        result = self.tts.synthesize_voice("你好")
        self.assertTrue(result)
        args, kwargs = self.session.post.call_args
        self.assertTrue(args[0].endswith("/services/aigc/multimodal-generation/generation"))
        self.assertEqual(kwargs["json"]["input"]["voice"], "Cherry")
        self.assertNotIn("headers", self.session.get.call_args.kwargs)
        self.assertTrue(post.closed and get.closed)
        self.assertEqual(Path(result).read_bytes(), wav_bytes())

    def test_cosy_http_payload_and_nested_base64(self):
        post = Response(json_data={"output": {"audio": {"data": base64.b64encode(wav_bytes()).decode()}}})
        self.session.post.return_value = post
        with patch.dict("sys.modules", {"dashscope": None}):
            self.assertTrue(self.tts.synthesize_voice("你好"))
        args, kwargs = self.session.post.call_args
        self.assertTrue(args[0].endswith("/services/audio/tts/SpeechSynthesizer"))
        self.assertEqual(kwargs["json"]["input"]["format"], "wav")
        self.assertEqual(kwargs["json"]["input"]["sample_rate"], 24000)
        self.assertTrue(post.closed)

    def test_invalid_or_oversized_http_audio_is_closed_and_not_saved(self):
        self.config.TTS_MODEL = "qwen3-tts-flash"
        post = Response(json_data={"output": {"audio": {"url": "https://example.invalid/a.wav"}}}, headers={"Content-Length": "22000000"})
        self.session.post.return_value = post
        self.assertIsNone(self.tts.synthesize_voice("你好"))
        self.assertTrue(post.closed)
        self.session.get.assert_not_called()
        self.assertFalse(Path(self.config.TTS_OUTPUT_DIR).exists())

    def test_qwen_limit_and_unsupported_model_fail_before_http(self):
        self.config.TTS_MODEL = "qwen3-tts-flash"
        self.assertIsNone(self.tts.synthesize_voice("字" * 601))
        self.config.TTS_MODEL = "qwen3-tts-flash-realtime"
        self.assertIsNone(self.tts.synthesize_voice("hello"))
        self.session.post.assert_not_called()

    def test_tts_age_cleanup_retains_many_fresh_and_only_owns_tts_prefix(self):
        directory = Path(self.config.TTS_OUTPUT_DIR)
        directory.mkdir()
        for index in range(50):
            (directory / f"tts_{index}.wav").write_bytes(b"fresh")
        old = directory / "tts_old.wav"
        old.write_bytes(b"old")
        unrelated = directory / "shared_old.wav"
        unrelated.write_bytes(b"old")
        age = time.time() - 8 * 86400
        os.utime(old, (age, age))
        os.utime(unrelated, (age, age))
        self.config.CACHE_RETENTION_DAYS = 1
        self.tts._prune_tts_cache(keep=2)
        self.assertEqual(len(list(directory.glob("tts_*.wav"))), 50)
        self.assertTrue(unrelated.exists())

    def test_atomic_media_write_preserves_existing_destination_on_failure(self):
        destination = self.root / "output.bin"
        destination.write_bytes(b"original")
        with self.assertRaises(ValueError):
            self.media.write_bytes_atomic(destination, b"oversized", max_bytes=2)
        with patch.object(self.media.os, "replace", side_effect=OSError("mock replacement failure")):
            with self.assertRaises(OSError):
                self.media.write_bytes_atomic(destination, b"new")
        self.assertEqual(destination.read_bytes(), b"original")
        self.assertFalse(list(self.root.glob("*.part")))

    def test_download_bounds_actual_stream_and_cleans_partial_files(self):
        response = Response(b"too much")
        self.session.get.return_value = response
        with patch.object(self.media, "media_limit", return_value=3):
            with self.assertRaises(ValueError):
                self.media.download("https://example.invalid/file", str(self.root))
        self.assertTrue(response.closed)
        self.assertFalse(list(self.root.glob("media-*")))
        with self.assertRaises(ValueError):
            self.media.response_bytes(Response(b"abc", headers={"Content-Length": "wrong"}), 100)

    def test_download_verifies_actual_image_format_and_extension(self):
        source = self.image(self.root / "source.jpg")
        self.session.get.return_value = Response(Path(source).read_bytes())
        result = self.media.download("https://example.invalid/image", str(self.root), ".png", image=True)
        self.assertTrue(result.endswith(".jpg"))
        self.session.get.return_value = Response(b"not an image")
        with self.assertRaises(Exception):
            self.media.download("https://example.invalid/image", str(self.root), ".png", image=True)
        self.assertFalse(list(self.root.glob("*.part")))

    def test_download_rejects_truncated_jpeg_and_cleans_owned_file(self):
        source = self.image(self.root / "source.jpg", size=(64, 64))
        self.session.get.return_value = Response(Path(source).read_bytes()[:-8])
        with self.assertRaises(ValueError):
            self.media.download("https://example.invalid/truncated.jpg", str(self.root), image=True)
        self.assertFalse(list(self.root.glob("media-*")))
        self.assertTrue(Path(source).exists())

    def test_image_pixel_limit_fails_before_verification_or_decode(self):
        path = self.root / "large.png"
        path.write_bytes(b"synthetic metadata")
        source = Mock(format="PNG", size=(8000, 6000))
        context = Mock()
        context.__enter__ = Mock(return_value=source)
        context.__exit__ = Mock(return_value=False)
        with patch("PIL.Image.open", return_value=context) as opened:
            with self.assertRaisesRegex(ValueError, "4000"):
                self.media.validate_image_file(path)
        opened.assert_called_once()
        source.verify.assert_not_called()
        source.load.assert_not_called()

    def test_nested_media_leases_keep_old_image_until_last_consumer_releases(self):
        path = self.root / "media-owned.png"
        path.write_bytes(b"old")
        age = time.time() - 8 * 86400
        os.utime(path, (age, age))
        with self.media.lease_file(path):
            with self.media.lease_file(str(path.resolve())):
                self.media.prune_cache(self.root, 7)
                self.assertTrue(path.exists())
            self.media.prune_cache(self.root, 7)
            self.assertTrue(path.exists())
        self.assertFalse(self.media._LEASED_FILES)
        self.media.prune_cache(self.root, 7)
        self.assertFalse(path.exists())

    def test_cache_delete_refuses_escape_and_new_files(self):
        owned = self.root / "cache"
        owned.mkdir()
        outside = self.root / "user.png"
        outside.write_bytes(b"preserve")
        fresh = owned / "fresh.png"
        fresh.write_bytes(b"fresh")
        self.assertFalse(self.media.remove_cached_file(outside, owned))
        self.assertFalse(self.media.remove_cached_file(fresh, owned, older_than=time.time() - 86400))
        self.assertTrue(outside.exists())
        self.assertTrue(fresh.exists())
        with self.media.lease_file(fresh):
            self.assertFalse(self.media.remove_cached_file(fresh, owned))
        self.assertTrue(self.media.remove_cached_file(fresh, owned))
        self.assertFalse(fresh.exists())

    def test_nested_sticker_manifests_merge_string_lists_and_reject_escape(self):
        directory = Path(self.config.STICKER_DIR)
        first = self.image(directory / "first.png")
        nested = self.image(directory / "nested" / "second.png")
        outside = self.image(self.root / "outside.png")
        (directory / "manifest.json").write_text(json.dumps({"happy": "first.png", "bad": "../outside.png", "malformed": 42}), encoding="utf-8")
        (directory / "nested" / "manifest.json").write_text(json.dumps({"happy": ["bad\u0000path.png", "second.png", "../../outside.png", 3]}), encoding="utf-8")
        snapshot = self.sticker._ensure_cache(str(directory))
        self.assertEqual(set(snapshot["manifest"]["happy"]), {first, nested})
        self.assertNotIn(outside, snapshot["files"])
        self.assertNotIn("bad", snapshot["manifest"])
        for _ in range(10):
            self.assertIn(self.sticker.select_sticker("HAPPY"), {first, nested})

    def test_sticker_manifest_edits_refresh_without_directory_mtime_change(self):
        directory = Path(self.config.STICKER_DIR)
        first = self.image(directory / "first.png")
        second = self.image(directory / "second.png")
        manifest = directory / "manifest.json"
        manifest.write_text(json.dumps({"happy": "first.png"}))
        self.assertEqual(self.sticker.select_sticker("happy"), first)
        stamp = manifest.stat().st_mtime_ns
        manifest.write_text(json.dumps({"happy": "second.png"}))
        os.utime(manifest, ns=(stamp + 1000000, stamp + 1000000))
        self.assertEqual(self.sticker.select_sticker("happy"), second)

    def test_sticker_snapshots_remain_consistent_across_concurrent_roots(self):
        roots = [self.root / f"set{index}" for index in range(4)]
        for index, directory in enumerate(roots):
            self.image(directory / f"{index}.png")
        def check(index):
            directory = roots[index % 4]
            snapshot = self.sticker._ensure_cache(str(directory))
            # Another selection cannot mutate an already returned root snapshot.
            time.sleep(.001)
            return snapshot["dir"] == str(directory.resolve()) and all(Path(path).parent == directory for path in snapshot["files"])
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(pool.map(check, range(40))))

    def test_external_sticker_symlink_is_not_eligible(self):
        directory = Path(self.config.STICKER_DIR)
        directory.mkdir()
        outside = Path(self.image(self.root / "outside.png"))
        try:
            (directory / "linked.png").symlink_to(outside)
        except OSError:
            # Windows may deny creating symlinks. Exercise the same canonical
            # target escape without requiring elevated OS permissions.
            linked = directory / "linked.png"
            linked.write_bytes(b"synthetic link placeholder")
            realpath = self.sticker.os.path.realpath
            def resolved(path):
                return str(outside) if os.fspath(path) == str(linked) else realpath(path)
            with patch.object(self.sticker.os.path, "realpath", side_effect=resolved):
                self.assertIsNone(self.sticker.select_sticker())
        else:
            self.assertIsNone(self.sticker.select_sticker())


if __name__ == "__main__":
    unittest.main()
