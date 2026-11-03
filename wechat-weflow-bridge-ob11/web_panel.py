"""Local 微信bot workbench, independent of the application entrypoint.

Runtime callbacks are registered on state by main.initialize_runtime. HTTP
sessions and CSRF tokens are separate. Config secrets never return to clients.
"""
import html
import base64
import binascii
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import audio_helper
import config
import config_model
import state

log = logging.getLogger("ob11-bridge")
ASSET_DIR = Path(__file__).with_name("panel_assets")
PAGE = (ASSET_DIR / "index.html").read_text(encoding="utf-8") if (ASSET_DIR / "index.html").exists() else ""
_LOG_CACHE = {"stamp": None, "text": ""}
_LOG_LOCK = threading.Lock()
_SESSIONS = {}
_SESSION_LOCK = threading.Lock()
_SESSION_TTL = 8 * 60 * 60
_MAX_BODY = 256 * 1024
_COOKIE_NAME = "akasha_panel_session"


def _fields():
    return {f.key: f for g in config_model.CONFIG_SCHEMA for f in g.fields}


def _secret_key(key):
    field = _fields().get(str(key))
    return bool(field and field.sensitive) or bool(re.search(r"(?:token|api_?key|password|secret|authorization)$", str(key), re.I))


def _read_config():
    # Atomic config.save_config replacement ensures reads are full snapshots.
    with open(config.CONFIG_FILE, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    return value


def _secret_values():
    try:
        cfg = _read_config()
    except (OSError, ValueError):
        cfg = getattr(config, "config", {})
    return sorted((str(v) for k, v in cfg.items() if _secret_key(k) and v), key=len, reverse=True)


def _redact_text(value, secret_values=None):
    text = str(value)
    for secret in _secret_values() if secret_values is None else secret_values:
        text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"(?i)(bearer\s+)[^\s,;\"']+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)((?:api[_ -]?key|access[_ -]?token|password|secret)\s*[=:]\s*)[^\s,;]+", r"\1[已隐藏]", text)
    text = re.sub(r"(https?://|wss?://)[^/\s:@]+:[^/\s@]+@", r"\1[已隐藏]@", text)
    text = re.sub(r"(?i)([?&](?:api_key|key|token|access_token)=)[^&\s]+", r"\1[已隐藏]", text)
    return text


def _redact(value, secret_values=None):
    if secret_values is None:
        secret_values = _secret_values()
    if isinstance(value, dict):
        return {k: "[已隐藏]" if _secret_key(k) else _redact(v, secret_values) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, secret_values) for v in value]
    return _redact_text(value, secret_values) if isinstance(value, str) else value


