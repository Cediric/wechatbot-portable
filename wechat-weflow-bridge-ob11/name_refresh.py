"""Stable-ID contact names: live observations and a small local API polling monitor.

WeFlow documents message.new/revoke SSE events, not a rename revision feed. A reviewed
upstream identity-snapshot extension is detected automatically. Legacy polling remains
available, with its upstream cache limitation exposed in status rather than hidden.
Network refresh never delays a normal send that already has a reliable cached name.
"""
from __future__ import annotations
import atexit
import hashlib
import json
import logging
import re
import threading
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor

import config
import state
import http_util
import stable_id

log = logging.getLogger("ob11-bridge")
_REFRESH_TTL = 10.0
_last_refresh = {}
_refresh_lock = threading.Lock()
_sync_lock = threading.Lock()
_inflight = set()
_etags = {}
_fingerprints = {}
_last_sync = 0.0
_last_status = {"status": "idle", "changed": 0, "last_sync": 0}
_snapshot_probe = {}
_requested = set()
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="identity-refresh")
atexit.register(lambda: _POOL.shutdown(wait=False, cancel_futures=True))


def _sync_interval():
    return max(1.0, min(300.0, float(getattr(config, "CONTACT_REFRESH_SECONDS", getattr(config, "NAME_SYNC_SECONDS", 3.0)))))


def _valid_generation(generation):
    return generation is None or (state.running and state.bridge_generation == generation)


def _clean_group_name(name):
    return str(name or "").strip()


