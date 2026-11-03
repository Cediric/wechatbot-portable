"""Explicit CosyVoice enrollment management; no automatic record uploading.

Protocol: https://help.aliyun.com/en/model-studio/voice-clone-design-http-api
Creation and preview are paid actions invoked by the authenticated panel only.
"""
from __future__ import annotations
import json
import re
import ipaddress
import hashlib
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from contextlib import nullcontext

import config
import http_util
import media_util


_BINDING_TTL_SECONDS = 60
_BINDING_CACHE_LIMIT = 128
_binding_cache = {}
_binding_lock = threading.RLock()
_active_scope = None
_binding_epoch = 0


def _voice_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", value):
        raise ValueError("音色 ID 无效")
    return value


def _target_model(value, *, optional=False):
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value):
        raise ValueError("音色目标模型无法核验")
    return value


def _request_config(api_key=None, base_url=None, config_identity=None):
    # Configuration writes use this lock; it is released before any network I/O.
    with getattr(config, "_config_lock", nullcontext()):
        key = getattr(config, "DASHSCOPE_API_KEY", "") if api_key is None else api_key
        base = getattr(config, "DASHSCOPE_BASE_URL", "") if base_url is None else base_url
        identity = (str(getattr(config, "TTS_MODEL", "")), str(getattr(config, "TTS_VOICE", ""))) if config_identity is None else config_identity
    if not isinstance(key, str) or not isinstance(base, str):
        raise ValueError("DashScope 配置无效")
    return base.rstrip("/"), key, tuple(identity)


def _scope_for_request(base, key, identity):
    # Neither the cache nor its diagnostics retain the raw API key.
    key_hash = hashlib.sha256(key.encode("utf-8")).digest()
    return (base, key_hash, *identity)


def _configuration_scope():
    return _scope_for_request(*_request_config())


def _invalidate_bindings():
    global _binding_epoch
    with _binding_lock:
        _binding_cache.clear()
        _binding_epoch += 1


def verify_for_synthesis(voice_id, model, *, api_key=None, base_url=None, config_identity=None):
    """Validate an enrollment voice before SDK or HTTP synthesis, without retries."""
    return VoiceCloningClient().verify_for_synthesis(voice_id, model, api_key=api_key,
        base_url=base_url, config_identity=config_identity)


def _audio_url(value):
    value = media_util.validate_http_url(value)
    parsed = urlsplit(value)
    host = parsed.hostname.lower().rstrip(".")
    if parsed.scheme != "https" or host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost")):
        raise ValueError("克隆录音需为公开 HTTPS 地址，不能使用本地文件")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if re.fullmatch(r"[0-9.:]+", host):
            raise ValueError("克隆录音地址无效") from None
    else:
        if not address.is_global:
            raise ValueError("克隆录音不能为私网地址")
    return value


