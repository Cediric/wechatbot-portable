"""
桥接核心模块：WeFlowBridge 类。
"""

from __future__ import annotations

import atexit
import hashlib
import html
import io
import json
import logging
import os
import re
import threading
import time
import uuid
from urllib.parse import urljoin, urlparse
from collections import deque
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor

import state
import config
import stable_id
import reply_judge
import sticker_selector
import tools
import weflow_image
import http_util
import humanize
import name_refresh
from ob_protocol import push_event, make_message_event

log = logging.getLogger("ob11-bridge")

_PENDING_IMAGE_TTL = 90
_MEDIA_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media")
_MEDIA_SLOTS = threading.BoundedSemaphore(24)
_IDENTITY_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="identity-resolve")
_IDENTITY_SLOTS = threading.BoundedSemaphore(24)
atexit.register(lambda: _MEDIA_POOL.shutdown(wait=False, cancel_futures=True))
atexit.register(lambda: _IDENTITY_POOL.shutdown(wait=False, cancel_futures=True))
_MEDIA_CONTENTS = ("[图片]", "[动画表情]", "[表情]", "[视频]")
_URL_FIELDS = ("url", "linkUrl")
_XML_FIELDS = ("xml", "rawXml", "rawContent")
_SHARE_FIELDS = (*_URL_FIELDS, *_XML_FIELDS, "pagePath", "pagepath", "appId", "appid", "miniProgramAppId",
                 "miniProgramPagePath", "appmsg", "appMsg", "weappinfo", "weAppInfo")
_APPMSG = re.compile(r"(?:<|&lt;)appmsg\b", re.I)


def _share_card(data):
    """Normalize WeFlow app messages without treating a card title as a chat command."""
    content = data.get("content")
    payload = content if isinstance(content, dict) else data
    text = content if isinstance(content, str) else ""
    kind = str(data.get("localType") or data.get("type") or data.get("msgType") or "")
    xml_content = bool(_APPMSG.search(text[:512]))
    card = (kind in {"49", "21474836529", "141733920817", "154618822705", "link", "appmsg", "miniapp"}
            or text.startswith(("[链接]", "[小程序]")) or xml_content
            or any(k in payload for k in ("appmsg", "appMsg", "weappinfo", "weAppInfo", "linkUrl", "url",
                                          "xml", "rawXml", "pagePath", "pagepath", "miniProgramPagePath"))
            or _APPMSG.search(str(payload.get("rawContent") or "")[:512]))
    if not card:
        return data if isinstance(content, str) else dict(data, content="")
    sources = [data] if payload is data else [payload, data]
    for keys in (("appmsg", "appMsg"), ("weappinfo", "weAppInfo")):
        sources.extend(sub for src in list(sources) for key in keys if isinstance(sub := src.get(key), dict))
    fields = [src[key][:65536] for src in sources for key in (*_XML_FIELDS, *_URL_FIELDS)
              if isinstance(src.get(key), str) and src[key]]
    if xml_content or text.startswith(("https://", "http://")):
        fields.append(text[:65536])
    display = "" if xml_content else text
    if not display:
        title = next((value for src in sources for key in ("linkTitle", "title")
                      if isinstance(value := src.get(key), str) and value), "")
        if not title:
            for value in fields:
                match = re.search(r"<title>(.*?)</title>", html.unescape(value), re.I | re.S)
                if match:
                    title = match[1].strip().removeprefix("<![CDATA[").removesuffix("]]>")
                    break
        display = "[分享卡片] " + " ".join(str(title or "").split())[:240]
    ref = None
    bili_card = False
    if getattr(config, "BILI_VIDEO_ENABLED", True):
        import bili_video
        for value in fields:
            # Quoted and forwarded chat history is not a new video share.
            if re.search(r"(?:<|&lt;)(?:refermsg|recorditem)\b", value, re.I):
                continue
            bili_card = bili_card or bili_video.is_bili_card(value)
            ref = bili_video.find_video(value)
            if ref:
                break
        if not ref:
            for src in sources:
                appid = src.get("appid") or src.get("appId") or src.get("miniProgramAppId")
                if appid != bili_video.BILI_APP_ID:
                    continue
                bili_card = True
                ref = bili_video.miniapp_video(appid, src.get("pagepath") or src.get("pagePath") or src.get("miniProgramPagePath") or "")
                if ref:
                    break
    if ref and ref.url not in display:
        display += " " + ref.url
    lookup = not (fields or bili_card or any(src.get(k) for src in sources for k in ("pagepath", "pagePath", "miniProgramPagePath"))
                  or text.startswith(("[文件]", "[聊天记录]", "[引用消息]", "[音乐]", "[红包]", "[转账]")))
    return dict(data, content=display.strip(), _share_card=True, _video_ref=ref, _lookup_share=lookup, _bili_card=bili_card)


_REQUEST_PREFIX = r"(?:^|[。！？!?；;：:\n]\s*|(?:帮我|给我|请|麻烦你|能不能|可以|想要|我要|再|先|来)\s*)"
_GEN_IMAGE_RE = re.compile(_REQUEST_PREFIX + r"(?:(?:生成|绘制|制作|做).{0,16}(?:图片|图像|插画|头像|海报|照片|图)|画(?:一?[张幅个只条棵朵辆]).+|(?:给我|发我|拍|发|来)(?:一?张)?.{0,8}自拍|ai\s*生图)", re.I)
_PHOTO_REQUEST_RE = re.compile(
    r"^\s*(?:(?:请|麻烦你|能不能|可不可以|可以|帮我|给我|你|再|先)\s*){0,4}"
    r"(?:(?:发(?:我|给我)?|拍|来)(?:一?(?:张|个|幅))?.{0,24}?(?:照片|图片|自拍|图像|图)"
    r"|(?:看看|看|瞧瞧)(?:你(?:自己)?的.{0,16}?|自己(?:的)?.{0,16}?)?(?:照片|图片|自拍)"
    r"|(?:我)?想(?:要)?(?:看看|看|瞧瞧)(?:你(?:自己)?的.{0,16}?|自己(?:的)?.{0,16}?)?(?:照片|图片|自拍)"
    r"|(?:按|根据|照着).{0,16}(?:图片|照片|图|附件).{0,12}(?:创作|生成|绘制|制作|画))")
_IMAGE_OTHER_RECIPIENT_RE = re.compile(
    r"(?:发给|送给|给)(?:我(?:的)?(?:朋友|同学|家人)|(?!我)[^，,。.!！?？；;~～\s]{1,16})")
_IMAGE_SEARCH_RE = re.compile(r"(?:搜|找|查找|搜索).{0,12}(?:图片|照片|壁纸|头像|图)")
_WEB_SEARCH_RE = re.compile(r"(?:上网|联网|网上).{0,8}(?:查|搜)|(?:帮我|请|麻烦).{0,4}(?:搜索|查一下|查找)|(?:搜索|查一下).{2,}")
_VOICE_RE = re.compile(_REQUEST_PREFIX + r"(?:你\s*)?(?:(?:用|发|来|给我|回复|回答|说|讲).{0,12}语音|语音(?:回复|回答|说|讲)|(?:说|讲)(?:一?句|个话))")


class _MissingSenderIdentity(ValueError):
    pass


