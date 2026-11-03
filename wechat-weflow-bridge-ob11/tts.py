"""DashScope speech synthesis with bounded, validated, atomic WAV output.

CosyVoice uses the official realtime SDK when installed; the documented HTTP
interface is used when the SDK is absent. Qwen-TTS uses its own HTTP endpoint.
Cache cleanup uses age rather than file count so queued fresh audio survives.
"""
import base64
import io
import json
import logging
import math
import os
import hashlib
import wave
import threading
import struct
import time
from contextlib import nullcontext

import config
import http_util
import media_util
import tts_cache

log = logging.getLogger("ob11-bridge")
_sdk_construction_lock = threading.Lock()
_prune_lock = threading.Lock()
_prune_key = None
_prune_after = 0.0
# CosyVoice's completed WebSocket WAV may retain this streaming-only length
# sentinel. It is PCM mono/24 kHz/16-bit; other headers are never rewritten.
_SDK_WAV_SENTINEL_HEADER = bytes.fromhex(
    "52494646bfffff7f57415645666d74201000000001000100"
    "c05d000080bb000002001000646174619bffff7f")


def _prune_tts_cache(keep=None, *, directory=None, days=None):
    # keep is accepted for old integrations; it never causes count-based deletion.
    global _prune_key, _prune_after
    try:
        directory = config.TTS_OUTPUT_DIR if directory is None else directory
        days = max(7, float(getattr(config, "CACHE_RETENTION_DAYS", 7) if days is None else days))
        key, now = (os.path.realpath(directory), days), time.monotonic()
        with _prune_lock:
            if key == _prune_key and now < _prune_after:
                return
            _prune_key, _prune_after = key, now + 600
        # A busy cache needs one age sweep per ten minutes, not one full
        # directory scan on every newly synthesized reply.
        media_util.prune_cache(directory, days, prefix="tts_")
    except (OSError, ValueError):
        log.debug("[TTS] 清理过期缓存失败", exc_info=True)


def _audio_limit():
    return min(media_util.media_limit(), 20 * 1024 * 1024)


def _validate_wav(audio):
    if not isinstance(audio, (bytes, bytearray)) or not audio or len(audio) > _audio_limit():
        raise ValueError("语音为空、类型无效或超过大小限制")
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            frames = source.getnframes()
            expected = frames * source.getnchannels() * source.getsampwidth()
            if frames <= 0 or source.getframerate() <= 0 or expected > _audio_limit() or len(source.readframes(frames)) != expected:
                raise ValueError("语音数据为空或不完整")
    except (wave.Error, EOFError) as error:
        raise ValueError("返回内容不是有效的 WAV 语音") from error
    return audio