def _redact_runtime_snapshot(value):
    """Keep a validated runtime reconciliation ID opaque and round-trippable.

    A short configured secret may be a substring of a generated UUID. Only
    this known runtime status field may bypass substring replacement; prose,
    errors, paths and arbitrary dictionaries still use the normal redactor.
    """
    result = _redact(value)
    pending = value.get("autopilot_pending") if isinstance(value, dict) else None
    if (isinstance(pending, dict) and type(pending.get("version")) is int and pending.get("version") == 1
            and pending.get("status") == "pending_review"
            and isinstance(pending.get("intent_id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", pending["intent_id"])):
        result["autopilot_pending"]["intent_id"] = pending["intent_id"]
    interactions = value.get("moments_interactions") if isinstance(value, dict) else None
    pending = interactions.get("pending") if isinstance(interactions, dict) else None
    if (isinstance(pending, dict) and type(pending.get("version")) is int and pending.get("version") == 1
            and pending.get("status") == "pending_review"
            and pending.get("type") in ("like", "comment", "reply")
            and isinstance(pending.get("intent_id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", pending["intent_id"])):
        result["moments_interactions"]["pending"]["intent_id"] = pending["intent_id"]
    return result


def _tail_log_text():
    path = os.path.join(config.BASE_DIR, "bridge.log")
    try:
        info = os.stat(path)
        stamp = (info.st_mtime_ns, info.st_size)
        with _LOG_LOCK:
            if _LOG_CACHE["stamp"] != stamp:
                with open(path, "rb") as stream:
                    stream.seek(max(0, info.st_size - 65536))
                    raw = stream.read(65536).decode("utf-8", errors="replace")
                _LOG_CACHE.update(stamp=stamp, text="\n".join(raw.splitlines()[-300:]))
            return _redact_text(_LOG_CACHE["text"])
    except OSError:
        return ""


def _schema():
    result = []
    for group in config_model.CONFIG_SCHEMA:
        fields = []
        for field in group.fields:
            entry = {k: getattr(field, k) for k in ("key", "label", "type", "placeholder", "description", "sensitive")}
            entry["default"] = "" if field.sensitive else field.default
            entry["options"] = [[item[0], item[1]] for item in field.options]
            for key in ("min", "max", "step", "restart_required"):
                if getattr(field, key, None) is not None:
                    entry[key] = getattr(field, key)
            fields.append(entry)
        result.append({"title": group.title, "fields": fields})
    return result


class PanelError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def _local_context(self, mutation=False):
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed.update(("127.0.0.1", "localhost"))
        if len(hosts) != 1 or hosts[0].lower() not in allowed:
            raise PanelError("面板只接受本机地址，请使用 127.0.0.1 打开", 403)
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            raise PanelError("面板只允许本机访问", 403)
        origin = "http://" + hosts[0].lower()
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins[0].lower() != origin):
            raise PanelError("请求来源不属于当前面板", 403)
        fetch_site = self.headers.get("Sec-Fetch-Site", "")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            raise PanelError("请从当前面板执行操作", 403)
        referer = self.headers.get("Referer")
        if referer:
            parsed = urlsplit(referer)
            if parsed.scheme != "http" or parsed.netloc.lower() != hosts[0].lower():
                raise PanelError("请求来源不属于当前面板", 403)
        if mutation and not origins:
            raise PanelError("操作需要当前面板的来源标识", 403)

    def _authenticate(self, mutation=False):
        self._local_context(mutation)
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            sid = cookie[_COOKIE_NAME].value if _COOKIE_NAME in cookie else ""
        except Exception:
            sid = ""
        now = time.monotonic()
        with _SESSION_LOCK:
            session = _SESSIONS.get(sid)
            if not session or now - session["touched"] > _SESSION_TTL:
                _SESSIONS.pop(sid, None)
                raise PanelError("面板会话已过期，请刷新页面", 401)
            if mutation:
                supplied = self.headers.get("X-Panel-Token", "")
                if not supplied or not secrets.compare_digest(supplied.encode("utf-8"), session["csrf"].encode("utf-8")):
                    raise PanelError("操作凭证无效，请刷新页面后重试", 403)
            session["touched"] = now

    def _session_page(self):
        self._local_context()
        now = time.monotonic()
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            sid = cookie[_COOKIE_NAME].value if _COOKIE_NAME in cookie else ""
        except Exception:
            sid = ""
        with _SESSION_LOCK:
            for key in list(_SESSIONS):
                if now - _SESSIONS[key]["touched"] > _SESSION_TTL:
                    _SESSIONS.pop(key, None)
            if sid not in _SESSIONS:
                sid = secrets.token_urlsafe(32)
                if len(_SESSIONS) >= 64:
                    _SESSIONS.pop(min(_SESSIONS, key=lambda k: _SESSIONS[k]["touched"]))
                _SESSIONS[sid] = {"csrf": secrets.token_urlsafe(32), "touched": now}
            else:
                _SESSIONS[sid]["touched"] = now
            csrf = _SESSIONS[sid]["csrf"]
        body = PAGE.replace("__PANEL_CSRF__", html.escape(csrf, quote=True)).encode("utf-8")
        self._send(body, "text/html; charset=utf-8", cookie=f"{_COOKIE_NAME}={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={_SESSION_TTL}")

    def _json_body(self, max_size=_MAX_BODY):
        if self.headers.get("Transfer-Encoding"):
            raise PanelError("不支持分块请求")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise PanelError("请求需要明确的内容长度", 411)
        try:
            size = int(lengths[0])
        except ValueError:
            raise PanelError("内容长度无效")
        if size < 0 or size > max_size:
            raise PanelError("请求内容超过此接口大小上限", 413)
        if size == 0:
            return {}
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise PanelError("请使用 JSON 提交操作", 415)
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError()
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise PanelError("请求不是有效的 JSON")
        if not isinstance(value, dict):
            raise PanelError("请求内容必须是 JSON 对象")
        return value

    def _callback(self, name, *args):
        callback = getattr(state, name, None)
        if not callable(callback):
            raise PanelError("运行环境尚未初始化，请通过启动脚本打开面板", 503)
        result = callback(*args)
        if isinstance(result, dict) and result.get("ok") is False:
            raise PanelError(result.get("error", "操作未完成"), 409)
        return result

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            path, qs = parsed.path, parse_qs(parsed.query)
            if path in ("/", "/index.html"):
                self._session_page()
                return
            self._authenticate()
            assets = {"/assets/panel.css": ("panel.css", "text/css; charset=utf-8"), "/assets/panel.js": ("panel.js", "text/javascript; charset=utf-8")}
            if path in assets:
                filename, mime = assets[path]
                self._send((ASSET_DIR / filename).read_bytes(), mime)
            elif path in ("/status", "/api/status"):
                snapshot = self._callback("get_status")
                if not isinstance(snapshot, dict):
                    raise PanelError("状态暂时不可用", 503)
                snapshot = dict(snapshot)
                if snapshot.get("preview") is not True:
                    performance = getattr(state, "response_performance_snapshot", None)
                    if callable(performance):
                        snapshot["response_performance"] = performance(snapshot.get("send_queue"))
                    cache = sys.modules.get("tts_cache")
                    if cache is not None:
                        snapshot["tts_cache"] = cache.stats()
                snapshot["log"] = _tail_log_text()
                self.send_json(_redact_runtime_snapshot(snapshot))
            elif path == "/api/config":
                cfg = _read_config()
                keys = [key for key in cfg if _secret_key(key)]
                safe = {key: "" if key in keys else _redact(value) for key, value in cfg.items()}
                safe.update(_redacted_keys=keys, _configured_secrets=[key for key in keys if cfg[key]])
                self.send_json(safe)
            elif path == "/api/config/schema":
                self.send_json(_schema())
            elif path == "/api/audio/status":
                devices = audio_helper.list_playback_devices()
                recording_devices = getattr(audio_helper, "list_recording_devices", lambda: [])()
                recording_default = getattr(audio_helper, "get_default_recording_device_info", lambda: None)()
                target = getattr(config, "VB_CABLE_DEVICE", "")
                self.send_json(_redact({"ok": True, "device": audio_helper.get_default_playback_device(), "config_vb_cable": target, "devices": devices,
                                       "recording_devices": recording_devices, "recording_default": recording_default,
                                       "recording_target_available": sum("CABLE" in str(item.get("name", "")).upper() for item in recording_devices) == 1,
                                       "recording_target_count": sum("CABLE" in str(item.get("name", "")).upper() for item in recording_devices),
                                       "target_available": any(target.casefold() in str(item.get("name", "")).casefold() for item in devices) if target else False}))
            elif path == "/api/uia/diagnostics":
                diagnostic = getattr(state, "refresh_uia_diagnostics", None)
                if not callable(diagnostic):
                    diagnostic = getattr(getattr(state, "sender_instance", None), "diagnostics", None)
                if not callable(diagnostic):
                    self.send_json({"ok": False, "available": False, "error": "发送器尚未就绪，请先启动桥接", "read_only": True})
                else:
                    self.send_json(_redact({"ok": True, "read_only": True, "diagnostics": diagnostic()}))
            elif path == "/api/logs":
                level = qs.get("level", [""])[0].upper()
                query = qs.get("q", [""])[0][:200].casefold()
                lines = [line for line in _tail_log_text().splitlines() if (not level or level in line.upper()) and (not query or query in line.casefold())]
                self.send_json({"ok": True, "lines": lines, "count": len(lines)})
            elif path == "/api/memories":
                self._memories(qs)
            elif path == "/api/references":
                import reference_store
                self.send_json({"ok": True, "images": reference_store.list_images()})
            elif path.startswith("/api/references/image/"):
                import reference_store
                raw, mime = reference_store.read(path.rsplit("/", 1)[-1])
                self._send(raw, mime)
            elif path.startswith("/api/voices/audio/"):
                from voice_cloning import VoiceCloningClient
                self._send(VoiceCloningClient().read_preview(path.rsplit("/", 1)[-1]), "audio/wav")
            else:
                raise PanelError("接口不存在", 404)
        except PanelError as error:
            self.send_json({"ok": False, "error": _redact_text(error)}, error.status)
        except Exception:
            log.exception("[Web] 读取面板数据失败")
            self.send_json({"ok": False, "error": "读取失败，请查看运行日志后重试"}, 500)

    def do_POST(self):
        try:
            self._authenticate(mutation=True)
            path = urlsplit(self.path).path
            data = self._json_body(max_size=28*1024*1024 if path == "/api/references/upload" else _MAX_BODY)
            callbacks = {"/start": "start_bridge", "/stop": "stop_bridge", "/pause": "pause_bridge", "/resume": "resume_bridge"}
            if path in callbacks:
                result = self._callback(callbacks[path])
                self.send_json(_redact(result) if isinstance(result, dict) else {"ok": True})
            elif path == "/api/service/exit":
                if data:
                    raise PanelError("完全退出不接受额外参数。")
                result = self._callback("exit_panel_services")
                self.close_connection = True
                try:
                    self.send_json(_redact(result))
                    self.wfile.flush()
                finally:
                    # This handler runs on its own thread; the serving thread
                    # returns to normal application cleanup after the reply.
                    self.server.shutdown()
            elif path == "/mode":
                modes = ["mention", "all", "batch"]
                current = getattr(state, "group_reply_mode", "mention")
                new_mode = modes[(modes.index(current) + 1) % len(modes)] if current in modes else "mention"
                self._callback("update_config", {"group_reply_mode": new_mode})
                self.send_json({"ok": True, "group_reply_mode": new_mode})
            elif path == "/api/config":
                result = self._callback("update_config", self._config_updates(data))
                response = {"ok": True, "message": "设置已保存"}
                if isinstance(result, dict):
                    for key in ("restart_required", "warnings", "changed_keys"):
                        if key in result:
                            response[key] = result[key]
                self.send_json(_redact(response))
            elif path == "/api/autopilot/resolve-pending":
                self._resolve_autopilot_pending(data)
            elif path == "/api/autopilot/first-online":
                if data:
                    raise PanelError("上线通告无需额外参数。")
                pilot = getattr(state, "autopilot", None)
                if pilot is None:
                    raise PanelError("请先启动机器人。", 503)
                try:
                    pilot.request_first_online()
                except ValueError as error:
                    raise PanelError(str(error), 409) from None
                self.send_json({"ok": True, "status": "accepted", "message": "已接单，正在创作上线通告和配图。"}, 202)
            elif path == "/api/moments/interactions/run":
                if data:
                    raise PanelError("立即浏览不接受额外指令，请通过设置调整规则")
                self._require_live_interactions()
                callback = getattr(state, "run_moments_interactions", None)
                if not callable(callback):
                    raise PanelError("运行环境尚未初始化，请通过启动脚本打开面板", 503)
                failure = {"ok": False, "status": "failed", "error": "浏览任务未能排队，请检查桥接与朋友圈互动设置"}
                try:
                    result = callback()
                except Exception:
                    self.send_json(failure, 503)
                    return
                reasons = {"disabled": "请先启用朋友圈互动", "busy": "朋友圈互动正在处理",
                           "unavailable": "请先启动并继续桥接",
                           "restore_failed": "UIA 临时状态尚未还原，请先重试停止桥接",
                           "account_identity_missing": "请配置当前账号的真实微信 ID",
                           "pending_review": "朋友圈互动待核对，请先核对上次操作结果",
                           "outside_hours": "当前不在朋友圈互动活跃时段，请调整活跃时间或在时段内再试",
                           "private_priority": "私聊优先，等私聊处理完成并达到空闲时间后再试"}
                status = result.get("status") if isinstance(result, dict) else None
                if isinstance(status, str) and status in reasons:
                    self.send_json({"ok": False, "status": status, "error": reasons[status]}, 409)
                    return
                if not isinstance(result, dict) or result.get("ok") is not True:
                    if isinstance(result, dict) and result.get("error") == "桥接已暂停":
                        self.send_json({"ok": False, "status": "unavailable", "error": "桥接已暂停"}, 409)
                    else:
                        self.send_json(failure, 503)
                    return
                self.send_json(_redact_runtime_snapshot(result), 202)
            elif path == "/api/moments/interactions/resolve":
                self._resolve_moments_interactions(data)
            elif path == "/api/audio/open-settings":
                self.send_json({"ok": bool(audio_helper.open_sound_settings())})
            elif path == "/api/references/upload":
                import reference_store
                value = data.get("data_base64")
                if not isinstance(value, str) or len(value) > 28*1024*1024:
                    raise PanelError("参考图编码无效或过大")
                try:
                    raw = base64.b64decode(value, validate=True)
                except (binascii.Error, ValueError):
                    raise PanelError("参考图不是有效的 Base64") from None
                self.send_json({"ok": True, "image": reference_store.upload(raw, name=data.get("name") or "参考图", note=data.get("note") or "")}, 201)
            elif path == "/api/references/remove":
                import reference_store
                identifier = data.get("id")
                if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
                    raise PanelError("参考图编号无效")
                entry = next((item for item in reference_store.list_images() if item.get("id") == identifier), None)
                if not entry:
                    raise PanelError("参考图不存在", 404)
                if entry.get("builtin"):
                    raise PanelError("内置示例请取消选择，无需删除")
                # Removing a selected image first updates the active snapshot;
                # failure to save must leave the asset available to the model.
                refs = list(getattr(config, "BOT_IMAGE_REFS", []))
                selected = "ref:" + identifier
                if selected in refs:
                    self._callback("update_config", {"bot_image_refs": [item for item in refs if item != selected]})
                self.send_json({"ok": bool(reference_store.remove(identifier))})
            elif path.startswith("/api/voices/"):
                self._voice_action(path, data)
            elif path == "/api/memories/delete":
                value = data.get("id")
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise PanelError("请提供有效的记忆编号")
                if not self._memory_store().delete_memory(value):
                    raise PanelError("记忆已被删除，请刷新列表", 404)
                self.send_json({"ok": True})
            elif path == "/api/memories/clear":
                if data.get("confirm") != "clear_all_memories":
                    raise PanelError("清空记忆需要明确确认")
                self.send_json({"ok": bool(self._memory_store().clear())})
            else:
                raise PanelError("接口不存在", 404)
        except PanelError as error:
            self.close_connection = True
            self.send_json({"ok": False, "error": _redact_text(error)}, error.status)
        except ValueError as error:
            self.close_connection = True
            self.send_json({"ok": False, "error": _redact_text(error)}, 400)
        except Exception:
            self.close_connection = True
            log.exception("[Web] 面板操作失败")
            self.send_json({"ok": False, "error": "操作失败，请查看运行日志后重试"}, 500)

    def _voice_action(self, path, data):
        from voice_cloning import VoiceCloningClient
        client = VoiceCloningClient()
        action = path.rsplit("/", 1)[-1]
        allowed = {
            "list": {"prefix", "page_index", "page_size"}, "query": {"voice_id"},
            "create": {"url", "prefix", "target_model", "language_hints", "max_prompt_audio_length", "enable_preprocess", "enable_volume_normalization", "confirm"},
            "delete": {"voice_id", "confirm"}, "preview": {"text"},
        }
        if action not in allowed or set(data) - allowed[action]:
            raise PanelError("音色操作或参数无效")
        values = dict(data)
        if action in {"create", "delete"} and values.pop("confirm", None) != action + "_voice":
            raise PanelError("请在面板确认此音色操作")
        try:
            if action == "list":
                result = {"voices": client.list(**values)}
            else:
                result = getattr(client, action)(**values)
        except (ValueError, RuntimeError) as error:
            raise PanelError(str(error), 400) from None
        self.send_json(_redact({"ok": True, **result}))

    def _config_updates(self, data):
        fields = _fields()
        if set(data) - set(fields) - {"_clear_secrets"}:
            raise PanelError("配置中含有未知字段，请刷新设置页面")
        clear = data.get("_clear_secrets", [])
        if not isinstance(clear, list) or any(not isinstance(key, str) or key not in fields or not _secret_key(key) for key in clear):
            raise PanelError("清除凭证的字段无效")
        updates = {key: value for key, value in data.items() if key in fields and not (_secret_key(key) and value == "")}
        if any(_secret_key(key) and not isinstance(value, str) for key, value in updates.items()):
            raise PanelError("凭证必须是文本")
        for key in clear:
            updates[key] = ""
        return updates

    def _resolve_autopilot_pending(self, data):
        # Resolution only records a user's check in WeChat. It must not send,
        # toggle autopilot, or remove its journal outside the runtime's lock.
        if set(data) != {"outcome", "intent_id", "confirm"}:
            raise PanelError("核对请求需要结果、记录编号和微信核对确认")
        outcome = data.get("outcome")
        if not isinstance(outcome, str) or outcome not in {"published", "not_published"}:
            raise PanelError("请选择已发表或确认未发表")
        intent_id = data.get("intent_id")
        if (not isinstance(intent_id, str) or not 1 <= len(intent_id) <= 128
                or intent_id != intent_id.strip() or any(ord(char) < 32 for char in intent_id)):
            raise PanelError("待核对记录编号无效，请刷新面板")
        if data.get("confirm") != "checked_in_wechat":
            raise PanelError("请先在微信朋友圈核对这条动态，再确认结果")
        autopilot = getattr(state, "autopilot", None)
        resolve = getattr(autopilot, "resolve_pending", None)
        snapshot = getattr(autopilot, "pending_snapshot", None)
        if not callable(resolve) or not callable(snapshot):
            raise PanelError("朋友圈运行环境尚未初始化", 503)
        # The captured intent ID, rather than the latest record's ID, lets the
        # runtime reject stale tabs and a concurrent publication atomically.
        if resolve(outcome, intent_id=intent_id) is not True:
            raise PanelError("核对记录已变化、正在处理或无法保存。请刷新面板并在微信重新核对", 409)
        self.send_json(_redact_runtime_snapshot({"ok": True, "autopilot_pending": snapshot(),
                                "message": "核对结果已保存；启用时将在正常周期继续决策，本次不会立即重发"}))

    def _memory_store(self):
        store = getattr(state, "memory_store", None)
        if store is None:
            raise PanelError("记忆库尚未就绪，请先启动桥接", 503)
        return store

    def _require_live_interactions(self):
        snapshot = self._callback("get_status")
        if not isinstance(snapshot, dict) or snapshot.get("preview") is True:
            raise PanelError("预览模式不会浏览或互动，请打开实际运行的微信bot面板", 409)

    def _resolve_moments_interactions(self, data):
        if set(data) != {"outcome", "intent_id", "confirm"}:
            raise PanelError("核对请求需要结果、记录编号和微信核对确认")
        outcome, intent_id = data.get("outcome"), data.get("intent_id")
        if not isinstance(outcome, str) or outcome not in {"published", "not_published"}:
            raise PanelError("请选择已完成或确认未完成")
        if not isinstance(intent_id, str) or not re.fullmatch(r"[0-9a-f]{32}", intent_id):
            raise PanelError("待核对记录编号无效，请刷新面板")
        if data.get("confirm") != "checked_in_wechat":
            raise PanelError("请先在微信朋友圈核对点赞或评论是否完成，再确认结果")
        self._require_live_interactions()
        controller = getattr(state, "moments_interactions", None)
        resolve = getattr(controller, "resolve_pending", None)
        snapshot = getattr(controller, "status_snapshot", None)
        if not callable(resolve) or not callable(snapshot):
            raise PanelError("朋友圈互动运行环境尚未初始化", 503)
        if resolve(outcome, intent_id=intent_id) is not True:
            raise PanelError("核对记录已变化、正在处理或无法保存。请刷新面板并在微信重新核对", 409)
        self.send_json(_redact_runtime_snapshot({"ok": True, "moments_interactions": snapshot(),
                        "message": "核对结果已保存；只恢复正常周期的决策，不会立即重复互动"}))

    def _memories(self, qs):
        try:
            limit = min(100, max(1, int(qs.get("limit", ["30"])[0])))
            offset = max(0, int(qs.get("offset", ["0"])[0]))
        except ValueError:
            raise PanelError("分页参数无效")
        query = qs.get("q", [""])[0].strip()[:200]
        store = getattr(state, "memory_store", None)
        if store is None:
            self.send_json({"ok": True, "available": False, "memories": [], "count": 0, "total": 0, "limit": limit, "offset": offset})
            return
        items = store.list_memories(limit=limit, offset=offset, query=query)
        self.send_json(_redact({"ok": True, "available": True, "memories": items, "count": len(items), "total": store.count(query=query), "limit": limit, "offset": offset}))

    def _send(self, body, mime, code=200, cookie=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, code=200):
        self._send(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8", code)

    def do_OPTIONS(self):
        self.send_json({"ok": False, "error": "面板不接受跨域请求"}, 403)

    def log_message(self, fmt, *args):
        pass