class VoiceCloningClient:
    def _call(self, action, *, api_key=None, base_url=None, **fields):
        base, key, _ = _request_config(api_key, base_url)
        if not key:
            raise ValueError("请先配置 DashScope API 密钥")
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("DashScope API 地址无效")
        response = http_util.session().post(base + "/services/audio/tts/customization",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={"model": "voice-enrollment", "input": {"action": action, **fields}},
            timeout=(5, 40), stream=True, allow_redirects=False)
        try:
            if response.status_code != 200:
                raise RuntimeError("音色服务 HTTP " + str(response.status_code))
            data = json.loads(media_util.response_bytes(response, max_bytes=512*1024, timeout=40).decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("output"), dict) or data.get("code"):
                raise RuntimeError("音色服务返回格式无效")
            return data["output"]
        finally:
            response.close()

    def create(self, url, prefix="myvoice", target_model=None, *, language_hints=None,
               max_prompt_audio_length=None, enable_preprocess=False, enable_volume_normalization=False):
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9]{1,10}", prefix):
            raise ValueError("音色前缀只能为 1–10 位英文或数字")
        model = str(target_model or getattr(config, "TTS_MODEL", ""))
        if not re.fullmatch(r"cosyvoice-v(?:2|3(?:\.5)?-(?:plus|flash))", model):
            raise ValueError("音色复刻目前支持 CosyVoice v2 / v3 / v3.5，需与合成模型一致")
        if type(enable_preprocess) is not bool or type(enable_volume_normalization) is not bool:
            raise ValueError("录音预处理开关无效")
        fields = {"url": _audio_url(url), "prefix": prefix, "target_model": model}
        advanced = model in {"cosyvoice-v3.5-plus", "cosyvoice-v3.5-flash", "cosyvoice-v3-flash"}
        if enable_preprocess:
            if not advanced:
                raise ValueError("此模型不支持录音预处理")
            fields["enable_preprocess"] = True
        if max_prompt_audio_length is not None:
            if not advanced or isinstance(max_prompt_audio_length, bool) or not isinstance(max_prompt_audio_length, (float, int)) or not 3 <= max_prompt_audio_length <= 30:
                raise ValueError("此模型不支持所选录音截取时长，范围为 3–30 秒")
            fields["max_prompt_audio_length"] = float(max_prompt_audio_length)
        if language_hints:
            if model == "cosyvoice-v2" or not isinstance(language_hints, list) or len(language_hints) != 1 or language_hints[0] not in {"zh", "en", "fr", "de", "ja", "ko", "ru", "pt", "th", "id", "vi"}:
                raise ValueError("录音语言提示无效或当前模型不支持")
            if model == "cosyvoice-v3-plus" and language_hints[0] not in {"zh", "en", "fr", "de", "ja", "ko", "ru"}:
                raise ValueError("CosyVoice v3-plus 不支持所选录音语言提示")
            fields["language_hints"] = language_hints
        fields["enable_volume_normalization"] = "true" if enable_volume_normalization else "false"
        output = self._call("create_voice", **fields)
        voice_id = _voice_id(output.get("voice_id"))
        # The requested target_model is the binding; an ID is not a model parser.
        returned_model = _target_model(output.get("target_model"), optional=True)
        if returned_model and returned_model != model:
            raise RuntimeError("克隆音色返回的目标模型与请求不匹配")
        _invalidate_bindings()
        return {"voice_id": voice_id, "target_model": model, "status": "DEPLOYING"}

    def list(self, prefix="", page_index=0, page_size=10):
        if prefix and (not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9]{1,10}", prefix)):
            raise ValueError("音色前缀无效")
        if type(page_index) is not int or not 0 <= page_index <= 1000 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("音色分页参数无效")
        output = self._call("list_voice", prefix=prefix, page_index=page_index, page_size=page_size)
        rows = output.get("voice_list")
        if not isinstance(rows, list) or len(rows) > page_size or any(not isinstance(item, dict) for item in rows):
            raise RuntimeError("音色列表返回格式无效")
        return [{"voice_id": _voice_id(item.get("voice_id")), "status": str(item.get("status", "unknown"))[:32],
                 "target_model": _target_model(item.get("target_model"), optional=True),
                 "gmt_create": str(item.get("gmt_create", ""))[:64], "gmt_modified": str(item.get("gmt_modified", ""))[:64]}
                for item in rows if isinstance(item, dict)]

    def query(self, voice_id, *, api_key=None, base_url=None):
        voice_id = _voice_id(voice_id)
        output = self._call("query_voice", voice_id=voice_id, api_key=api_key, base_url=base_url)
        status = output.get("status")
        if status not in {"OK", "DEPLOYING", "UNDEPLOYED"}:
            raise RuntimeError("音色状态无法核验")
        if "voice_id" in output and _voice_id(output["voice_id"]) != voice_id:
            raise RuntimeError("音色服务返回的 ID 与查询不匹配")
        return {"voice_id": voice_id, "status": status, "target_model": _target_model(output.get("target_model")),
                "gmt_create": str(output.get("gmt_create", ""))[:64], "gmt_modified": str(output.get("gmt_modified", ""))[:64]}

    def verify_for_synthesis(self, voice_id, model, *, api_key=None, base_url=None, config_identity=None):
        voice_id, model = _voice_id(voice_id), _target_model(model)
        global _active_scope
        explicit_credentials = api_key is not None and base_url is not None
        base, key, identity = _request_config(api_key, base_url, config_identity)
        scope = _scope_for_request(base, key, identity)
        with _binding_lock:
            if _active_scope != scope:
                _binding_cache.clear()
                _active_scope = scope
            epoch = _binding_epoch
        enrolled = voice_id.startswith(("cosyvoice-", "qwen-audio-")) or "." in voice_id or model.startswith("cosyvoice-v3.5")
        if not enrolled:
            return None  # System voices do not have voice-enrollment resources.
        if not re.fullmatch(r"cosyvoice-v(?:2|3(?:\.5)?-(?:plus|flash))", model):
            raise ValueError("当前合成流程仅支持 CosyVoice 复刻音色及其绑定模型")
        cache_key = (scope[0], scope[1], voice_id, model)
        with _binding_lock:
            now = time.monotonic()
            for cached_key, (expires, _) in list(_binding_cache.items()):
                if expires <= now:
                    del _binding_cache[cached_key]
            cached = _binding_cache.get(cache_key)
            if cached:
                if _binding_epoch != epoch:
                    raise ValueError("音色资源已变更，请重新核验")
                return dict(cached[1])
        info = self.query(voice_id, api_key=key, base_url=base)
        if info["status"] != "OK" or info["target_model"] != model:
            raise ValueError("克隆音色尚未就绪或合成模型不匹配")
        with _binding_lock:
            if _binding_epoch != epoch:
                raise ValueError("音色资源已变更，请重新核验")
            if not explicit_credentials and _configuration_scope() != scope:
                raise ValueError("语音配置已变更，请重新核验音色")
            if len(_binding_cache) >= _BINDING_CACHE_LIMIT:
                _binding_cache.pop(next(iter(_binding_cache)))
            _binding_cache[cache_key] = (time.monotonic() + _BINDING_TTL_SECONDS, dict(info))
        return info

    def delete(self, voice_id):
        voice_id = _voice_id(voice_id)
        if voice_id == getattr(config, "TTS_VOICE", ""):
            raise ValueError("正在使用此音色，请先切换再删除")
        self._call("delete_voice", voice_id=voice_id)
        _invalidate_bindings()
        return {"voice_id": voice_id, "deleted": True}

    def preview(self, text, voice_id=None, model=None, *, speech_rate=None, pitch_rate=None, volume=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise ValueError("试听文本需为 1–300 字")
        selected = voice_id or getattr(config, "TTS_VOICE", "")
        model = str(model or getattr(config, "TTS_MODEL", ""))
        if selected:
            self.verify_for_synthesis(selected, model)
        import tts
        path = tts.synthesize_voice(text, voice=selected or None, model=model, speech_rate=speech_rate,
                                    pitch_rate=pitch_rate, volume=volume)
        if not path:
            raise RuntimeError("音色试听合成失败")
        target, root = Path(path).resolve(), Path(config.TTS_OUTPUT_DIR).resolve()
        if target.parent != root or target.suffix != ".wav" or not re.fullmatch(r"tts_[0-9]+_[0-9a-f]{32}\.wav", target.name):
            raise RuntimeError("试听文件不在受管目录")
        return {"audio_id": target.name, "voice_id": selected, "model": model}

    def read_preview(self, audio_id):
        if not isinstance(audio_id, str) or not re.fullmatch(r"tts_[0-9]+_[0-9a-f]{32}\.wav", audio_id):
            raise ValueError("试听文件 ID 无效")
        path = Path(config.TTS_OUTPUT_DIR) / audio_id
        if path.is_symlink() or path.resolve().parent != Path(config.TTS_OUTPUT_DIR).resolve() or not path.is_file():
            raise ValueError("试听文件不存在")
        import tts
        if not 0 < path.stat().st_size <= tts._audio_limit():
            raise ValueError("试听文件超过大小上限")
        return bytes(tts._validate_wav(path.read_bytes()))
