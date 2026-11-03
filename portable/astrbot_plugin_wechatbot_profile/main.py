"""Versioned per-event main-model profiles; no second LLM/image-caption call."""
from __future__ import annotations
import asyncio
import copy
import json
import os
from pathlib import Path
import time
import types
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderRequest
from .policy import apply_request, main_profile, media_identity_note, profile_digest, profile_ready, provider_config
from .streaming import bind_streaming, stop_superseded_runs, verified_identity_note, verified_reply_plan_note, verified_delivery_note, log_latency
from .context_summary import ContextBudgetError, messages_cost, prepare_context

MAX_PROFILE_VERSIONS = 32


def bind_context_guard(provider, profile):
    """Check the SDK's finished payload, including tools/reasoning, before HTTP."""
    if getattr(provider, "_wechatbot_budget_guard", False):
        return
    original = getattr(provider, "_finally_convert_payload", None)
    resource = getattr(getattr(getattr(provider,"client",None),"chat",None),"completions",None)
    create = getattr(resource,"create",None)
    if not callable(original) and not callable(create):
        return
    cap = int(profile["context_tokens"])
    output = int(profile["max_tokens"])
    def check(payload, extra):
        reserve = max(output, int(payload.get("max_tokens", extra.get("max_tokens", output)) or output),
                      int(payload.get("max_completion_tokens", extra.get("max_completion_tokens", output)) or output))
        cost = messages_cost(payload.get("messages", [])) + len(json.dumps(payload.get("tools", []), ensure_ascii=False).encode("utf-8")) + reserve
        if cost > cap:
            raise ContextBudgetError("主模型最终请求超出上下文预算，已停止调用；请减少当前附件或工具输出")
    # SDK adds function descriptors in _query/_query_stream after its conversion
    # helper. Count only the completed HTTP payload when that boundary exists;
    # recounting the same history during conversion adds no protection.
    if callable(create):
        async def guarded_create(*args, **kwargs):
            payload = dict(kwargs)
            extra = payload.pop("extra_body",{}) or {}
            if isinstance(extra,dict):
                payload.update(extra)
            check(payload,{})
            return await create(*args,**kwargs)
        resource.create = guarded_create
    else:
        def guarded(instance, payload):
            original(payload)
            check(payload,getattr(instance,"provider_config",{}).get("custom_extra_body",{}))
        provider._finally_convert_payload = types.MethodType(guarded, provider)
    provider._wechatbot_budget_guard = True

class ProfileCapacityReached(ValueError):
    pass

