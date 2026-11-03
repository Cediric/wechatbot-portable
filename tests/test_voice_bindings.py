"""Voice resource compatibility and binding regressions; mocked services only."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch
import wave

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
MODEL = "cosyvoice-v3.5-plus"
VOICE = MODEL + "-bailian-" + "a" * 32


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
    def __init__(self, output, status=200):
        self.status_code, self.headers, self.closed = status, {}, False
        self.data = output if isinstance(output, bytes) else json.dumps({"output": output}).encode()

    def iter_content(self, *_args, **_kwargs):
        yield self.data

    def close(self):
        self.closed = True


class VoiceBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = types.SimpleNamespace(DATA_DIR=self.temp.name, BASE_DIR=self.temp.name, MEDIA_MAX_MB=30,
            DASHSCOPE_API_KEY="synthetic-only-key", DASHSCOPE_BASE_URL="https://synthetic.cn-beijing.maas.aliyuncs.com/api/v1",
            TTS_PROVIDER="dashscope", TTS_MODEL=MODEL, TTS_VOICE=VOICE,
            TTS_OUTPUT_DIR=str(Path(self.temp.name) / "tts"), CACHE_RETENTION_DAYS=7)
        self.cfg._config_lock = threading.RLock()
        self.session = Mock()
        self.session.post.return_value = Response({"status": "OK", "target_model": MODEL})
        self.modules = patch.dict(sys.modules, {"config": self.cfg, "http_util": types.SimpleNamespace(session=lambda: self.session)})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.media = load("voice_binding_media", "media_util.py")
        self.media_patch = patch.dict(sys.modules, {"media_util": self.media})
        self.media_patch.start()
        self.addCleanup(self.media_patch.stop)
        self.module = load("voice_binding_client", "voice_cloning.py")
        self.voice_patch = patch.dict(sys.modules, {"voice_cloning": self.module})
        self.voice_patch.start()
        self.addCleanup(self.voice_patch.stop)
        self.client = self.module.VoiceCloningClient()
        self.tts = load("voice_binding_tts", "tts.py")

    def sdk(self, audio=None, response=None):
        speech = Mock(return_value=wav_bytes() if audio is None else audio)
        instance = types.SimpleNamespace(call=speech)
        if response is not None:
            instance.get_response = Mock(return_value=response)
        constructor = Mock(return_value=instance)
        dashscope = types.ModuleType("dashscope")
        audio_module = types.ModuleType("dashscope.audio.tts_v2")
        audio_module.AudioFormat = types.SimpleNamespace(WAV_24000HZ_MONO_16BIT="wav24k")
        audio_module.SpeechSynthesizer = constructor
        return patch.dict(sys.modules, {"dashscope": dashscope, "dashscope.audio": types.ModuleType("dashscope.audio"),
            "dashscope.audio.tts_v2": audio_module}), constructor, speech

    def test_thirty_dotted_model_ids_round_trip_without_widening_to_urls(self):
        models = [MODEL, "cosyvoice-v3.5-flash", "qwen-audio-3.0-tts-flash"]
        rows = [{"voice_id": models[index % 3] + "-bailian-" + format(index, "032x"),
                 "target_model": models[index % 3], "status": "OK"} for index in range(30)]
        self.session.post.return_value = Response({"voice_list": rows})
        self.assertEqual(self.client.list(page_size=30), [{**row, "gmt_create": "", "gmt_modified": ""} for row in rows])
        for row in rows:
            self.session.post.return_value = Response({"status": "OK", "target_model": row["target_model"]})
            self.assertEqual(self.client.query(row["voice_id"])["target_model"], row["target_model"])

    def test_voice_id_bounds_and_invalid_characters_fail_before_http(self):
        self.assertEqual(self.module._voice_id("a" * 256), "a" * 256)
        for value in ["", "a" * 257, ".voice", "../voice", "a/b", "a\\b", "a:b", "https://x/a", "a b", "a\n", "a\x00", "音色", None, 1]:
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                self.client.query(value)
        self.session.post.assert_not_called()

    def test_create_retains_binding_without_parsing_the_returned_id(self):
        self.session.post.return_value = Response({"voice_id": "enrollment.synthetic-resource"})
        result = self.client.create("https://example.invalid/sample.wav", prefix="mine", target_model=MODEL)
        self.assertEqual(result, {"voice_id": "enrollment.synthetic-resource", "target_model": MODEL, "status": "DEPLOYING"})
        request = self.session.post.call_args.kwargs
        self.assertEqual(request["json"]["input"]["target_model"], MODEL)
        self.assertFalse(request["allow_redirects"])
        self.session.post.assert_called_once()

    def test_create_conflicting_explicit_binding_is_not_retried(self):
        self.session.post.return_value = Response({"voice_id": VOICE, "target_model": "cosyvoice-v2"})
        with self.assertRaises(RuntimeError):
            self.client.create("https://example.invalid/sample.wav")
        self.session.post.assert_called_once()

    def test_list_retains_provided_binding_and_leaves_missing_binding_unknown(self):
        self.session.post.return_value = Response({"voice_list": [{"voice_id": VOICE, "target_model": MODEL},
            {"voice_id": "cosyvoice-v3.5-plus-test-" + "b" * 32}]})
        self.assertEqual([row["target_model"] for row in self.client.list()], [MODEL, ""])
        self.session.post.assert_called_once()

    def test_query_rejects_missing_invalid_or_truncated_binding(self):
        for model in [None, "", "x" * 201, "cosyvoice-v3.5-plus\n", {"model": MODEL}]:
            self.session.post.return_value = Response({"status": "OK", "target_model": model})
            with self.subTest(model=repr(model)), self.assertRaises(ValueError):
                self.client.query(VOICE)

    def test_query_rejects_returned_resource_id_mismatch(self):
        self.session.post.return_value = Response({"status": "OK", "target_model": MODEL, "voice_id": VOICE + "b"})
        with self.assertRaises(RuntimeError):
            self.client.query(VOICE)

    def test_service_binding_is_authoritative_not_the_id_model_prefix(self):
        alternate = "cosyvoice-v3.5-flash"
        self.session.post.return_value = Response({"status": "OK", "target_model": alternate})
        self.assertEqual(self.client.verify_for_synthesis(VOICE, alternate)["target_model"], alternate)
        with self.assertRaises(ValueError):
            self.client.verify_for_synthesis(VOICE, MODEL)
        self.assertEqual(self.session.post.call_count, 2)

    def test_unready_or_wrong_model_is_not_cached_or_synthesized(self):
        synth = Mock()
        with patch.dict(sys.modules, {"tts": types.SimpleNamespace(synthesize_voice=synth)}):
            for output in [{"status": "DEPLOYING", "target_model": MODEL},
                           {"status": "UNDEPLOYED", "target_model": MODEL},
                           {"status": "OK", "target_model": "cosyvoice-v2"}]:
                self.session.post.return_value = Response(output)
                with self.assertRaises(ValueError):
                    self.client.preview("试听", VOICE, MODEL)
        self.assertEqual(self.session.post.call_count, 3)
        self.assertEqual(self.module._binding_cache, {})
        synth.assert_not_called()

    def test_unsupported_clone_model_is_rejected_without_even_a_query(self):
        for model in ["qwen-audio-3.0-tts-flash", "qwen3-tts-flash", "qwen-tts-realtime", "cosyvoice-v1"]:
            with self.subTest(model=model), self.assertRaises(ValueError):
                self.client.verify_for_synthesis(VOICE, model)
        self.session.post.assert_not_called()

    def test_builtin_system_voice_has_no_enrollment_query(self):
        for voice, model in [("longxiaochun_v2", "cosyvoice-v2"), ("longanyang", "cosyvoice-v3-plus"), ("Cherry", "qwen3-tts-flash")]:
            self.assertIsNone(self.client.verify_for_synthesis(voice, model))
        self.session.post.assert_not_called()

    def test_ready_binding_cached_for_sixty_seconds_returns_independent_dict(self):
        with patch.object(self.module.time, "monotonic", return_value=100):
            result = self.client.verify_for_synthesis(VOICE, MODEL)
        result["target_model"] = "tampered"
        with patch.object(self.module.time, "monotonic", return_value=159):
            self.assertEqual(self.client.verify_for_synthesis(VOICE, MODEL)["target_model"], MODEL)
        with patch.object(self.module.time, "monotonic", return_value=160):
            self.client.verify_for_synthesis(VOICE, MODEL)
        self.assertEqual(self.session.post.call_count, 2)

    def test_workspace_key_model_and_voice_config_changes_invalidate_cached_binding(self):
        self.client.verify_for_synthesis(VOICE, MODEL)
        for field, value in [("DASHSCOPE_BASE_URL", "https://other.invalid/api/v1"),
                             ("DASHSCOPE_API_KEY", "second-synthetic-key"),
                             ("TTS_MODEL", "cosyvoice-v3.5-flash"), ("TTS_VOICE", VOICE + "b")]:
            previous = getattr(self.cfg, field)
            setattr(self.cfg, field, value)
            self.client.verify_for_synthesis(VOICE, MODEL)
            setattr(self.cfg, field, previous)
            self.client.verify_for_synthesis(VOICE, MODEL)
        self.assertEqual(self.session.post.call_count, 9)
        key = next(iter(self.module._binding_cache))
        self.assertEqual(len(key[1]), 32)
        self.assertNotIn(self.cfg.DASHSCOPE_API_KEY, repr(self.module._binding_cache))

    def test_switch_to_builtin_and_back_requires_fresh_clone_check(self):
        self.client.verify_for_synthesis(VOICE, MODEL)
        self.cfg.TTS_MODEL, self.cfg.TTS_VOICE = "cosyvoice-v2", "longxiaochun_v2"
        self.client.verify_for_synthesis(self.cfg.TTS_VOICE, self.cfg.TTS_MODEL)
        self.cfg.TTS_MODEL, self.cfg.TTS_VOICE = MODEL, VOICE
        self.client.verify_for_synthesis(VOICE, MODEL)
        self.assertEqual(self.session.post.call_count, 2)

    def test_binding_cache_is_bounded(self):
        for index in range(130):
            self.client.verify_for_synthesis(MODEL + "-test-" + format(index, "032x"), MODEL)
        self.assertEqual(len(self.module._binding_cache), self.module._BINDING_CACHE_LIMIT)

    def test_configuration_change_during_query_stops_before_synthesis(self):
        def change(_voice, **_credentials):
            self.cfg.DASHSCOPE_API_KEY = "changed-synthetic-key"
            return {"voice_id": VOICE, "status": "OK", "target_model": MODEL}
        with patch.object(self.client, "query", side_effect=change), self.assertRaises(ValueError):
            self.client.verify_for_synthesis(VOICE, MODEL)
        self.assertEqual(self.module._binding_cache, {})

    def test_delete_invalidates_verified_resource_without_retry(self):
        self.client.verify_for_synthesis(VOICE, MODEL)
        self.cfg.TTS_VOICE = ""
        self.session.post.return_value = Response({})
        self.assertTrue(self.client.delete(VOICE)["deleted"])
        self.assertEqual(self.module._binding_cache, {})
        self.assertEqual(self.session.post.call_args.kwargs["json"]["input"]["action"], "delete_voice")

    def test_redirect_not_followed_for_voice_actions(self):
        response = Response({}, status=302)
        self.session.post.return_value = response
        with self.assertRaises(RuntimeError):
            self.client.query(VOICE)
        self.session.post.assert_called_once()
        self.assertFalse(self.session.post.call_args.kwargs["allow_redirects"])
        self.assertTrue(response.closed)

    def test_automatic_tts_wrong_binding_blocks_sdk_and_paid_http(self):
        self.session.post.return_value = Response({"status": "OK", "target_model": "cosyvoice-v2"})
        sdk, constructor, speech = self.sdk()
        with sdk, patch.object(self.tts, "_http_audio") as fallback:
            self.assertIsNone(self.tts.synthesize_voice("你好"))
        constructor.assert_not_called()
        speech.assert_not_called()
        fallback.assert_not_called()
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["input"]["action"], "query_voice")

    def test_sdk_receives_explicit_workspace_url_and_verified_model(self):
        sdk, constructor, speech = self.sdk()
        with sdk:
            self.assertTrue(Path(self.tts.synthesize_voice("你好")).is_file())
        self.assertEqual(constructor.call_args.kwargs["url"], "wss://synthetic.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference")
        self.assertEqual(constructor.call_args.kwargs["model"], MODEL)
        self.assertEqual(constructor.call_args.kwargs["voice"], VOICE)
        speech.assert_called_once()
        self.session.post.assert_called_once()

    def test_preview_and_auto_tts_share_one_read_only_binding_query(self):
        sdk, constructor, speech = self.sdk()
        with sdk, patch.dict(sys.modules, {"tts": self.tts}):
            result = self.client.preview("试听", VOICE, MODEL)
            self.assertEqual(result["model"], MODEL)
            self.assertEqual(result["voice_id"], VOICE)
            self.assertTrue((Path(self.cfg.TTS_OUTPUT_DIR) / result["audio_id"]).is_file())
            self.assertTrue(self.tts.synthesize_voice("又一句"))
        self.session.post.assert_called_once()
        self.assertEqual(speech.call_count, 2)

    def test_sdk_bad_audio_does_not_retry_through_paid_http(self):
        sdk, constructor, speech = self.sdk(audio=b"invalid")
        with sdk, patch.object(self.tts, "_http_audio") as fallback:
            self.assertIsNone(self.tts.synthesize_voice("你好"))
        speech.assert_called_once()
        fallback.assert_not_called()
        self.session.post.assert_called_once()

    def test_http_fallback_also_requires_verified_model(self):
        with patch.dict(sys.modules, {"dashscope": None}), patch.object(self.tts, "_http_audio", return_value=wav_bytes()) as fallback:
            self.assertTrue(self.tts.synthesize_voice("你好"))
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.args[1:3], (MODEL, VOICE))
        self.session.post.assert_called_once()

    def test_sdk_uses_one_snapshot_when_hot_config_changes_after_verification(self):
        original = self.module.verify_for_synthesis
        initial_base, initial_key = self.cfg.DASHSCOPE_BASE_URL, self.cfg.DASHSCOPE_API_KEY
        def switch_after_verify(*args, **kwargs):
            info = original(*args, **kwargs)
            self.cfg.DASHSCOPE_BASE_URL = "https://second-workspace.invalid/api/v1"
            self.cfg.DASHSCOPE_API_KEY = "second-synthetic-key"
            self.cfg.TTS_MODEL, self.cfg.TTS_VOICE = "cosyvoice-v2", "longxiaochun_v2"
            self.cfg.TTS_SPEECH_RATE, self.cfg.TTS_PITCH_RATE, self.cfg.TTS_VOLUME = 1.9, 1.8, 95
            return info
        sdk, constructor, speech = self.sdk()
        with sdk, patch.object(self.module, "verify_for_synthesis", side_effect=switch_after_verify):
            self.assertTrue(self.tts.synthesize_voice("你好"))
        request = self.session.post.call_args
        self.assertTrue(request.args[0].startswith(initial_base + "/"))
        self.assertEqual(request.kwargs["headers"]["Authorization"], "Bearer " + initial_key)
        fields = constructor.call_args.kwargs
        self.assertEqual(fields["url"], "wss://synthetic.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference")
        self.assertEqual(fields["headers"]["Authorization"], "Bearer " + initial_key)
        self.assertEqual((fields["voice"], fields["model"]), (VOICE, MODEL))
        self.assertEqual((fields["speech_rate"], fields["pitch_rate"], fields["volume"]), (1.0, 1.0, 50))
        speech.assert_called_once()

    def test_http_uses_one_snapshot_when_hot_config_changes_after_verification(self):
        original = self.module.verify_for_synthesis
        initial_base, initial_key = self.cfg.DASHSCOPE_BASE_URL, self.cfg.DASHSCOPE_API_KEY
        self.session.post.side_effect = [Response({"status": "OK", "target_model": MODEL}), Response(wav_bytes())]
        def switch_after_verify(*args, **kwargs):
            info = original(*args, **kwargs)
            self.cfg.DASHSCOPE_BASE_URL = "https://second-workspace.invalid/api/v1"
            self.cfg.DASHSCOPE_API_KEY = "second-synthetic-key"
            self.cfg.TTS_MODEL, self.cfg.TTS_VOICE = "cosyvoice-v2", "longxiaochun_v2"
            self.cfg.TTS_SPEECH_RATE, self.cfg.TTS_PITCH_RATE, self.cfg.TTS_VOLUME = 1.9, 1.8, 95
            return info
        with patch.dict(sys.modules, {"dashscope": None}), patch.object(self.module, "verify_for_synthesis", side_effect=switch_after_verify):
            self.assertTrue(self.tts.synthesize_voice("你好"))
        self.assertEqual(self.session.post.call_count, 2)
        for request in self.session.post.call_args_list:
            self.assertTrue(request.args[0].startswith(initial_base + "/"))
            self.assertEqual(request.kwargs["headers"]["Authorization"], "Bearer " + initial_key)
        fields = self.session.post.call_args.kwargs["json"]
        self.assertEqual((fields["model"], fields["input"]["voice"]), (MODEL, VOICE))
        self.assertEqual((fields["input"]["rate"], fields["input"]["pitch"], fields["input"]["volume"]), (1.0, 1.0, 50))

    def test_query_uses_captured_credentials_even_if_hot_config_changes_before_post(self):
        original = self.module._request_config
        initial_base, initial_key = self.cfg.DASHSCOPE_BASE_URL, self.cfg.DASHSCOPE_API_KEY
        def switch_before_query(api_key=None, base_url=None, config_identity=None):
            result = original(api_key, base_url, config_identity)
            if api_key is not None and base_url is not None:
                self.cfg.DASHSCOPE_BASE_URL = "https://second-workspace.invalid/api/v1"
                self.cfg.DASHSCOPE_API_KEY = "second-synthetic-key"
            return result
        with patch.object(self.module, "_request_config", side_effect=switch_before_query):
            self.client.verify_for_synthesis(VOICE, MODEL, api_key=initial_key, base_url=initial_base, config_identity=(MODEL, VOICE))
        request = self.session.post.call_args
        self.assertTrue(request.args[0].startswith(initial_base + "/"))
        self.assertEqual(request.kwargs["headers"]["Authorization"], "Bearer " + initial_key)

    def test_delete_during_query_rejects_old_authorization_and_cache_refill(self):
        self.cfg.TTS_VOICE = ""
        def post(_url, **kwargs):
            action = kwargs["json"]["input"]["action"]
            if action == "query_voice":
                self.client.delete(VOICE)
                return Response({"status": "OK", "target_model": MODEL})
            self.assertEqual(action, "delete_voice")
            return Response({})
        self.session.post.side_effect = post
        with self.assertRaisesRegex(ValueError, "资源已变更"):
            self.client.verify_for_synthesis(VOICE, MODEL)
        self.assertEqual(self.module._binding_cache, {})
        self.assertEqual(self.module._binding_epoch, 1)
        self.assertEqual(self.session.post.call_count, 2)

    def test_create_during_query_rejects_old_authorization_and_cache_refill(self):
        def post(_url, **kwargs):
            action = kwargs["json"]["input"]["action"]
            if action == "query_voice":
                self.client.create("https://example.invalid/sample.wav")
                return Response({"status": "OK", "target_model": MODEL})
            self.assertEqual(action, "create_voice")
            return Response({"voice_id": VOICE})
        self.session.post.side_effect = post
        with self.assertRaisesRegex(ValueError, "资源已变更"):
            self.client.verify_for_synthesis(VOICE, MODEL)
        self.assertEqual(self.module._binding_cache, {})
        self.assertEqual(self.module._binding_epoch, 1)

    def test_synthesis_network_calls_do_not_hold_config_binding_or_sdk_lock(self):
        def check_unlocked():
            self.assertFalse(self.cfg._config_lock._is_owned())
            self.assertFalse(self.module._binding_lock._is_owned())
            self.assertFalse(self.tts._sdk_construction_lock.locked())
        def post(_url, **kwargs):
            check_unlocked()
            return Response({"status": "OK", "target_model": MODEL})
        self.session.post.side_effect = post
        sdk, constructor, speech = self.sdk()
        def call(*_args, **_kwargs):
            check_unlocked()
            return wav_bytes()
        speech.side_effect = call
        with sdk:
            self.assertTrue(self.tts.synthesize_voice("你好"))
        speech.assert_called_once()

    def test_completed_sdk_streaming_wav_sentinel_is_sealed_without_second_request(self):
        header = bytes.fromhex("52494646bfffff7f57415645666d74201000000001000100c05d000080bb000002001000646174619bffff7f")
        pcm = b"\x00\x00" * 76800
        sdk, constructor, speech = self.sdk(audio=header + pcm, response={"header": {"event": "task-finished"}})
        with sdk, patch.object(self.tts, "_http_audio") as fallback:
            path = self.tts.synthesize_voice("你好")
        with wave.open(path, "rb") as source:
            self.assertEqual((source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getnframes()), (1, 2, 24000, 76800))
            self.assertEqual(source.readframes(76800), pcm)
        self.assertEqual(Path(path).stat().st_size, 153644)
        speech.assert_called_once()
        fallback.assert_not_called()
        self.session.post.assert_called_once()

    def test_streaming_wav_sentinel_requires_unambiguous_success(self):
        header = bytes.fromhex("52494646bfffff7f57415645666d74201000000001000100c05d000080bb000002001000646174619bffff7f")
        for response in [None, {}, {"header": {"event": "task-started"}}, {"header": {"event": "task-failed"}},
                         {"header": {"event": "task-finished", "error_code": "synthetic_error"}},
                         {"header": {"event": "task-finished"}, "code": "synthetic_error"}]:
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.tts._finalize_sdk_wav(header + b"\x00\x00", response)

    def test_streaming_wav_empty_odd_oversized_or_unknown_header_never_repaired(self):
        header = bytes.fromhex("52494646bfffff7f57415645666d74201000000001000100c05d000080bb000002001000646174619bffff7f")
        finished = {"header": {"event": "task-finished"}}
        for pcm in [b"", b"\x00"]:
            with self.assertRaises(ValueError):
                self.tts._finalize_sdk_wav(header + pcm, finished)
        with patch.object(self.tts, "_audio_limit", return_value=45), self.assertRaises(ValueError):
            self.tts._finalize_sdk_wav(header + b"\x00\x00", finished)
        malformed = bytearray(header + b"\x00\x00")
        malformed[24] = 1  # Different sample rate; not the known canonical header.
        with self.assertRaises(ValueError):
            self.tts._finalize_sdk_wav(malformed, finished)

    def test_normal_and_truncated_wav_keep_original_strict_validation(self):
        good = wav_bytes()
        finished = json.dumps({"header": {"event": "task-finished"}})
        self.assertEqual(self.tts._finalize_sdk_wav(good, finished), good)
        self.assertEqual(self.tts._finalize_sdk_wav(good), good)
        with self.assertRaises(ValueError):
            self.tts._finalize_sdk_wav(good[:-2], finished)
        with self.assertRaises(ValueError):
            self.tts._finalize_sdk_wav(good, {"header": {"event": "task-failed"}})

    def test_sdk_partial_sentinel_failure_does_not_retry_or_write_output(self):
        header = bytes.fromhex("52494646bfffff7f57415645666d74201000000001000100c05d000080bb000002001000646174619bffff7f")
        sdk, constructor, speech = self.sdk(audio=header + b"\x00\x00", response={"header": {"event": "task-failed"}})
        with sdk, patch.object(self.tts, "_http_audio") as fallback:
            self.assertIsNone(self.tts.synthesize_voice("你好"))
        self.assertFalse(Path(self.cfg.TTS_OUTPUT_DIR).exists())
        speech.assert_called_once()
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
