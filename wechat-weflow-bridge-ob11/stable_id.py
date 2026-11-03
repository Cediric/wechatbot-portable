"""
stable_id.py — 稳定身份识别与名字注册表
========================================

用微信侧的稳定标识（sessionId / senderUsername / @chatroom）生成 OneBot 的
user_id / group_id，避免因为用户改昵称、群昵称、群名导致身份漂移。

同时本地维护 “ID → 最新名字” 映射，并持久化到 data/identity_map.json，
这样即使重启后也能用最新名字定位聊天窗口。
"""

import hashlib
import json
import os
import re
import threading
import time
import uuid

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
IDENTITY_FILE = os.path.join(DATA_DIR, "identity_map.json")


def validate_username(value, *, group=False):
    """Validate an identifier from a documented identity field, never a display label."""
    if not isinstance(value, str) or not value or len(value) > 256 or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", value):
        raise ValueError("微信身份字段缺失或格式无效")
    is_group = value.endswith("@chatroom") and len(value) > len("@chatroom")
    if is_group != bool(group) or value in ("unknown", "null", "None"):
        raise ValueError("微信用户/群身份类型不一致")
    return value


def stable_id(value: str) -> int:
    """把任意微信侧字符串映射为稳定的正整数 ID。

    使用 SHA-256 而不是内置 hash()，保证跨进程、跨重启稳定。
    """
    if not value:
        value = "unknown"
    digest = hashlib.sha256(value.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


def user_id(session_id: str, sender_username: str = "") -> int:
    """私聊/群成员稳定 user_id。

    私聊：sessionId（wxid）
    群聊：group_session + senderUsername，这样成员改群昵称也不影响 ID。
    """
    group = session_id.endswith("@chatroom") if isinstance(session_id, str) else False
    validate_username(session_id, group=group)
    if group and not sender_username:
        raise ValueError("群成员身份必须带实际发言人微信 ID")
    if not group:
        if sender_username and sender_username != session_id:
            raise ValueError("私聊微信 ID 与会话 ID 不一致")
        sender_username = session_id
    if sender_username:
        validate_username(sender_username)
        return stable_id(f"u:{session_id}:{sender_username}")
    return stable_id(f"u:{session_id}")


def group_id(session_id: str) -> int:
    """群聊稳定 group_id，基于群 sessionId（@chatroom）。"""
    return stable_id(f"g:{validate_username(session_id, group=True)}")


def member_id(group_session: str, sender_username: str) -> int:
    """群成员稳定 ID（备用）。"""
    validate_username(group_session, group=True)
    validate_username(sender_username)
    return stable_id(f"m:{group_session}:{sender_username}")


class IdentityRegistry:
    """ID → 最新名字 注册表，自动持久化到 data/identity_map.json。"""

    def __init__(self, path: str = IDENTITY_FILE):
        self._lock = threading.RLock()
        self.path = path
        self.private = {}
        self.groups = {}
        self.members = {}
        self.revision = 0
        self._dirty = False
        self._save_timer = None
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.private = self._validated_records(data.get("private", {}), "private")
                self.groups = self._validated_records(data.get("groups", {}), "groups")
                self.members = self._validated_records(data.get("members", {}), "members")
                self.revision = int(data.get("revision", 0))
                if not all(isinstance(value, dict) for value in (self.private, self.groups, self.members)):
                    raise ValueError("身份映射格式无效")
        except Exception:
            self.private = {}
            self.groups = {}
            self.members = {}

    @staticmethod
    def _validated_records(records, kind):
        if not isinstance(records, dict):
            raise ValueError("身份映射格式无效")
        accepted = {}
        for key, record in records.items():
            try:
                if not isinstance(record, dict):
                    continue
                session = record.get("group_session" if kind == "members" else "session_id")
                validate_username(session, group=kind != "private")
                if kind == "private":
                    if record.get("username", session) != session:
                        continue
                    expected = user_id(session, session)
                elif kind == "groups":
                    expected = group_id(session)
                else:
                    expected = user_id(session, validate_username(record.get("username")))
                if str(key) != str(expected):
                    continue
                safe = dict(record)
                safe["aliases"] = [name for name in record.get("aliases", []) if isinstance(name, str)][-16:] if isinstance(record.get("aliases", []), list) else []
                if not isinstance(safe.get("name"), str):
                    safe["name"] = session
                accepted[str(expected)] = safe
            except (ValueError, TypeError):
                continue
        return accepted

    def _save(self):
        tmp = None
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + "." + uuid.uuid4().hex + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "private": self.private,
                    "groups": self.groups,
                    "members": self.members,
                    "revision": self.revision,
                }, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
            return True
        except Exception as e:
            print(f"[identity] 保存身份映射失败: {e}")
            return False
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _schedule_save(self):
        self._dirty = True
        if self._save_timer is not None:
            return
        t = threading.Timer(2.0, self.flush)
        t.daemon = True
        self._save_timer = t
        t.start()

    def flush(self):
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            self._save_timer = None
            if not self._dirty:
                return
            self._dirty = not self._save()

    @staticmethod
    def _aliases(old, *names):
        aliases = list(old.get("aliases", []))
        for name in (old.get("name", ""), *names):
            if name and name not in aliases:
                aliases.append(name)
        return aliases[-16:]

    def get_revision(self):
        with self._lock:
            return self.revision


    # ---------- 注册 ----------

    def register_private(self, session_id: str, name: str, username: str = "", *,
                         remark=None, nickname=None, alias=None, source="message") -> int:
        # A private chat's identity must survive both nickname and remark changes.
        validate_username(session_id)
        if username and username != session_id:
            raise ValueError("私聊微信 ID 与会话 ID 不一致")
        username = session_id
        uid = user_id(session_id, username)
        with self._lock:
            key = str(uid)
            old = self.private.get(key, {})
            old_name = old.get("name", "")
            actual_remark = old.get("remark", "") if remark is None else str(remark)
            actual_nick = (nickname if nickname is not None else name) or old.get("nickname", "")
            actual_name = actual_remark or name or actual_nick or old_name or str(uid)
            actual_alias = old.get("alias", "") if alias is None else str(alias)
            if (old_name == actual_name and old.get("session_id") == session_id
                    and old.get("username") == username and old.get("remark", "") == actual_remark
                    and old.get("nickname", "") == actual_nick and old.get("alias", "") == actual_alias):
                return uid
            self.private[key] = {
                "session_id": session_id,
                "name": actual_name, "remark": actual_remark, "nickname": actual_nick,
                "username": username, "alias": actual_alias,
                "aliases": self._aliases(old, name, actual_name, actual_nick, actual_alias),
                "source": source,
                "updated_at": time.time(),
            }
            self.revision += 1
            self._schedule_save()
            return uid

    def register_group(self, session_id: str, name: str, *, source="message", observed_at=None) -> int:
        gid = group_id(session_id)
        with self._lock:
            key = str(gid)
            old = self.groups.get(key, {})
            old_name = old.get("name", "")
            stamp = time.time() if observed_at is None else float(observed_at)
            if source == "session" and (old.get("source") in ("contact", "snapshot") or old.get("contact_observed_at")):
                # /sessions exposes WeFlow's avatar/name cache; contacts is the
                # authoritative contact.db snapshot for a group represented there.
                return gid
            if source == "message" and old.get("contact_observed_at"):
                # SSE groupName uses the same cached session/avatar name. A later
                # message timestamp does not prove that its name was freshly read.
                return gid
            if source == "message" and old and stamp < old.get("name_observed_at", 0):
                return gid
            if old_name == (name or old_name) and old.get("session_id") == session_id:
                old["name_observed_at"] = max(stamp, old.get("name_observed_at", 0))
                if source in ("contact", "snapshot"):
                    old["contact_observed_at"] = stamp
                    self._schedule_save()
                if old.get("source") != "contact" or source != "session":
                    old["source"] = source
                return gid
            self.groups[key] = {
                "session_id": session_id,
                "name": name or old_name or str(gid),
                "aliases": self._aliases(old, name),
                "source": source,
                "name_observed_at": stamp,
                "contact_observed_at": stamp if source in ("contact", "snapshot") else old.get("contact_observed_at", 0),
                "updated_at": time.time(),
            }
            self.revision += 1
            self._schedule_save()
            return gid

    def register_member(self, group_session: str, username: str, name: str) -> int:
        # OneBot's member query/list IDs must equal the sender ID on incoming events.
        mid = user_id(group_session, username)
        with self._lock:
            key = str(mid)
            old = self.members.get(key, {})
            old_name = old.get("name", "")
            if old_name == (name or old_name) and old.get("username") == username:
                return mid
            self.members[key] = {
                "group_session": group_session,
                "username": username,
                "name": name or old_name or username or str(mid),
                "aliases": self._aliases(old, name),
                "updated_at": time.time(),
            }
            self.revision += 1
            self._schedule_save()
            return mid

    # ---------- 查询 ----------

    def get_private_name(self, uid: int) -> str:
        with self._lock:
            item = self.private.get(str(uid))
            return (item or {}).get("name", "") or str(uid)

    def get_group_name(self, gid: int) -> str:
        with self._lock:
            item = self.groups.get(str(gid))
            return (item or {}).get("name", "") or str(gid)

    def get_member_name(self, mid: int) -> str:
        with self._lock:
            item = self.members.get(str(mid))
            if item is None:
                item = next((record for record in self.members.values()
                             if user_id(record.get("group_session", ""), record.get("username", "")) == mid), None)
            return (item or {}).get("name", "") or str(mid)

    def get_target_name(self, target_id: int, is_group: bool = False) -> str:
        return self.get_group_name(target_id) if is_group else self.get_private_name(target_id)

    def get_private_by_session(self, session_id: str) -> dict:
        uid = user_id(session_id, session_id)
        with self._lock:
            direct = self.private.get(str(uid))
            if direct:
                return dict(direct)
            return next((dict(item) for item in self.private.values() if item.get("session_id") == session_id), {})

    def get_group_by_session(self, session_id: str) -> dict:
        gid = group_id(session_id)
        with self._lock:
            return dict(self.groups.get(str(gid), {}))

    def get_private_record(self, uid: int) -> dict:
        with self._lock:
            return dict(self.private.get(str(uid), {}))

    def get_group_record(self, gid: int) -> dict:
        with self._lock:
            return dict(self.groups.get(str(gid), {}))

    # ---------- 统计/清空 ----------

    def stats(self) -> dict:
        with self._lock:
            return {
                "private": len(self.private),
                "groups": len(self.groups),
                "members": len(self.members),
                "revision": self.revision,
            }

    def clear(self):
        with self._lock:
            self.private = {}
            self.groups = {}
            self.members = {}
            self.revision += 1
            self._dirty = True
            self._dirty = not self._save()