@register("wechatbot_profile", "微信bot", "本包主模型实时配置与上下文隔离", "7.0.0")
class WechatbotProfile(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self._lock = asyncio.Lock()
        self._providers = set()
        self._location = None
        self._snapshot = None
        self._stamp = None
        self._last_warning = 0.0
        self._last_status = None

    def _read(self):
        if self._location is None:
            instance = Path(os.environ.get("ASTRBOT_ROOT", ".")).resolve()
            location = json.loads((instance / "data" / "wechatbot-profile-location.json").read_text(encoding="utf-8"))
            root = Path(location["package_root"]).resolve()
            config = Path(location["bridge_config"]).resolve()
            if config != root / "wechat-weflow-bridge-ob11" / "config.json" or not (root / "portable" / "runtime-manifest.json").is_file():
                raise ValueError("本包主模型配置位置无效")
            self._location = {"root": root, "config": config, "platform": location["platform_id"],
                "status": instance / "data" / "wechatbot-profile-status.json"}
        info = self._location["config"].stat()
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp != self._stamp:
            data = json.loads(self._location["config"].read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict): raise ValueError("主模型配置不是对象")
            profile = main_profile(data, self._location["root"])
            self._snapshot = (data, profile)
            self._stamp = stamp
        return self._location, self._snapshot

    def _owned(self, event):
        location, snapshot = self._read()
        return event.get_platform_id() == location["platform"], location, snapshot

    def _warning(self):
        now = time.monotonic()
        if now - self._last_warning > 30:
            self._last_warning = now
            logger.warning("微信bot 主模型配置未就绪；请在微信bot控制面板检查地址、密钥、模型和思考参数。")

    def _status(self, state, reason=""):
        value = (state, reason, len(self._providers))
        if value == self._last_status or not self._location: return
        try:
            path = self._location["status"]
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps({"schema": 1, "state": state, "reason": reason,
                "provider_versions": len(self._providers), "max_provider_versions": MAX_PROFILE_VERSIONS,
                "updated_at": time.time()}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, path)
            self._last_status = value
        except OSError:
            logger.warning("微信bot 主模型状态文件无法写入。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def select_profile(self, event: AstrMessageEvent):
        # Nothing is yielded or sent. The ordinary AstrBot pipeline answers once.
        try:
            owned, location, snapshot = self._owned(event)
            if not owned: return
            settings, profile = snapshot
            if not profile_ready(profile):
                self._status("unconfigured", "请在控制面板填写主模型地址、密钥与模型。")
                self._warning()
                event.stop_event()
                return
            stop_superseded_runs(event)
            provider_id = "wechatbot-main-" + profile_digest(profile)
            async with self._lock:
                if provider_id not in self._providers:
                    # A finite set keeps immutable clients safe for in-flight calls.
                    # Refuse a new generation instead of closing a provider still in use.
                    if len(self._providers) >= MAX_PROFILE_VERSIONS:
                        raise ProfileCapacityReached()
                    manager = self.context.provider_manager
                    await manager.load_provider(provider_config(settings, provider_id, location["root"]))
                    instance = await manager.get_provider_by_id(provider_id)
                    if instance is None:
                        raise ValueError("本包主模型适配器未就绪")
                    bind_context_guard(instance, profile)
                    self._providers.add(provider_id)
            self._status("ready")
            # Each new profile gets its own provider. Running requests keep their instance.
            event.set_extra("selected_provider", provider_id)
            event.set_extra("wechatbot_main_profile", copy.deepcopy(profile))
            event.set_extra("enable_reasoning", False)
            bind_streaming(event, profile)
        except ProfileCapacityReached:
            reason = "主模型配置已累计32个版本，请双击本包根目录“重启AstrBot.bat”，释放旧版本后应用新配置。"
            self._status("restart_required", reason)
            logger.warning("微信bot：" + reason)
            event.stop_event()
        except Exception:
            if event.get_platform_id() == "wechatbot-portable":
                self._status("error", "主模型适配器配置失败，请检查控制面板参数和本包日志。")
                self._warning()
                event.stop_event()

    @filter.on_llm_request(priority=10000)
    async def configure_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if event.get_platform_id() != "wechatbot-portable": return
        profile = event.get_extra("wechatbot_main_profile")
        if not isinstance(profile, dict):
            event.stop_event()
            return
        started = time.monotonic()
        if event.get_extra("wechatbot_main_request_started_at") is None:
            event.set_extra("wechatbot_main_request_started_at", started)
        raw = getattr(event.message_obj, "raw_message", None)
        interests = raw.get("wechat_character_interests") if isinstance(raw, dict) else None
        if isinstance(interests, str) and interests and interests not in (req.system_prompt or ""):
            req.system_prompt = (req.system_prompt or "") + "\n\n" + interests
        note = media_identity_note(raw)
        if note and "[本条媒体真实身份关联]" not in (req.prompt or ""):
            req.prompt = (req.prompt or "") + "\n\n" + note
        identity_note = verified_identity_note(getattr(event.message_obj, "raw_message", None))
        if identity_note and "[本条已核验微信身份]" not in (req.prompt or ""):
            req.prompt = (req.prompt or "") + "\n\n" + identity_note
        plan_note = verified_reply_plan_note(getattr(event.message_obj, "raw_message", None))
        if plan_note and plan_note not in (req.system_prompt or ""):
            req.system_prompt = (req.system_prompt or "") + "\n\n" + plan_note
        delivery_note = verified_delivery_note(getattr(event.message_obj, "raw_message", None))
        if delivery_note and delivery_note not in (req.system_prompt or ""):
            req.system_prompt = (req.system_prompt or "") + "\n\n" + delivery_note
        apply_request(req, profile, self._location["root"], defer_history_compression=True)
        provider = await self.context.provider_manager.get_provider_by_id(event.get_extra("selected_provider"))
        status = await prepare_context(req, profile, provider)
        event.set_extra("wechatbot_context_status", status)
        log_latency(event, "main_context_prepare", time.monotonic() - started)

    async def terminate(self):
        # Retired versions remain available until plugin shutdown so live calls are safe.
        manager = self.context.provider_manager
        for provider_id in self._providers:
            instance = await manager.get_provider_by_id(provider_id)
            if instance is not None and hasattr(instance, "terminate"):
                await instance.terminate()
