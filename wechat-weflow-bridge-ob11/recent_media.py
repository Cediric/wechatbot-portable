"""Short-lived, identity-bound references to already verified inbound media.

This cache never downloads, decodes or describes media. Selections retain the
original files for one main-model request; only provenance belongs in text.
"""
from collections import OrderedDict
from contextlib import ExitStack
import json
import math
import os
import re
import threading
import time

import stable_id

_ID = re.compile(r"[A-Za-z0-9_.:@-]{1,512}\Z")
_SOURCE_KEYS = ("rawid", "rawId", "serverIdRaw", "serverId", "messageId", "msgId", "id", "localId")
_EXPLICIT = re.compile(
    r"(?:刚才|刚刚|上面|前面|之前|前一条|上一条|我发的).{0,12}(?:图片|照片|截图|视频|画面|表情|图)"
    r"|(?:这|那)(?:张|幅|段|个)?(?:图片|照片|截图|视频|画面|表情|图)"
    r"|(?:这些|那些)(?:图片|照片|截图|视频|画面|表情|图)"
    r"|(?:图片|照片|截图|视频|画面|图)(?:里|中|上|里面)|(?:看图|识图|读图)", re.I)
_SHORT = re.compile(
    r"(?:(?:你|你们|帮我|请|能|可以|能不能|能否|再|也|给我)\s*)*"
    r"(?:这(?:个)?是(?:什么|啥)|这(?:个)?(?:什么意思|啥意思|怎么回事|有什么问题|有啥问题)"
    r"|(?:看|看得|看不看得|看得明|看不看得明)(?:见|懂|清|清楚|白)|看得到"
    r"|看到了|看见了|看到(?:了)?什么|好看|怎么样|什么意思|啥意思|看看|看一下|说说看|评价一下|分析一下)"
    r"(?:吗|么|呢|啊|呀|不|没|了|的|一下|这个|这张|这个图)*[？?！!。.,，~～\s]*\Z")


def _identity(session, actor):
    try:
        stable_id.user_id(session, actor)
        return bool(actor)
    except (TypeError, ValueError, AttributeError):
        return False


