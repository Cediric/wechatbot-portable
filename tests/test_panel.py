"""Panel regressions exercise real handlers with synthetic runtime dependencies.

No socket, live WeChat, application entrypoint or external API is used.
"""
import importlib.util
import io
import json
from email.message import Message
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"


class FakeMemory:
    def __init__(self):
        self.items = [{"id": i + 1, "content": f"测试记忆 {i}", "source": "好友", "created_at": 1000} for i in range(65)]
        self.calls = []

    def list_memories(self, limit, offset, query=""):
        self.calls.append((limit, offset, query))
        return [item for item in self.items if query in item["content"]][offset:offset + limit]

    def count(self, query=""):
        return len([item for item in self.items if query in item["content"]])

    def delete_memory(self, ident):
        count = len(self.items)
        self.items = [item for item in self.items if item["id"] != ident]
        return len(self.items) < count

    def clear(self):
        self.items = []
        return True


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix=".panel-test-", dir=str(SOURCE.parent / "tests"))
        cls.config = types.ModuleType("config")
        cls.config.CONFIG_FILE = str(Path(cls.directory.name) / "config.json")
        cls.config.BASE_DIR = cls.directory.name
        cls.config.VB_CABLE_DEVICE = "CABLE Input"
        cls.audio = types.ModuleType("audio_helper")
        cls.audio.list_playback_devices = lambda: [{"name": "CABLE Input", "is_default": False}]
        cls.audio.get_default_playback_device = lambda: "扬声器"
        cls.audio.open_sound_settings = lambda: True
        cls.state = types.ModuleType("state")
        field = lambda key, default="", sensitive=False: types.SimpleNamespace(key=key, label=key, type="password" if sensitive else "text", placeholder="", description="", sensitive=sensitive, default=default, options=[])
        cls.model = types.ModuleType("config_model")
        cls.model.CONFIG_SCHEMA = [types.SimpleNamespace(title="连接", fields=[field("access_token", "schema-secret-do-not-expose", True), field("bot_wxid", "", True), field("group_reply_mode", "mention"), field("bot_nicknames", []), field("voice_enabled", True), field("web_port", 8766)])]
        with patch.dict("sys.modules", {"config": cls.config, "audio_helper": cls.audio, "config_model": cls.model, "state": cls.state}):
            spec = importlib.util.spec_from_file_location("test_web_panel", SOURCE / "web_panel.py")
            cls.panel = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.panel)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.current = {"access_token": "synthetic-test-secret", "bot_wxid": "synthetic-bot-id", "group_reply_mode": "mention", "bot_nicknames": ["小助手"], "voice_enabled": True, "web_port": 8766}
        Path(self.config.CONFIG_FILE).write_text(json.dumps(self.current), encoding="utf-8")
        self.config.config = self.current
        self.updates, self.actions = [], []
        self.state.update_config = self.update_config
        self.state.start_bridge = lambda: self.actions.append("start")
        self.state.stop_bridge = lambda: self.actions.append("stop")
        self.state.pause_bridge = lambda: self.actions.append("pause")
        self.state.resume_bridge = lambda: self.actions.append("resume")
        self.state.get_status = lambda: {"running": True, "log": "ignored", "token": "synthetic-test-secret", "recent_activity": [{"message": "synthetic-test-secret"}]}
        self.state.group_reply_mode = "mention"
        self.state.memory_store = FakeMemory()
        self.pending = {"status": "pending_review", "intent_id": "intent-123", "time": "2026-10-11 18:20:30",
                        "content": "测试朋友圈待核对", "image_path": "synthetic.png"}
        self.resolve_calls = []
        self.state.autopilot = types.SimpleNamespace(pending_snapshot=lambda: self.pending,
                                                     resolve_pending=self.resolve_pending)
        self.social_status = {"enabled": True, "running": False, "daily": {"like": 2, "comment": 1, "reply": 0}, "pending": None, "recent": []}
        self.social_runs, self.social_resolves = [], []
        self.state.run_moments_interactions = lambda: self.social_runs.append(True) or {"ok": True, "message": "已安排浏览"}
        self.state.moments_interactions = types.SimpleNamespace(status_snapshot=lambda: self.social_status,
                                                              resolve_pending=self.resolve_social)
        self.diagnostic_calls = []
        self.state.sender_instance = types.SimpleNamespace(diagnostics=lambda: self.diagnostic_calls.append(True) or {"window_found": True, "last_error": "synthetic-test-secret"})
        self.panel._SESSIONS.clear()
        self.cookie, self.csrf = "", ""
        code, headers, body = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = re.search(r'content="([^"]+)"', body.split('name="panel-csrf"', 1)[1]).group(1)

    def resolve_pending(self, outcome, *, intent_id):
        self.resolve_calls.append((outcome, intent_id))
        if not self.pending or self.pending.get("intent_id") != intent_id:
            return False
        self.pending = None
        return True

    def pending_request(self, **changes):
        return {"outcome": "published", "intent_id": "intent-123", "confirm": "checked_in_wechat", **changes}

    def resolve_social(self, outcome, *, intent_id):
        self.social_resolves.append((outcome, intent_id))
        pending = self.social_status["pending"]
        if not pending or pending.get("intent_id") != intent_id or pending.get("status") != "pending_review":
            return False
        self.social_status["pending"] = None
        return True

    def social_request(self, **changes):
        return {"outcome": "published", "intent_id": "0123456789abcdef0123456789abcdef", "confirm": "checked_in_wechat", **changes}

    def set_social_pending(self):
        self.social_status["pending"] = {"version": 1, "status": "pending_review", "type": "reply", "intent_id": "0123456789abcdef0123456789abcdef",
                                        "content": "待核对的动态", "text": "测试回复", "author_name": "合成作者", "time": 1000}

    def update_config(self, updates):
        self.updates.append(updates)
        self.current.update(updates)
        Path(self.config.CONFIG_FILE).write_text(json.dumps(self.current), encoding="utf-8")
        return {"ok": True, "restart_required": ["web_port"] if "web_port" in updates else [], "access_token": "never-echo-this"}

    def request(self, method, path, data=None, headers=None, authenticated=True, raw_body=None):
        handler = self.panel.WebHandler.__new__(self.panel.WebHandler)
        handler.server = types.SimpleNamespace(server_address=("127.0.0.1", 8766),
            shutdown=lambda: self.actions.append(("shutdown", handler.wfile.getvalue())))
        handler.client_address = ("127.0.0.1", 9999)
        handler.path, handler.command = path, method
        handler.request_version, handler.requestline = "HTTP/1.1", f"{method} {path} HTTP/1.1"
        handler.close_connection = False
        handler.headers = Message()
        values = {"Host": "127.0.0.1:8766"}
        if authenticated and self.cookie:
            values["Cookie"] = self.cookie
        if method == "POST":
            raw_body = raw_body if raw_body is not None else json.dumps(data if data is not None else {}).encode()
            values.update({"Origin": "http://127.0.0.1:8766", "X-Panel-Token": self.csrf, "Content-Type": "application/json", "Content-Length": str(len(raw_body))})
        if headers:
            for key, value in headers.items():
                if value is None:
                    values.pop(key, None)
                else:
                    values[key] = value
        for key, value in values.items():
            handler.headers[key] = value
        handler.rfile, handler.wfile = io.BytesIO(raw_body or b""), io.BytesIO()
        getattr(handler, f"do_{method}")()
        result = handler.wfile.getvalue().decode("utf-8")
        head, body = result.split("\r\n\r\n", 1)
        lines = head.split("\r\n")
        code = int(lines[0].split()[1])
        response_headers = dict(line.split(": ", 1) for line in lines[1:])
        return code, response_headers, body

    def json_request(self, *args, **kwargs):
        code, headers, body = self.request(*args, **kwargs)
        return code, headers, json.loads(body)

    def test_full_exit_returns_success_before_closing_listener(self):
        with patch.object(self.state, "exit_panel_services", create=True,
                          return_value={"ok": True, "message": "正在退出"}) as stop:
            status, _, result = self.json_request("POST", "/api/service/exit", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        stop.assert_called_once_with()
        [(action, wire)] = self.actions
        self.assertEqual(action, "shutdown")
        self.assertEqual(json.loads(wire.split(b"\r\n\r\n", 1)[1]), result)

    def test_full_exit_rejection_keeps_panel_open(self):
        with patch.object(self.state, "exit_panel_services", create=True,
                          return_value={"ok": False, "error": "还原未完成"}) as stop:
            self.assertEqual(self.json_request("POST", "/api/service/exit", {"pid": 12})[0], 400)
            self.assertEqual(self.json_request("POST", "/api/service/exit", {}, headers={"X-Panel-Token":"bad"})[0], 403)
            stop.assert_not_called()
            status, _, result = self.json_request("POST", "/api/service/exit", {})
            self.assertEqual(status, 409)
            self.assertEqual(result["error"], "还原未完成")
        self.assertEqual(self.actions, [])

    def test_first_online_publish_only_accepts_an_authenticated_empty_post(self):
        task = Mock()
        self.state.autopilot.request_first_online = task
        path = "/api/autopilot/first-online"
        self.assertEqual(self.request("POST", path, {}, authenticated=False)[0], 401)
        self.assertEqual(self.request("POST", path, {"content": "injected"})[0], 400)
        task.assert_not_called()
        code, _, result = self.json_request("POST", path, {})
        self.assertEqual((code, result["status"]), (202, "accepted"))
        task.assert_called_once_with()
        task.side_effect = ValueError("此账号的上线通告已发表。")
        self.assertEqual(self.json_request("POST", path, {})[0], 409)

    def test_bootstrap_has_hardened_cookie_and_no_secret(self):
        code, headers, body = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("synthetic-test-secret", body)
        self.assertNotIn("__PANEL_CSRF__", body)
        self.assertIn(self.csrf, body, "Opening another tab should reuse the browser session")

    def test_config_and_schema_never_return_secrets(self):
        code, _, cfg = self.json_request("GET", "/api/config")
        self.assertEqual(code, 200)
        self.assertEqual(cfg["access_token"], "")
        self.assertEqual(cfg["bot_wxid"], "")
        self.assertEqual(set(cfg["_configured_secrets"]), {"access_token", "bot_wxid"})
        code, _, schema = self.json_request("GET", "/api/config/schema")
        self.assertEqual(schema[0]["fields"][0]["default"], "")
        self.assertNotIn("schema-secret-do-not-expose", json.dumps(schema))

    def test_read_requires_session(self):
        for route in ("/status", "/api/config", "/api/config/schema", "/api/memories", "/api/uia/diagnostics", "/assets/panel.js"):
            code, _, body = self.json_request("GET", route, authenticated=False)
            self.assertEqual(code, 401, route)
            self.assertFalse(body["ok"])

    def test_external_origin_and_rebinding_host_rejected(self):
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Host": "evil.example:8766"}, {"Sec-Fetch-Site": "cross-site"}, {"Referer": "http://evil.example/"}):
            code, _, _ = self.request("GET", "/", headers=headers)
            self.assertEqual(code, 403, headers)
            code, _, _ = self.request("POST", "/pause", headers=headers)
            self.assertEqual(code, 403, headers)
        self.assertFalse(self.actions)

    def test_mutations_require_origin_session_and_csrf(self):
        for headers in ({"Origin": None}, {"Cookie": None}, {"X-Panel-Token": None}, {"X-Panel-Token": "wrong-token"}, {"X-Panel-Token": "无效凭证"}):
            code, _, _ = self.request("POST", "/pause", headers=headers)
            self.assertIn(code, (401, 403))
        self.assertFalse(self.actions)
        for route, action in (("/start", "start"), ("/stop", "stop"), ("/pause", "pause"), ("/resume", "resume")):
            code, _, response = self.json_request("POST", route)
            self.assertEqual(code, 200)
            self.assertTrue(response["ok"])
            self.assertEqual(self.actions[-1], action)

    def test_empty_secrets_retained_explicit_clear_supported(self):
        code, _, response = self.json_request("POST", "/api/config", {"access_token": "", "voice_enabled": False})
        self.assertEqual(code, 200)
        self.assertEqual(self.updates[-1], {"voice_enabled": False})
        self.assertEqual(self.current["access_token"], "synthetic-test-secret")
        self.assertNotIn("never-echo-this", json.dumps(response))
        code, _, _ = self.json_request("POST", "/api/config", {"_clear_secrets": ["access_token"]})
        self.assertEqual(code, 200)
        self.assertEqual(self.current["access_token"], "")

    def test_invalid_config_and_body_are_rejected(self):
        for data in ({"unknown": "value"}, {"_clear_secrets": ["web_port"]}, {"_clear_secrets": "access_token"}, {"access_token": 123}):
            code, _, _ = self.request("POST", "/api/config", data)
            self.assertEqual(code, 400, data)
        for raw in (b"{", b"[]", b"\xff"):
            code, _, _ = self.request("POST", "/api/config", raw_body=raw)
            self.assertEqual(code, 400)
        code, _, _ = self.request("POST", "/api/config", headers={"Content-Length": "999999"})
        self.assertEqual(code, 413)
        code, _, _ = self.request("POST", "/api/config", headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 415)
        self.assertFalse(self.updates)

    def test_status_and_logs_redact_secrets_and_allow_filters(self):
        Path(self.directory.name, "bridge.log").write_text("INFO bearer fresh-credential\nWARNING access_token=synthetic-test-secret\nERROR https://user:pass@example/a?key=hidden-key\n", encoding="utf-8")
        code, _, status = self.json_request("GET", "/status")
        self.assertEqual(code, 200)
        text = json.dumps(status)
        self.assertNotIn("synthetic-test-secret", text)
        self.assertNotIn("fresh-credential", text)
        self.assertNotIn("user:pass", text)
        self.assertNotIn("hidden-key", text)
        code, _, logs = self.json_request("GET", "/api/logs?level=WARNING&q=access_token")
        self.assertEqual(logs["count"], 1)
        self.assertIn("[已隐藏]", logs["lines"][0])

    def test_status_adds_memory_only_performance_and_cache_without_diagnostics(self):
        snapshot = Mock(return_value={"stages": {"first_text": {"sample_count": 2, "average_seconds": 7.5}}})
        cache = types.SimpleNamespace(stats=lambda: {"hits": 3, "misses": 1, "synthesis_samples": 1})
        queue = {"pending": 1, "recent": [{"queue_wait_seconds": .4}]}
        self.state.get_status = lambda: {"running": True, "send_queue": queue,
            "runtime_watchdog": {"state": "recovering", "reason": "等待当前操作结束"}}
        with patch.object(self.state, "response_performance_snapshot", snapshot, create=True), \
                patch.dict("sys.modules", {"tts_cache": cache}):
            code, _, result = self.json_request("GET", "/api/status")
        self.assertEqual(code, 200)
        snapshot.assert_called_once_with(queue)
        self.assertEqual(result["response_performance"]["stages"]["first_text"]["average_seconds"], 7.5)
        self.assertEqual(result["tts_cache"]["hits"], 3)
        self.assertEqual(result["runtime_watchdog"]["state"], "recovering")
        self.assertEqual(self.diagnostic_calls, [])

    def test_preview_status_cannot_mix_actual_performance_or_cache(self):
        self.state.get_status = lambda: {"preview": True, "response_performance": {"synthetic": True}}
        with patch.object(self.state, "response_performance_snapshot", Mock(), create=True) as sample, \
                patch.dict("sys.modules", {"tts_cache": types.SimpleNamespace(stats=lambda: self.fail("No real cache in preview"))}):
            _, _, result = self.json_request("GET", "/api/status")
        sample.assert_not_called()
        self.assertEqual(result["response_performance"], {"synthetic": True})

    def test_memory_pagination_search_and_delete(self):
        code, _, result = self.json_request("GET", "/api/memories?limit=10&offset=10&q=1")
        self.assertEqual(code, 200)
        self.assertEqual(self.state.memory_store.calls[-1], (10, 10, "1"))
        self.assertEqual(result["total"], self.state.memory_store.count(query="1"))
        code, _, _ = self.request("GET", "/api/memories?limit=nope")
        self.assertEqual(code, 400)
        code, _, _ = self.request("POST", "/api/memories/delete", {"id": True})
        self.assertEqual(code, 400)
        code, _, _ = self.request("POST", "/api/memories/delete", {"id": 1})
        self.assertEqual(code, 200)
        code, _, _ = self.request("POST", "/api/memories/delete", {"id": 1})
        self.assertEqual(code, 404)

    def test_clear_requires_explicit_confirmation(self):
        code, _, _ = self.request("POST", "/api/memories/clear")
        self.assertEqual(code, 400)
        self.assertEqual(self.state.memory_store.count(), 65)
        code, _, _ = self.request("POST", "/api/memories/clear", {"confirm": "clear_all_memories"})
        self.assertEqual(code, 200)
        self.assertEqual(self.state.memory_store.count(), 0)

    def test_pending_resolution_records_both_outcomes_without_sending(self):
        for outcome in ("published", "not_published"):
            self.pending = {"intent_id": "intent-123", "content": "测试正文", "image_path": ""}
            code, _, result = self.json_request("POST", "/api/autopilot/resolve-pending", self.pending_request(outcome=outcome))
            self.assertEqual(code, 200)
            self.assertTrue(result["ok"])
            self.assertIsNone(result["autopilot_pending"])
            self.assertEqual(self.resolve_calls[-1], (outcome, "intent-123"))
            self.assertIn("不会立即重发", result["message"])
        self.assertFalse(self.actions)
        self.assertFalse(self.updates)
        self.assertFalse(self.diagnostic_calls)

    def test_pending_resolution_requires_explicit_wechat_check_and_valid_fields(self):
        cases = [self.pending_request(confirm=""), self.pending_request(outcome="retry"),
                 self.pending_request(outcome=[]), self.pending_request(intent_id=""),
                 self.pending_request(intent_id=True), self.pending_request(intent_id="x" * 129),
                 self.pending_request(intent_id=" intent-123"), self.pending_request(intent_id="intent\n123"),
                 {"outcome": "published", "intent_id": "intent-123"}, self.pending_request(send_now=True)]
        for body in cases:
            code, _, result = self.json_request("POST", "/api/autopilot/resolve-pending", body)
            self.assertEqual(code, 400, body)
            self.assertFalse(result["ok"])
        self.assertFalse(self.resolve_calls)
        self.assertIsNotNone(self.pending)

    def test_pending_resolution_preserves_session_origin_and_csrf_guards(self):
        for headers in ({"Cookie": None}, {"X-Panel-Token": None}, {"Origin": None}, {"Origin": "http://evil.example"}):
            code, _, _ = self.request("POST", "/api/autopilot/resolve-pending", self.pending_request(), headers=headers)
            self.assertIn(code, (401, 403))
        code, _, _ = self.request("GET", "/api/autopilot/resolve-pending")
        self.assertEqual(code, 404)
        self.assertFalse(self.resolve_calls)
        self.assertIsNotNone(self.pending)

    def test_pending_resolution_rejects_stale_intent_and_runtime_contention(self):
        before = dict(self.pending)
        code, _, result = self.json_request("POST", "/api/autopilot/resolve-pending", self.pending_request(intent_id="old-intent"))
        self.assertEqual(code, 409)
        self.assertFalse(result["ok"])
        self.assertEqual(self.resolve_calls, [("published", "old-intent")])
        self.assertEqual(self.pending, before)
        self.state.autopilot.resolve_pending = lambda *args, **kwargs: False
        code, _, _ = self.request("POST", "/api/autopilot/resolve-pending", self.pending_request())
        self.assertEqual(code, 409)
        self.assertEqual(self.pending, before)

    def test_pending_resolution_missing_runtime_and_record_errors_stay_blocked(self):
        self.state.autopilot = None
        code, _, _ = self.request("POST", "/api/autopilot/resolve-pending", self.pending_request())
        self.assertEqual(code, 503)
        self.assertFalse(self.resolve_calls)
        self.pending = {"status": "record_error", "error": "无法读取"}
        self.state.autopilot = types.SimpleNamespace(pending_snapshot=lambda: self.pending, resolve_pending=self.resolve_pending)
        code, _, _ = self.request("POST", "/api/autopilot/resolve-pending", self.pending_request())
        self.assertEqual(code, 409)
        self.assertEqual(self.pending["status"], "record_error")

    def test_pending_status_and_resolution_response_keep_secret_redaction(self):
        self.pending["content"] = "synthetic-test-secret"
        self.state.get_status = lambda: {"autopilot_pending": self.pending}
        code, _, result = self.json_request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertEqual(result["autopilot_pending"]["intent_id"], "intent-123")
        self.assertNotIn("synthetic-test-secret", json.dumps(result))
        self.state.autopilot.resolve_pending = lambda *args, **kwargs: True
        code, _, result = self.json_request("POST", "/api/autopilot/resolve-pending", self.pending_request())
        self.assertEqual(code, 200)
        self.assertNotIn("synthetic-test-secret", json.dumps(result))

    def test_short_secret_does_not_corrupt_runtime_pending_id(self):
        self.current["access_token"] = "1"
        Path(self.config.CONFIG_FILE).write_text(json.dumps(self.current), encoding="utf-8")
        ident = "0123456789abcdef0123456789abcdef"
        self.pending = {"version": 1, "status": "pending_review", "intent_id": ident,
                        "content": "token=1", "time": "2026-10-11 18:20:30", "image_path": "photo1.png"}
        self.state.get_status = lambda: {"autopilot_pending": self.pending, "other_id": ident}
        code, _, result = self.json_request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertEqual(result["autopilot_pending"]["intent_id"], ident)
        self.assertNotEqual(result["other_id"], ident, "Unrelated string fields still use the normal redactor")
        self.assertIn("[已隐藏]", result["autopilot_pending"]["content"])
        self.assertIn("[已隐藏]", result["autopilot_pending"]["image_path"])
        # Resolution response has the same serialization, including a newer
        # pending intent that can appear after a successful reconciliation.
        self.state.autopilot.resolve_pending = lambda *args, **kwargs: True
        code, _, result = self.json_request("POST", "/api/autopilot/resolve-pending", self.pending_request(intent_id=ident))
        self.assertEqual(code, 200)
        self.assertEqual(result["autopilot_pending"]["intent_id"], ident)
        self.assertIn("[已隐藏]", result["autopilot_pending"]["content"])
        self.state.autopilot.resolve_pending = self.resolve_pending
        code, _, _ = self.request("POST", "/api/autopilot/resolve-pending", self.pending_request(intent_id=ident))
        self.assertEqual(code, 200)
        self.assertEqual(self.resolve_calls[-1], ("published", ident))

    def test_pending_identifier_exception_is_not_a_general_redaction_bypass(self):
        ident = "0123456789abcdef0123456789abcdef"
        for pending in ({"intent_id": ident}, {"version": 1, "status": "record_error", "intent_id": ident},
                        {"version": 1, "status": "pending_review", "intent_id": "client-token-1"},
                        {"version": 2, "status": "pending_review", "intent_id": ident},
                        {"version": True, "status": "pending_review", "intent_id": ident}):
            with patch.object(self.panel, "_secret_values", return_value=["1"]):
                result = self.panel._redact_runtime_snapshot({"autopilot_pending": pending})
            self.assertNotEqual(result["autopilot_pending"]["intent_id"], pending["intent_id"])

    def test_read_only_uia_diagnostics_and_audio_controls(self):
        code, _, result = self.json_request("GET", "/api/uia/diagnostics")
        self.assertEqual(code, 200)
        self.assertTrue(result["read_only"])
        self.assertEqual(len(self.diagnostic_calls), 1)
        self.assertNotIn("synthetic-test-secret", json.dumps(result))
        code, _, audio = self.json_request("GET", "/api/audio/status")
        self.assertTrue(audio["target_available"])
        code, _, response = self.json_request("POST", "/api/audio/open-settings")
        self.assertTrue(response["ok"])

    def test_moments_browse_queues_runtime_callback_without_enabling(self):
        code, _, result = self.json_request("POST", "/api/moments/interactions/run")
        self.assertEqual(code, 202)
        self.assertTrue(result["ok"])
        self.assertEqual(self.social_runs, [True])
        self.assertFalse(self.updates)
        self.assertFalse(self.actions)
        self.assertFalse(self.diagnostic_calls)
        for data in ({"enabled": True}, {"force": True}, {"action": "like"}):
            code, _, _ = self.request("POST", "/api/moments/interactions/run", data)
            self.assertEqual(code, 400)
        self.assertEqual(self.social_runs, [True])

    def test_moments_browse_preserves_runtime_rejections_and_preview(self):
        self.state.run_moments_interactions = lambda: {"ok": False, "error": "桥接已暂停"}
        code, _, result = self.json_request("POST", "/api/moments/interactions/run")
        self.assertEqual(code, 409)
        self.assertIn("暂停", result["error"])
        self.state.get_status = lambda: {"preview": True}
        self.state.run_moments_interactions = lambda: self.social_runs.append(True) or {"ok": True}
        for route, data in (("/api/moments/interactions/run", {}), ("/api/moments/interactions/resolve", self.social_request())):
            code, _, _ = self.request("POST", route, data)
            self.assertEqual(code, 409)
        self.assertFalse(self.social_runs)
        self.assertFalse(self.social_resolves)
        self.state.get_status = lambda: {"preview": False}
        self.state.run_moments_interactions = None
        code, _, _ = self.request("POST", "/api/moments/interactions/run")
        self.assertEqual(code, 503)

    def test_moments_routes_keep_authentication_and_csrf(self):
        for route, data in (("/api/moments/interactions/run", {}), ("/api/moments/interactions/resolve", self.social_request())):
            for headers in ({"Cookie": None}, {"X-Panel-Token": None}, {"Origin": None}, {"Origin": "https://evil.example"}):
                code, _, _ = self.request("POST", route, data, headers=headers)
                self.assertIn(code, (401, 403))
            code, _, _ = self.request("GET", route)
            self.assertEqual(code, 404)
        self.assertFalse(self.social_runs)
        self.assertFalse(self.social_resolves)

    def test_moments_manual_fixed_blocked_status_is_preserved_without_callback_prose(self):
        reasons = {"disabled": "请先启用朋友圈互动", "busy": "朋友圈互动正在处理",
                   "unavailable": "请先启动并继续桥接",
                   "restore_failed": "UIA 临时状态尚未还原，请先重试停止桥接",
                   "account_identity_missing": "请配置当前账号的真实微信 ID",
                   "pending_review": "朋友圈互动待核对，请先核对上次操作结果",
                   "outside_hours": "当前不在朋友圈互动活跃时段，请调整活跃时间或在时段内再试",
                   "private_priority": "私聊优先，等私聊处理完成并达到空闲时间后再试"}
        for status, reason in reasons.items():
            with self.subTest(status=status):
                self.state.run_moments_interactions = lambda: {"ok": False, "status": status,
                    "error": "PRIVATE_PROMPT_CANARY synthetic-test-secret", "debug": "PRIVATE_DEBUG_CANARY"}
                code, _, result = self.json_request("POST", "/api/moments/interactions/run")
                self.assertEqual(code, 409)
                self.assertEqual(result, {"ok": False, "status": status, "error": reason})
        self.assertEqual(self.social_runs, [])

    def test_moments_manual_waiting_status_is_not_reported_as_successfully_queued(self):
        for status in ("outside_hours", "private_priority"):
            with self.subTest(status=status):
                self.state.run_moments_interactions = lambda: {"ok": True, "status": status, "message": "已排队"}
                code, _, result = self.json_request("POST", "/api/moments/interactions/run")
                self.assertEqual(code, 409)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], status)
                self.assertNotIn("已排队", json.dumps(result, ensure_ascii=False))

    def test_moments_manual_unknown_failure_and_exception_do_not_expose_private_prose(self):
        def broken():
            raise ValueError("PRIVATE_PROMPT_CANARY synthetic-test-secret")
        for callback in (lambda: {"ok": False, "status": "PRIVATE_STATUS_CANARY", "error": "PRIVATE_PROMPT_CANARY"},
                         lambda: {"ok": False, "status": ["PRIVATE_STATUS_CANARY"], "prompt": "PRIVATE_PROMPT_CANARY"},
                         broken):
            with self.subTest(callback=callback):
                self.state.run_moments_interactions = callback
                code, _, result = self.json_request("POST", "/api/moments/interactions/run")
                self.assertEqual(code, 503)
                self.assertEqual(result, {"ok": False, "status": "failed", "error": "浏览任务未能排队，请检查桥接与朋友圈互动设置"})
        self.assertFalse(self.social_runs)
        self.assertFalse(self.social_resolves)

    def test_moments_resolution_records_only_explicit_current_intent(self):
        for outcome in ("published", "not_published"):
            self.set_social_pending()
            code, _, result = self.json_request("POST", "/api/moments/interactions/resolve", self.social_request(outcome=outcome))
            self.assertEqual(code, 200)
            self.assertIsNone(result["moments_interactions"]["pending"])
            self.assertEqual(self.social_resolves[-1], (outcome, self.social_request()["intent_id"]))
            self.assertIn("不会立即重复互动", result["message"])
        self.assertFalse(self.social_runs)
        self.assertFalse(self.updates)
        self.assertFalse(self.actions)

    def test_moments_resolution_validates_fields_and_keeps_blocked_record(self):
        self.set_social_pending()
        for changes in ({"outcome": "retry"}, {"outcome": []}, {"intent_id": "old-id"}, {"intent_id": "0" * 31},
                        {"intent_id": "A" * 32}, {"intent_id": True}, {"confirm": ""}, {"force": True}):
            code, _, _ = self.request("POST", "/api/moments/interactions/resolve", self.social_request(**changes))
            self.assertEqual(code, 400, changes)
        self.assertFalse(self.social_resolves)
        before = dict(self.social_status["pending"])
        code, _, _ = self.request("POST", "/api/moments/interactions/resolve", self.social_request(intent_id="f" * 32))
        self.assertEqual(code, 409)
        self.assertEqual(self.social_status["pending"], before)
        self.state.moments_interactions.resolve_pending = lambda *args, **kwargs: False
        code, _, _ = self.request("POST", "/api/moments/interactions/resolve", self.social_request())
        self.assertEqual(code, 409)
        self.assertEqual(self.social_status["pending"], before)
        self.state.moments_interactions = None
        code, _, _ = self.request("POST", "/api/moments/interactions/resolve", self.social_request())
        self.assertEqual(code, 503)

    def test_moments_opaque_pending_id_roundtrips_but_prose_is_redacted(self):
        self.current["access_token"] = "1"
        Path(self.config.CONFIG_FILE).write_text(json.dumps(self.current), encoding="utf-8")
        self.set_social_pending()
        self.social_status["pending"].update(content="token=1", text="1")
        self.state.get_status = lambda: {"moments_interactions": self.social_status, "other_id": self.social_request()["intent_id"]}
        code, _, result = self.json_request("GET", "/status")
        pending = result["moments_interactions"]["pending"]
        self.assertEqual(code, 200)
        self.assertEqual(pending["intent_id"], self.social_request()["intent_id"])
        self.assertEqual(pending["text"], "[已隐藏]")
        self.assertNotEqual(result["other_id"], pending["intent_id"])
        self.state.moments_interactions.resolve_pending = lambda *args, **kwargs: True
        code, _, result = self.json_request("POST", "/api/moments/interactions/resolve", self.social_request())
        self.assertEqual(code, 200)
        self.assertEqual(result["moments_interactions"]["pending"]["intent_id"], pending["intent_id"])
        for changes in ({"version": 2}, {"version": True}, {"status": "record_error"}, {"type": "send"}, {"intent_id": "client-1"}):
            original = {**self.social_status["pending"], **changes}
            with patch.object(self.panel, "_secret_values", return_value=["1"]):
                redacted = self.panel._redact_runtime_snapshot({"moments_interactions": {"pending": original}})
            self.assertNotEqual(redacted["moments_interactions"]["pending"]["intent_id"], original["intent_id"])

    def test_unknown_routes_missing_runtime_and_expired_sessions(self):
        code, _, _ = self.request("GET", "/api/memories/unknown")
        self.assertEqual(code, 404)
        code, _, _ = self.request("POST", "/api/uia/send")
        self.assertEqual(code, 404)
        self.state.start_bridge = None
        code, _, _ = self.request("POST", "/start")
        self.assertEqual(code, 503)
        for session in self.panel._SESSIONS.values():
            session["touched"] = time.monotonic() - self.panel._SESSION_TTL - 1
        code, _, _ = self.request("GET", "/status")
        self.assertEqual(code, 401)

    def test_all_assets_are_local_and_html_has_required_controls(self):
        for path in ("/assets/panel.css", "/assets/panel.js"):
            code, headers, body = self.request("GET", path)
            self.assertEqual(code, 200)
            self.assertNotIn("Access-Control-Allow-Origin", headers)
            self.assertTrue(body)
        for identifier in ("featureToggles", "settingsForm", "memorySearch", "uiaDiagnostics", "audioDiagnostics", "nameSyncStatus", "logLevel", "btnPause", "saveConfig",
                           "autopilotPendingNotice", "autopilotPendingCard", "autopilotPendingContent", "autopilotPublished", "autopilotNotPublished",
                           "momentsInteractionNotice", "momentsInteractionPending", "momentsInteractionCompleted", "momentsBrowseNow", "momentsInteractionRecent"):
            self.assertIn(f'id="{identifier}"', self.panel.PAGE)
        self.assertNotIn("onclick=", self.panel.PAGE)
        self.assertNotIn("https://fonts.", self.panel.PAGE)

    @unittest.skipUnless(shutil.which("node"), "Node.js needed for synthetic panel DOM regression")
    def test_pending_frontend_state_and_confirmation_behaviors(self):
        result = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("test_panel_pending.js"))],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Panel pending behavior: PASS", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js needed for synthetic panel DOM regression")
    def test_moments_frontend_controls_and_uncertainty_behaviors(self):
        result = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("test_moments_panel.js"))],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Moments panel behavior: PASS", result.stdout)


class MomentsConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("moments_panel_config_model", SOURCE / "config_model.py")
        cls.model = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {spec.name: cls.model}), patch("sys.path", [str(SOURCE), *__import__("sys").path]):
            spec.loader.exec_module(cls.model)

    def test_moments_defaults_are_complete_and_match_public_example(self):
        example = json.loads((SOURCE / "config.example.json").read_text(encoding="utf-8"))
        fields = next(group.fields for group in self.model.CONFIG_SCHEMA if group.title == "朋友圈互动")
        self.assertTrue({"moments_reply_poll_seconds", "moments_private_quiet_seconds",
                         "moments_browse_interval_min", "moments_reply_cooldown_seconds"}
                        <= {field.key for field in fields})
        self.assertFalse(self.model.get_field("moments_interactions_enabled").default)
        self.assertEqual(self.model.get_field("moments_interaction_contacts").default, [])
        for field in fields:
            self.assertIn(field.key, example)
            self.assertEqual(example[field.key], field.default)

    def test_reply_style_is_configured_in_the_main_persona_only(self):
        example = json.loads((SOURCE / "config.example.json").read_text(encoding="utf-8"))
        for key in ("reply_style_max_chars", "reply_style_no_letters", "reply_style_allowed_punctuation"):
            self.assertIsNone(self.model.get_field(key))
            self.assertNotIn(key, example)
        persona = "  我在这一行！\r\n保留我的自定义符号 & English。\n"
        self.assertEqual(self.model.normalize_config({"bot_persona": persona})["bot_persona"], persona)
        self.assertIn("标点", self.model.get_field("bot_persona").description)

    def test_moments_numeric_limits_and_hours_are_validated(self):
        fields = next(group.fields for group in self.model.CONFIG_SCHEMA if group.title == "朋友圈互动")
        for field in fields:
            if field.type != "number":
                continue
            for value in (field.min, field.max):
                self.assertEqual(self.model.normalize_config({field.key: value})[field.key], value)
            for value in (field.min - 1, field.max + 1, True, float("nan"), float("inf")):
                with self.assertRaises(ValueError, msg=f"{field.key}: {value}"):
                    self.model.normalize_config({field.key: value})
            if field.integer:
                with self.assertRaises(ValueError):
                    self.model.normalize_config({field.key: field.min + 0.5})

    def test_contact_filter_normalizes_ids_but_rejects_display_names_and_groups(self):
        key = "moments_interaction_contacts"
        self.assertEqual(self.model.normalize_config({key: " wxid_a，wechat-user\nwxid_a\r\n"})[key], ["wxid_a", "wechat-user"])
        self.assertEqual(self.model.normalize_config({key: ["wxid_a", " wxid_a ", "", "member@openim"]})[key], ["wxid_a", "member@openim"])
        self.assertEqual(self.model.normalize_config({key: ""})[key], [])
        for value in ("自用测试联系人", "备注 好友", "123@chatroom", "unknown", "null", "None", "wxid_a\x00", "a" * 257, 123, ["wxid_a", True]):
            with self.assertRaises(ValueError, msg=repr(value)):
                self.model.normalize_config({key: value})


if __name__ == "__main__":
    unittest.main()