def _trim_leading_quiet(audio):
    """Shorten a long quiet lead on a validated, canonical 24 kHz PCM16 mono WAV."""
    if (audio[8:40] != _SDK_WAV_SENTINEL_HEADER[8:40]
            or int.from_bytes(audio[40:44], "little") != len(audio) - 44):
        return audio

    # Inspect only the first two seconds. Stop at the first audible peak OR
    # sharp sample transition so a low-level consonant keeps its own pre-roll.
    block = struct.Struct("<240h")  # 10 ms
    previous = 0
    for window in range(min((len(audio) - 44) // block.size, 200)):
        samples = block.unpack_from(audio, 44 + window * block.size)
        peak = max(-min(samples), max(samples))
        if window < 8 and peak > 8:  # require 80 ms of near-digital silence
            return audio
        if (peak > 300 or abs(samples[0] - previous) > 48
                or any(abs(a - b) > 48 for a, b in zip(samples, samples[1:]))):
            break
        previous = samples[-1]
    else:
        return audio
    if window < 25:  # less than 250 ms of quiet
        return audio

    # Keep 70 ms before even the weak onset. Fade only the first 5 ms of
    # retained background to avoid a discontinuity at a nonzero cut sample.
    cut = (window - 7) * block.size
    ready = bytearray(audio[:44])
    ready.extend(memoryview(audio)[44 + cut:])
    for index in range(120):
        offset = 44 + 2 * index
        sample = struct.unpack_from("<h", ready, offset)[0]
        struct.pack_into("<h", ready, offset, round(sample * index / 119))
    struct.pack_into("<I", ready, 4, len(ready) - 8)
    struct.pack_into("<I", ready, 40, len(ready) - 44)
    return bytes(ready)


def _finalize_sdk_wav(audio, response=None):
    """Seal the one known streaming WAV header only after task-finished."""
    if isinstance(response, str):
        if len(response) > 65536:
            raise ValueError("语音任务完成信息无效")
        try:
            response = json.loads(response)
        except (ValueError, TypeError):
            raise ValueError("语音任务完成信息无效") from None
    header = response.get("header") if isinstance(response, dict) else None
    if response is not None and (not isinstance(header, dict) or header.get("event") != "task-finished"
            or header.get("error_code") or header.get("error_message") or response.get("code")):
        raise ValueError("语音任务未确认成功完成")
    if isinstance(audio, (bytes, bytearray)) and audio[:44] == _SDK_WAV_SENTINEL_HEADER:
        if not isinstance(header, dict) or header.get("event") != "task-finished":
            raise ValueError("流式 WAV 尚未确认完成，不能封装")
        size = len(audio) - 44
        if size <= 0 or size % 2 or len(audio) > _audio_limit():
            raise ValueError("流式 WAV 数据为空、不完整或超过大小限制")
        finalized = bytearray(audio)
        struct.pack_into("<I", finalized, 4, len(finalized) - 8)
        struct.pack_into("<I", finalized, 40, size)
        audio = bytes(finalized)
    return _validate_wav(audio)


def _default_voice(model):
    if model.startswith("qwen"):
        return "Cherry"
    if model.startswith("cosyvoice-v2"):
        return "longxiaochun_v2"
    if model.startswith("cosyvoice-v3"):
        if model.startswith("cosyvoice-v3.5"):
            raise ValueError("CosyVoice 3.5 需要配置匹配的克隆或设计音色")
        return "longanyang"
    return "longxiaochun"


def _voice_options(speech_rate=None, pitch_rate=None, volume=None):
    values = {"speech_rate": getattr(config, "TTS_SPEECH_RATE", 1.0) if speech_rate is None else speech_rate,
              "pitch_rate": getattr(config, "TTS_PITCH_RATE", 1.0) if pitch_rate is None else pitch_rate,
              "volume": getattr(config, "TTS_VOLUME", 50) if volume is None else volume}
    for name in ("speech_rate", "pitch_rate"):
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.5 <= value <= 2:
            raise ValueError("语速与音调需在 0.5–2.0 之间")
        values[name] = float(value)
    if type(values["volume"]) is not int or not 0 <= values["volume"] <= 100:
        raise ValueError("音量需为 0–100 的整数")
    return values


def _http_audio(text, model, voice, options=None, *, base_url=None, api_key=None):
    options = options or _voice_options()
    base = (config.DASHSCOPE_BASE_URL if base_url is None else base_url).rstrip("/")
    key = config.DASHSCOPE_API_KEY if api_key is None else api_key
    if model.startswith("qwen"):
        if "realtime" in model or model.startswith("qwen-audio"):
            raise ValueError("当前语音发送支持 CosyVoice 与 Qwen-TTS 非实时模型，请配置对应模型名")
        if len(text) > 600:
            raise ValueError("Qwen-TTS 单次文字不得超过 600 字符")
        url = base + "/services/aigc/multimodal-generation/generation"
        payload = {"model": model, "input": {"text": text, "voice": voice, "language_type": "Auto"}}
        if options != {"speech_rate": 1.0, "pitch_rate": 1.0, "volume": 50}:
            raise ValueError("当前 Qwen-TTS HTTP 模型不支持 CosyVoice 的语速/音调/音量参数，请使用 CosyVoice")
    elif model.startswith("cosyvoice"):
        if model == "cosyvoice-v1":
            raise ValueError("CosyVoice v1 需要安装 DashScope SDK")
        url = base + "/services/audio/tts/SpeechSynthesizer"
        # CosyVoice HTTP uses input.rate/input.pitch, while its Python SDK uses
        # speech_rate/pitch_rate. Do not send SDK field names to this endpoint.
        payload = {"model": model, "input": {"text": text, "voice": voice, "format": "wav", "sample_rate": 24000,
            "rate": options["speech_rate"], "pitch": options["pitch_rate"], "volume": options["volume"]}}
    else:
        raise ValueError("不支持的 TTS 模型，请配置 CosyVoice 或 Qwen-TTS")
    response = http_util.session().post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=payload, timeout=(5, 60), stream=True, allow_redirects=False)
    try:
        if response.status_code != 200:
            log.warning("[TTS] HTTP 合成失败: %s", response.status_code)
            return None
        raw = media_util.response_bytes(response, max_bytes=_audio_limit())
        if raw[:4] == b"RIFF":
            return _validate_wav(raw)
        data = json.loads(raw.decode("utf-8"))
        output = data.get("output", {}) if isinstance(data, dict) else {}
        audio = output.get("audio", {}) if isinstance(output, dict) else {}
        # Older compatible servers may return a base64 string or output.url.
        encoded = audio if isinstance(audio, str) else audio.get("data", "") if isinstance(audio, dict) else ""
        audio_url = audio.get("url", "") if isinstance(audio, dict) else ""
        audio_url = audio_url or output.get("url", "")
        if encoded:
            if len(encoded) > _audio_limit() * 4 / 3 + 8:
                raise ValueError("编码语音超过大小限制")
            return _validate_wav(base64.b64decode(encoded, validate=True))
        if audio_url:
            media_util.validate_http_url(audio_url)
            downloaded = http_util.session().get(audio_url, timeout=(5, 30), stream=True)
            try:
                downloaded.raise_for_status()
                return _validate_wav(media_util.response_bytes(downloaded, max_bytes=_audio_limit()))
            finally:
                downloaded.close()
        raise ValueError("响应中没有语音数据或下载地址")
    finally:
        response.close()


def synthesize_voice(text, output_path="", *, voice=None, model=None, speech_rate=None, pitch_rate=None, volume=None, cancelled=None):
    if cancelled is not None and cancelled():
        return None
    if not isinstance(text, str) or not text.strip():
        log.warning("[TTS] 文本为空，跳过语音合成")
        return None
    if len(text) > 20000:
        log.warning("[TTS] 单次语音文字超过 20000 字符")
        return None
    try:
        # Snapshot the complete request while config writes are excluded, then
        # release the short lock before verification or synthesis network I/O.
        with getattr(config, "_config_lock", nullcontext()):
            provider = config.TTS_PROVIDER
            key, base = config.DASHSCOPE_API_KEY, str(config.DASHSCOPE_BASE_URL).rstrip("/")
            config_identity = (str(config.TTS_MODEL), str(config.TTS_VOICE))
            model = str(config.TTS_MODEL if model is None else model).strip()
            voice = str((config.TTS_VOICE if voice is None else voice) or "").strip() or _default_voice(model)
            options = _voice_options(speech_rate, pitch_rate, volume)
            output_dir = config.TTS_OUTPUT_DIR
            account = str(getattr(config, "BOT_WXID", "") or "")
            retention = max(7, float(getattr(config, "CACHE_RETENTION_DAYS", 7))) * 86400
        if provider != "dashscope" or not key:
            log.warning("[TTS] 语音服务未配置")
            return None
        if not model or not voice or len(model) > 200 or len(voice) > 256:
            raise ValueError("语音模型或音色无效")
        identity = {"version": 2, "text": text, "provider": provider, "base": base,
            "model": model, "voice": voice, **options, "format": "wav", "sample_rate": 24000,
            "channels": 1, "sample_width": 2, "language": "Auto",
            "account_fingerprint": hashlib.sha256((str(key)+"\0"+account).encode("utf-8")).hexdigest()}
        def render():
            from voice_cloning import verify_for_synthesis
            verify_for_synthesis(voice, model, api_key=key, base_url=base, config_identity=config_identity)
            if not model.startswith("cosyvoice"):
                return _http_audio(text, model, voice, options, base_url=base, api_key=key)
            try:
                import dashscope
                from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer
            except ImportError:
                log.debug("[TTS] DashScope SDK 不可用，使用 HTTP 合成")
                return _http_audio(text, model, voice, options, base_url=base, api_key=key)
            else:
                ws_url = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1).removesuffix("/api/v1") + "/api-ws/v1/inference"
                # SDK lacks an api_key argument. Its documented custom headers
                # override Authorization, so this request keeps its snapshot key
                # even if another SDK consumer changes the global after init.
                with _sdk_construction_lock:
                    previous_key = getattr(dashscope, "api_key", None)
                    dashscope.api_key = key
                    try:
                        synthesizer = SpeechSynthesizer(model=model, voice=voice, format=AudioFormat.WAV_24000HZ_MONO_16BIT,
                            url=ws_url, headers={"Authorization": "Bearer " + key}, **options)
                    finally:
                        dashscope.api_key = previous_key
                # A failed/oversized SDK result must not trigger a second billable synthesis.
                sdk_audio = synthesizer.call(text, timeout_millis=60000)
                get_response = getattr(synthesizer, "get_response", None)
                response = get_response() if callable(get_response) else None
                return _finalize_sdk_wav(sdk_audio, response)
        def write(path, audio):
            result = media_util.write_bytes_atomic(path, _trim_leading_quiet(audio), max_bytes=_audio_limit())
            _prune_tts_cache(directory=output_dir, days=retention / 86400)
            return result
        result = tts_cache.cache.get_or_create(identity, output_dir, retention, render,
            _validate_wav, write, _audio_limit(), cancelled=cancelled, previous_identity={**identity, "version": 1})
        if not result or (cancelled is not None and cancelled()):
            return None
        if output_path and os.path.abspath(output_path) != result:
            with open(result, "rb") as stream:
                audio = _validate_wav(stream.read(_audio_limit()+1))
            result = media_util.write_bytes_atomic(output_path, audio, max_bytes=_audio_limit())
        log.info("[TTS] 语音已保存: %s", result)
        return result
    except Exception as error:
        log.warning("[TTS] 语音合成失败: %s", error)
        return None


def synthesize_reply_voice(text, output_path="", *, cancelled=None, max_chars=None, **options):
    """Synthesize a completed, speech-appropriate reply once, off the text path.

    The caller owns stream finalization and schedules this helper after its text
    receipts. Cancellation is checked before the paid request and before the
    returned file can be dispatched; an in-flight vendor request is not retried.
    """
    from media_policy import prepare_voice
    is_cancelled = cancelled if callable(cancelled) else lambda: bool(cancelled)
    if is_cancelled():
        return None
    limit = getattr(config, "VOICE_MAX_CHARS", 600) if max_chars is None else max_chars
    plan = prepare_voice(text, max_chars=limit)
    if not plan.allowed:
        log.debug("[TTS] 回复保持文字: %s", plan.reason)
        return None
    if is_cancelled():
        return None
    path = synthesize_voice(plan.text, output_path, cancelled=is_cancelled, **options)
    return None if is_cancelled() else path