def _token(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    value = str(value)
    return value if _ID.fullmatch(value) and not re.fullmatch(r"0+", value) else ""


def _signature(path):
    stat = os.stat(path)
    if not os.path.isfile(path) or stat.st_size <= 0:
        raise ValueError("missing or empty media")
    return (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)


class RecentMediaSelection:
    """Caller must close after taking its own leases or handing off to outbox."""
    def __init__(self, segments, sources, leases):
        self.segments = segments
        self.sources = sources
        self._leases = leases
        self.note = ("[历史媒体引用：以下附件来自当前会话中同一发言者此前发送的原消息，按原消息顺序排列，"
                     "不是本条文字新发送的附件，也不是对画面的描述；仅依据实际附上的画面回答，"
                     "视频抽帧不含声音且不代表完整视频。来源："
                     + json.dumps(sources, ensure_ascii=False, separators=(",", ":")) + "]")

    def close(self):
        self._leases.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RecentMediaCache:
    def __init__(self, *, ttl_seconds=180, followup_seconds=45, max_records=64,
                 max_bytes=64 * 1024 * 1024, max_per_actor=3, max_files=12,
                 clock=time.monotonic, automatic_cleanup=True):
        for value in (ttl_seconds, followup_seconds, max_records, max_bytes, max_per_actor, max_files):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("recent media limits must be positive and finite")
        self.ttl_seconds = min(float(ttl_seconds), 600)
        self.followup_seconds = min(float(followup_seconds), self.ttl_seconds)
        self.max_records = max(1, min(int(max_records), 128))
        self.max_bytes = max(1, min(int(max_bytes), 128 * 1024 * 1024))
        self.max_per_actor = max(1, min(int(max_per_actor), 4))
        self.max_files = max(1, min(int(max_files), 16))
        self._clock = clock
        self._automatic_cleanup = automatic_cleanup
        self._records = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self._timer = None
        self._closed = False

    def _remove(self, key):
        item = self._records.pop(key)
        self._bytes -= item["bytes"]
        item["leases"].close()

    def _prune(self):
        now = self._clock()
        for key, item in list(self._records.items()):
            if now >= item["expires"]:
                self._remove(key)

    def _arm(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._closed or not self._automatic_cleanup or not self._records:
            return
        delay = max(.01, min(item["expires"] for item in self._records.values()) - self._clock())
        self._timer = threading.Timer(delay, self.prune)
        self._timer.daemon = True
        self._timer.start()

    def prune(self):
        """Expire references even during idle chats; also useful for deterministic tests."""
        with self._lock:
            self._prune()
            self._arm()

    def remember(self, identity, record, *, kind="图片"):
        if not isinstance(identity, dict) or not isinstance(record, dict):
            return False
        session, actor = identity.get("session_id"), identity.get("sender_username")
        if not _identity(session, actor) or identity.get("sender_stable") is not True:
            return False
        if identity.get("is_group") != session.endswith("@chatroom"):
            return False
        if record.get("sender_username") != actor or kind not in ("图片", "视频", "表情"):
            return False
        message_id = _token(record.get("original_message_id"))
        raw_ids = record.get("message_ids", {})
        if not isinstance(raw_ids, dict):
            return False
        message_ids = {key: value for key in _SOURCE_KEYS if (value := _token(raw_ids.get(key)))}
        if not message_id or (message_ids and message_id not in message_ids.values()):
            return False
        seq = record.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return False
        segments = record.get("segments", [])
        if not isinstance(segments, (tuple, list)) or not 0 < len(segments) <= self.max_files:
            return False
        key = (session, actor, message_id)
        with self._lock:
            self._prune()
            if self._closed or key in self._records:
                return False
            now = self._clock()
            received = record.get("at", now)
            if not isinstance(received, (int, float)) or not math.isfinite(received):
                return False
            received = min(now, received)
            if now >= received + self.ttl_seconds:
                return False
            import media_util
            leases = ExitStack()
            copied, files = [], {}
            try:
                with media_util._LEASE_LOCK:
                    for segment in segments:
                        data = segment.get("data", {})
                        path = data.get("file")
                        if (segment.get("type") not in ("image", "video") or not isinstance(path, str)
                                or not os.path.isabs(path) or path.startswith(("\\\\", "//"))):
                            raise ValueError("only original local visual attachments are reusable")
                        if any(data.get(name) not in (None, "", expected) for name, expected in (
                            ("wechat_session_id", session), ("wechat_actor_id", actor), ("wechat_message_id", message_id))):
                            raise ValueError("attachment identity mismatch")
                        real = os.path.normcase(os.path.realpath(path))
                        if real not in files:
                            leases.enter_context(media_util.lease_file(path))
                            files[real] = _signature(path)
                        # No remote URL, base64 payload or arbitrary inbound text is retained.
                        copied.append({"type": segment["type"], "data": {
                            "file": path, "wechat_session_id": session, "wechat_actor_id": actor,
                            "wechat_message_id": message_id, "wechat_recent_media": True}})
                    size = sum(signature[0] for signature in files.values())
                    if size > self.max_bytes:
                        raise ValueError("recent media byte budget exceeded")
                self._records[key] = dict(session=session, actor=actor, message_id=message_id,
                    message_ids=message_ids, seq=seq, received=received, expires=received + self.ttl_seconds,
                    segments=copied, files=files, bytes=size, leases=leases, kind=kind, implicit=True)
                self._bytes += size
                owned = sorted((k for k in self._records if k[:2] == key[:2]), key=lambda k: self._records[k]["seq"])
                while len(owned) > self.max_per_actor:
                    self._remove(owned.pop(0))
                while len(self._records) > self.max_records or self._bytes > self.max_bytes:
                    oldest = min(self._records, key=lambda k: (self._records[k]["received"], self._records[k]["seq"]))
                    self._remove(oldest)
                self._arm()
                return key in self._records
            except (OSError, ValueError, TypeError, AttributeError):
                leases.close()
                return False

    def select(self, session_id, actor_id, text, *, before_seq, exclude_message_ids=()):
        """Call on every text turn: unrelated text ends the implicit followup window.

        A selection is a recent burst of originals, never a guessed image from
        another actor. Explicit references can recall it within TTL; an implicit
        visual followup is allowed once, within the shorter followup window.
        """
        if not _identity(session_id, actor_id) or not isinstance(before_seq, int) or not isinstance(text, str):
            return None
        with self._lock:
            self._prune()
            if self._closed:
                return None
            rows = [item for item in self._records.values()
                    if (item["session"], item["actor"]) == (session_id, actor_id) and item["seq"] < before_seq]
            explicit = bool(_EXPLICIT.search(text))
            short = len(text.strip()) <= 48 and bool(_SHORT.fullmatch(text.strip()))
            if not explicit and not short:
                for item in rows:
                    item["implicit"] = False
                return None
            excluded = {str(value) for value in exclude_message_ids}
            now = self._clock()
            eligible = [item for item in rows if not excluded.intersection({item["message_id"], *item["message_ids"].values()})
                        and (explicit or item["implicit"] and now - item["received"] <= self.followup_seconds)]
            if not eligible:
                return None
            eligible.sort(key=lambda item: item["seq"])
            # Only a contiguous burst, bounded by source arrival time. Old media
            # from the same actor is not silently bundled into a fresh question.
            latest = eligible[-1]["received"]
            eligible = [item for item in eligible if abs(latest - item["received"]) <= self.followup_seconds]
            # Keep complete media messages and prefer the newest burst when the
            # attachment budget cannot fit all of its frames; never truncate a video.
            planned, file_count = [], 0
            for item in reversed(eligible):
                if file_count + len(item["segments"]) <= self.max_files:
                    planned.append(item)
                    file_count += len(item["segments"])
            eligible = list(reversed(planned))
            import media_util
            leases = ExitStack()
            segments, sources = [], []
            with media_util._LEASE_LOCK:
                for item in eligible:
                    try:
                        if any(_signature(path) != signature for path, signature in item["files"].items()):
                            raise ValueError("original file changed")
                    except (OSError, ValueError):
                        self._remove((item["session"], item["actor"], item["message_id"]))
                        continue
                    if len(segments) + len(item["segments"]) > self.max_files:
                        continue
                    for path in item["files"]:
                        leases.enter_context(media_util.lease_file(path))
                    segments.extend({"type": segment["type"], "data": dict(segment["data"])} for segment in item["segments"])
                    sources.append({"session_id": session_id, "actor_id": actor_id,
                                    "message_id": item["message_id"], "message_ids": dict(item["message_ids"]),
                                    "seq": item["seq"], "kind": item["kind"], "attachments": len(item["segments"])})
            for item in rows:
                item["implicit"] = False
            if not segments:
                leases.close()
                return None
            return RecentMediaSelection(segments, sources, leases)

    def drop(self, session_id=None, actor_id=None):
        with self._lock:
            for key in list(self._records):
                if (session_id is None or key[0] == session_id) and (actor_id is None or key[1] == actor_id):
                    self._remove(key)
            self._arm()

    def close(self):
        with self._lock:
            self._closed = True
            self.drop()
