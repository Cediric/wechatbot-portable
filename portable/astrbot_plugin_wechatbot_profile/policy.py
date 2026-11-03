"""Pure policy shared by bootstrap/plugin. AstrBot is the sole chat caller."""
from __future__ import annotations
import copy
import builtins
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit

_bridge_modules = {}
_bridge_import_lock = threading.RLock()


def _bridge_module(bridge, name):
    """Load local code and its flat local imports without global name reuse."""
    source = bridge / (name + ".py")
    if not source.is_file() or source.resolve(strict=True) != source:
        raise ValueError("本包主模型策略模块位置无效")
    origin = os.path.normcase(str(source))
    with _bridge_import_lock:
        if origin in _bridge_modules:
            return _bridge_modules[origin]
        module_name = "wechatbot_policy_bridge_" + hashlib.sha256(origin.encode("utf-8")).hexdigest()
        spec = importlib.util.spec_from_file_location(module_name, source)
        module = importlib.util.module_from_spec(spec)
        original_import = builtins.__import__
        def owned_import(import_name, globals=None, locals=None, fromlist=(), level=0):
            if not level and "." not in import_name and (bridge / (import_name + ".py")).is_file():
                return _bridge_module(bridge, import_name)
            return original_import(import_name, globals, locals, fromlist, level)
        module.__dict__["__builtins__"] = {**vars(builtins), "__import__": owned_import}
        _bridge_modules[origin] = module
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            _bridge_modules.pop(origin, None)
            sys.modules.pop(module_name, None)
            raise
        return module


def _model_client(package_root=None):
    supplied = Path(package_root) if package_root is not None else Path(__file__).resolve().parents[2]
    if not supplied.is_absolute():
        raise ValueError("本包主模型根目录必须是绝对路径")
    root = supplied.resolve(strict=True)
    bridge = root / "wechat-weflow-bridge-ob11"
    manifest = root / "portable" / "runtime-manifest.json"
    if not manifest.is_file() or manifest.resolve(strict=True) != manifest or bridge.resolve(strict=True) != bridge:
        raise ValueError("本包主模型策略模块位置无效")
    return _bridge_module(bridge, "model_client")

def bridge_modules(package_root=None):
    module = _model_client(package_root)
    return module.profile_settings, module.reasoning_options, module.trim_messages

def main_profile(settings, package_root=None):
    profile_settings, reasoning_options, _ = bridge_modules(package_root)
    profile = profile_settings("main", settings)
    profile["reasoning_options"] = reasoning_options(profile)
    return profile

def profile_digest(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]

def profile_ready(profile):
    local = urlsplit(profile.get("api_base", "")).hostname in {"localhost", "127.0.0.1", "::1"}
    return bool(profile.get("api_base") and profile.get("model") and (profile.get("api_key") or local))

def media_identity_note(raw_message):
    """Keep each current media item tied to the bridge's real actor/session IDs."""
    if not isinstance(raw_message, dict): return ""
    records = []
    for index, segment in enumerate(raw_message.get("message", [])[:64]):
        if not isinstance(segment, dict) or segment.get("type") not in {"image", "video", "record"}: continue
        data = segment.get("data", {})
        if not isinstance(data, dict): continue
        values = {key: str(data.get(key, "")) for key in ("wechat_session_id", "wechat_actor_id", "wechat_message_id")}
        if all(values.values()) and all(len(v) <= 160 and not any(ord(c)<32 for c in v) for v in values.values()):
            records.append({"segment_index": index, "media_type": segment["type"], **values})
    return "[本条媒体真实身份关联]\n"+json.dumps(records,ensure_ascii=False,separators=(",",":")) if records else ""

def provider_config(settings, provider_id="wechatbot-main", package_root=None):
    profile = main_profile(settings, package_root)
    extra = dict(profile["reasoning_options"])
    extra["max_tokens"] = profile["max_tokens"]
    extra.update(_model_client(package_root).sampling_options(profile, profile["reasoning_options"]))
    local = urlsplit(profile["api_base"]).hostname in {"localhost", "127.0.0.1", "::1"}
    # The pinned OpenAI SDK requires a nonempty key even for a local no-auth API.
    # This synthetic marker is only for loopback; it is never saved as a user key.
    keys = [profile["api_key"]] if profile["api_key"] else ["local-no-key"] if local else []
    return {"id": provider_id, "provider": "openai", "type": "openai_chat_completion",
        "provider_type": "chat_completion", "enable": profile_ready(profile),
        "api_base": profile["api_base"], "key": keys,
        "model": profile["model"], "timeout": profile["timeout"], "max_context_tokens": profile["context_tokens"],
        "custom_extra_body": extra, "modalities": ["text", "image"], "proxy": "", "custom_headers": {}}

def apply_request(request, profile, package_root=None, *, defer_history_compression=False):
    prompt = str(profile.get("prompt", "")).strip()
    if prompt:
        marker = "[微信bot 主模型提示词]"
        if marker not in request.system_prompt:
            request.system_prompt = (request.system_prompt + "\n\n" + marker + "\n" + prompt).strip()
    # A native AstrBot persona may contain our marker. That cannot suppress
    # the fixed collaboration contract independently supplied by the profile.
    fixed_workflow = profile.get("workflow_prompt", "")
    if fixed_workflow and fixed_workflow not in request.system_prompt:
        request.system_prompt = (request.system_prompt + "\n\n" + fixed_workflow).strip()
    request.model = profile["model"]
    if defer_history_compression:
        # The async owned-session adapter must see complete old turns before
        # summarizing them. prepare_context owns the one isolated history copy
        # and validates system/current/media and output budget.
        try:
            from .context_summary import SUMMARY_BOUNDARY
        except ImportError:
            from context_summary import SUMMARY_BOUNDARY
        boundary_marker = "[微信bot 历史摘要边界]"
        if boundary_marker not in request.system_prompt:
            request.system_prompt = (request.system_prompt + "\n\n" + boundary_marker + "\n" + SUMMARY_BOUNDARY).strip()
        return request
    _, _, trim_messages = bridge_modules(package_root)
    current_parts = [{"type": "text", "text": request.prompt or ""}]
    current_parts += [{"type": "image_url", "image_url": {"url": x}} for x in (request.image_urls or [])]
    for part in getattr(request, "extra_user_content_parts", []) or []:
        if isinstance(part, dict): current_parts.append(copy.deepcopy(part))
        elif hasattr(part, "model_dump_for_context"): current_parts.append(part.model_dump_for_context())
        else: raise ValueError("额外多模态输入格式无效")
    messages = ([{"role": "system", "content": request.system_prompt}] if request.system_prompt else [])
    messages += copy.deepcopy(request.contexts or [])
    messages.append({"role": "user", "content": current_parts})
    trimmed = trim_messages(messages, profile["context_tokens"], profile["max_tokens"])
    # Preserve the real image references, current media and all system/fact text.
    if not trimmed or trimmed[-1].get("role") != "user":
        raise ValueError("主模型上下文预算无效")
    current = trimmed.pop()["content"]
    request.prompt = next((p["text"] for p in current if p.get("type") == "text"), "")
    if messages and messages[0].get("role") == "system":
        if not trimmed or trimmed[0].get("content") != request.system_prompt:
            raise ValueError("主模型上下文过短，无法完整保留系统提示词")
        trimmed.pop(0)
    request.contexts = trimmed
    return request