def _id_token(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    token = str(value).strip()
    return token if token and len(token) <= 512 and not re.fullmatch(r"0+", token) else ""


def _message_ids(data):
    return {token for k in ("rawid", "rawId", "id", "messageId", "msgId", "localId", "serverId", "serverIdRaw", "mediaId", "md5", "imageMd5") if (token := _id_token(data.get(k)))}


def _source_message_ids(data):
    return {key: token for key in ("rawid", "rawId", "serverIdRaw", "serverId", "messageId", "msgId", "id", "localId")
            if (token := _id_token(data.get(key)))}


def _is_voice(data):
    return str(data.get("type") or data.get("msgType") or data.get("localType") or "") == "34" or str(data.get("content", "")).strip() == "[语音]"

def _is_video(data):
    return str(data.get("type") or data.get("msgType") or data.get("localType") or "") == "43" or str(data.get("content", "")).strip() == "[视频]"


def _same_message(original, candidate):
    # rawid is WeFlow's server message identifier. Don't accidentally match another
    # message's local row ID when both fields exist.
    def server_ids(data):
        precise = {token for k in ("rawid", "rawId", "serverIdRaw") if (token := _id_token(data.get(k)))}
        if len(precise) > 1:
            return None
        return precise or ({token} if (token := _id_token(data.get("serverId"))) else set())
    original_server, candidate_server = server_ids(original), server_ids(candidate)
    if original_server is None or candidate_server is None:
        return False
    if original_server and candidate_server:
        return bool(original_server.intersection(candidate_server))
    original_message = {token for k in ("messageId", "msgId") if (token := _id_token(original.get(k)))}
    candidate_message = {token for k in ("messageId", "msgId") if (token := _id_token(candidate.get(k)))}
    if len(original_message) > 1 or len(candidate_message) > 1:
        return False
    if (original_server or original_message) and (candidate_server or candidate_message):
        return bool((original_server or original_message).intersection(candidate_server or candidate_message))
    if original_server or candidate_server or original_message or candidate_message:
        return False
    for key in ("id", "localId"):
        left, right = _id_token(original.get(key)), _id_token(candidate.get(key))
        if left and right:
            return left == right
    for key in ("md5", "imageMd5", "mediaId"):
        if _id_token(original.get(key)) and _id_token(original.get(key)) == _id_token(candidate.get(key)):
            return True
    return False


def _strip_author_prefix(text):
    text = re.sub(r'成员"(?:\\.|[^"\\\n])*"(?:（微信ID：[A-Za-z0-9_.@:-]+）)?在群"(?:\\.|[^"\\\n])*"中对你说：', '', text)
    for nick in config.BOT_NICKNAMES or ():
        text = re.sub(r"[@＠]" + re.escape(nick) + r"(?=$|[\s\u2005，,。.!！?？：:])", "", text)
    return text.strip()


def _song_command_text(data, mentioned):
    """Only remove a verified, sole addressee from an original command."""
    text = data["content"].strip()
    if not mentioned or not text.startswith(("@", "＠")):
        return text
    mentions = data.get("mentions") or data.get("atUsers") or data.get("atUserList") or []
    if isinstance(mentions, str):
        mentions = [item for item in re.split(r"[,;\s]+", mentions.strip()) if item]
    if not isinstance(mentions, list) or len(mentions) > 1 or len(re.findall(r"[@＠]", text)) != 1:
        return ""
    native_at = any(data.get(key) is True for key in ("atMe", "isMentioned", "mentioned"))
    names = list(config.BOT_NICKNAMES or ())
    if mentions:
        target = mentions[0]
        wxid = (target.get("wxid") or target.get("username") or target.get("id")) if isinstance(target, dict) else target
        if not wxid or wxid != config.BOT_WXID:
            return ""
        native_at = True
        if isinstance(target, dict):
            names.extend(target.get(key) for key in ("displayName", "nickname", "name") if target.get(key))
    # WeChat uses U+2005 after an inserted mention. Together with a verified
    # sole native target this works even when the group uses a different card name.
    if native_at and "\u2005" in text:
        return text.split("\u2005", 1)[1].lstrip(" \t，,：:")
    for name in names:
        match = re.match(r"^[@＠]" + re.escape(name) + r"(?=$|[\s，,：:])[\s，,：:]*", text)
        if match:
            return text[match.end():]
    return text


def _action_request_text(text):
    # Preserve the complete conversation for the main model. Only pending
    # media intent before the latest explicit cancellation is superseded.
    clauses = re.split(r"[，,。！？!?；;\n]+", text)
    last_cancel = max((index for index, clause in enumerate(clauses) if _explicit_cancel(clause)), default=-1)
    return "\n".join(clauses[last_cancel:]) if last_cancel >= 0 else text


def _explicit_tool(text):
    """Only explicit requests may trigger paid generation; photo discussion is ordinary text."""
    text = _strip_author_prefix(text)
    # Quoted/negated/reported requests are conversation data, not a new order.
    text = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"', "", text)
    text = _action_request_text(text)
    clauses = [re.sub(r"^(?:(?:然后|接着|接下来|顺便|另外|还有|也|之后|同时)\s*[，,、]?\s*)+", "", clause.strip())
               for clause in re.split(r"[。！？!?；;\n]+", text)]
    clauses = [clause for clause in clauses if not re.match(
        r"^(?:(?:请|麻烦你|你|我|还是|暂时|先)\s*)*(?:不要|别|不用|不必|不需要|不想|不)\s*(?:给我|帮我|发|生成|拍|画)", clause)
        and not re.match(r"^(?:他说|她说|他们说|她们说|有人说|别人说|转述|引用)", clause)]
    if any(_IMAGE_SEARCH_RE.search(clause) for clause in clauses):
        return "image_search"
    if any((_GEN_IMAGE_RE.search(clause) or _PHOTO_REQUEST_RE.match(clause))
           and not _IMAGE_OTHER_RECIPIENT_RE.search(clause)
           and not re.search(r"(?:不用|不要|别|不必|不需要)\s*(?:再)?\s*(?:生成|生图|画|制作)", clause)
           for clause in clauses):
        return "generate_image"
    if any(_WEB_SEARCH_RE.search(clause) for clause in clauses):
        return "web_search"
    return "none"


def _conversation_command(text):
    command = _strip_author_prefix(text).strip()
    for nick in config.BOT_NICKNAMES or ():
        command = command.removeprefix('@' + nick).removeprefix('＠' + nick).strip()
    return command if command in ('/reset', '/new') else ''


def _explicit_voice(text):
    text = _strip_author_prefix(text)
    text = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"', "", text)
    text = _action_request_text(text)
    for clause in re.split(r"[，,。！？!?；;\n]+", text):
        clause = re.sub(r"^(?:(?:然后|接着|接下来|顺便|另外|还有|也|之后|同时)\s*)+", "", clause.strip())
        if re.match(r"^(?:(?:请|麻烦你|你|我|还是|暂时|先)\s*)*(?:不要|别|不用|不必|不需要|不想|不)", clause):
            continue
        if re.match(r"^(?:他说|她说|他们说|她们说|有人说|别人说|转述|引用)", clause):
            continue
        if _VOICE_RE.search(clause):
            return True
    return False


def _explicit_cancel(text):
    text = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"', "", _strip_author_prefix(text))
    return any(re.match(r"^\s*(?:(?:请|你|先|还是|那就)\s*)*(?:取消(?:吧|了|回复|发送)?(?=$|\s)|停(?:下|止|一下|了)?(?:吧|了)?(?=$|\s)|"
                        r"(?:别|不要|不用|不必)(?:再|继续)?(?:给我)?(?:发|回复|生成|画|说|播|录)(?:了|语音|图|图片|照片|消息))", clause)
               for clause in re.split(r"[，,。！？!?；;\n]+", text))


def _group_author_prefix(ident):
    name = json.dumps(str(ident["sender_name"]), ensure_ascii=False)
    group = json.dumps(str(ident["group_name_raw"] or ident["source_name"]).strip(), ensure_ascii=False)
    return f'成员{name}（微信ID：{ident["sender_username"]}）在群{group}中对你说：'


def _prune_dir(path: str, keep: int = 80):
    """Bound cache count while preserving queued media and active Moments leases."""
    try:
        import media_util
        with media_util._LEASE_LOCK:
            # Lease-to-outbox handoff cannot finish between snapshot and removal.
            box = getattr(state, "outbox", None)
            protected = {os.path.normcase(os.path.realpath(p)) for p in box.referenced_files()} if box else set()
            files = [
                os.path.join(path, name)
                for name in os.listdir(path)
                if os.path.isfile(os.path.join(path, name))
            ]
            extra = len(files) - keep
            if extra <= 0:
                return
            files.sort(key=lambda p: os.path.getmtime(p))
            removed = 0
            for p in files:
                if removed >= extra:
                    break
                if os.path.normcase(os.path.realpath(p)) in protected:
                    continue
                if media_util.remove_cached_file(p, path):
                    removed += 1
    except Exception:
        pass


def _prune_voice_cache():
    """Expire generated inbound audio only after pending events and transport leases release it."""
    import media_util
    with media_util._LEASE_LOCK:
        return _prune_voice_cache_locked(media_util)


def _prune_voice_cache_locked(media_util):
    box = getattr(state, "outbox", None)
    try:
        protected = {os.path.normcase(os.path.realpath(path)) for path in box.referenced_files()} if box else set()
    except Exception:
        log.warning("语音缓存清理暂缓：无法读取待发送媒体引用", exc_info=True)
        return 0
    days = max(1, float(getattr(config, "CACHE_RETENTION_DAYS", 7)))
    cutoff = time.time() - days * 86400
    roots = {os.path.realpath(os.path.join(config.DATA_DIR, "wechat_voice"))}
    if config.ASTRBOT_ATTACHMENTS:
        roots.add(os.path.realpath(os.path.join(config.ASTRBOT_ATTACHMENTS, "wechat_voice")))
    removed = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    try:
                        path = os.path.realpath(entry.path)
                        if (entry.name.startswith("wechat_") and entry.is_file(follow_symlinks=False)
                                and entry.stat(follow_symlinks=False).st_mtime < cutoff
                                and os.path.normcase(path) not in protected
                                and os.path.commonpath((root, path)) == root):
                            if media_util.remove_cached_file(entry.path, root, older_than=cutoff):
                                removed += 1
                    except OSError:
                        continue
        except OSError:
            log.debug("无法清理语音缓存目录 %s", root, exc_info=True)
    return removed


class _DebounceScheduler:
    """Dispatch due work to a bounded pool; serialize a session without blocking others."""

    def __init__(self, callback, workers=4):
        self._cb = callback
        self._cv = threading.Condition()
        self._items = {}
        self._active_sessions = set()
        self._workers = max(1, min(16, int(workers)))
        self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="conversation")
        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="buffer-sched")
        self._thread.start()

    def schedule(self, key: str, delay: float, version: int, session=None):
        when = time.monotonic() + max(0.05, float(delay or 0))
        with self._cv:
            if not self._alive:
                return
            self._items[key] = (when, version, session or key)
            self._cv.notify()

    def cancel(self, key: str):
        with self._cv:
            self._items.pop(key, None)

    def stop(self):
        with self._cv:
            self._alive = False
            self._items.clear()
            self._cv.notify()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _execute(self, key, version, session):
        try:
            self._cb(key, version)
        except Exception:
            log.exception("[buffer] 定时处理异常")
        finally:
            with self._cv:
                self._active_sessions.discard(session)
                self._cv.notify_all()

    def _run(self):
        while True:
            with self._cv:
                if not self._alive:
                    return
                eligible = [(key, item) for key, item in self._items.items()
                            if item[2] not in self._active_sessions]
                if not eligible or len(self._active_sessions) >= self._workers:
                    self._cv.wait()
                    continue
                key, (when, version, session) = min(eligible, key=lambda kv: kv[1][0])
                now = time.monotonic()
                if when > now:
                    self._cv.wait(when - now)
                    continue
                self._items.pop(key, None)
                self._active_sessions.add(session)
            try:
                self._pool.submit(self._execute, key, version, session)
            except RuntimeError:
                with self._cv:
                    self._active_sessions.discard(session)
                    if not self._alive:
                        return


