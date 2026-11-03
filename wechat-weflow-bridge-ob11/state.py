"""
微信 ↔ AstrBot 桥接（OneBot v11 版）
=====================================
消息接收：WeFlow SSE 推送
AI 服务：AstrBot 通过 aiocqhttp (OneBot v11) 接入
消息发送：bridge 接收 AstrBot 的 API 调用 → UIA / WeFlow API

架构：
  WeFlow ──SSE──→ bridge.py ──WS 客户端──→ AstrBot (aiocqhttp 服务端)
                   ↑ 连接 ws://127.0.0.1:19777  ↑ 监听端口，等待客户端连入
                   发送 OneBot 事件             返回 API 响应
"""

# 共享状态：所有模块通过 import state 访问这些变量
import threading
import time
import hashlib
import secrets
import logging
import math
from collections import deque

from stable_id import IdentityRegistry, stable_id

# ============ 状态控制 ============

running = False
paused = threading.Event()
paused.clear()
run_lock = threading.RLock()
send_cancel = threading.Event()
outbox = None
send_queue = None
bili_sharing = None
start_bridge = stop_bridge = pause_bridge = resume_bridge = update_config = get_status = None
started_at = None
_metrics_lock = threading.Lock()
metrics = {"received": 0, "forwarded": 0, "sent": 0, "send_failed": 0, "cancelled": 0}
recent_activity = deque(maxlen=100)
latency_samples = deque(maxlen=200)
_performance_lock = threading.Lock()
_response_timings = {stage: deque(maxlen=200) for stage in
    ("first_text", "first_voice", "voice_prepare", "voice_queue", "queue_wait", "wechat_send")}


def note_performance_sample(stage, seconds):
    """Record a measured stage only; no text, identity, or invented receive time."""
    if stage not in _response_timings or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        return False
    with _performance_lock:
        _response_timings[stage].append((float(seconds), time.time()))
    return True


def _timing_summary(samples):
    values = sorted(value for value, _ in samples)
    return {"sample_count": len(values),
        "latest_seconds": round(samples[-1][0], 3) if samples else None,
        "average_seconds": round(sum(values) / len(values), 3) if values else None,
        "p95_seconds": round(values[max(0, math.ceil(len(values) * .95) - 1)], 3) if values else None,
        "last_at": samples[-1][1] if samples else None}


def response_performance_snapshot(queue_snapshot=None):
    with _performance_lock:
        stages = {stage: _timing_summary(samples) for stage, samples in _response_timings.items()}
    if isinstance(queue_snapshot, dict):
        samples = [(item["queue_wait_seconds"], item.get("started") or item.get("completed"))
                   for item in reversed(queue_snapshot.get("recent", [])[:200])
                   if type(item.get("queue_wait_seconds")) in (int, float)
                   and math.isfinite(item["queue_wait_seconds"]) and item["queue_wait_seconds"] >= 0]
        stages["queue_wait"] = _timing_summary(samples)
    return {"sample_limit": 200, "scope": "current_process", "verification": "local_ui_accepted", "stages": stages}

def record_activity(kind, message, **details):
    with _metrics_lock:
        recent_activity.appendleft({"time": time.time(), "kind": kind, "message": str(message), **details})

def increment(key, count=1):
    with _metrics_lock:
        metrics[key] = metrics.get(key, 0) + count