def _get_list(endpoint, *, keyword=None, limit=10000, conditional=False):
    params = {"access_token": config.ACCESS_TOKEN, "limit": limit}
    if keyword:
        params["keyword"] = keyword
    headers = {}
    if conditional and endpoint in _etags:
        headers["If-None-Match"] = _etags[endpoint]
    try:
        response = http_util.session().get(f"{config.WE_FLOW_BASE_URL}/api/v1/{endpoint}",
                                          params=params, headers=headers, timeout=3)
        if response.status_code == 304:
            return [], True
        if response.status_code != 200:
            return [], False
        data = response.json()
        if isinstance(data, dict) and data.get("success") is False:
            return [], False
        rows = data if isinstance(data, list) else data.get(endpoint, data.get("data", [])) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            return [], False
        if conditional:
            etag = response.headers.get("ETag")
            if etag:
                _etags[endpoint] = etag
            name_fields = [{key: row.get(key) for key in ("username", "sessionId", "id", "displayName", "name", "nickname", "remark", "alias", "type")}
                           for row in rows if isinstance(row, dict)]
            fingerprint = hashlib.sha256(json.dumps(name_fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if _fingerprints.get(endpoint) == fingerprint:
                return [], True
            _fingerprints[endpoint] = fingerprint
        return [row for row in rows if isinstance(row, dict)], True
    except Exception:
        log.debug("[name_refresh] %s 获取失败", endpoint, exc_info=True)
        return [], False


def _get_snapshot(force=False):
    """Use only an explicit versioned capability; a legacy endpoint is not fresh."""
    key = (config.WE_FLOW_BASE_URL, config.ACCESS_TOKEN)
    probe = _snapshot_probe.setdefault(key, {})
    if probe.get("supported") is False and time.monotonic() < probe.get("retry_at", 0):
        return None, "unavailable"
    headers = {}
    if not force and probe.get("etag") and probe.get("supported") is True:
        headers["If-None-Match"] = probe["etag"]
    try:
        response = http_util.session().get(f"{config.WE_FLOW_BASE_URL}/api/v1/identity-snapshot",
            params={"access_token": config.ACCESS_TOKEN}, headers=headers, timeout=(2, 3))
        if response.status_code in (404, 405, 501):
            if probe.get("supported") is True:
                return None, "offline"
            probe.update(supported=False, retry_at=time.monotonic() + 60)
            return None, "unavailable"
        if response.status_code == 304 and probe.get("supported") is True:
            return None, "unchanged"
        if response.status_code != 200:
            return None, "offline"
        data = response.json()
        if (not isinstance(data, dict) or data.get("success") is not True or type(data.get("version")) is not int or data.get("version") != 1
                or data.get("fresh") is not True or not isinstance(data.get("capabilities"), dict)
                or type(data["capabilities"].get("freshIdentitySnapshot")) is not int or data["capabilities"].get("freshIdentitySnapshot") != 1):
            # A similarly named endpoint must never silently claim this contract.
            if probe.get("supported") is not True:
                probe.update(supported=False, retry_at=time.monotonic() + 60)
            return None, "invalid"
        for kind in ("contacts", "sessions"):
            rows = data.get(kind)
            if not isinstance(rows, list):
                return None, "invalid"
            for row in rows:
                if not isinstance(row, dict):
                    return None, "invalid"
                username = row.get("username")
                stable_id.validate_username(username, group=isinstance(username, str) and username.endswith("@chatroom"))
                if row.get("sessionId") not in (None, username):
                    return None, "invalid"
                if any(not isinstance(row.get(field), str) for field in ("displayName", "remark", "nickname", "alias") if field in row):
                    return None, "invalid"
        probe.update(supported=True, etag=response.headers.get("ETag", ""))
        return data, "fresh"
    except (ValueError, TypeError):
        return None, "invalid"
    except Exception:
        log.debug("[name_refresh] fresh identity snapshot 获取失败", exc_info=True)
        return None, "offline"


def observe_name(session_id, name, *, is_group=False, username="", remark=None,
                 nickname=None, alias=None, source="message", observed_at=None):
    stable_id.validate_username(session_id, group=is_group or isinstance(session_id, str) and session_id.endswith("@chatroom"))
    if is_group or "@chatroom" in session_id:
        existing = state.identity_registry.get_group_by_session(session_id)
        # WeFlow supplies actual group names. UIA alone handles UI member-count suffixes.
        display = _clean_group_name(name)
        target = state.identity_registry.register_group(session_id, display, source=source, observed_at=observed_at)
        new_name = state.identity_registry.get_group_name(target)
        if existing.get("name") and existing["name"] != new_name and hasattr(state, "record_activity"):
            state.record_activity("rename", f"{existing['name']} → {new_name}", target_id=target, session_id=session_id, is_group=True)
        return target
    existing = state.identity_registry.get_private_by_session(session_id)
    if username and username != session_id:
        raise ValueError("联系人真实微信 ID 与会话不一致")
    if source in ("contact", "snapshot") and remark is None:
        # Official JSON omits undefined/empty remarks after the user removes one.
        remark = ""
    # A session's display name may be a remark, so don't rewrite the true nickname with it.
    if nickname is None and source in ("session", "message"):
        nickname = existing.get("nickname") or name
    target = state.identity_registry.register_private(
        session_id, name, username=session_id, remark=remark,
        nickname=nickname, alias=alias, source=source)
    new_name = state.identity_registry.get_private_name(target)
    if existing.get("name") and existing["name"] != new_name and hasattr(state, "record_activity"):
        state.record_activity("rename", f"{existing['name']} → {new_name}", target_id=target, session_id=session_id, is_group=False)
    return target


def observe_event(data):
    """Accept explicit future contact/session update events without treating them as chat."""
    event = str(data.get("event", "")).lower()
    if not event.startswith(("contact.", "session.", "group.")):
        return False
    session = data.get("username") or data.get("sessionId") or data.get("talkerId")
    name = data.get("displayName") or data.get("groupName") or data.get("sourceName") or data.get("nickname") or ""
    is_group = isinstance(session, str) and session.endswith("@chatroom") or data.get("type") in ("group", "chatroom", 2)
    try:
        observe_name(session, name, is_group=is_group, remark=data.get("remark"),
                     nickname=data.get("nickname"), alias=data.get("alias"), source="event")
    except ValueError:
        log.warning("[name_refresh] 已忽略缺少真实微信 ID 的名称事件")
    return True


def _consume(rows, source, generation=None):
    count = 0
    for row in rows:
        with getattr(state, "run_lock", nullcontext()):
            if not _valid_generation(generation):
                break
            session = row.get("username") or row.get("sessionId")
            if not session:
                continue
            name = row.get("displayName") or row.get("name") or row.get("nickname") or ""
            is_group = isinstance(session, str) and session.endswith("@chatroom") or row.get("type") in ("group", "chatroom", 2)
            try:
                observe_name(session, name, is_group=is_group, nickname=row.get("nickname"),
                             remark=row.get("remark", "") if source in ("contact", "snapshot") else None,
                             alias=row.get("alias", "") if source in ("contact", "snapshot") else row.get("alias"), source=source)
            except (ValueError, TypeError):
                log.warning("[name_refresh] 已忽略身份无效的联系人行")
                continue
            count += 1
    return count


def get_sync_status():
    with _refresh_lock:
        return dict(_last_status)


def refresh_all_names(force=False, generation=None):
    """Sync contact remarks/nicknames and group session names with stable IDs."""
    global _last_sync, _last_status
    interval = _sync_interval()
    if not _valid_generation(generation) or (not force and time.monotonic() - _last_sync < interval):
        return get_sync_status()
    if not _sync_lock.acquire(blocking=False):
        return get_sync_status()
    try:
        before = state.identity_registry.get_revision()
        snapshot, snapshot_mode = _get_snapshot(force)
        if not _valid_generation(generation):
            return get_sync_status()
        if snapshot_mode in ("fresh", "unchanged"):
            contact_count = _consume(snapshot["contacts"], "snapshot", generation) if snapshot else 0
            session_count = _consume(snapshot["sessions"], "session", generation) if snapshot else 0
            _last_sync = time.monotonic()
            _last_status = {"status": "ok", "changed": state.identity_registry.get_revision() - before,
                "revision": state.identity_registry.get_revision(), "last_sync": time.time(),
                "contacts": contact_count, "sessions": session_count, "interval": interval,
                "generation": generation, "name_source": "fresh_snapshot", "fresh_capability": True,
                "snapshot_revision": snapshot.get("revision", "") if snapshot else _last_status.get("snapshot_revision", ""),
                "upstream_limit": "名称变化受微信数据库落盘及本地查询完成时间影响"}
            return get_sync_status()
        probe = _snapshot_probe.get((config.WE_FLOW_BASE_URL, config.ACCESS_TOKEN), {})
        if probe.get("supported") is True:
            # Never replace fresh names with stale legacy data during an outage.
            _last_sync = time.monotonic()
            _last_status = dict(_last_status, status="offline", changed=0, generation=generation,
                interval=interval, fresh_capability=True, name_source="fresh_snapshot", snapshot_error=snapshot_mode)
            return get_sync_status()
        contacts, contacts_ok = _get_list("contacts", conditional=not force)
        if not _valid_generation(generation):
            return get_sync_status()
        contact_count = _consume(contacts, "contact", generation)
        sessions, sessions_ok = _get_list("sessions", conditional=not force)
        if not _valid_generation(generation):
            return get_sync_status()
        session_count = _consume(sessions, "session", generation)
        _last_sync = time.monotonic()
        _last_status = {"status": "ok" if contacts_ok and sessions_ok else "partial" if contacts_ok or sessions_ok else "offline",
                        "changed": state.identity_registry.get_revision() - before,
                        "revision": state.identity_registry.get_revision(), "last_sync": time.time(),
                        "contacts": contact_count, "sessions": session_count, "interval": interval}
        _last_status.update(generation=generation, name_source="legacy_cached", fresh_capability=False,
            snapshot_error=snapshot_mode,
            upstream_limit="WeFlow contacts 名称可缓存约 3 分钟；sessions 名称缓存无保证的 HTTP 刷新时限")
        return get_sync_status()
    finally:
        _sync_lock.release()


def request_name_refresh(generation=None):
    """Coalesce verified system-message hints without blocking the receive thread."""
    with _refresh_lock:
        if generation in _requested:
            return
        _requested.add(generation)
    def run():
        try:
            return refresh_all_names(force=True, generation=generation)
        finally:
            with _refresh_lock:
                _requested.discard(generation)
    try:
        _POOL.submit(run)
    except RuntimeError:
        with _refresh_lock:
            _requested.discard(generation)


def _refresh_session(session_id, is_group=False, generation=None):
    probe = _snapshot_probe.get((config.WE_FLOW_BASE_URL, config.ACCESS_TOKEN), {})
    if probe.get("supported") is True:
        refresh_all_names(force=True, generation=generation)
        info = state.identity_registry.get_group_by_session(session_id) if is_group else state.identity_registry.get_private_by_session(session_id)
        return info.get("name")
    rows, ok = _get_list("sessions" if is_group else "contacts", keyword=session_id, limit=50)
    if not _valid_generation(generation):
        return None
    matches = [row for row in rows if (row.get("username") or row.get("sessionId")) == session_id]
    if matches:
        _consume(matches, "session" if is_group else "contact", generation)
        info = state.identity_registry.get_group_by_session(session_id) if is_group else state.identity_registry.get_private_by_session(session_id)
        return info.get("name")
    return None


def refresh_private_name(session_id, username=""):
    return _refresh_session(session_id)


def refresh_group_name(session_id):
    return _refresh_session(session_id, True)


def refresh_member_name(group_session, username):
    if not group_session or not username:
        return None
    try:
        response = http_util.session().get(f"{config.WE_FLOW_BASE_URL}/api/v1/group-members",
                                          params={"access_token": config.ACCESS_TOKEN, "chatroomId": group_session},
                                          timeout=3)
        if response.status_code != 200:
            return None
        rows = response.json().get("members", [])
        for row in rows:
            if (row.get("wxid") or row.get("accountId") or row.get("username")) == username:
                name = row.get("groupNickname") or row.get("displayName") or row.get("nickname") or ""
                state.identity_registry.register_member(group_session, username, name)
                return name or None
    except Exception:
        log.debug("[name_refresh] 群成员刷新失败", exc_info=True)
    return None


def refresh_target(target_id, is_group=False, *, force=False, wait=False):
    """Return cached names immediately; forced blocking refresh is only for a failed UIA lookup."""
    key = (int(target_id), bool(is_group))
    cached = state.identity_registry.get_target_name(target_id, is_group)
    reliable = cached and cached != str(target_id)
    with _refresh_lock:
        if not force and time.monotonic() - _last_refresh.get(key, 0) < _REFRESH_TTL:
            return cached if reliable else None
        if key in _inflight:
            return cached if reliable else None
        if len(_inflight) >= 64:
            return cached if reliable else None
        _inflight.add(key)
    info = state.identity_registry.get_group_record(target_id) if is_group else state.identity_registry.get_private_record(target_id)
    session_id = info.get("session_id", "")
    generation = state.bridge_generation if state.running else None
    def run():
        try:
            name = _refresh_session(session_id, is_group, generation)
            with _refresh_lock:
                _last_refresh[key] = time.monotonic()
                if len(_last_refresh) > 1024:
                    cutoff = time.monotonic() - _REFRESH_TTL
                    for old_key, stamp in list(_last_refresh.items()):
                        if stamp < cutoff:
                            _last_refresh.pop(old_key, None)
            return name or (cached if reliable else None)
        finally:
            with _refresh_lock:
                _inflight.discard(key)
    if wait:
        return run()
    _POOL.submit(run)
    return cached if reliable else None


class NameMonitor:
    def __init__(self, generation=None):
        self.generation = state.bridge_generation if generation is None else generation
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="contact-names")
        self._thread.start()

    def _run(self):
        first = True
        while not self._stop.is_set() and _valid_generation(self.generation):
            result = refresh_all_names(force=first, generation=self.generation)
            first = result.get("generation") != self.generation
            interval = _sync_interval()
            self._stop.wait(interval)

    def stop(self):
        self._stop.set()


def start_name_monitor(generation=None):
    return NameMonitor(generation)