class WeFlowBridge:
    """WeFlow ↔ AstrBot 桥接器（OneBot v11 版）。"""

    def __init__(self, sender):
        self.sender = sender
        self.generation = state.bridge_generation
        self.cancel_event = getattr(state, "send_cancel", threading.Event())
        self.processed_ids = deque(maxlen=10000)
        self.processed_id_set = set()
        self.start_timestamp = int(time.time())
        self.pending_buffers = {}
        self.buffer_lock = threading.Lock()
        self._sse_session = None
        self._pending_mention_images = {}
        self._pending_lock = threading.Lock()
        self._last_pending_cleanup = 0.0
        self._sequence = 0
        self._processed_lock = threading.Lock()
        self._identity_lock = threading.Lock()
        self._identity_pending = {}
        self._closed = threading.Event()
        from recent_media import RecentMediaCache
        self._recent_media = RecentMediaCache()
        _prune_voice_cache()
        self._sched = _DebounceScheduler(self.process_sender, getattr(config, "CORE_WORKERS", 4))

    def _active(self):
        return (state.running and state.bridge_generation == self.generation
                and not state.paused.is_set() and not self.cancel_event.is_set() and not self._closed.is_set())

    @staticmethod
    def _release_media_leases(leases):
        contexts = list(leases.values()) if isinstance(leases, dict) else list(leases)
        leases.clear()
        for context in contexts:
            try:
                context.__exit__(None, None, None)
            except Exception:
                log.exception("释放入站媒体引用失败")

    def _discard_entry_media(self, entry):
        for record in entry.get("messages", []):
            if isinstance(record, dict):
                self._release_media_leases(record.pop("media_leases", {}))
        self._release_media_leases(entry.get("retry_media_leases", []))

    def close(self):
        """Cancel new work and release pending references; running batches own theirs until exit."""
        self._closed.set()
        self._sched.stop()
        with self.buffer_lock:
            for entry in self.pending_buffers.values():
                self._discard_entry_media(entry)
            self.pending_buffers.clear()
        self._recent_media.close()

    @staticmethod
    def _lease_record_path(record, path):
        import media_util
        key = os.path.normcase(os.path.realpath(os.fspath(path)))
        leases = record.setdefault("media_leases", {})
        if key not in leases:
            context = media_util.lease_file(path)
            context.__enter__()
            leases[key] = context

    def _protect_media(self, data, path):
        """Acquire before cache publication, keeping the exact reserved message slot alive."""
        with self.buffer_lock:
            if not self._active():
                return False
            entry = self.pending_buffers.get(data.get("_buffer_key"))
            if entry:
                for record in entry["messages"]:
                    if isinstance(record, dict) and record["seq"] == data.get("_seq"):
                        self._lease_record_path(record, path)
                        return True
        return False

    def _publish_event(self, event, reply_mode=None):
        # Config reconnect/stop and durable acceptance share a boundary. A task
        # that lost its generation while waiting may not publish stale context.
        with getattr(state, "run_lock", nullcontext()):
            if not self._active():
                return None
            if reply_mode:
                target_id, info = reply_mode
                with state.reply_state_lock:
                    state.pending_reply_modes[target_id] = info
            register_request = getattr(state, "register_reply_request", None)
            if callable(register_request) and not event.get("wechat_reply_request"):
                bound_mode = dict(reply_mode[1]) if reply_mode else {"reply_type": "text"}
                bound_mode["user_text"] = event.pop("_wechat_reply_user_text", "")
                bound_mode["explicit_voice"] = _explicit_voice(bound_mode["user_text"])
                bound_mode["image_requested"] = bool(event.pop("_wechat_image_requested", False))
                register_request(event, self.generation, bound_mode,
                                 event.pop("_wechat_reply_revision", None))
            accepted = push_event(event)
            mark = getattr(state, "mark_response_started", None)
            if accepted and callable(mark):
                target = event.get("group_id") if event.get("message_type") == "group" else event.get("user_id")
                # The receive time already includes buffering/tool processing.
                mark(target, self.generation, event.get("wechat_received_monotonic"))
            return accepted

    def _next_sequence(self):
        with self._pending_lock:
            self._sequence += 1
            return self._sequence

    def should_ignore(self, data):
        content = data.get("content", "")
        msg_type = data.get("type", 0) or data.get("msgType", 0)
        sender_id = (data.get("senderUsername", "")
                     or data.get("senderWxid", "")
                     or data.get("sender", ""))
        direction = str(data.get("direction", "")).lower()
        if data.get("isSend") in (True, 1, "1") or direction in ("out", "outgoing", "sent", "send"):
            return True
        if config.BOT_WXID and sender_id == config.BOT_WXID:
            return True
        if _is_voice(data):
            return False
        if content in _MEDIA_CONTENTS:
            return False
        if str(msg_type) in ("3", "43", "47") or _is_video(data):
            return False
        if not content or content.strip() == "":
            return True
        return False

    def _is_self_echo(self, data):
        content = data.get("content", "")
        if not content:
            return False
        try:
            return state.is_self_echo(
                content, session_id=data.get("sessionId") or data.get("talkerId") or "",
                sender_id=data.get("senderUsername") or data.get("senderWxid") or data.get("sender") or "",
                direction=data.get("direction", ""),
                message_id=data.get("rawid") or data.get("id") or data.get("messageId") or "",
            )
        except TypeError:
            # Old global-text echo caches cannot establish who sent a message.
            return False

    def _cleanup_pending_mention_images(self, force: bool = False):
        now = time.time()
        if not force and now - self._last_pending_cleanup < 20:
            return
        self._last_pending_cleanup = now
        with self._pending_lock:
            if not self._pending_mention_images:
                return
            expired = [k for k, v in self._pending_mention_images.items()
                       if not v or now - v[-1].get("time", 0) > _PENDING_IMAGE_TTL]
            for k in expired:
                self._pending_mention_images.pop(k, None)

    def _arm(self, entry, key: str, delay: float) -> int:
        """递增 version 并重新预约到期处理。调用方须持有 buffer_lock。"""
        if entry.get("timer"):
            try:
                entry["timer"].cancel()
            except Exception:
                pass
            entry["timer"] = None
        entry["timer_version"] = int(entry.get("timer_version") or 0) + 1
        version = entry["timer_version"]
        self._sched.schedule(key, delay, version, session=entry.get("session_id_data", key))
        return version

    def _buffer_delay(self, entry, immediate=False):
        if immediate:
            return 0.05
        cap = max(0.05, float(config.BUFFER_SECONDS or 0.05))
        if not getattr(config, "SMART_BUFFER_ENABLED", True):
            return cap
        burst = float(getattr(config, "GROUP_BUFFER_SECONDS" if entry.get("is_group") else "PRIVATE_BUFFER_SECONDS", 0.7 if entry.get("is_group") else 0.35))
        max_wait = min(cap, max(0.05, float(getattr(config, "BUFFER_MAX_WAIT", 2.0))))
        elapsed = time.monotonic() - entry.get("first_at", time.monotonic())
        return max(0.05, min(cap, burst, max_wait - elapsed))

    def _mark_processed(self, raw_id: str) -> bool:
        if not raw_id:
            return False
        with self._processed_lock:
            if raw_id in self.processed_id_set:
                return True
            if len(self.processed_ids) == self.processed_ids.maxlen:
                self.processed_id_set.discard(self.processed_ids[0])
            self.processed_ids.append(raw_id)
            self.processed_id_set.add(raw_id)
        return False

    def _spawn_media(self, fn, data):
        if not self._active():
            return
        if not _MEDIA_SLOTS.acquire(blocking=False):
            self._finish_media(data, "媒体处理繁忙，请稍后重试")
            log.warning("[media] 识别队列已满，已保留文字提示")
            return
        def run():
            try:
                if self._active():
                    fn(data)
            finally:
                _MEDIA_SLOTS.release()
        try:
            _MEDIA_POOL.submit(run)
        except RuntimeError:
            _MEDIA_SLOTS.release()

    def _is_mentioned(self, data):
        if data.get("isMentioned") is True or data.get("mentioned") is True or data.get("atMe") is True:
            return True
        mentions = data.get("mentions") or data.get("atUsers") or data.get("atUserList") or []
        if isinstance(mentions, str):
            mentions = re.split(r"[,;\s]+", mentions)
        for mention in mentions if isinstance(mentions, list) else []:
            wxid = mention.get("wxid") or mention.get("username") or mention.get("id") if isinstance(mention, dict) else str(mention)
            if wxid and wxid in (config.BOT_WXID, "@all", "notify@all"):
                return True
        if data.get("_share_card"):
            return False  # A nickname in the publisher's card title is not a WeChat mention.
        content = data.get("content", "")
        if not content:
            return False
        for nick in config.BOT_NICKNAMES or []:
            if re.search(r"[@＠]" + re.escape(nick) + r"(?=$|[\s\u2005，,。.!！?？：:])", content):
                return True
        return False

    @staticmethod
    def _route_identity(data):
        session_id = data.get("sessionId") or data.get("talkerId")
        if data.get("sessionId") and data.get("talkerId") and data["sessionId"] != data["talkerId"]:
            raise ValueError("sessionId 与 talkerId 不一致，无法安全路由")
        is_group = isinstance(session_id, str) and session_id.endswith("@chatroom")
        stable_id.validate_username(session_id, group=is_group)
        kind = data.get("sessionType")
        if kind in ("group", "chatroom", 2) and not is_group or kind in ("private", "friend", 1) and is_group:
            raise ValueError("会话类型与微信根 ID 不一致")
        if data.get("groupName") and not is_group:
            raise ValueError("群名不能替代真实 @chatroom 群 ID")
        return session_id, is_group

    @staticmethod
    def _sender_identity(data):
        values = [data[key] for key in ("senderUsername", "senderWxid") if data.get(key)]
        if not values:
            return ""
        for value in values:
            stable_id.validate_username(value)
        if len(set(values)) != 1:
            raise ValueError("senderUsername 与 senderWxid 不一致")
        return values[0]

    def _extract_identities(self, data):
        session_id, is_group = self._route_identity(data)
        source_name = data.get("sourceName", "") or data.get("talkerName", "") or "未知"
        group_name_raw = data.get("groupName", "")
        sender_name = data.get("senderName", "") or data.get("sender", "") or source_name
        sender_username = self._sender_identity(data)
        if is_group and not sender_username:
            raise _MissingSenderIdentity("群消息缺少真实发言人微信 ID")
        if not is_group and sender_username and sender_username != session_id:
            raise ValueError("私聊发言人微信 ID 与会话 ID 不一致")

        if is_group:
            group_display = group_name_raw or source_name
            base_name = str(group_display).strip()
            stamp = data.get("timestamp") or data.get("time")
            try:
                stamp = float(stamp) if stamp else None
                if stamp and stamp > 10 ** 12:
                    stamp /= 1000
            except (TypeError, ValueError):
                stamp = None
            name_refresh.observe_name(session_id, base_name or group_display, is_group=True, source="message", observed_at=stamp)
            state.identity_registry.register_member(session_id, sender_username, sender_name)
        else:
            sender_username = session_id
            name_refresh.observe_name(session_id, source_name, source="message")
        return {
            "session_id": session_id,
            "source_name": source_name,
            "sender_name": sender_name,
            "sender_username": sender_username,
            "sender_stable": True,
            "is_group": is_group,
            "group_name_raw": group_name_raw,
        }

    def _buffer_key(self, ident):
        session = ident["session_id"]
        if ident["is_group"] and state.group_reply_mode == "batch":
            return f"__batch__{session}"
        if ident["is_group"]:
            return f"{session}::{ident['sender_username']}"
        return session

    def _new_entry(self, ident):
        group_name = str(ident["group_name_raw"] or ident["source_name"]).strip()
        return {
            "messages": [], "timer": None, "timer_version": 0, "processing": False,
            "contact": group_name if ident["is_group"] else ident["source_name"],
            "is_group": ident["is_group"], "source_name": ident["source_name"],
            "group_name": group_name if ident["is_group"] else "",
            "sender_in_group": ident["sender_name"] if ident["is_group"] else "",
            "sender_username": ident["sender_username"], "session_id_data": ident["session_id"],
            "first_at": time.monotonic(), "retry_events": deque(), "retry_media_leases": [],
        }

    def _append_message(self, ident, text, *, seq=None, ready=True, immediate=False, mentioned=False, message_id="", message_ids=None, chat_media=None):
        if not self._active():
            return None
        mark_private = getattr(state, "note_private_activity", None)
        if not ident["is_group"] and callable(mark_private):
            mark_private()
        key = self._buffer_key(ident)
        seq = seq if seq is not None else self._next_sequence()
        with self.buffer_lock:
            if not self._active():
                return None
            entry = self.pending_buffers.setdefault(key, self._new_entry(ident))
            advance = getattr(state, "advance_reply_revision", None)
            target_id = stable_id.group_id(ident["session_id"]) if ident["is_group"] else stable_id.user_id(ident["session_id"], ident["session_id"])
            interrupt = not (chat_media or {}).get("share_card") and _explicit_cancel(text)
            # Ordinary additions wait behind the current reply. Only an actual
            # stop/change instruction invalidates its in-flight desktop work.
            revision = advance(target_id, ident["is_group"], self.generation) if interrupt and callable(advance) else None
            entry["messages"].append({"seq": seq, "text": text, "ready": ready, "at": time.monotonic(),
                                      "reply_revision": revision, "interrupt_previous": interrupt,
                                      "mentioned_bot": bool(mentioned),
                                      "extractable": bool(ready),
                                      "sender_username": ident["sender_username"], "sender_name": ident["sender_name"],
                                      "original_message_id": message_id or next(iter((message_ids or {}).values()), ""),
                                      "message_ids": dict(message_ids or {}), **(chat_media or {})})
            entry["messages"].sort(key=lambda msg: msg["seq"])
            if not entry["processing"]:
                self._arm(entry, key, self._buffer_delay(entry, immediate))
        return key, seq

    def _queue_media(self, data, ident=None, seq=None):
        ident = ident or self._extract_identities(data)
        media = dict(data)
        media["_identity"] = ident
        result = self._append_message(ident, "", seq=seq, ready=False,
                                      mentioned=self._is_mentioned(data),
                                      message_ids=_source_message_ids(data))
        if not result:
            return
        media["_buffer_key"], media["_seq"] = result
        fn = self.process_voice_message if _is_voice(data) else self.process_video_message if _is_video(data) else self.process_emoji_message if data.get("content") in ("[动画表情]", "[表情]") or str(data.get("type") or data.get("msgType")) == "47" else self.process_image_message
        self._spawn_media(fn, media)

    def add_to_buffer(self, data):
        data = _share_card(data)
        if not self._active() or self.should_ignore(data) or self._is_self_echo(data):
            return
        log.info("📩 %s → %s", data.get("sourceName", ""), data.get("content", "")[:50])
        try:
            session, is_group = self._route_identity(data)
            sender = self._sender_identity(data)
            with self._identity_lock:
                waiting = session in self._identity_pending
            if is_group and (not sender or waiting or str(data.get("localType", "")) == "10000"):
                self._queue_identity_resolution(dict(data), session)
                return
            with getattr(state, "run_lock", nullcontext()):
                if not self._active():
                    return
                ident = self._extract_identities(data)
        except ValueError as error:
            self._reject_identity(data, str(error))
            return
        self._add_resolved_to_buffer(data, ident)

    def _reject_identity(self, data, reason):
        log.warning("[identity] 已拒绝无法核验身份的消息: %s", reason)
        if hasattr(state, "record_activity"):
            state.record_activity("identity", reason, session_id=data.get("sessionId") or data.get("talkerId") or "")

    def _queue_identity_resolution(self, data, session):
        # Official SSE may expose only rawid and display names. Resolve the exact
        # message asynchronously; a display name is never a member identifier.
        with self._identity_lock:
            pending = self._identity_pending.get(session)
            if pending is not None:
                if len(pending) >= 64:
                    self._reject_identity(data, "发言人身份核验队列已满，请稍后重试")
                    return
                pending.append(data)
                return
            if not _IDENTITY_SLOTS.acquire(blocking=False):
                self._reject_identity(data, "发言人身份核验繁忙，请稍后重试")
                return
            pending = deque([data])
            self._identity_pending[session] = pending
        try:
            _IDENTITY_POOL.submit(self._drain_identity_resolution, session, pending)
        except RuntimeError:
            with self._identity_lock:
                self._identity_pending.pop(session, None)
            _IDENTITY_SLOTS.release()
            self._reject_identity(data, "发言人身份核验已停止")

    def _resolve_sender_from_rows(self, original, session, rows):
        matches = [row for row in rows if isinstance(row, dict) and _same_message(original, row)]
        if len(matches) != 1:
            raise ValueError("无法唯一匹配原始群消息，拒绝猜测发言人")
        row = matches[0]
        row_session = row.get("sessionId") or row.get("talkerId")
        if row_session and row_session != session:
            raise ValueError("群消息结果属于其他会话")
        if str(row.get("localType", "")) == "10000":
            # Refresh names only for a uniquely matched native system row. Ordinary
            # chat text that resembles a rename notice cannot change routing names.
            return dict(original, _verified_system=True)
        if row.get("isSend") not in (False, 0, "0"):
            raise ValueError("无法确认原始消息是对方发言")
        sender = self._sender_identity(row)
        if not sender:
            raise ValueError("原始群消息没有发言人微信 ID")
        resolved = dict(original, senderUsername=sender, senderWxid=sender, isSend=0,
                        senderName=row.get("senderDisplayName") or original.get("senderName") or original.get("sourceName") or sender)
        # Reuse the already verified native row when legacy SSE has only a title.
        if row.get("localType") or original.get("_share_card"):
            for key in ("localType", *_SHARE_FIELDS):
                if row.get(key) and not resolved.get(key):
                    resolved[key] = row[key]
            resolved = _share_card(resolved)
        return resolved

    def _drain_identity_resolution(self, session, owned_queue):
        rows = []
        try:
            while self._active():
                with self._identity_lock:
                    pending = self._identity_pending.get(session)
                    if pending is not owned_queue:
                        return
                    if not pending:
                        self._identity_pending.pop(session, None)
                        return
                    original = pending.popleft()
                try:
                    resolved = original
                    if not self._sender_identity(original) or str(original.get("localType", "")) == "10000":
                        if not any(_id_token(original.get(key)) for key in ("rawid", "rawId", "serverId", "serverIdRaw", "messageId", "msgId", "id", "localId")):
                            raise ValueError("群消息缺少原始消息 ID，不能核验实际发言人")
                        if not any(isinstance(row, dict) and _same_message(original, row) for row in rows):
                            response = http_util.session().get(
                                f"{config.WE_FLOW_BASE_URL}/api/v1/messages",
                                params={"access_token": config.ACCESS_TOKEN, "talker": session, "limit": 100}, timeout=3)
                            if response.status_code != 200 or not self._active():
                                raise ValueError("WeFlow 原始消息查询失败，不能核验发言人")
                            body = response.json()
                            if isinstance(body, dict) and (body.get("success") is False or body.get("talker") not in (None, "", session)):
                                raise ValueError("WeFlow 原始消息查询会话不一致")
                            rows = body if isinstance(body, list) else body.get("messages", []) if isinstance(body, dict) else []
                            if not isinstance(rows, list):
                                raise ValueError("WeFlow 原始消息结果格式无效")
                        resolved = self._resolve_sender_from_rows(original, session, rows)
                    if resolved.get("_verified_system"):
                        with getattr(state, "run_lock", nullcontext()):
                            if self._active():
                                name_refresh.request_name_refresh(self.generation)
                                if hasattr(state, "record_activity"):
                                    state.record_activity("identity", "已核验群系统消息，立即检查名称变化", session_id=session, is_group=True)
                        continue
                    if self._active() and not self.should_ignore(resolved) and not self._is_self_echo(resolved):
                        with getattr(state, "run_lock", nullcontext()):
                            if self._active():
                                ident = self._extract_identities(resolved)
                                self._add_resolved_to_buffer(resolved, ident)
                except (ValueError, TypeError, AttributeError) as error:
                    if self._active():
                        self._reject_identity(original, str(error))
                except Exception:
                    if self._active():
                        self._reject_identity(original, "无法核验原始群发言人，请检查 WeFlow 连接")
        finally:
            with self._identity_lock:
                if self._identity_pending.get(session) is owned_queue:
                    self._identity_pending.pop(session, None)
            _IDENTITY_SLOTS.release()

    def _add_resolved_to_buffer(self, data, ident):
        if not self._active():
            return
        content = data.get("content", "")
        if _is_voice(data):
            transcript = data.get("transcription") or data.get("voiceText") or data.get("asrText") or data.get("transcript")
            if not transcript and content and content.strip() != "[语音]":
                transcript = content.replace("[语音]", "").strip()
            if transcript:
                data = dict(data, content=f"[对方语音转写] {transcript}", type=1, msgType=1)
                content = data["content"]
        is_media = content in _MEDIA_CONTENTS or str(data.get("type") or data.get("msgType")) in ("3", "43", "47") or _is_voice(data) or _is_video(data)
        association = (ident["session_id"], ident["sender_username"])
        mentioned = self._is_mentioned(data)
        mention_mode = ident["is_group"] and state.group_reply_mode == "mention"
        media_request = {}
        if not is_media and getattr(config, "BILI_VIDEO_ENABLED", True):
            import bili_video
            ref = data.get("_video_ref") if data.get("_share_card") else bili_video.find_video(content)
            if ref:
                media_request["video_ref"] = ref
            elif data.get("_bili_card"):
                media_request["unresolved_bili_card"] = True
            elif data.get("_lookup_share"):
                media_request["lookup_share"] = True
        if media_request and data.get("_share_card"):
            media_request["share_card"] = True
        if mention_mode and not mentioned and (is_media or media_request):
            self._cleanup_pending_mention_images()
            seq = self._next_sequence()
            with self._pending_lock:
                items = self._pending_mention_images.setdefault(association, [])
                items.append({"data": dict(data), "time": time.time(), "seq": seq, "chat_media": media_request})
                del items[:-8]
            return
        if is_media:
            self._queue_media(data, ident, self._next_sequence())
            return
        self._cleanup_pending_mention_images()
        if mention_mode and not mentioned:
            return
        if not data.get("_share_card") and not media_request and getattr(config, "QQ_MUSIC_ENABLED", True):
            import qq_music
            request = qq_music.parse_song_request(_song_command_text(data, mentioned))
            if request:
                media_request["song_request"] = request
        if mention_mode and not media_request.get("song_request"):
            with self._pending_lock:
                cached = self._pending_mention_images.pop(association, [])
            for item in cached:
                if time.time() - item["time"] < _PENDING_IMAGE_TTL:
                    cached_data = dict(item["data"])
                    if item.get("chat_media"):
                        self._append_message(ident, cached_data["content"], seq=item["seq"],
                            message_ids=_source_message_ids(cached_data), chat_media=item["chat_media"])
                        continue
                    if not ident["sender_stable"] or not (cached_data.get("senderUsername") or cached_data.get("senderWxid")):
                        cached_data["_association_reference"] = dict(data)
                    self._queue_media(cached_data, ident, item["seq"])
        if ident["is_group"] and state.group_reply_mode == "batch":
            content = _group_author_prefix(ident) + content
        if data.get("_share_card"):
            media_request["share_card"] = True
        self._append_message(ident, content, immediate=mentioned, mentioned=mentioned,
                             message_ids=_source_message_ids(data), chat_media=media_request)

    def process_sender(self, sender_id, version=None):
        if not self._active():
            # Pause cancels current work; resuming creates a new bridge generation.
            with self.buffer_lock:
                discarded = self.pending_buffers.pop(sender_id, None)
                if discarded:
                    self._discard_entry_media(discarded)
            return
        with self.buffer_lock:
            entry = self.pending_buffers.get(sender_id)
            if entry is None or entry["processing"]:
                return
            if version is not None and entry.get("timer_version", 0) != version:
                return
            target_id = stable_id.group_id(entry["session_id_data"]) if entry["is_group"] else stable_id.user_id(entry["session_id_data"], entry["session_id_data"])
            busy = getattr(state, "reply_target_busy", None)
            if not entry["retry_events"] and callable(busy) and busy(target_id, entry["is_group"], self.generation):
                self._arm(entry, sender_id, .5)
                return
            # A late caption occupies its original position. Later text cannot overtake it.
            count = 0
            for msg in (() if entry["retry_events"] else entry["messages"]):
                if isinstance(msg, dict) and not msg["ready"]:
                    break
                if isinstance(msg, dict) and msg.get("song_request"):
                    # Only this command is consumed by the direct song delivery.
                    if count == 0:
                        count = 1
                    break
                count += 1
                if isinstance(msg, dict) and not msg.get("share_card") and _conversation_command(msg.get("text", "")):
                    # Save the preceding evidence and execute this command alone;
                    # text after it belongs to the newly opened conversation.
                    break
            records = entry["messages"][:count]
            if records:
                advance = getattr(state, "advance_reply_revision", None)
                revision = advance(target_id, entry["is_group"], self.generation) if callable(advance) else None
                for record in records:
                    if isinstance(record, dict):
                        record["reply_revision"] = revision
            entry["memory_records"] = [{k: v for k, v in record.items() if k != "media_leases"}
                                       for record in records if isinstance(record, dict)]
            entry["identity_records"] = [{"username": record["sender_username"], "display_name": record.get("sender_name", ""),
                                           "original_message_id": record.get("original_message_id", ""),
                                           "message_ids": dict(record.get("message_ids", {}))}
                                          for record in records if isinstance(record, dict) and record.get("sender_username")]
            msgs = [msg["text"] if isinstance(msg, dict) else msg for msg in records]
            entry["forward_segments"] = [segment for msg in entry["messages"][:count] if isinstance(msg, dict)
                                         for segment in msg.get("segments", [])]
            if not msgs and not entry["retry_events"]:
                return
            del entry["messages"][:count]
            entry["processing"] = True
            if entry["messages"]:
                entry["first_at"] = entry["messages"][0].get("at", time.monotonic())
            # The worker's identity belongs to this frozen batch. A later member
            # can append to the same group buffer while this batch is processing.
            work_entry = dict(entry)
            if entry["identity_records"]:
                actor = entry["identity_records"][0]
                work_entry["sender_username"] = actor["username"]
                if entry["is_group"]:
                    work_entry["sender_in_group"] = actor["display_name"]
        records_requeued = False
        try:
            # Retrying a built event never repeats image generation, search, or desktop actions.
            while entry["retry_events"] and self._active():
                accepted = self._publish_event(entry["retry_events"][0])
                if accepted is None:
                    return
                if not accepted:
                    if msgs:
                        with self.buffer_lock:
                            entry["messages"] = records + entry["messages"]
                            records_requeued = True
                    return
                entry["retry_events"].popleft()
            if not entry["retry_events"]:
                with self.buffer_lock:
                    self._release_media_leases(entry["retry_media_leases"])
            if msgs and self._active():
                private_work = getattr(state, "private_processing", None)
                if not work_entry["is_group"] and callable(private_work):
                    private_work(work_entry["session_id_data"], True)
                try:
                    self._process_sender_body(sender_id, work_entry, msgs)
                finally:
                    if not work_entry["is_group"] and callable(private_work):
                        private_work(work_entry["session_id_data"], False)
        except Exception:
            log.exception("处理缓冲消息异常 [%s]", sender_id)
            # Fall back to one plain incoming event; retrying must not repeat a paid tool
            # or a desktop send that may already have succeeded before the exception.
            if self._active():
                session = work_entry["session_id_data"]
                uid = stable_id.user_id(session, work_entry.get("sender_username") or session) if work_entry["is_group"] else stable_id.user_id(session, session)
                segments = [{"type": "text", "data": {"text": "[系统提示：附加处理异常，请直接回复原始消息，不要声称工具已经执行。]\n" + "\n".join(msgs)}}] + work_entry.get("forward_segments", [])
                event = make_message_event("group" if work_entry["is_group"] else "private", uid, segments,
                                           group_id=stable_id.group_id(session) if work_entry["is_group"] else 0,
                                           group_name=work_entry.get("group_name", ""),
                                           nickname=work_entry.get("sender_in_group") or work_entry.get("source_name", ""))
                self._attach_event_identity(event, work_entry)
                if self._publish_event(event) is False:
                    with self.buffer_lock:
                        entry["retry_events"].append(event)
            if hasattr(state, "record_activity"):
                state.record_activity("error", "附加消息处理异常，已回退原始消息", session_id=entry["session_id_data"])
        finally:
            with self.buffer_lock:
                context_leases = work_entry.pop("context_media_leases", {})
                if entry["retry_events"] and self._active():
                    entry["retry_media_leases"].extend(context_leases.values())
                    context_leases.clear()
                else:
                    self._release_media_leases(context_leases)
                if not records_requeued or not self._active():
                    for record in records:
                        if not isinstance(record, dict):
                            continue
                        leases = record.pop("media_leases", {})
                        if entry["retry_events"] and self._active():
                            entry["retry_media_leases"].extend(leases.values())
                            leases.clear()
                        else:
                            self._release_media_leases(leases)
            self._release_buffer(sender_id)

    def _release_buffer(self, sender_id):
        with self.buffer_lock:
            entry = self.pending_buffers.get(sender_id)
            if entry is None:
                return
            if not self._active():
                self._discard_entry_media(entry)
                self._sched.cancel(sender_id)
                self.pending_buffers.pop(sender_id, None)
                return
            entry["processing"] = False
            if entry["retry_events"]:
                self._arm(entry, sender_id, 1.0)
            elif entry["messages"]:
                first = entry["messages"][0]
                if not isinstance(first, dict) or first["ready"]:
                    delay = self._buffer_delay(entry)
                    if getattr(config, "SMART_BUFFER_ENABLED", True):
                        # A burst that arrived during the previous worker has
                        # already spent part (or all) of its debounce window.
                        ready = []
                        for message in entry["messages"]:
                            if not isinstance(message, dict) or not message.get("ready"):
                                break
                            ready.append(message)
                        if ready:
                            age = max(0.0, time.monotonic() - ready[-1].get("at", time.monotonic()))
                            burst = float(getattr(config, "GROUP_BUFFER_SECONDS" if entry.get("is_group") else "PRIVATE_BUFFER_SECONDS", .7 if entry.get("is_group") else .35))
                            delay = max(.05, min(delay, burst - age))
                    self._arm(entry, sender_id, delay)
            else:
                self._sched.cancel(sender_id)
                self.pending_buffers.pop(sender_id, None)

    def _send(self, method, contact, *args, target_id=None, is_group=False, reply_revision=None, **kwargs):
        def cancelled():
            revision_check = getattr(state,"reply_revision_active",None)
            return not self._active() or bool(reply_revision is not None and callable(revision_check) and not revision_check(target_id,is_group,self.generation,reply_revision))
        if cancelled():
            return False
        wait_floor = getattr(humanize, "wait_response_floor", None)
        pop_started = getattr(state, "pop_response_started", None)
        if callable(wait_floor):
            started = pop_started(target_id, self.generation) if callable(pop_started) else None
            if not wait_floor(started if started is not None else time.monotonic(), cancelled):
                return False
        dispatch = getattr(state, "dispatch_send", None)
        if dispatch:
            if reply_revision is not None:
                kwargs["reply_revision"] = reply_revision
            return bool(dispatch(method, contact, *args, target_id=target_id, is_group=is_group,
                                 generation=self.generation, **kwargs))
        record = (state.identity_registry.get_group_record(target_id) if is_group
                  else state.identity_registry.get_private_record(target_id))
        return bool(getattr(self.sender, method)(contact, *args, identity=record,
                                                generation=self.generation,
                                                cancel_event=self.cancel_event, **kwargs))

    @staticmethod
    def _attach_event_identity(event, entry):
        # Preserve every verified author in a batch; aliases/display labels are
        # presentation data and never determine OneBot routing IDs.
        session = entry["session_id_data"]
        registry = getattr(state, "identity_registry", None)
        messages = []
        for row in entry.get("identity_records", []):
            actor = row["username"]
            item = dict(row, user_id=stable_id.user_id(session, actor), session_id=session,
                        group_id=stable_id.group_id(session) if entry["is_group"] else None)
            if not entry["is_group"]:
                getter = getattr(registry, "get_private_by_session", None)
                current = getter(actor) if callable(getter) else {}
                if isinstance(current, dict) and current.get("username") == actor:
                    for key in ("nickname", "remark"):
                        if isinstance(current.get(key), str):
                            item[key] = current[key]
                    item["display_name"] = current.get("name") or item.get("display_name", "")
            messages.append(item)
        display = entry.get("group_name", "") if entry["is_group"] else entry.get("source_name", "")
        if entry["is_group"]:
            getter = getattr(registry, "get_group_by_session", None)
            group = getter(session) if callable(getter) else {}
            if isinstance(group, dict) and group.get("session_id") == session:
                display = group.get("name") or display
            if display:
                event["group_name"] = display
        elif messages:
            current = messages[-1]
            display = current.get("display_name") or display
            if isinstance(event.get("sender"), dict):
                event["sender"]["nickname"] = current.get("nickname") or display
                if current.get("remark"):
                    event["sender"]["remark"] = current["remark"]
        event["wechat_identity"] = {"session_id": session, "is_group": entry["is_group"],
                                    "session_display_name": display, "messages": messages}
        if getattr(config, "CHARACTER_INTERESTS_ENABLED", False):
            from character_interests import snapshot
            interests = snapshot()
            if interests:
                event["wechat_character_interests"] = interests
        event["wechat_received_monotonic"] = min((record.get("at", time.monotonic()) for record in entry.get("memory_records", [])), default=time.monotonic())
        revisions = [record.get("reply_revision") for record in entry.get("memory_records", []) if isinstance(record.get("reply_revision"), int)]
        if revisions:
            event["_wechat_reply_revision"] = max(revisions)
        event["wechat_interrupt_previous"] = any(record.get("interrupt_previous") for record in entry.get("memory_records", []))

    def _process_sender_body(self, sender_id, entry, msgs):
        processing_started = time.monotonic()
        received_at = min((record.get("at", processing_started) for record in entry.get("memory_records", [])),
                          default=processing_started)
        contact = entry.get("contact", sender_id)
        is_group = entry.get("is_group", False)
        combined = "\n".join(msgs)
        original_combined = combined

        session_id_data = entry.get("session_id_data", sender_id)
        sender_username = entry.get("sender_username", "")
        if is_group:
            target_id = stable_id.group_id(session_id_data)
            user_id = stable_id.user_id(session_id_data, sender_username or session_id_data)
        else:
            user_id = stable_id.user_id(session_id_data, session_id_data)
            target_id = user_id
        revisions = [record.get("reply_revision") for record in entry.get("memory_records",[]) if isinstance(record.get("reply_revision"),int)]
        reply_revision = max(revisions) if revisions else None
        def current():
            check = getattr(state,"reply_revision_active",None)
            return self._active() and not (reply_revision is not None and callable(check) and not check(target_id,is_group,self.generation,reply_revision))
        if not current():
            return
        # Keep the user's evidence even when the participation decision is to
        # stay quiet. This work is local SQLite only, never a model round trip.
        self._record_incoming(entry, session_id_data, contact)
        records = entry.get("memory_records", [])
        command = _conversation_command(msgs[-1]) if msgs and not (records and records[-1].get("share_card")) else ''
        if command:
            self._forward_conversation_command(command, entry, user_id, target_id)
            return
        song = (records[0].get("song_request") if len(records) == 1 and getattr(config, "QQ_MUSIC_ENABLED", True) else None)
        shares = ([r for r in records if r.get("video_ref") or r.get("lookup_share") or r.get("unresolved_bili_card")]
                  if getattr(config, "BILI_VIDEO_ENABLED", True) else [])
        media_note = "" if shares or song else self._attach_recent_media(entry, original_combined)
        if media_note:
            combined = media_note + "\n\n" + combined
        mark = getattr(state, "mark_response_started", None)
        if callable(mark):
            mark(target_id, self.generation, received_at)
        suppress_target = str(target_id)
        with state.reply_state_lock:
            state.suppress_text_until.pop(suppress_target, None)
            state.pending_reply_modes.pop(target_id, None)
        state.prune_suppress()

        sender_name = entry.get("sender_in_group", "") or entry.get("source_name", "")
        group_display = entry.get("group_name", contact) if is_group else "私聊"
        # A preview title and description are publisher content, not an instruction
        # by the sender to generate a picture, speak, or execute a command.
        action_text = ("\n".join(r["text"] for r in records if not r.get("share_card"))
                       if any(r.get("share_card") for r in records) else original_combined)
        user_content = _strip_author_prefix(action_text)
        explicit_tool = _explicit_tool(action_text)
        explicit_voice = _explicit_voice(user_content) and config.VOICE_ENABLED
        should_reply, reply_type, reason, sticker_keyword, tool = True, "text", "private", "", explicit_tool
        image_status = "none"
        image_after_voice = None
        tone_hint = ""
        decision_context = None
        decision_source = "explicit_user_request_default"
        # A direct private media request already decides participation and mode.
        # Do not spend another model round trip on a choice overridden below.
        direct_private_media = not is_group and (explicit_voice or song or shares or
            config.TOOLS_ENABLED and explicit_tool in ("generate_image", "image_search"))
        if (is_group and config.GROUP_REPLY_JUDGE_ENABLED or
                not is_group and not direct_private_media and getattr(config, "HUMANLIKE_DECISION_ENABLED", False)):
            from interaction_context import chat_context
            actor_ids = {row["username"] for row in entry.get("identity_records", []) if isinstance(row, dict) and row.get("username")}
            decision_context = chat_context(state.memory_store, text=original_combined,
                session_id=session_id_data, actor_ids=actor_ids or {sender_username or session_id_data}, is_group=is_group,
                display_name=group_display, sender_name=sender_name, records=entry.get("identity_records", []))
            if is_group:
                # Preserve the native mention across buffering and media loading;
                # a changed group display name need not match BOT_NICKNAMES.
                decision_context["mentioned_bot"] = any(record.get("mentioned_bot") for record in entry.get("memory_records", []))
        decision_started = time.monotonic()
        decision_role = "group_reply_judge" if is_group and config.GROUP_REPLY_JUDGE_ENABLED else "moments_decision" if decision_context is not None else "none"
        if is_group and config.GROUP_REPLY_JUDGE_ENABLED:
            should_reply, reply_type, reason, sticker_keyword, judged_tool = reply_judge.should_reply_group(
                original_combined, group_name=group_display, sender_name=sender_name,
                context=decision_context, cancelled=lambda: not current())
            decision_source = "group_reply_judge" if reason not in {"no model response", "parse failed", "decision_failed", "cancelled"} else "group_judge_fallback"
            # Only the user's explicit request authorizes a tool. A judge cannot
            # convert ordinary discussion into a paid image/search operation.
            if not should_reply and reason in {"no model response", "parse failed", "decision_failed"} and decision_context.get("mentioned_bot"):
                should_reply, reply_type = True, "text"
            if not should_reply:
                log.info("群消息不回复 [%s]: %s", contact, reason)
                return
        elif not is_group and decision_context is not None:
            from humanlike_decision import choose
            should_reply, reply_type, reason, sticker_keyword, tone_hint = choose(decision_context, cancel=lambda: not current())
            decision_source = "shared_humanlike_decision" if reason not in {"decision_failed", "disabled", "cancelled"} else "private_text_fallback"
            if not should_reply and explicit_tool == "none" and not explicit_voice:
                return
            if explicit_tool != "none" or explicit_voice:
                should_reply = True  # A direct private tool request is not an ending pleasantry.
            if tone_hint:
                combined = "[交流语气：" + tone_hint + "]\n" + combined
        decision_seconds = max(0.0, time.monotonic() - decision_started) if decision_role != "none" else 0.0
        if explicit_voice:
            reply_type = "voice"
        if reply_type == "text_voice":
            reply_type = "voice"
        image_requested = bool(config.TOOLS_ENABLED and tool in ("generate_image", "image_search"))
        if image_requested and not explicit_voice and reply_type in ("voice", "text_voice"):
            # A requested picture should not occupy the desktop again with an
            # incidental voice after delivery. An explicit request for both stays valid.
            reply_type = "text"
        if reply_type == "voice" and not config.VOICE_ENABLED:
            reply_type = "text"
        if not current():
            return
        if song:
            self._deliver_song(entry, song, contact, target_id, reply_revision, current)
            return
        if shares:
            notes = self._attach_shared_videos(entry, shares, current)
            if not current():
                return
            combined += "\n" + "\n".join(notes)
            # The link is the media input for this turn, including when an
            # explicit "搜一下" accompanies it; generic search supplies no frames.
            if tool == "web_search":
                tool = "none"
        if reply_type == "sticker":
            # Judge-selected decoration cannot replace the answer or bypass
            # the complete-reply semantic/cooldown checks at stream completion.
            reply_type = "text_sticker"
        tool_started = time.monotonic()
        if config.TOOLS_ENABLED and tool != "none":
            if tool in ("generate_image", "image_search"):
                log.info("[tools] 图片请求已识别，执行 %s", tool)
                image_started = time.monotonic()
                if tool == "generate_image":
                    # Image planning still receives native-scoped context when
                    # the optional humanlike judge is disabled. Reuse a real
                    # decision when present; never manufacture another call.
                    if decision_context is None:
                        from interaction_context import chat_context
                        actor_ids = {row["username"] for row in entry.get("identity_records", []) if isinstance(row, dict) and row.get("username")}
                        decision_context = chat_context(state.memory_store, text=original_combined,
                            session_id=session_id_data, actor_ids=actor_ids or {sender_username or session_id_data}, is_group=is_group,
                            display_name=group_display, sender_name=sender_name, records=entry.get("identity_records", []))
                    image_context = dict(decision_context, humanlike_plan={"source": decision_source,
                        "should_reply": should_reply, "reply_type": reply_type,
                        "tone_hint": tone_hint, "sticker_keyword": sticker_keyword})
                    img_path = tools.generate_image(user_content, cancelled=lambda: not current(), context=image_context)
                else:
                    img_path = tools.search_image(user_content)
                log.info("[耗时] stage=image_%s chat=%s seconds=%.3f result=%s",
                         "generation" if tool == "generate_image" else "search",
                         "group" if is_group else "private",
                         max(0.0, time.monotonic() - image_started),
                         "cancelled" if not current() else "generated" if img_path else "failed")
                if not current():
                    return
                image_sent = False
                voice_position = user_content.find("语音")
                picture_position = min((user_content.find(word) for word in ("图片", "照片", "自拍", "图像", "图", "画", "生成")
                                        if user_content.find(word) >= 0), default=-1)
                if img_path and explicit_voice and 0 <= voice_position < picture_position:
                    image_after_voice = img_path
                elif img_path:
                    dispatch_started = time.monotonic()
                    image_sent = self._send("send_image", contact, img_path, target_id=target_id,
                                            is_group=is_group, reply_revision=reply_revision)
                    import delivery_context
                    delivery_context.record(session_id_data, "image", "confirmed" if image_sent else "unconfirmed",
                        actor_id=sender_username or session_id_data,
                        message_id=str(entry.get("memory_records", [{}])[-1].get("original_message_id", ""))
                        if entry.get("memory_records") else "")
                    log.info("[耗时] stage=image_dispatch chat=%s seconds=%.3f result=%s",
                             "group" if is_group else "private",
                             max(0.0, time.monotonic() - dispatch_started),
                             "sender_confirmed" if image_sent else "cancelled" if not current() else "unconfirmed")
                if not current():
                    return
                if image_after_voice:
                    image_status = "prepared"
                    combined = "[系统提示：图片已生成，尚未发送；程序会在本轮语音确认后发送图片，不要提前声称图片已发出。]\n" + combined
                elif image_sent:
                    image_status = "sent"
                    combined = "[系统提示：工具图片已经成功发送，无需重复请求发图。可以正常简短回应；后续问题请正常完整回答。]\n" + combined
                elif img_path:
                    image_status = "send_unconfirmed"
                    log.warning("[tools] 图片发送未确认，不自动重发")
                    combined = "[系统提示：图片发送未确认，请如实说明；不要声称已经发送，不要自动重发。]\n" + combined
                else:
                    image_status = "generation_failed"
                    log.warning("[tools] 图片生成或获取未完成，没有发送图片")
                    combined = "[系统提示：本次图片生成或获取失败，没有发送图片。请简短说明失败，不要声称已经发图。]\n" + combined
                import delivery_context
                if image_status == "generation_failed":  # Actual sends were recorded before cancellation checks.
                    delivery_context.record(session_id_data, "image", "failed",
                        actor_id=sender_username or session_id_data,
                        message_id=str(entry.get("memory_records", [{}])[-1].get("original_message_id", ""))
                        if entry.get("memory_records") else "")
            elif tool == "web_search":
                search_context = tools.web_search(user_content)
                if not current():
                    return
                if search_context.startswith("[web_search]"):
                    log.info("[tools] %s", search_context)
                    search_context += " 本次未取得可用资料，回答时请如实说明。"
                combined = search_context + "\n\n" + combined
        elif tool == "web_search":
            combined = "[联网搜索已关闭，本次未查询。回答时请如实说明。]\n" + combined
        tool_seconds = max(0.0, time.monotonic() - tool_started) if config.TOOLS_ENABLED and tool != "none" else 0.0
        log.info(f"推送 {len(msgs)} 条消息 [{'群' if is_group else '私'}|{contact}]")

        if state.memory_store is not None and config.MEMORY_ENABLED:
            try:
                actors = {record["username"] for record in entry.get("identity_records", [])}
                memories = state.memory_store.search(original_combined, config.MEMORY_TOP_K, session_id=session_id_data,
                                                     actor_ids=actors or {sender_username or session_id_data})
                if memories:
                    combined = state.memory_store.format_context(memories) + combined
            except Exception as e:
                log.warning(f"[memory] 检索注入异常: {e}")

        if is_group:
            group_id = target_id
            sender_name = entry.get("sender_in_group", "") or entry.get("source_name", "未知")
            if state.group_reply_mode == "batch":
                formatted = combined
            else:
                clean_text = combined
                for nick in config.BOT_NICKNAMES or ():
                    clean_text = clean_text.replace(f"@{nick}", "").replace(f"＠{nick}", "").strip()
                formatted = clean_text
                if sender_name:
                    formatted = f'{sender_name}（微信ID：{sender_username}）在群{entry.get("group_name", contact)}中说：{clean_text}'
            formatted = humanize.style_prefix() + formatted
            msg_segments = [
                {"type": "at", "data": {"qq": str(state._self_id_int)}},
                {"type": "text", "data": {"text": f" {formatted}"}},
            ]
            msg_segments.extend(entry.get("forward_segments", []))
            event = make_message_event("group", user_id, msg_segments,
                                       group_id=group_id,
                                       group_name=entry.get("group_name", contact),
                                       nickname=sender_name)
            state._ob_id_to_contact[group_id] = entry.get("group_name", contact)
        else:
            sender_name = entry.get("source_name", contact)
            event = make_message_event("private", user_id,
                                       [{"type": "text", "data": {"text": humanize.style_prefix() + combined}}] + entry.get("forward_segments", []),
                                       nickname=sender_name)
            state._ob_id_to_contact[user_id] = contact
        self._attach_event_identity(event, entry)
        import delivery_context
        event["wechat_delivery_feedback"] = {"session_id": session_id_data,
            "context": delivery_context.format_context(session_id_data)}
        event["wechat_reply_plan"] = {"reply_type": reply_type, "decision_source": decision_source,
                                      "image_status": image_status}
        event["_wechat_reply_user_text"] = user_content
        event["_wechat_image_requested"] = image_requested
        with state.ob_map_lock:
            if len(state._ob_id_to_contact) > 4000:
                extras = list(state._ob_id_to_contact.keys())[:2000]
                for k in extras:
                    state._ob_id_to_contact.pop(k, None)

        reply_mode = None
        if reply_type != "text":
            reply_mode = (target_id, {"reply_type": reply_type, "sticker_keyword": sticker_keyword,
                                     "request_message_id": event.get("message_id"),
                                     "generation": self.generation, "created_at": time.time()})
            if image_after_voice:
                reply_mode[1]["image_after_voice"] = image_after_voice
        push_started = time.monotonic()
        sent = self._publish_event(event, reply_mode)
        push_finished = time.monotonic()
        if sent is None:
            return
        request_id = event.get("wechat_reply_request", {}).get("request_id", "")
        turn = hashlib.sha256(str(request_id).encode()).hexdigest()[:12] if request_id else "-"
        log.info("[耗时] turn=%s stage=bridge_prepare chat=%s chars=%d buffer_seconds=%.3f decision_role=%s decision_seconds=%.3f context_seconds=%.3f tool_seconds=%.3f push_seconds=%.3f seconds=%.3f result=%s",
                 turn, "group" if is_group else "private", len(original_combined),
                 max(0.0, processing_started - received_at), decision_role, decision_seconds,
                 max(0.0, push_started - processing_started - decision_seconds - tool_seconds), tool_seconds,
                 max(0.0, push_finished - push_started), max(0.0, push_finished - received_at),
                 "outbox_accepted" if sent else "outbox_pending")
        if sent:
            log.info(f"✅ 已加入 AstrBot 待转交队列 [{contact}]")
        else:
            with self.buffer_lock:
                entry["retry_events"].append(event)
            log.error("[outbox] 事件尚未持久化，保留缓冲等待重试 [%s]", contact)

        if state.memory_store is not None and config.MEMORY_ENABLED:
            try:
                by_actor = {}
                for record in entry.get("memory_records", []):
                    actor = record.get("sender_username", "")
                    if not actor or not record.get("text"):
                        continue
                    message_id = record.get("original_message_id", "")
                    # Only the original user's text is evidence. Generated tool/model
                    # results and other members' statements never enter this claim.
                    text = _strip_author_prefix(record["text"])
                    if record.get("extractable", True):
                        by_actor.setdefault(actor, []).append((text, message_id))
                for actor, statements in by_actor.items():
                    state.memory_store.extract_memories_async("\n".join(text for text, _ in statements),
                        source=contact, session_id=session_id_data, actor_id=actor,
                        message_id=json.dumps([mid for _, mid in statements if mid], ensure_ascii=False),
                        source_message_ids=tuple(mid for _, mid in statements if mid))
            except Exception as e:
                log.warning(f"[memory] 后台抽取/记录异常: {e}")

    def _deliver_song(self, entry, song, contact, target_id, revision, current):
        import qq_music
        import delivery_context
        try:
            reply = qq_music.lookup_song_reply(song)
        except Exception:
            log.warning("[music] QQ 音乐检索失败", exc_info=True)
            reply = "暂时连不上 QQ 音乐的歌曲搜索，过一会儿再点吧。"
        if not current():
            return
        # A single literal bubble preserves the official link through UIA.
        # A failed/uncertain desktop result must never start a second reply.
        try:
            sent = self._send("send_text", contact, reply, target_id=target_id,
                              is_group=entry["is_group"], reply_revision=revision, stream_sentence=True)
            session = entry["session_id_data"]
            record = entry["memory_records"][0]
            delivery_context.record(session, "text", "confirmed" if sent else "unconfirmed",
                                    text=reply, actor_id=record["sender_username"], message_id=record["original_message_id"])
            if sent and state.memory_store is not None and config.MEMORY_ENABLED:
                state.memory_store.add_chat(reply, session, actor_id=config.BOT_WXID or "self", role="assistant")
        except Exception:
            log.exception("[music] 点歌发送或记录异常，不重复发送")

    @staticmethod
    def _lookup_share_card(session, record):
        ids = record.get("message_ids", {})
        if not ids:
            return {}
        # WeFlow's push often contains only a type-49 title. Only its exact
        # incoming row, in this chat and from this actor, can supply the URL.
        response = http_util.session().get(f"{config.WE_FLOW_BASE_URL}/api/v1/messages",
            params={"access_token": config.ACCESS_TOKEN, "talker": session, "media": "0", "limit": 100}, timeout=3)
        if response.status_code != 200:
            return {}
        body = response.json()
        if isinstance(body, dict) and (body.get("success") is False or body.get("talker") not in (None, "", session)):
            return {}
        rows = body.get("messages", []) if isinstance(body, dict) else body
        matches = [row for row in rows if isinstance(row, dict) and _same_message(ids, row)]
        if len(matches) != 1:
            return {}
        row = matches[0]
        actor = WeFlowBridge._sender_identity(row)
        if (any(row.get(key) not in (None, "", session) for key in ("sessionId", "talkerId"))
                or row.get("isSend") not in (False, 0, "0")
                or (actor or session.endswith("@chatroom")) and actor != record["sender_username"]):
            return {}
        return _share_card(row)

    def _attach_shared_videos(self, entry, shares, current):
        import bili_video
        session = entry["session_id_data"]
        notes = []
        if len(shares) > 1:
            notes.append(f"[本轮只处理最后一条分享。前面的 {len(shares)-1} 条未观看，不要描述其画面。]")
        record = shares[-1]
        actor, mid = record["sender_username"], record["original_message_id"]
        note = "本条分享没有可核验的视频地址，未取得画面。如需观看，请发送视频链接。"
        try:
            ref = record.get("video_ref")
            if record.get("lookup_share") and current():
                recovered = self._lookup_share_card(session, record)
                ref = recovered.get("_video_ref")
                if recovered and not ref and not (recovered.get("_bili_card") or recovered.get("_lookup_share")):
                    return notes  # The verified original is an unrelated app/link.
            if ref and current():
                note, paths = bili_video.inspect_video(ref, cancelled=lambda: not current())
                if not current():
                    return notes
                holder = {"media_leases": entry.setdefault("context_media_leases", {})}
                segments = []
                for path in paths:
                    self._lease_record_path(holder, path)
                    segments.append({"type": "image", "data": {"file": path, "wechat_session_id": session,
                                     "wechat_actor_id": actor, "wechat_message_id": mid}})
                entry.setdefault("forward_segments", []).extend(segments)
                if segments:
                    ident = {"session_id": session, "sender_username": actor, "sender_stable": True, "is_group": entry["is_group"]}
                    self._recent_media.remember(ident, dict(record, segments=segments), kind="视频")
        except Exception:
            note = "本条分享的视频画面读取失败，请如实说明，不能据标题描述画面。"
            log.warning("[video] 本条分享处理失败", exc_info=True)
        notes.append(f"[分享消息 {mid} 来自 {actor}：{note}]")
        return notes

    @staticmethod
    def _record_incoming(entry, session, source):
        store = state.memory_store
        if store is None or not config.MEMORY_ENABLED:
            return
        for record in entry.get("memory_records", []):
            actor = record.get("sender_username", "")
            text = _strip_author_prefix(record.get("text", ""))
            if not actor or not text:
                continue
            try:
                message_id = record.get("original_message_id", "")
                store.add_chat(text, session_id=session, actor_id=actor,
                               message_id=message_id, role="user", extractable=record.get("extractable", True))
                if record.get("extractable", True):
                    remember = getattr(store, "remember_explicit", None)
                    if callable(remember):
                        remember(text, source=source, session_id=session,
                                 actor_id=actor, message_id=message_id)
            except Exception:
                log.warning("[memory] 原始消息保存失败，聊天继续", exc_info=True)

    def _attach_recent_media(self, entry, text):
        if entry.get("forward_segments"):
            return ""
        records = entry.get("memory_records", [])
        actors = {record.get("sender_username") for record in records if record.get("sender_username")}
        sequences = [record["seq"] for record in records if isinstance(record.get("seq"), int)]
        if len(actors) != 1 or not sequences:
            return ""
        followup = _strip_author_prefix(text)
        for nick in config.BOT_NICKNAMES or ():
            followup = followup.replace('@' + nick, '').replace('＠' + nick, '').strip()
        selection = self._recent_media.select(entry["session_id_data"], next(iter(actors)),
            followup, before_seq=min(sequences),
            exclude_message_ids=[record.get("original_message_id", "") for record in records])
        if selection is None:
            return ""
        try:
            holder = {"media_leases": entry.setdefault("context_media_leases", {})}
            for segment in selection.segments:
                self._lease_record_path(holder, segment["data"]["file"])
            entry["forward_segments"] = selection.segments
            return selection.note
        finally:
            selection.close()

    def _forward_conversation_command(self, command, entry, user_id, target_id):
        session = entry["session_id_data"]
        if state.memory_store is not None and config.MEMORY_ENABLED:
            try:
                state.memory_store.checkpoint_session(session)
            except Exception:
                # Do not clear the conversation if its evidence cannot be saved.
                log.exception("[memory] 清空前保存失败，未执行聊天清空")
                if hasattr(state, "record_activity"):
                    state.record_activity("error", "聊天清空未执行：记忆保存失败", session_id=session)
                return
        self._recent_media.drop(session_id=session)
        import delivery_context
        delivery_context.clear(session)
        group = entry["is_group"]
        # A group debounce batch may contain preceding statements by others.
        # The command belongs to its own original sender, not the batch's first.
        command_entry = dict(entry)
        command_entry["identity_records"] = entry.get("identity_records", [])[-1:]
        command_entry["memory_records"] = entry.get("memory_records", [])[-1:]
        if group and command_entry["identity_records"]:
            author = command_entry["identity_records"][0]
            user_id = stable_id.user_id(session, author["username"])
            command_entry["sender_in_group"] = author.get("display_name", "")
        segments = ([{"type": "at", "data": {"qq": str(state._self_id_int)}}] if group else [])
        segments.append({"type": "text", "data": {"text": command}})
        event = make_message_event("group" if group else "private", user_id, segments,
            group_id=target_id if group else 0, group_name=entry.get("group_name", ""),
            nickname=command_entry.get("sender_in_group") or entry.get("source_name", ""))
        self._attach_event_identity(event, command_entry)
        event["wechat_reply_plan"] = {"reply_type": "text", "decision_source": "conversation_command", "image_status": "none"}
        with state.ob_map_lock:
            state._ob_id_to_contact[target_id] = entry.get("contact") or entry.get("source_name", "")
        if self._publish_event(event) is False:
            with self.buffer_lock:
                entry["retry_events"].append(event)

    def listen_sse(self, generation=None):
        """Return whether this attempt established a current 200 push stream."""
        gen = generation if generation is not None else state.bridge_generation
        if not state.running or state.bridge_generation != gen:
            return False
        sse_url = f"{config.WE_FLOW_BASE_URL}/api/v1/push/messages?access_token={config.ACCESS_TOKEN}"
        log.info("连接 WeFlow 推送服务: %s/api/v1/push/messages", config.WE_FLOW_BASE_URL)
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
        sess = None
        resp = None
        connected = False
        try:
            sess = http_util.new_session(pool=1)
            resp = sess.get(sse_url, headers=headers, stream=True, timeout=(5, 30))
            if not state.running or state.bridge_generation != gen:
                return False
            if resp.status_code != 200:
                if resp.status_code == 403:
                    log.error("连接失败: HTTP 403（请求被拒绝，请核对 WeFlow 消息推送开关及代理设置）")
                else:
                    log.error("连接失败: HTTP %s", resp.status_code)
                return False
            # Stop uses the same lifecycle lock when taking the response to
            # close. It must not miss a stream published just after its check.
            with state.run_lock:
                if not state.running or state.bridge_generation != gen:
                    return False
                connected = True
                self._sse_session = resp
                self._sse_response = resp
            resp.encoding = "utf-8"
            log.info("✅ 已连接到 WeFlow 推送")

            for line in resp.iter_lines(decode_unicode=True):
                if not state.running or state.bridge_generation != gen:
                    break
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if name_refresh.observe_event(data):
                    continue
                if data.get("event") == "message.revoke":
                    continue
                msg_time = data.get("timestamp") or data.get("time") or 0
                try:
                    msg_time = int(msg_time)
                except (TypeError, ValueError):
                    msg_time = 0
                if msg_time > 10 ** 12:
                    msg_time //= 1000
                if msg_time and msg_time < self.start_timestamp - 5:
                    continue
                raw_id = str(data.get("rawid") or data.get("id") or data.get("messageId") or "")
                if not raw_id:
                    import hashlib
                    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    raw_id = f"fallback:{digest}|{msg_time}"
                if self._mark_processed(f"{data.get('sessionId') or data.get('talkerId', '')}|{raw_id}"):
                    continue
                self.add_to_buffer(data)
        except Exception as e:
            if not state.running or state.bridge_generation != gen:
                return connected
            if "Connection" in type(e).__name__:
                log.error("无法连接 WeFlow")
            else:
                log.error("SSE 异常（%s）", type(e).__name__)
        finally:
            self._sse_session = None
            self._sse_response = None
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            if sess is not None:
                try:
                    sess.close()
                except Exception:
                    pass
        return connected

    def _fetch_wechat_image(self, talker: str, message=None) -> str | None:
        return self._fetch_wechat_media(talker, message, kind="image")

    def _fetch_wechat_media(self, talker: str, message=None, kind="image") -> str | None:
        """Fetch exactly this message's image/audio/video; never guess the latest attachment."""
        message = message or {}
        if kind not in ("image", "voice", "video"):
            raise ValueError("媒体类型不支持")
        if not self._active():
            return None
        sess = http_util.session()
        candidates = [message] if message.get("mediaUrl") else []
        try:
            ids = _message_ids(message)
            if not candidates and ids:
                resp = sess.get(f"{config.WE_FLOW_BASE_URL}/api/v1/messages",
                                params={"access_token": config.ACCESS_TOKEN, "talker": talker,
                                        "media": "1", "image": "1" if kind == "image" else "0",
                                        "voice": "1" if kind == "voice" else "0", "video": "1" if kind == "video" else "0",
                                        "emoji": "1" if kind == "image" else "0", "limit": 30}, timeout=8)
                if resp.status_code == 200:
                    data = resp.json()
                    messages = data if isinstance(data, list) else data.get("messages", data.get("data", [])) if isinstance(data, dict) else []
                    if isinstance(messages, dict):
                        messages = messages.get("messages", messages.get("list", []))
                    for msg in messages if isinstance(messages, list) else []:
                        if not isinstance(msg, dict):
                            continue
                        msg_talker = msg.get("sessionId") or msg.get("talkerId") or ""
                        if msg_talker and str(msg_talker) != str(talker):
                            continue
                        media_type = str(msg.get("mediaType", "")).lower()
                        allowed = ("image", "sticker", "emoji") if kind == "image" else ("video",) if kind == "video" else ("voice", "audio", "record")
                        if media_type and media_type not in allowed:
                            continue
                        expected_sender = message.get("senderUsername") or message.get("senderWxid")
                        observed_sender = msg.get("senderUsername") or msg.get("senderWxid")
                        if expected_sender and observed_sender and expected_sender != observed_sender:
                            continue
                        if _same_message(message, msg) and msg.get("mediaUrl"):
                            candidates.append(msg)
            if len({str(msg.get("mediaUrl")) for msg in candidates}) > 1:
                log.warning("[media] 本条媒体地址不唯一，拒绝猜测附件")
                return None
            for msg in candidates:
                if not self._active():
                    return None
                # SSE direct URLs are per-message evidence. Tokens only go to WeFlow's origin.
                media_url = urljoin(config.WE_FLOW_BASE_URL + "/", str(msg.get("mediaUrl", "")))
                base = urlparse(config.WE_FLOW_BASE_URL)
                parsed = urlparse(media_url)
                if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
                    continue
                same_origin = (parsed.scheme, parsed.hostname, parsed.port) == (base.scheme, base.hostname, base.port)
                if not same_origin:
                    log.warning("[media] 拒绝非 WeFlow 源媒体 URL")
                    continue
                max_bytes = int(getattr(config, "MEDIA_MAX_MB", 30) * 1024 * 1024)
                img_resp = sess.get(media_url, params={"access_token": config.ACCESS_TOKEN}, timeout=15, stream=True, allow_redirects=False)
                try:
                    if img_resp.status_code != 200:
                        continue
                    declared = img_resp.headers.get("Content-Length", "")
                    if declared and int(declared) > max_bytes:
                        continue
                    payload = bytearray()
                    deadline = time.monotonic()+30
                    for chunk in img_resp.iter_content(chunk_size=65536):
                        if not self._active():
                            return None
                        payload.extend(chunk)
                        if len(payload) > max_bytes or time.monotonic() > deadline:
                            raise ValueError("媒体超出大小或下载时限")
                    if kind == "image":
                        from PIL import Image
                        with Image.open(io.BytesIO(payload)) as image:
                            if image.width * image.height > 40_000_000:
                                raise ValueError("图片像素数量超出上限")
                            image.verify()
                            fmt = (image.format or "").lower()
                        ext = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp", "bmp": ".bmp"}.get(fmt)
                    elif kind == "video":
                        ext = (".mp4" if len(payload) >= 12 and payload[4:8] == b"ftyp" else
                               ".webm" if payload[:4] == b"\x1a\x45\xdf\xa3" else
                               ".avi" if payload[:4] == b"RIFF" and payload[8:12] == b"AVI " else None)
                    else:
                        ext = (".wav" if payload[:4] == b"RIFF" and payload[8:12] == b"WAVE" else
                               ".ogg" if payload[:4] == b"OggS" else ".amr" if payload.startswith(b"#!AMR") else
                               ".silk" if b"#!SILK_V3" in payload[:12] else ".mp3" if payload[:3] == b"ID3" or payload[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") else None)
                    if not ext:
                        continue
                    save_dir = os.path.join(config.ASTRBOT_ATTACHMENTS or config.DATA_DIR, {"image": "wechat_images", "video": "wechat_videos", "voice": "wechat_voice"}[kind])
                    os.makedirs(save_dir, exist_ok=True)
                    save_path = os.path.join(save_dir, f"wechat_{uuid.uuid4().hex}{ext}")
                    temp_path = save_path + ".part"
                    if not self._protect_media(message, save_path) and "_seq" in message:
                        return None
                    import media_util
                    with media_util.lease_file(save_path), media_util.lease_file(temp_path):
                        try:
                            with open(temp_path, "xb") as f:
                                f.write(payload)
                            os.replace(temp_path, save_path)
                        finally:
                            if os.path.exists(temp_path):
                                os.unlink(temp_path)
                        if kind in ("image", "video"):
                            _prune_dir(save_dir, keep=80)
                        else:
                            _prune_voice_cache()
                    return save_path
                finally:
                    img_resp.close()
        except Exception:
            log.exception("获取本条微信媒体失败 [%s]", talker)
        if not self._active():
            return None
        path = weflow_image.decode_latest_for_talker(talker, message=message) if kind == "image" else None
        if path and not self._protect_media(message, path) and "_seq" in message:
            return None
        return path

    def _finish_media(self, data, caption, kind="图片", segments=None):
        if not self._active():
            return
        key, seq = data.get("_buffer_key"), data.get("_seq")
        ident = data.get("_identity") or self._extract_identities(data)
        text = f"[{kind}: {caption or '内容无法描述'}]"
        if ident["is_group"] and state.group_reply_mode == "batch":
            text = _group_author_prefix(ident) + text
        with self.buffer_lock:
            entry = self.pending_buffers.get(key)
            if entry is None or not self._active():
                return
            for item in entry["messages"]:
                if isinstance(item, dict) and item["seq"] == seq:
                    item["text"], item["ready"] = text, True
                    if segments:
                        for segment in segments:
                            path = segment.get("data", {}).get("file")
                            if path:
                                self._lease_record_path(item, path)
                        item["segments"] = [dict(segment, data=dict(segment.get("data", {}),
                            wechat_session_id=ident["session_id"], wechat_actor_id=ident["sender_username"],
                            wechat_message_id=item.get("original_message_id", ""))) for segment in segments]
                        self._recent_media.remember(ident, item, kind=kind)
                    if not entry["processing"]:
                        self._arm(entry, key, 0.05)
                    return

    def _verify_media_sender(self, data):
        reference = data.get("_association_reference")
        if reference is None:
            return True
        if not _message_ids(data) or not _message_ids(reference) or not self._active():
            return False
        try:
            talker = data["_identity"]["session_id"]
            response = http_util.session().get(
                f"{config.WE_FLOW_BASE_URL}/api/v1/messages",
                params={"access_token": config.ACCESS_TOKEN, "talker": talker, "limit": 30}, timeout=3)
            if response.status_code != 200 or not self._active():
                return False
            body = response.json()
            rows = body if isinstance(body, list) else body.get("messages", [])
            senders = []
            for original in (data, reference):
                exact = [row for row in rows if isinstance(row, dict) and _same_message(original, row)
                         and (not (row.get("sessionId") or row.get("talkerId"))
                              or (row.get("sessionId") or row.get("talkerId")) == talker)]
                sender_ids = {row.get("senderUsername") or row.get("senderWxid") for row in exact}
                sender_ids.discard(None)
                sender_ids.discard("")
                if len(sender_ids) != 1:
                    return False
                senders.append(sender_ids.pop())
            return senders[0] == senders[1]
        except Exception:
            log.debug("[media] 未能核实图片发言者", exc_info=True)
            return False

    def process_image_message(self, data):
        # Public compatibility entry; standard receive path has already reserved an ordered slot.
        if "_seq" not in data:
            self._queue_media(data)
            return
        if not self._active():
            return
        if not self._verify_media_sender(data):
            self._finish_media(data, "无法确认属于当前发言者，请重新发送图片并@")
            return
        try:
            path = self._fetch_wechat_image(data["_identity"]["session_id"], data)
            if path and self._active():
                self._finish_media(data, "原图已附上，请主模型直接查看，不要猜测缺失画面", segments=[{"type": "image", "data": {"file": path}}])
                return
        except Exception:
            log.exception("[media] 本条图片获取异常")
        self._finish_media(data, "本条原图尚未就绪，请对方稍后重发或补充文字")

    def process_emoji_message(self, data):
        if "_seq" not in data:
            self._queue_media(data)
            return
        if not self._active():
            return
        if not self._verify_media_sender(data):
            self._finish_media(data, "无法确认属于当前发言者，请重新发送表情并@", kind="表情")
            return
        try:
            path = self._fetch_wechat_image(data["_identity"]["session_id"], data)
            if path and self._active():
                import multimodal
                paths = multimodal.sticker_frame_paths(path, cancelled=lambda: not self._active())
                self._finish_media(data, "表情画面已附上，请主模型直接查看" + ("；依次为动画抽帧" if len(paths) > 1 else ""), kind="表情",
                    segments=[{"type": "image", "data": {"file": frame}} for frame in paths])
                return
        except Exception:
            log.exception("[media] 本条表情获取异常")
        self._finish_media(data, "本条表情尚未就绪，请对方稍后重发或补充文字", kind="表情")

    def process_video_message(self, data):
        if "_seq" not in data:
            self._queue_media(data)
            return
        if not self._active():
            return
        if not self._verify_media_sender(data):
            self._finish_media(data, "无法确认属于当前发言者，请重新发送视频并@", kind="视频")
            return
        try:
            path = self._fetch_wechat_media(data["_identity"]["session_id"], data, kind="video")
            if path and self._active():
                if getattr(config, "CHAT_VIDEO_MODE", "frames") == "native":
                    segments = [{"type": "video", "data": {"file": path}}]
                    note = "原视频已附上，需要主模型提供商支持视频输入"
                else:
                    import multimodal
                    paths = multimodal.video_frame_paths(path, cancelled=lambda: not self._active())
                    segments = [{"type": "image", "data": {"file": frame}} for frame in paths]
                    note = f"本条视频已抽取 {len(paths)} 张顺序画面；仅查看前 {getattr(config, 'CHAT_VIDEO_MAX_SECONDS', 120)} 秒内的部分画面，未包含声音，不要推断未见内容"
                self._finish_media(data, note, kind="视频", segments=segments)
                return
        except Exception:
            log.exception("[media] 本条视频处理失败")
        self._finish_media(data, "原视频未就绪或抽帧失败，请检查 FFmpeg 或重发、补充文字", kind="视频")

    def process_voice_message(self, data):
        if "_seq" not in data:
            self._queue_media(data)
            return
        if not self._active():
            return
        if not self._verify_media_sender(data):
            self._finish_media(data, "无法确认属于当前发言者，请重新发送并@", kind="语音消息")
            return
        path = self._fetch_wechat_media(data["_identity"]["session_id"], data, kind="voice")
        if path:
            self._finish_media(data, "音频已附上，可进行语音识别", kind="语音消息",
                               segments=[{"type": "record", "data": {"file": path}}])
        else:
            self._finish_media(data, "音频尚未就绪，请对方稍后重发或补充文字", kind="语音消息")
            if hasattr(state, "record_activity") and self._active():
                state.record_activity("media", "收到语音，未能取得音频", session_id=data["_identity"]["session_id"])

    def _inject_cached_image(self, session_id, buffer_key, version, ident=None):
        # Retained for integrations; cached images must carry their original message, never latest.
        log.warning("[media] 无原始消息标识的旧式图片注入已忽略 [%s]", session_id)

    def add_text_to_buffer(self, session_id_data, source_name, group_name,
                           session_id, content, is_group, sender_key, sender_username=""):
        ident = self._extract_identities({"sessionId": session_id or session_id_data,
                                         "sourceName": source_name, "groupName": group_name,
                                         "senderUsername": sender_username,
                                         "sessionType": "group" if is_group else "private"})
        self._append_message(ident, content)