def metrics_snapshot():
    with _metrics_lock:
        samples = sorted(latency_samples)
        result = {"metrics": dict(metrics), "recent_activity": list(recent_activity),
                "latency": {"samples": len(samples), "median_ms": round(samples[len(samples)//2]*1000, 1) if samples else None,
                            "p95_ms": round(samples[min(len(samples)-1, int(len(samples)*0.95))]*1000, 1) if samples else None}}
    result["response_performance"] = response_performance_snapshot()
    return result
bridge_thread = None
bridge_generation = 0
client_generation = 0
_response_started = {}
_response_lock = threading.Lock()


def mark_response_started(key, generation, started_monotonic=None):
    now = time.monotonic()
    started = started_monotonic if isinstance(started_monotonic, (int, float)) and 0 < started_monotonic <= now else now
    with _response_lock:
        expired = [k for k, (_, at) in _response_started.items() if now - at > 300]
        for item in expired:
            _response_started.pop(item, None)
        if generation != bridge_generation:
            return
        previous = _response_started.get(key)
        if previous and previous[0] == generation:
            # A new batch does not inherit elapsed time from a skipped request.
            started = max(started, previous[1])
        _response_started[key] = (generation, started)
        while len(_response_started) > 2048:
            _response_started.pop(next(iter(_response_started)))


def pop_response_started(key, generation):
    with _response_lock:
        value = _response_started.get(key)
        if value and value[0] == generation:
            _response_started.pop(key, None)
            if generation == bridge_generation and time.monotonic() - value[1] <= 300:
                return value[1]
        return None

# ============ OneBot WebSocket 客户端管理 ============

_ob_ws = None          # WebSocket 连接实例
_ob_ws_loop = None     # 事件循环
_ob_ws_ready = threading.Event()
_self_id_int = 0       # 启动时从 config 初始化


def _wxid_to_int(wxid: str) -> int:
    """兼容旧接口：使用稳定的 SHA-256 映射。"""
    return stable_id(wxid)


# ============ 稳定身份注册表 ============

identity_registry = IdentityRegistry()

import atexit
atexit.register(identity_registry.flush)

# ============ 桥接实例 / 发送器 ============

bridge_instance = None
bridge_lock = threading.Lock()
sender_instance = None
_ob_id_to_contact: dict[int, str] = {}  # 兼容旧字段：OneBot user_id/group_id → 最新名字
ob_map_lock = threading.Lock()
ob_client_started = False

# 群聊回复模式（运行时可变，启动时从 config 初始化）
group_reply_mode = "mention"

# 图片描述配置（运行时可变，Web 面板保存后即时生效）
image_caption_provider = "ollama"
image_caption_model = "llava:7b"
image_caption_api_key = ""
image_caption_api_base = ""
image_caption_prompt = "请用中文简短描述这张图片的内容"
ollama_base_url = "http://127.0.0.1:61000"
ollama_timeout = 60

# 共享记忆库 / 自动朋友圈（运行时由 main 注入）
memory_store = None
autopilot = None
autopilot_thread = None
autopilot_stop = threading.Event()
moments_interactions = None
run_moments_interactions = None

# 群消息回复方式（由 reply_judge 决定，发送时消费）
# group_id -> dict(reply_type, sticker_keyword, ...) 或旧的纯字符串
reply_state_lock = threading.Lock()
pending_reply_modes: dict[int, dict | str] = {}

# Native-session revisions and opaque request tickets bind every streamed segment
# to its original input. Names and mutable target-level mode slots never route it.
_reply_requests = {}
_reply_revisions = {}
_reply_lock = threading.RLock()
_private_activity_at = 0.0
_private_processing = {}


def note_private_activity():
    global _private_activity_at
    with _reply_lock:
        _private_activity_at = time.monotonic()


def private_processing(session_id, active):
    """Cover buffering-side model/tool work before an AstrBot ticket exists."""
    with _reply_lock:
        if active:
            _private_processing[session_id] = time.monotonic()
        else:
            _private_processing.pop(session_id, None)


def private_priority_snapshot():
    now = time.monotonic()
    with _reply_lock:
        busy = any(0 <= now - started < 600 for started in _private_processing.values())
        for record in _reply_requests.values():
            if record.get("is_group") or not reply_request_active(record["request_id"]) or now - record["created"] >= 600:
                continue
            packets = list(record.get("segments", {}).values())
            pending = any(not packet["future"].done() for packet in packets)
            # Historical voice-only head packets retain "collecting" forever.
            # The latest final packet owns the mutable asynchronous voice state;
            # text_voice can be synthesizing even after its text was accepted.
            final_delivery = packets[-1].get("delivery", {}) if packets else {}
            voice_state = record.get("voice_state", final_delivery.get("voice_state"))
            pending = pending or voice_state in {"synthesizing", "queued"}
            if not record.get("closed") or pending:
                busy = True
                break
        return {"private_inflight": busy, "seconds_since_private_activity":
            max(0, now - _private_activity_at) if _private_activity_at else None}


def social_should_yield():
    import config
    snapshot = private_priority_snapshot()
    quiet = max(0, min(3600, float(getattr(config, "MOMENTS_PRIVATE_QUIET_SECONDS", 60))))
    elapsed = snapshot["seconds_since_private_activity"]
    return snapshot["private_inflight"] or elapsed is not None and elapsed < quiet


def advance_reply_revision(target_id, is_group, generation):
    with _reply_lock:
        key = (int(target_id), bool(is_group), generation)
        revision = _reply_revisions.get(key, 0) + 1
        _reply_revisions[key] = revision
        if len(_reply_revisions) > 4096:
            for old in list(_reply_revisions)[:2048]:
                _reply_revisions.pop(old, None)
        return revision


def register_reply_request(event, generation, mode=None, revision=None):
    target = event.get("group_id") if event.get("message_type") == "group" else event.get("user_id")
    if not target:
        return None
    is_group = event.get("message_type") == "group"
    identity = event.get("wechat_identity", {})
    with _reply_lock:
        key = (int(target), is_group, generation)
        actual_revision = _reply_revisions.get(key, 0)
        for previous in _reply_requests.values():
            if (previous["target_id"], previous["is_group"], previous["generation"]) == key and previous.pop("idle_expired", False):
                event["wechat_interrupt_previous"] = True
        ticket = {"request_id": secrets.token_hex(24), "target_id": int(target), "is_group": is_group,
                  "generation": generation, "revision": actual_revision if revision is None else revision,
                  "request_message_id": str(event.get("message_id", "")), "session_id": identity.get("session_id", "")}
        _reply_requests[ticket["request_id"]] = {**ticket, "mode": dict(mode or {}), "started": event.get("wechat_received_monotonic") or time.monotonic(),
            "created": time.monotonic(), "last_progress": time.monotonic(), "segments": {}, "stream_id": None, "last_sent_at": None, "texts": [], "voice_scheduled": False}
        for request_id, record in list(_reply_requests.items()):
            if time.monotonic() - record["created"] > 600:
                _reply_requests.pop(request_id, None)
        while len(_reply_requests) > 2048:
            _reply_requests.pop(next(iter(_reply_requests)))
        event["wechat_reply_request"] = ticket
        event["wechat_reply_policy"] = dict(mode or {})
        return ticket


def prepare_outbox_reply(event, generation):
    """Restore pending durable inputs after a process restart, without reviving cancelled turns."""
    identity = event.get("wechat_identity")
    if not isinstance(identity,dict) or not isinstance(identity.get("session_id"),str) or not identity.get("messages"):
        # Legacy events keep the ordinary adapter instead of inventing native IDs.
        return True
    ticket = event.get("wechat_reply_request", {})
    with _reply_lock:
        if isinstance(ticket,dict) and ticket.get("request_id") in _reply_requests:
            return reply_request_active(ticket["request_id"])
        target = event.get("group_id") if event.get("message_type") == "group" else event.get("user_id")
        if not target:
            return False
        if isinstance(ticket,dict) and ticket.get("generation") == generation:
            current_revision = _reply_revisions.get((int(target),event.get("message_type") == "group",generation),0)
            if isinstance(ticket.get("revision"),int) and ticket["revision"] < current_revision:
                return False
        # monotonic receive timestamps from the old OS/process are not comparable.
        event["wechat_received_monotonic"] = time.monotonic()
        revision = advance_reply_revision(target,event.get("message_type") == "group",generation)
        register_reply_request(event,generation,event.get("wechat_reply_policy"),revision)
        return True


def reply_request_active(request_id):
    with _reply_lock:
        record = _reply_requests.get(request_id)
        return bool(record and not record.get("aborted") and record["generation"] == bridge_generation and record["revision"] == _reply_revisions.get((record["target_id"], record["is_group"], record["generation"]), 0))


def reply_revision_active(target_id, is_group, generation, revision):
    with _reply_lock:
        return generation == bridge_generation and revision == _reply_revisions.get((int(target_id),bool(is_group),generation),0)


def reply_target_busy(target_id, is_group, generation):
    """Keep additions in the existing buffer until this turn's receipt closes."""
    import config
    now = time.monotonic()
    idle_limit = max(1.0, float(getattr(config, "MAIN_LLM_TIMEOUT", 35))) + max(0.0, float(getattr(config, "API_SEND_WAIT_SECONDS", 8)))
    with _reply_lock:
        for record in _reply_requests.values():
            if (record["target_id"], record["is_group"], record["generation"]) != (int(target_id), bool(is_group), generation):
                continue
            packets = list(record.get("segments", {}).values())
            delivery = packets[-1].get("delivery", {}) if packets else {}
            for packet in packets:
                future = packet["future"]
                if future.done():
                    try:
                        failed = not future.result()
                    except Exception:
                        failed = True
                    if failed:
                        record["aborted"] = record["closed"] = True
                        packet.get("delivery", {})["voice_pending"] = False
            voice_future = record.get("voice_future")
            if any(not packet["future"].done() for packet in packets) or voice_future is not None and not voice_future.done():
                return True
            if not reply_request_active(record["request_id"]):
                continue
            if record.get("voice_state", delivery.get("voice_state")) in {"synthesizing", "queued"}:
                return True
            if not record.get("closed") and now - record.get("last_progress", record["created"]) >= idle_limit:
                record["aborted"] = record["closed"] = True
                record["idle_expired"] = True
                delivery["voice_state"], delivery["voice_pending"] = "cancelled", False
                continue
            if not record.get("closed"):
                return True
    return False


def claim_reply_segment(metadata, target_id, is_group, generation, future_factory):
    """Atomically validate ticket and deduplicate even failed/uncertain sends."""
    if not isinstance(metadata, dict):
        raise ValueError("流式请求元数据无效")
    request_id, stream_id, seq = metadata.get("request_id"), metadata.get("stream_id"), metadata.get("seq")
    if not isinstance(stream_id, str) or not 1 <= len(stream_id) <= 64 or isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= 10000:
        raise ValueError("流式请求段标识无效")
    with _reply_lock:
        record = _reply_requests.get(request_id)
        if not record or not reply_request_active(request_id):
            raise ValueError("旧会话回复已取消")
        for field, value in (("target_id", target_id), ("is_group", is_group), ("generation", generation), ("request_message_id", record["request_message_id"]), ("session_id", record["session_id"]), ("revision", record["revision"])):
            if metadata.get(field) != value or record.get(field) != value:
                raise ValueError("流式请求身份与原消息不一致")
        if record["stream_id"] not in (None, stream_id):
            raise ValueError("同一请求不得重开流式重发")
        if seq in record["segments"]:
            return record, record["segments"][seq], True
        if record.get("closed"):
            raise ValueError("已完成的流式请求不能继续发段")
        if seq != len(record["segments"]):
            raise ValueError("流式段序号不连续")
        record["stream_id"] = stream_id
        record["last_progress"] = time.monotonic()
        # This result record survives caller retries; no UIA operation is retried.
        result = future_factory(record)
        record["segments"][seq] = result
        if metadata.get("final"):
            record["closed"] = True
        return record, result, False


class ReplyCancel:
    def __init__(self, base, request_id):
        self.base, self.request_id = base, request_id

    def is_set(self):
        return self.base.is_set() or not reply_request_active(self.request_id)


class ReplyRevisionCancel:
    def __init__(self, base, target_id, is_group, generation, revision):
        self.base, self.target_id, self.is_group, self.generation, self.revision = base,target_id,is_group,generation,revision
    def is_set(self):
        return self.base.is_set() or not reply_revision_active(self.target_id,self.is_group,self.generation,self.revision)

# 旧版本静默状态兼容字段；目前不用于抑制回复，以免吞掉后续正常问题。
suppress_text_until: dict[str, float] = {}

# 刚发出去的文本，用来识别 WeFlow 回投的自回复
_sent_lock = threading.Lock()
_sent_recently: dict[str, float] = {}
_SENT_TTL = 45.0


def _text_key(text: str) -> str:
    t = (text or "").strip()
    return t[:240] if t else ""


def note_sent_text(text: str, session_id="", message_id=None, **kwargs) -> None:
    if not text or not session_id:
        return
    key = (str(session_id), hashlib.sha256(str(text).strip().encode()).hexdigest())
    now = time.time()
    with _sent_lock:
        _sent_recently[key] = now
        if len(_sent_recently) > 300:
            cutoff = now - _SENT_TTL
            expired = [k for k, ts in _sent_recently.items() if ts < cutoff]
            for k in expired:
                _sent_recently.pop(k, None)


def is_self_echo(text: str, *, session_id="", sender_id="", direction=None, message_id=None) -> bool:
    # Text equality alone never proves a self echo; friends can reply with the same words.
    own = direction in (True, 1, "1", "sent", "outgoing", "self")
    if not own:
        import config
        own = bool(config.BOT_WXID and str(sender_id) == config.BOT_WXID)
    if not own or not text or not session_id:
        return False
    key = (str(session_id), hashlib.sha256(str(text).strip().encode()).hexdigest())
    now = time.time()
    with _sent_lock:
        ts = _sent_recently.get(key)
        if ts is None:
            return False
        if now - ts > _SENT_TTL:
            _sent_recently.pop(key, None)
            return False
        return True


def dispatch_send(method, contact, *args, target_id=0, is_group=False, generation=None, reply_request_id=None, reply_revision=None, proactive=False, proactive_cancel=None, **kwargs):
    """Single checked boundary for all sends. The sender owns the desktop lock."""
    if generation is None:
        generation = bridge_generation
    cancel = ReplyCancel(send_cancel, reply_request_id) if reply_request_id else send_cancel
    if proactive and proactive_cancel is not None:
        cancel = proactive_cancel
    if reply_revision is not None:
        cancel = ReplyRevisionCancel(cancel,target_id,is_group,generation,reply_revision)
    if not running or paused.is_set() or cancel.is_set() or generation != bridge_generation:
        increment("cancelled")
        return False
    sender = sender_instance
    if sender is None:
        increment("send_failed")
        return False
    record = (identity_registry.get_group_record(target_id) if is_group else identity_registry.get_private_record(target_id)) if target_id else None
    identity = dict(record or {})
    identity["is_group"] = is_group
    if identity.get("name"):
        contact = identity["name"]
    func = getattr(sender, method) if isinstance(method, str) else method
    if not is_group and not proactive:
        note_private_activity()
    started = time.monotonic()
    try:
        ok = bool(func(contact, *args, identity=identity, cancel_event=cancel, generation=generation, **kwargs))
    except Exception as exc:
        logging.getLogger("ob11-bridge").exception("发送失败: %s", exc)
        ok = False
    finally:
        if not is_group and not proactive:
            note_private_activity()
            if not reply_request_id:
                # Legacy non-streaming completion has no ticket metadata; close
                # the current target's waiting marker once its send finishes.
                with _reply_lock:
                    for request in _reply_requests.values():
                        if request["target_id"] == target_id and not request["is_group"] and request["generation"] == generation:
                            request["closed"] = True
    increment("sent" if ok else "send_failed")
    with _metrics_lock:
        latency_samples.append(time.monotonic()-started)
    if ok:
        note_performance_sample("wechat_send", time.monotonic() - started)
        sharing = bili_sharing
        if sharing and identity.get("session_id") and not proactive:
            sharing.note_reply(identity["session_id"])
    record_activity("sent" if ok else "error", "发送完成" if ok else "发送未确认", target=contact, method=str(method))
    if ok:
        logging.getLogger("ob11-bridge").info("[发送] %s → %s：发送器已确认", getattr(method, "__name__", method), contact)
    if ok and (method == "send_text" or getattr(method, "__name__", "") == "send_text") and args:
        note_sent_text(args[0], session_id=identity.get("session_id") or identity.get("username") or str(target_id))
    return ok


def prune_suppress(now: float | None = None) -> None:
    """清掉过期的文字静默标记，避免字典无限涨。"""
    ts = now if now is not None else time.time()
    with reply_state_lock:
        if len(suppress_text_until) < 64:
            return
        dead = [k for k, t in suppress_text_until.items() if t < ts]
        for k in dead:
            suppress_text_until.pop(k, None)
