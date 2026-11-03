"""Core checks use synthetic events and stubs: no paid API or desktop interaction."""
import ast
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import struct
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeDataAttributesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-data-attributes-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = self.root / "custom-memory.db"
        self.identity = self.root / "identity_map.json"
        self.config = types.SimpleNamespace(DATA_DIR=str(self.root), MEMORY_DB_PATH=str(self.memory))
        tree = ast.parse((SOURCE / "main.py").read_text(encoding="utf-8-sig"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_prepare_writable_runtime_data")
        namespace = {"os": os, "stat": stat, "config": self.config,
                     "state": types.SimpleNamespace(identity_registry=types.SimpleNamespace(path=str(self.identity)))}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE / "main.py"), "exec"), namespace)
        self.prepare = namespace[function.name]

    def readonly(self, path, content):
        path.write_bytes(content)
        os.chmod(path, stat.S_IREAD)
        self.addCleanup(lambda: os.chmod(path, stat.S_IWRITE) if path.exists() else None)

    @unittest.skipUnless(os.name == "nt", "Windows runtime data attributes")
    def test_only_declared_runtime_data_and_sqlite_sidecars_become_writable(self):
        databases = (self.memory, self.root / "outbox.db", self.root / "moments_interactions.sqlite3")
        declared = [self.identity] + [Path(str(path) + suffix) for path in databases for suffix in ("", "-wal", "-shm")]
        for path in declared:
            self.readonly(path, b"preserve runtime content")
        unrelated = self.root / "other.db"
        self.readonly(unrelated, b"unrelated content")
        self.prepare()
        for path in declared:
            self.assertEqual(path.read_bytes(), b"preserve runtime content")
            self.assertFalse(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
        self.assertTrue(unrelated.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
        self.assertEqual(unrelated.read_bytes(), b"unrelated content")
        replacement = self.root / "identity.tmp"
        replacement.write_bytes(b"updated identity")
        os.replace(replacement, self.identity)
        self.assertEqual(self.identity.read_bytes(), b"updated identity")

    @unittest.skipUnless(os.name == "nt", "Windows runtime data attributes")
    def test_configured_readonly_sqlite_database_preserves_records_and_accepts_writes(self):
        with contextlib.closing(sqlite3.connect(self.memory)) as db, db:
            db.execute("CREATE TABLE records (value TEXT)")
            db.execute("INSERT INTO records VALUES ('existing')")
        os.chmod(self.memory, stat.S_IREAD)
        self.addCleanup(lambda: os.chmod(self.memory, stat.S_IWRITE) if self.memory.exists() else None)
        original = self.memory.read_bytes()
        self.prepare()
        self.assertEqual(self.memory.read_bytes(), original)
        with contextlib.closing(sqlite3.connect(self.memory)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO records VALUES ('new')")
            self.assertEqual(db.execute("SELECT value FROM records ORDER BY rowid").fetchall(), [("existing",), ("new",)])
        self.assertFalse((self.root / "outbox.db").exists())
        self.assertFalse(self.identity.exists())

    def test_non_windows_does_not_change_attributes(self):
        with patch.object(os, "name", "posix"), patch.object(os, "chmod") as chmod:
            self.prepare()
        chmod.assert_not_called()


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix=".core-test-", dir=str(SOURCE.parent / "tests"))
        cls.config = types.ModuleType("config")
        values = dict(BOT_NICKNAMES=["小助手"], BOT_WXID="bot", BUFFER_SECONDS=30,
                      SMART_BUFFER_ENABLED=True, PRIVATE_BUFFER_SECONDS=.15,
                      GROUP_BUFFER_SECONDS=.2, BUFFER_MAX_WAIT=.6, CORE_WORKERS=4,
                      GROUP_REPLY_JUDGE_ENABLED=False, VOICE_ENABLED=True, TOOLS_ENABLED=True,
                      MEMORY_ENABLED=False, MEMORY_TOP_K=3, DATA_DIR=cls.temp.name,
                      ASTRBOT_ATTACHMENTS="", WE_FLOW_BASE_URL="http://127.0.0.1:5031",
                      ACCESS_TOKEN="synthetic", NAME_SYNC_SECONDS=5)
        cls.config.__dict__.update(values)
        cls.state = types.ModuleType("state")
        cls.stable = load_module("test_stable", "stable_id.py")
        cls.judge = types.ModuleType("reply_judge")
        cls.judge.should_reply_group = lambda *a, **k: (True, "text", "", "", "none")
        cls.tools = types.ModuleType("tools")
        cls.tools.generate_image = lambda text, **kwargs: None
        cls.tools.search_image = lambda text: None
        cls.tools.web_search = lambda text: "search result"
        sticker = types.ModuleType("sticker_selector")
        sticker.select_sticker = lambda keyword: None
        cls.http = types.ModuleType("http_util")
        cls.http.session = lambda: None
        human = types.ModuleType("humanize")
        human.style_prefix = lambda: ""
        cls.protocol = types.ModuleType("ob_protocol")
        cls.protocol.make_message_event = lambda kind, uid, segments, **kw: {"message_type": kind, "user_id": uid,
            "sender": {"user_id": uid, "nickname": kw.get("nickname", "")}, "message": segments, **kw}
        cls.protocol.push_event = lambda event: True
        cls.module_patch = patch.dict("sys.modules", {"config": cls.config, "state": cls.state,
                               "stable_id": cls.stable, "reply_judge": cls.judge,
                               "sticker_selector": sticker, "tools": cls.tools,
                               "http_util": cls.http, "humanize": human,
                               "ob_protocol": cls.protocol})
        cls.module_patch.start()
        cls.image = load_module("weflow_image", "weflow_image.py")
        cls.names = load_module("name_refresh", "name_refresh.py")
        cls.media = load_module("test_core_media_util", "media_util.py")
        cls.sub_patch = patch.dict("sys.modules", {"weflow_image": cls.image, "name_refresh": cls.names, "media_util": cls.media})
        cls.sub_patch.start()
        cls.core = load_module("test_bridge_core", "bridge_core.py")

    @classmethod
    def tearDownClass(cls):
        cls.names._POOL.shutdown(wait=True, cancel_futures=True)
        cls.core._MEDIA_POOL.shutdown(wait=True, cancel_futures=True)
        cls.core._IDENTITY_POOL.shutdown(wait=True, cancel_futures=True)
        cls.sub_patch.stop()
        cls.module_patch.stop()
        cls.temp.cleanup()

    def setUp(self):
        state = self.state
        state.running, state.bridge_generation, state.group_reply_mode = True, 1, "batch"
        state.paused, state.send_cancel = threading.Event(), threading.Event()
        state.run_lock = threading.RLock()
        state.reply_state_lock, state.ob_map_lock = threading.Lock(), threading.Lock()
        state.pending_reply_modes, state.suppress_text_until, state._ob_id_to_contact = {}, {}, {}
        state.prune_suppress = lambda: None
        state.memory_store, state._self_id_int = None, 999
        state.outbox = None
        self.names._snapshot_probe.clear()
        self.names._fingerprints.clear()
        self.names._etags.clear()
        self.registry = self.stable.IdentityRegistry(os.path.join(self.temp.name, self._testMethodName + ".json"))
        state.identity_registry = self.registry
        state.is_self_echo = lambda text, **kw: kw.get("sender_id") == "bot"
        self.sent, self.events = [], []
        state.dispatch_send = lambda method, contact, *a, **kw: self.sent.append((method, contact, a, kw)) or True
        self.core.push_event = lambda event: self.events.append(event) or True
        self.config.GROUP_REPLY_JUDGE_ENABLED = False
        self.config.VOICE_ENABLED = True
        self.config.TOOLS_ENABLED = True
        self.tools.generate_image = lambda text, **kwargs: None
        self.judge.should_reply_group = lambda *a, **kw: (True, "text", "", "", "none")
        self.bridge = self.core.WeFlowBridge(types.SimpleNamespace())
        self.addCleanup(self.bridge.close)
        self.addCleanup(self.registry.flush)

    @staticmethod
    def message(session="one@chatroom", sender="alice", content="hello", **extra):
        return dict(sessionId=session, sourceName=sender, senderUsername=sender if "@chatroom" in session else session,
                    senderName=sender, groupName="同名群" if "@chatroom" in session else "",
                    content=content, **extra)

    def stop_scheduler(self):
        self.bridge._sched.stop()

    def test_sse_cancelled_stream_close_is_not_an_error(self):
        for mode in ("stopped", "old_generation", "active"):
            with self.subTest(mode=mode):
                self.state.running, self.state.bridge_generation = True, 1
                def closed_stream(**kwargs):
                    if mode == "stopped":
                        self.state.running = False
                    elif mode == "old_generation":
                        self.state.bridge_generation = 2
                    raise AttributeError("'NoneType' object has no attribute 'read'")
                response = types.SimpleNamespace(status_code=200, encoding=None,
                    iter_lines=closed_stream, close=lambda: None)
                session = types.SimpleNamespace(get=lambda *a, **kw: response, close=lambda: None)
                log_check = self.assertLogs("ob11-bridge", level="ERROR") if mode == "active" else self.assertNoLogs("ob11-bridge", level="ERROR")
                with patch.object(self.http, "new_session", return_value=session, create=True), log_check as captured:
                    self.bridge.listen_sse(generation=1)
                if mode == "active":
                    self.assertIn("SSE 异常", captured.output[0])
                self.assertIsNone(self.bridge._sse_response)

    def entry(self):
        return next(iter(self.bridge.pending_buffers.values()))

    def process_all(self):
        for key in list(self.bridge.pending_buffers):
            self.bridge.process_sender(key)

    def test_same_name_groups_use_stable_session(self):
        self.stop_scheduler()
        self.bridge.add_to_buffer(self.message("one@chatroom"))
        self.bridge.add_to_buffer(self.message("two@chatroom"))
        self.assertEqual(len(self.bridge.pending_buffers), 2)
        self.process_all()
        self.assertEqual({event["group_id"] for event in self.events},
                         {self.stable.group_id("one@chatroom"), self.stable.group_id("two@chatroom")})

    def test_count_prune_retains_oldest_moments_leased_inbound_image(self):
        media = load_module("test_core_media_lease", "media_util.py")
        folder = Path(self.temp.name) / "leased-inbound-images"
        folder.mkdir(exist_ok=True)
        paths = []
        for index in range(81):
            path = folder / (str(index) + ".png")
            path.write_bytes(b"synthetic cached bytes")
            os.utime(path, (100 + index, 100 + index))
            paths.append(path)
        with patch.dict("sys.modules", {"media_util": media}), media.lease_file(paths[0]):
            self.core._prune_dir(str(folder), keep=80)
            self.assertTrue(paths[0].exists())
            self.assertFalse(paths[1].exists())
            self.assertEqual(len(list(folder.iterdir())), 80)
        with patch.dict("sys.modules", {"media_util": media}):
            self.core._prune_dir(str(folder), keep=79)
            self.assertFalse(paths[0].exists())

    def test_count_prune_keeps_pending_outbox_image(self):
        media = load_module("test_core_media_outbox", "media_util.py")
        folder = Path(self.temp.name) / "pending-inbound-images"
        folder.mkdir(exist_ok=True)
        paths = []
        for index in range(3):
            path = folder / (str(index) + ".png")
            path.write_bytes(b"synthetic cached bytes")
            os.utime(path, (100 + index, 100 + index))
            paths.append(path)
        self.state.outbox = types.SimpleNamespace(referenced_files=lambda: {str(paths[0])})
        with patch.dict("sys.modules", {"media_util": media}):
            self.core._prune_dir(str(folder), keep=2)
        self.assertTrue(paths[0].exists())
        self.assertFalse(paths[1].exists())

    def _buffer_cached_image(self, suffix):
        self.stop_scheduler()
        jobs = []
        self.bridge._spawn_media = lambda fn, data: jobs.append(data)
        folder = Path(self.temp.name) / ("buffer-lease-" + suffix)
        folder.mkdir(exist_ok=True)
        path = folder / "original.png"
        path.write_bytes(b"synthetic cached bytes")
        self.bridge.add_to_buffer(self.message("friend", content="[图片]", type=3, rawid=suffix))
        self.bridge._finish_media(jobs[0], "原图直接查看", segments=[{"type": "image", "data": {"file": str(path)}}])
        return folder, path

    def test_frozen_processing_batch_keeps_media_until_durable_acceptance(self):
        folder, path = self._buffer_cached_image("processing")
        original = self.bridge._process_sender_body
        def process(key, entry, messages):
            self.assertEqual(self.bridge.pending_buffers[key]["messages"], [])
            self.core._prune_dir(str(folder), keep=0)
            self.assertTrue(path.exists(), "冻结后的批次仍然拥有原图")
            return original(key, entry, messages)
        self.bridge._process_sender_body = process
        self.process_all()
        self.assertEqual(len(self.events), 1)
        self.bridge._recent_media.drop()  # End the independently leased followup window.
        self.core._prune_dir(str(folder), keep=0)
        self.assertFalse(path.exists(), "durable 接受后由 outbox 接管，bridge 自身须释放租约")

    def test_failed_durable_acceptance_retains_media_across_retry(self):
        folder, path = self._buffer_cached_image("retry")
        self.core.push_event = lambda event: False
        self.process_all()
        self.assertEqual(len(self.entry()["retry_events"]), 1)
        self.core._prune_dir(str(folder), keep=0)
        self.assertTrue(path.exists())
        self.bridge.add_to_buffer(self.message("friend", content="later message", rawid="later"))
        self.process_all()  # Retry failure must requeue this batch and preserve older leases.
        self.core._prune_dir(str(folder), keep=0)
        self.assertTrue(path.exists())
        self.assertEqual(self.entry()["messages"][0]["text"], "later message")
        self.core.push_event = lambda event: self.events.append(event) or True
        self.process_all()
        self.assertEqual(len(self.events), 1)  # The retry keeps its own turn before the addition.
        self.process_all()
        self.assertEqual(len(self.events), 2)
        self.bridge._recent_media.drop()
        self.core._prune_dir(str(folder), keep=0)
        self.assertFalse(path.exists())

    def test_close_releases_pending_media_and_rejects_late_completion(self):
        folder, path = self._buffer_cached_image("closed")
        self.bridge.close()
        self.assertEqual(self.bridge.pending_buffers, {})
        self.assertFalse(self.bridge._active())
        self.bridge.add_to_buffer(self.message("friend", content="late"))
        self.core._prune_dir(str(folder), keep=0)
        self.assertFalse(path.exists())
        self.assertEqual(self.bridge.pending_buffers, {})

    def test_close_keeps_running_batch_media_until_worker_exits(self):
        folder, path = self._buffer_cached_image("running-close")
        entered, release = threading.Event(), threading.Event()
        def process(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError("Synthetic worker was not released")
        self.bridge._process_sender_body = process
        worker = threading.Thread(target=self.process_all)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.bridge.close()
            self.core._prune_dir(str(folder), keep=0)
            self.assertTrue(path.exists(), "停机不能删除仍在使用中的冻结批次附件")
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.core._prune_dir(str(folder), keep=0)
        self.assertFalse(path.exists())

    def test_same_text_from_other_sender_is_not_echo(self):
        self.stop_scheduler()
        self.bridge.add_to_buffer(self.message("friend", content="OK"))
        self.assertEqual(len(self.entry()["messages"]), 1)
        self.bridge.add_to_buffer(dict(self.message("friend", sender="bot", content="OK"), senderUsername="bot"))
        self.assertEqual(len(self.entry()["messages"]), 1)

    def test_same_display_name_private_users_have_distinct_wechat_routes(self):
        self.stop_scheduler()
        for session in ("wxid_a", "wxid_b"):
            self.bridge.add_to_buffer(self.message(session, sender="同名用户"))
        self.process_all()
        self.assertEqual({event["user_id"] for event in self.events},
                         {self.stable.user_id("wxid_a", "wxid_a"), self.stable.user_id("wxid_b", "wxid_b")})
        self.assertEqual({event["wechat_identity"]["session_id"] for event in self.events}, {"wxid_a", "wxid_b"})

    def test_private_current_nickname_and_remark_reach_model_event_without_changing_id(self):
        self.stop_scheduler()
        self.bridge.add_to_buffer(self.message("wxid_friend", sender="旧显示名"))
        self.state.identity_registry.register_private("wxid_friend", "新备注", nickname="实际昵称", remark="新备注", source="snapshot")
        self.process_all()
        event = self.events[0]
        identity = event["wechat_identity"]
        actor = identity["messages"][0]
        self.assertEqual(actor["username"], "wxid_friend")
        self.assertEqual(actor["nickname"], "实际昵称")
        self.assertEqual(actor["remark"], "新备注")
        self.assertEqual(actor["display_name"], "新备注")
        self.assertEqual(identity["session_display_name"], "新备注")
        self.assertEqual(event["sender"]["nickname"], "实际昵称")
        self.assertEqual(event["user_id"], self.stable.user_id("wxid_friend", "wxid_friend"))

    def test_same_display_name_members_remain_distinct_in_one_group_batch(self):
        self.stop_scheduler()
        for username in ("wxid_member_a", "wxid_member_b"):
            data = dict(self.message(sender=username, rawId="server-" + username, localId=username + "-local"), sourceName="同名成员", senderName="同名成员")
            self.bridge.add_to_buffer(data)
        self.process_all()
        identity = self.events[0]["wechat_identity"]
        self.assertEqual({row["username"] for row in identity["messages"]}, {"wxid_member_a", "wxid_member_b"})
        self.assertEqual(len({row["user_id"] for row in identity["messages"]}), 2)
        self.assertEqual(identity["session_id"], "one@chatroom")
        text = self.events[0]["message"][1]["data"]["text"]
        for username in ("wxid_member_a", "wxid_member_b"):
            self.assertIn("微信ID：" + username, text)
        for row in identity["messages"]:
            self.assertEqual(row["original_message_id"], "server-" + row["username"])
            self.assertEqual(row["message_ids"]["localId"], row["username"] + "-local")
            self.assertEqual(row["group_id"], self.stable.group_id("one@chatroom"))

    def test_group_identity_does_not_import_private_contact_remark(self):
        self.stop_scheduler()
        self.state.identity_registry.register_private("wxid_member_a", "私聊备注", nickname="私聊昵称", remark="私聊备注", source="snapshot")
        self.bridge.add_to_buffer(dict(self.message(sender="wxid_member_a"), sourceName="群内显示", senderName="群内显示"))
        self.process_all()
        identity = self.events[0]["wechat_identity"]
        actor = identity["messages"][0]
        self.assertEqual(actor["username"], "wxid_member_a")
        self.assertNotIn("remark", actor)
        self.assertNotIn("私聊", json.dumps(identity, ensure_ascii=False))

    def test_invalid_or_conflicting_identity_never_uses_display_name(self):
        self.stop_scheduler()
        base = self.message("wxid_a")
        cases = [dict(base, sessionId="同名用户"), dict(base, sessionId=7), dict(base, sessionId=""),
                 dict(base, talkerId="wxid_b"), dict(base, senderUsername="wxid_b"),
                 dict(base, groupName="群名"), dict(base, sessionType="group"),
                 dict(self.message(), senderWxid="bob"), dict(self.message(), senderUsername="同名成员")]
        for data in cases:
            with self.subTest(data=data):
                self.bridge.add_to_buffer(data)
        self.assertEqual(self.bridge.pending_buffers, {})
        self.assertEqual(self.registry.stats()["private"], 0)
        self.assertEqual(self.registry.stats()["groups"], 0)

    def test_missing_group_sender_is_resolved_by_exact_rawid_not_display(self):
        self.stop_scheduler()
        data = self.message(rawid="server-one"); data.pop("senderUsername")
        data["sourceName"] = "同名成员"
        row = {"serverId": "server-one", "senderUsername": "wxid_actual", "isSend": 0}
        response = types.SimpleNamespace(status_code=200, json=lambda: {"success": True, "talker": "one@chatroom", "messages": [row]})
        client = types.SimpleNamespace(get=lambda *a, **kw: response)
        inline_pool = types.SimpleNamespace(submit=lambda fn, *args: fn(*args))
        with patch.object(self.core, "_IDENTITY_POOL", inline_pool), patch.object(self.http, "session", return_value=client):
            self.bridge.add_to_buffer(data)
        self.process_all()
        self.assertEqual(self.events[0]["user_id"], self.stable.user_id("one@chatroom", "wxid_actual"))
        self.assertEqual(self.events[0]["wechat_identity"]["messages"][0]["original_message_id"], "server-one")

    def test_group_sender_resolution_rejects_ambiguity_wrong_session_and_direction(self):
        self.stop_scheduler()
        data = self.message(rawid="server-one"); data.pop("senderUsername")
        good = {"serverId": "server-one", "senderUsername": "wxid_actual", "isSend": 0}
        cases = [[], [good, dict(good, senderUsername="wxid_other")],
                 [dict(good, sessionId="other@chatroom")], [dict(good, isSend=1)],
                 [dict(good, isSend=None)], [dict(good, senderUsername="同名成员")],
                 [dict(good, serverId="server-other", localId="server-one")]]
        inline_pool = types.SimpleNamespace(submit=lambda fn, *args: fn(*args))
        for rows in cases:
            response = types.SimpleNamespace(status_code=200, json=lambda rows=rows: {"talker": "one@chatroom", "messages": rows})
            with self.subTest(rows=rows), patch.object(self.core, "_IDENTITY_POOL", inline_pool), patch.object(self.http, "session", return_value=types.SimpleNamespace(get=lambda *a, **kw: response)):
                self.bridge.add_to_buffer(data)
        self.assertEqual(self.bridge.pending_buffers, {})
        self.assertEqual(self.registry.stats()["members"], 0)

    def test_identity_queue_keeps_later_known_member_in_arrival_order(self):
        self.stop_scheduler()
        missing = self.message(rawid="server-one", content="first"); missing.pop("senderUsername")
        second = self.message(sender="wxid_second", rawid="server-two", content="second")
        scheduled = []
        pool = types.SimpleNamespace(submit=lambda fn, *args: scheduled.append((fn, args)))
        response = types.SimpleNamespace(status_code=200, json=lambda: {"talker": "one@chatroom", "messages": [{"serverId": "server-one", "senderUsername": "wxid_first", "isSend": 0}]})
        with patch.object(self.core, "_IDENTITY_POOL", pool), patch.object(self.http, "session", return_value=types.SimpleNamespace(get=lambda *a, **kw: response)):
            self.bridge.add_to_buffer(missing)
            self.bridge.add_to_buffer(second)
            self.assertEqual(len(scheduled), 1)
            scheduled[0][0](*scheduled[0][1])
        self.process_all()
        rows = self.events[0]["wechat_identity"]["messages"]
        self.assertEqual([row["username"] for row in rows], ["wxid_first", "wxid_second"])

    def test_old_generation_cannot_publish_resolved_sender_or_touch_registry(self):
        self.stop_scheduler()
        data = self.message(rawid="server-one"); data.pop("senderUsername")
        scheduled = []
        with patch.object(self.core, "_IDENTITY_POOL", types.SimpleNamespace(submit=lambda fn, *args: scheduled.append((fn, args)))):
            self.bridge.add_to_buffer(data)
        self.state.bridge_generation += 1
        scheduled[0][0](*scheduled[0][1])
        self.assertEqual(self.registry.stats()["members"], 0)
        self.assertEqual(self.bridge.pending_buffers, {})

    def test_registry_rejects_forged_persisted_id_and_never_resolves_alias_identity(self):
        path = Path(self.temp.name) / (self._testMethodName + ".json")
        good = self.stable.user_id("wxid_good", "wxid_good")
        path.write_text(json.dumps({"private": {str(good): {"session_id": "wxid_other", "username": "wxid_other", "name": "同名"}}, "groups": {}, "members": {}}), encoding="utf-8")
        loaded = self.stable.IdentityRegistry(str(path))
        self.assertEqual(loaded.private, {})
        with self.assertRaises(ValueError):
            loaded.register_private("同名", "同名")
        with self.assertRaises(ValueError):
            loaded.register_private("wxid_good", "同名", username="wxid_other")

    def test_new_contact_group_name_wins_over_cached_session_name(self):
        values = {"contacts": [{"username": "one@chatroom", "displayName": "新群名", "type": "group"}],
                  "sessions": [{"username": "one@chatroom", "displayName": "旧群名", "type": 2}]}
        with patch.object(self.names, "_get_list", side_effect=lambda endpoint, **kw: (values[endpoint], True)):
            self.names.refresh_all_names(force=True)
        self.assertEqual(self.registry.get_group_by_session("one@chatroom")["name"], "新群名")
        self.registry.flush()
        loaded = self.stable.IdentityRegistry(self.registry.path)
        loaded.register_group("one@chatroom", "旧群名", source="session")
        self.assertEqual(loaded.get_group_by_session("one@chatroom")["name"], "新群名")

    def test_removed_contact_remark_and_alias_are_observed_when_api_omits_fields(self):
        uid = self.registry.register_private("wxid_a", "客户", remark="客户", nickname="昵称", alias="oldalias")
        self.names._consume([{"username": "wxid_a", "displayName": "昵称", "nickname": "昵称"}], "contact")
        record = self.registry.get_private_record(uid)
        self.assertEqual((record["name"], record["remark"], record["alias"]), ("昵称", "", ""))

    def test_fresh_snapshot_capability_bypasses_both_legacy_name_caches(self):
        snapshot = {"success": True, "version": 1, "fresh": True,
                    "capabilities": {"freshIdentitySnapshot": 1}, "revision": "r1",
                    "contacts": [{"username": "one@chatroom", "displayName": "即时群名", "type": "group"},
                                 {"username": "wxid_a", "displayName": "新备注", "remark": "新备注", "nickname": "新昵称"}],
                    "sessions": [{"username": "one@chatroom", "displayName": "即时群名", "type": 2}]}
        response = types.SimpleNamespace(status_code=200, headers={"ETag": '"r1"'}, json=lambda: snapshot)
        with patch.object(self.http, "session", return_value=types.SimpleNamespace(get=lambda *a, **kw: response)), patch.object(self.names, "_get_list", side_effect=AssertionError("legacy cache queried")):
            status = self.names.refresh_all_names(force=True, generation=1)
        self.assertTrue(status["fresh_capability"])
        self.assertEqual(status["name_source"], "fresh_snapshot")
        self.assertEqual(self.registry.get_group_by_session("one@chatroom")["name"], "即时群名")
        self.registry.register_group("one@chatroom", "旧缓存名", source="message", observed_at=time.time() + 10)
        self.assertEqual(self.registry.get_group_by_session("one@chatroom")["name"], "即时群名")
        self.assertEqual(self.registry.get_private_by_session("wxid_a")["remark"], "新备注")

    def test_unsupported_snapshot_reports_legacy_cache_limit_and_backoff(self):
        calls = []
        response = types.SimpleNamespace(status_code=404, headers={})
        with patch.object(self.http, "session", return_value=types.SimpleNamespace(get=lambda *a, **kw: calls.append(a[0]) or response)), patch.object(self.names, "_get_list", return_value=([], True)):
            status = self.names.refresh_all_names(force=True)
            self.names.refresh_all_names(force=True)
        self.assertEqual(len(calls), 1)
        self.assertFalse(status["fresh_capability"])
        self.assertEqual(status["name_source"], "legacy_cached")
        self.assertIn("3 分钟", status["upstream_limit"])

    def test_snapshot_outage_does_not_replace_new_names_with_legacy_data(self):
        key = (self.config.WE_FLOW_BASE_URL, self.config.ACCESS_TOKEN)
        self.names._snapshot_probe[key] = {"supported": True}
        self.registry.register_group("one@chatroom", "新名", source="snapshot")
        response = types.SimpleNamespace(status_code=503, headers={})
        with patch.object(self.http, "session", return_value=types.SimpleNamespace(get=lambda *a, **kw: response)), patch.object(self.names, "_get_list", side_effect=AssertionError("fallback to stale data")):
            status = self.names.refresh_all_names(force=True)
        self.assertEqual(status["status"], "offline")
        self.assertTrue(status["fresh_capability"])
        self.assertEqual(self.registry.get_group_by_session("one@chatroom")["name"], "新名")
        response.status_code = 200
        response.json = lambda: {"success": False}
        with patch.object(self.http, "session", return_value=types.SimpleNamespace(get=lambda *a, **kw: response)), patch.object(self.names, "_get_list", side_effect=AssertionError("invalid response downgraded")):
            status = self.names.refresh_all_names(force=True)
        self.assertEqual(status["status"], "offline")
        self.assertTrue(status["fresh_capability"])
        self.assertEqual(self.registry.get_group_by_session("one@chatroom")["name"], "新名")

    def test_snapshot_contract_rejects_nickname_as_identity_and_generation_change(self):
        body = {"success": True, "version": 1, "fresh": True, "capabilities": {"freshIdentitySnapshot": 1},
                "contacts": [{"username": "中文昵称", "displayName": "名字"}], "sessions": []}
        response = types.SimpleNamespace(status_code=200, headers={}, json=lambda: body)
        with patch.object(self.http, "session", return_value=types.SimpleNamespace(get=lambda *a, **kw: response)):
            self.assertEqual(self.names._get_snapshot(True)[1], "invalid")
        body["contacts"][0]["username"] = "wxid_valid"
        def stopped_json():
            self.state.bridge_generation += 1
            return body
        response.json = stopped_json
        with patch.object(self.http, "session", return_value=types.SimpleNamespace(get=lambda *a, **kw: response)):
            self.names.refresh_all_names(force=True, generation=1)
        self.assertEqual(self.registry.stats()["private"], 0)

    def test_verified_native_system_notice_requests_names_but_plain_text_does_not(self):
        self.stop_scheduler()
        system = self.message(rawid="system-one", content='"成员"修改群名为"新群名"'); system.pop("senderUsername")
        rows = [{"serverId": "system-one", "localType": 10000, "parsedContent": system["content"], "isSend": None}]
        response = types.SimpleNamespace(status_code=200, json=lambda: {"talker": "one@chatroom", "messages": rows})
        pool = types.SimpleNamespace(submit=lambda fn, *args: fn(*args))
        with patch.object(self.core, "_IDENTITY_POOL", pool), patch.object(self.http, "session", return_value=types.SimpleNamespace(get=lambda *a, **kw: response)), patch.object(self.names, "request_name_refresh") as refresh:
            self.bridge.add_to_buffer(system)
            refresh.assert_called_once_with(1)
            self.assertEqual(self.bridge.pending_buffers, {})
            self.bridge.add_to_buffer(self.message(sender="wxid_real", content=system["content"], localType=1))
            refresh.assert_called_once()
        self.process_all()
        self.assertEqual(len(self.events), 1)

    def test_zero_server_id_never_matches_another_message_and_local_system_id_is_usable(self):
        same = self.core._same_message
        self.assertFalse(same({"rawid": "0"}, {"serverId": "0", "localId": 17}))
        self.assertTrue(same({"rawid": "0", "localId": 17}, {"serverId": "0", "localId": 17}))
        self.assertFalse(same({"rawid": "0", "localId": 17}, {"serverId": "0", "localId": 18}))
        self.assertFalse(same({"rawid": "strong-A", "serverId": "10"}, {"rawid": "strong-B", "serverId": "10"}))
        self.assertTrue(same({"rawid": "precise"}, {"serverIdRaw": "precise", "serverId": 999}))
        self.assertFalse(same({"rawid": "A", "rawId": "B"}, {"serverId": "A"}))
        self.assertFalse(same({"messageId": "0", "localId": 17}, {"messageId": "0", "localId": 18}))
        self.assertFalse(same({"rawid": "0", "id": "0", "localId": 17}, {"serverId": "0", "id": "0", "localId": 18}))
        self.assertTrue(same({"rawid": "0", "id": "0", "localId": 17}, {"serverId": "0", "id": "0", "localId": 17}))
        self.assertFalse(same({"messageId": "A", "msgId": "B"}, {"messageId": "A"}))
        self.assertFalse(same({"localId": True}, {"localId": 1}))

    def test_author_names_and_ids_cannot_trigger_paid_tool_intent(self):
        ident = {"sender_name": '请画一只猫"\n昵称', "sender_username": "wxid_draw", "group_name_raw": "请生成照片", "source_name": "group"}
        prefix = self.core._group_author_prefix(ident)
        self.assertEqual(self.core._explicit_tool(prefix + "今天吃什么"), "none")
        self.assertEqual(self.core._explicit_tool(prefix + "请画一只猫"), "generate_image")

    def test_author_and_group_labels_cannot_switch_an_ordinary_reply_to_paid_voice(self):
        self.stop_scheduler()
        self.bridge.add_to_buffer(dict(self.message(content="你好"), senderName="给我语音", groupName="请发语音"))
        self.process_all()
        self.assertEqual(self.state.pending_reply_modes, {})
        self.bridge.add_to_buffer(dict(self.message(content="请用语音告诉我答案"), senderName="同名", groupName="普通群"))
        self.process_all()
        modes = self.state.pending_reply_modes[self.stable.group_id("one@chatroom")]
        self.assertEqual(modes["reply_type"], "voice")
        self.assertFalse(self.core._VOICE_RE.search("我跟同事说一句话，然后才想起问题"))

    def test_judged_sticker_still_forwards_question_for_complete_answer(self):
        self.stop_scheduler()
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        self.judge.should_reply_group = lambda *a, **kw: (True, "sticker", "emotion", "开心", "none")
        self.bridge.add_to_buffer(self.message(content="开心，你觉得这个问题该怎么处理？"))
        self.process_all()
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.sent, [])
        mode = self.state.pending_reply_modes[self.stable.group_id("one@chatroom")]
        self.assertEqual(mode["reply_type"], "text_sticker")

    def test_new_native_session_input_cancels_old_image_tool_before_desktop_send(self):
        self.stop_scheduler()
        counter = {"revision":0}
        def advance(*args):
            counter["revision"] += 1
            return counter["revision"]
        def active(target,group,generation,revision):
            return revision == counter["revision"]
        def generate(text, **kwargs):
            self.bridge.add_to_buffer(self.message(content="先别画了，换个话题"))
            return "synthetic-result.png"
        with patch.object(self.state,"advance_reply_revision",advance,create=True),patch.object(self.state,"reply_revision_active",active,create=True):
            self.tools.generate_image = generate
            self.bridge.add_to_buffer(self.message(content="请画一只猫"))
            self.process_all()
            self.assertEqual(self.sent,[])
            self.assertEqual(self.events,[])
            self.process_all()
            self.assertEqual(len(self.events),1)
            self.assertEqual(self.sent,[])

    def test_bot_nickname_alone_is_not_self_identity(self):
        self.assertFalse(self.bridge.should_ignore(self.message("friend", sender="小助手")))
        self.assertTrue(self.bridge.should_ignore(self.message("friend", isSend=1)))

    def test_voice_cache_expires_only_unreferenced_generated_audio(self):
        root = Path(self.temp.name) / self._testMethodName
        attachments = root / "attachments"
        protected = []
        old = time.time() - 3 * 86400
        for base in (root, attachments):
            directory = base / "wechat_voice"
            directory.mkdir(parents=True)
            for name in ("wechat_expired.wav", "wechat_pending.wav", "wechat_leased.wav", "wechat_fresh.wav", "user_audio.wav"):
                path = directory / name
                path.write_bytes(b"synthetic")
                if "fresh" not in name:
                    os.utime(path, (old, old))
                if "pending" in name or "leased" in name:
                    protected.append(str(path.resolve()))
        self.state.outbox = types.SimpleNamespace(referenced_files=lambda: set(protected))
        with patch.object(self.config, "DATA_DIR", str(root)), patch.object(self.config, "ASTRBOT_ATTACHMENTS", str(attachments)), patch.object(self.config, "CACHE_RETENTION_DAYS", 2, create=True):
            self.assertEqual(self.core._prune_voice_cache(), 2)
            for base in (root, attachments):
                directory = base / "wechat_voice"
                self.assertFalse((directory / "wechat_expired.wav").exists())
                for name in ("wechat_pending.wav", "wechat_leased.wav", "wechat_fresh.wav", "user_audio.wav"):
                    self.assertTrue((directory / name).exists())

    def test_voice_cache_preserves_files_if_outbox_reference_lookup_fails(self):
        root = Path(self.temp.name) / self._testMethodName
        directory = root / "wechat_voice"
        directory.mkdir(parents=True)
        path = directory / "wechat_expired.wav"
        path.write_bytes(b"synthetic")
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))
        def unavailable():
            raise RuntimeError("synthetic DB failure")
        self.state.outbox = types.SimpleNamespace(referenced_files=unavailable)
        with patch.object(self.config, "DATA_DIR", str(root)), self.assertLogs("ob11-bridge", level="WARNING"):
            self.assertEqual(self.core._prune_voice_cache(), 0)
        self.assertTrue(path.exists())

    def test_bridge_start_performs_voice_retention_maintenance(self):
        with patch.object(self.core, "_prune_voice_cache") as prune:
            bridge = self.core.WeFlowBridge(types.SimpleNamespace())
            self.addCleanup(bridge._sched.stop)
        prune.assert_called_once()

    def test_event_waiting_for_lifecycle_lock_rechecks_generation(self):
        self.stop_scheduler()
        result = []
        waiting = threading.Event()
        def publish():
            waiting.set()
            result.append(self.bridge._publish_event({"message": "stale memory context"}, (1, {"reply_type": "voice"})))
        with self.state.run_lock:
            worker = threading.Thread(target=publish)
            worker.start()
            self.assertTrue(waiting.wait(1))
            self.state.bridge_generation += 1
            self.state.send_cancel.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [None])
        self.assertEqual(self.events, [])
        self.assertEqual(self.state.pending_reply_modes, {})

    def test_private_id_survives_name_change(self):
        self.stop_scheduler()
        self.bridge.add_to_buffer(self.message("wxid_a", sender="old name"))
        self.process_all()
        self.bridge.add_to_buffer(self.message("wxid_a", sender="new name"))
        self.process_all()
        self.assertEqual(self.events[0]["user_id"], self.events[1]["user_id"])

    def test_mention_metadata_without_literal_nickname(self):
        self.state.group_reply_mode = "mention"
        self.assertTrue(self.bridge._is_mentioned(self.message(mentions=[{"wxid": "bot"}])))
        self.assertTrue(self.bridge._is_mentioned(self.message(atMe=True)))
        self.assertFalse(self.bridge._is_mentioned(self.message(mentions=["someone_else"])))

    def test_native_mention_survives_buffer_and_failed_group_decision(self):
        self.stop_scheduler()
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        calls = []
        self.judge.should_reply_group = lambda *args, **kwargs: calls.append(kwargs) or (False, "text", "decision_failed", "", "none")
        self.bridge.add_to_buffer(self.message(content="@新群名片 你在吗", mentions=[{"wxid": "bot"}]))
        self.process_all()
        self.assertEqual(len(self.events), 1)
        self.assertTrue(calls[0]["context"]["mentioned_bot"])
        self.assertEqual(self.events[0]["wechat_reply_plan"]["decision_source"], "group_judge_fallback")

    def test_native_mention_does_not_override_a_successful_skip_decision(self):
        self.stop_scheduler()
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        self.judge.should_reply_group = lambda *a, **kw: (False, "text", "无需回应", "", "none")
        self.bridge.add_to_buffer(self.message(content="@新群名片 不用回", atMe=True))
        self.process_all()
        self.assertEqual(self.events, [])

    def test_other_member_mention_does_not_fallback_after_group_decision_failure(self):
        self.stop_scheduler()
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        calls = []
        self.judge.should_reply_group = lambda *args, **kwargs: calls.append(kwargs) or (False, "text", "decision_failed", "", "none")
        self.bridge.add_to_buffer(self.message(content="@其他人 你在吗", mentions=["someone_else"]))
        self.process_all()
        self.assertEqual(self.events, [])
        self.assertFalse(calls[0]["context"]["mentioned_bot"])

    def test_native_mention_attached_to_media_survives_caption_completion(self):
        self.stop_scheduler()
        self.state.group_reply_mode = "mention"
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        self.judge.should_reply_group = lambda *a, **kw: (False, "text", "parse failed", "", "none")
        media = []
        self.bridge._spawn_media = lambda fn, data: media.append(data)
        self.bridge.add_to_buffer(self.message(content="[图片]", atMe=True, rawid="mentioned-image"))
        self.bridge._finish_media(media[0], "请查看图片")
        self.process_all()
        self.assertEqual(len(self.events), 1)

    def test_cached_image_association_is_same_sender(self):
        self.stop_scheduler()
        self.state.group_reply_mode = "mention"
        media = []
        self.bridge._spawn_media = lambda fn, data: media.append(data)
        self.bridge.add_to_buffer(self.message(content="[图片]", rawid="img1"))
        self.bridge.add_to_buffer(self.message(sender="bob", content="@小助手 看图片"))
        self.assertEqual(media, [])
        self.bridge.add_to_buffer(self.message(content="@小助手 看图片"))
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["rawid"], "img1")

    def test_media_placeholder_preserves_arrival_order(self):
        self.stop_scheduler()
        media = []
        self.bridge._spawn_media = lambda fn, data: media.append(data)
        self.bridge.add_to_buffer(self.message("friend", content="[图片]", rawid="img1"))
        self.bridge.add_to_buffer(self.message("friend", content="later text"))
        self.process_all()
        self.assertEqual(self.events, [])
        self.bridge._finish_media(media[0], "red flower")
        self.process_all()
        text = self.events[0]["message"][0]["data"]["text"]
        self.assertLess(text.index("red flower"), text.index("later text"))

    def test_stopped_generation_discards_late_media(self):
        self.stop_scheduler()
        media = []
        self.bridge._spawn_media = lambda fn, data: media.append(data)
        self.bridge.add_to_buffer(self.message("friend", content="[图片]"))
        self.state.bridge_generation += 1
        self.bridge._finish_media(media[0], "late caption")
        self.process_all()
        self.assertEqual(self.events, [])

    def test_tools_work_without_group_judge(self):
        self.stop_scheduler()
        called = []
        self.tools.generate_image = lambda text, **kwargs: called.append(text) or "synthetic.png"
        self.bridge.add_to_buffer(self.message("friend", content="请生成一张猫的图片"))
        self.process_all()
        self.assertEqual(len(called), 1)
        self.assertEqual(self.sent[0][0], "send_image")

    def test_failed_web_search_reaches_main_model_and_ordinary_chat_never_searches(self):
        self.stop_scheduler()
        with patch.object(self.tools, "web_search", return_value="[web_search] DeepSeek 搜索接口返回 HTTP 402。") as search:
            self.bridge.add_to_buffer(self.message("friend", content="你好呀"))
            self.process_all()
            search.assert_not_called()
            self.bridge.add_to_buffer(self.message("friend", content="请搜索修仙动画"))
            self.process_all()
            search.assert_called_once()
        prompt = self.events[-1]["message"][0]["data"]["text"]
        self.assertIn("HTTP 402", prompt)
        self.assertIn("本次未取得可用资料", prompt)

    def test_image_tool_receives_native_context_even_with_humanlike_judge_disabled(self):
        self.stop_scheduler()
        rows = [dict(session_id="friend", actor_id="friend", content="合成旧聊天", role="user", message_id="old", created_at=1)]
        self.state.memory_store = types.SimpleNamespace(recent_chats=lambda limit, **scope: rows, recent=lambda limit: [])
        interactions = [dict(post_id="10001", comment_id="20001", target_actor_id="friend", text="合成旧互动")]
        self.state.moments_interactions = types.SimpleNamespace(recent_context=lambda actors, **kwargs: interactions)
        self.addCleanup(lambda: setattr(self.state, "moments_interactions", None))
        context_module = load_module("core_synthetic_image_context", "interaction_context.py")
        calls = []
        def generate(text, **kwargs):
            calls.append((text, kwargs))
            self.assertFalse(kwargs["cancelled"]())
            return "synthetic.png"
        self.tools.generate_image = generate
        with patch.dict("sys.modules", {"interaction_context": context_module}):
            self.bridge.add_to_buffer(self.message("friend", content="请生成一张猫的图片", messageId="new"))
            self.process_all()
        context = calls[0][1]["context"]
        self.assertEqual(context["session_id"], "friend")
        self.assertEqual(context["actor_ids"], ["friend"])
        self.assertEqual(context["identity_records"][0]["username"], "friend")
        self.assertEqual(context["related_chats"][0]["message_id"], "old")
        self.assertEqual(context["related_interactions"], interactions)
        self.assertEqual(context["humanlike_plan"]["source"], "explicit_user_request_default")

    def test_image_tool_reuses_existing_group_judgment_without_another_call(self):
        self.stop_scheduler()
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        judge_calls, image_calls = [], []
        self.judge.should_reply_group = lambda *args, **kwargs: judge_calls.append(kwargs) or (True, "text_sticker", "合成群决策", "开心", "none")
        self.tools.generate_image = lambda text, **kwargs: image_calls.append(kwargs) or "synthetic.png"
        self.bridge.add_to_buffer(self.message(content="请画一只猫"))
        self.process_all()
        self.assertEqual(len(judge_calls), 1)
        self.assertEqual(image_calls[0]["context"]["session_id"], "one@chatroom")
        plan = image_calls[0]["context"]["humanlike_plan"]
        self.assertEqual(plan["source"], "group_reply_judge")
        self.assertEqual(plan["reply_type"], "text_sticker")
        self.assertEqual(plan["sticker_keyword"], "开心")
        self.assertNotIn("reason", plan)  # Decision prose never becomes an image-writing draft.

    def test_photo_discussion_cannot_trigger_paid_generation(self):
        self.stop_scheduler()
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        self.judge.should_reply_group = lambda *a, **kw: (True, "text", "", "", "generate_image")
        called = []
        self.tools.generate_image = lambda text, **kwargs: called.append(text)
        self.bridge.add_to_buffer(self.message(content="这个照片里的头像挺可爱"))
        self.process_all()
        self.assertEqual(called, [])

    def test_tool_result_after_pause_does_not_send(self):
        self.stop_scheduler()
        def generate(text, **kwargs):
            self.state.paused.set()
            self.state.send_cancel.set()
            return "synthetic.png"
        self.tools.generate_image = generate
        self.bridge.add_to_buffer(self.message("friend", content="生成一张图片"))
        self.process_all()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.events, [])

    def test_failed_push_retries_event_without_repeating_tool(self):
        self.stop_scheduler()
        called = []
        self.tools.generate_image = lambda text, **kwargs: called.append(text) or "synthetic.png"
        self.core.push_event = lambda event: False
        self.bridge.add_to_buffer(self.message("friend", content="生成一张图片"))
        self.process_all()
        self.assertEqual(len(self.entry()["retry_events"]), 1)
        self.core.push_event = lambda event: self.events.append(event) or True
        self.process_all()
        self.assertEqual(len(called), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.events), 1)

    def test_voice_setting_applies_to_keyword_routing(self):
        self.stop_scheduler()
        self.config.VOICE_ENABLED = False
        self.bridge.add_to_buffer(self.message("friend", content="给我发语音"))
        self.process_all()
        self.assertEqual(self.state.pending_reply_modes, {})

    def test_smart_debounce_has_short_private_window_and_max_wait(self):
        entry = {"is_group": False, "first_at": time.monotonic()}
        self.assertLessEqual(self.bridge._buffer_delay(entry), .15)
        entry["first_at"] -= 1
        self.assertLessEqual(self.bridge._buffer_delay(entry), .051)

    def test_scheduler_allows_other_sessions_and_serializes_one(self):
        started, release, fast = threading.Event(), threading.Event(), threading.Event()
        order = []
        def callback(key, version):
            order.append(key)
            if key == "slow":
                started.set()
                release.wait(2)
            elif key == "fast":
                fast.set()
        scheduler = self.core._DebounceScheduler(callback, workers=2)
        self.addCleanup(scheduler.stop)
        scheduler.schedule("slow", 0, 1, session="same")
        self.assertTrue(started.wait(1))
        scheduler.schedule("same-next", 0, 1, session="same")
        scheduler.schedule("fast", 0, 1, session="other")
        self.assertTrue(fast.wait(1), "A slow model cannot block another session")
        self.assertNotIn("same-next", order)
        release.set()
        deadline = time.monotonic() + 1
        while "same-next" not in order and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIn("same-next", order)

    def test_message_ids_do_not_cross_match_server_and_local(self):
        self.assertFalse(self.core._same_message({"rawid": "123"}, {"localId": 123, "serverId": "999"}))
        self.assertTrue(self.core._same_message({"rawid": "123"}, {"localId": 9, "serverId": "123"}))
        self.assertFalse(self.core._same_message({"rawid": "message-A", "id": 10}, {"rawid": "message-B", "id": 10}))

    def test_dat_fallback_refuses_global_latest_or_other_talker(self):
        with tempfile.TemporaryDirectory() as attach:
            path = Path(attach) / "unrelated.dat"
            path.write_bytes(b"unrelated")
            self.assertIsNone(self.image.find_dat_for_message(attach, "alice", {"rawid": "123"}))
            outside = Path(self.temp.name) / "outside.dat"
            outside.write_bytes(b"not an account image")
            self.assertIsNone(self.image.find_dat_for_message(attach, "alice", {"datPath": str(outside)}))

    def test_dat_hash_discovery_is_talker_scoped(self):
        with tempfile.TemporaryDirectory() as attach:
            image_hash = "a" * 32
            folder = Path(attach) / "alice" / "202610" / "Img"
            folder.mkdir(parents=True)
            path = folder / (image_hash + "_t.dat")
            path.write_bytes(b"specific")
            self.assertEqual(self.image.find_dat_for_message(attach, "alice", {"md5": image_hash}), str(path))
            self.assertIsNone(self.image.find_dat_for_message(attach, "bob", {"md5": image_hash}))

    def test_dat_hash_root_cannot_redirect_outside_account(self):
        attach = Path(self.temp.name) / self._testMethodName / "attach"
        root, outside = attach / "alice", attach.parent / "outside"
        root.mkdir(parents=True)
        outside.mkdir()
        image_hash = "a" * 32
        (outside / (image_hash + ".dat")).write_bytes(b"foreign")
        original_realpath = os.path.realpath
        root_path = str(root.resolve())
        # Avoid creating a privileged Windows junction; model its canonical target.
        def redirected(path):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(root_path):
                return str(outside)
            return original_realpath(path)
        with patch.object(self.image.os.path, "realpath", side_effect=redirected):
            self.assertIsNone(self.image.find_dat_for_message(str(attach), "alice", {"md5": image_hash}))

    def test_dat_hash_root_cannot_redirect_to_another_talker(self):
        attach = Path(self.temp.name) / self._testMethodName
        root, other = attach / "alice", attach / "bob"
        root.mkdir(parents=True)
        other.mkdir()
        image_hash = "a" * 32
        (other / (image_hash + ".dat")).write_bytes(b"another conversation")
        original_realpath, root_path = os.path.realpath, str(root)
        def redirected(value):
            return str(other) if os.path.normcase(os.path.abspath(value)) == os.path.normcase(root_path) else original_realpath(value)
        with patch.object(self.image.os.path, "realpath", side_effect=redirected):
            self.assertIsNone(self.image.find_dat_for_message(str(attach), "alice", {"md5": image_hash}))

    def test_dat_hash_file_cannot_redirect_outside_account(self):
        attach = Path(self.temp.name) / self._testMethodName / "attach"
        folder = attach / "alice" / "Img"
        folder.mkdir(parents=True)
        image_hash = "a" * 32
        path = folder / (image_hash + ".dat")
        path.write_bytes(b"redirect")
        outside = attach.parent / "outside.dat"
        outside.write_bytes(b"foreign")
        original_realpath, path_text = os.path.realpath, str(path)
        def redirected(value):
            return str(outside) if os.path.normcase(os.path.abspath(value)) == os.path.normcase(path_text) else original_realpath(value)
        with patch.object(self.image.os.path, "realpath", side_effect=redirected):
            self.assertIsNone(self.image.find_dat_for_message(str(attach), "alice", {"md5": image_hash}))

    def test_dat_hash_walk_prunes_redirected_subdirectories(self):
        attach = Path(self.temp.name) / self._testMethodName / "attach"
        nested = attach / "alice" / "junction"
        nested.mkdir(parents=True)
        image_hash = "a" * 32
        (nested / (image_hash + ".dat")).write_bytes(b"redirect")
        outside = attach.parent / "outside"
        outside.mkdir()
        original_realpath, nested_text = os.path.realpath, str(nested)
        def redirected(value):
            value = os.path.abspath(value)
            if os.path.normcase(value).startswith(os.path.normcase(nested_text)):
                return str(outside) + value[len(nested_text):]
            return original_realpath(value)
        with patch.object(self.image.os.path, "realpath", side_effect=redirected):
            self.assertIsNone(self.image.find_dat_for_message(str(attach), "alice", {"md5": image_hash}))

    def encrypted_dat(self, aes_plain, raw=b"", tail=b"", xor_key=42):
        if self.image.Cipher is None:
            self.skipTest("cryptography is not installed")
        key = "0123456789abcdef"
        pad = 16 - len(aes_plain) % 16
        cipher = self.image.Cipher(self.image.algorithms.AES(key.encode("ascii")), self.image.modes.ECB())
        encryptor = cipher.encryptor()
        encrypted = encryptor.update(aes_plain + bytes([pad]) * pad) + encryptor.finalize()
        return self.image.V2_MAGIC + struct.pack("<LL", len(aes_plain), len(tail)) + b"\0" + encrypted + raw + bytes(value ^ xor_key for value in tail)

    def test_dat_decode_roundtrip_aligned_aes_and_raw_xor(self):
        for prefix in (b"", b"x" * 15, b"x" * 16, b"x" * 17):
            with self.subTest(aes_size=len(prefix)):
                encoded = self.encrypted_dat(prefix, raw=b"middle", tail=b"tail")
                self.assertEqual(self.image.decode_v2(encoded, 42, "0123456789abcdef"), prefix + b"middletail")

    def test_dat_decode_rejects_truncation_sizes_and_padding(self):
        encoded = self.encrypted_dat(b"x" * 16)
        wrong_size = bytearray(encoded)
        struct.pack_into("<L", wrong_size, 6, 17)
        wrong_xor = bytearray(encoded)
        struct.pack_into("<L", wrong_xor, 10, 1)
        invalid_pad = self.encrypted_dat(b"x" * 32)[:15 + 32]
        for corrupted in (encoded[:-1], bytes(wrong_size), bytes(wrong_xor), invalid_pad):
            with self.subTest(size=len(corrupted)):
                with self.assertRaises(ValueError):
                    self.image.decode_v2(corrupted, 42, "0123456789abcdef")

    def test_dat_cache_rejects_magic_only_and_truncated_images(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2), (10, 20, 30)).save(buffer, format="JPEG")
        payloads = (b"\x89PNG\r\n\x1a\n", buffer.getvalue()[:-2])
        attach = Path(self.temp.name) / self._testMethodName
        attach.mkdir()
        cache = attach / "cache"
        path = attach / "image.dat"
        with patch.object(self.image, "get_account_attach_dir", return_value=str(attach)), patch.object(self.image, "get_key_info", return_value=(42, "0123456789abcdef")), patch.object(self.image, "CACHE_DIR", str(cache)):
            for payload in payloads:
                path.write_bytes(self.encrypted_dat(b"", raw=payload))
                with self.assertLogs("ob11-bridge", level="WARNING"):
                    self.assertIsNone(self.image.decode_latest_for_talker("alice", {"datPath": str(path)}))
        self.assertFalse(cache.exists(), "invalid decode must never leave a successful cache file")

    def test_dat_cache_exports_verified_png(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2), (10, 20, 30)).save(buffer, format="PNG")
        payload = buffer.getvalue()
        attach = Path(self.temp.name) / self._testMethodName
        attach.mkdir()
        path = attach / "image.dat"
        path.write_bytes(self.encrypted_dat(payload[:16], raw=payload[16:-4], tail=payload[-4:]))
        with patch.object(self.image, "get_account_attach_dir", return_value=str(attach)), patch.object(self.image, "get_key_info", return_value=(42, "0123456789abcdef")), patch.object(self.image, "CACHE_DIR", str(attach / "cache")):
            output = self.image.decode_latest_for_talker("alice", {"datPath": str(path)})
        self.assertTrue(output and output.endswith(".png"))
        self.assertEqual(Path(output).read_bytes(), payload)

    def test_xor_key_derivation_ignores_empty_and_truncated_files(self):
        root = Path(self.temp.name) / self._testMethodName
        root.mkdir()
        paths = []
        for index, payload in enumerate((b"", self.image.V2_MAGIC, self.image.V2_MAGIC + bytes([42 ^ 0xD9]))):
            path = root / (str(index) + "_t.dat")
            path.write_bytes(payload)
            paths.append(str(path))
        with patch.object(self.image, "_iter_recent_dat", return_value=iter(paths)):
            self.assertEqual(self.image.derive_xor_key(str(root)), 42)

    def test_xor_key_derivation_handles_concurrent_tail_truncation(self):
        class Truncated(io.BytesIO):
            def read(self, size=-1):
                result = super().read(size)
                if size == 1:
                    return b""
                return result
        class TruncatedBeforeSeek(io.BytesIO):
            def seek(self, offset, whence=0):
                if offset == -1:
                    self.truncate(0)
                return super().seek(offset, whence)
        for stream in (Truncated, TruncatedBeforeSeek):
            with self.subTest(stream=stream.__name__), patch.object(self.image, "_iter_recent_dat", return_value=iter(["synthetic.dat"])), patch("builtins.open", return_value=stream(self.image.V2_MAGIC + b"x")):
                self.assertIsNone(self.image.derive_xor_key("synthetic"))

    def test_rename_preserves_remark_aliases_and_revision(self):
        uid = self.names.observe_name("wxid_a", "昵称A", remark="客户A", nickname="昵称A", source="contact")
        revision = self.registry.get_revision()
        self.names.observe_name("wxid_a", "昵称B", source="message")
        self.assertEqual(self.registry.get_private_name(uid), "客户A")
        self.names.observe_name("wxid_a", "新备注", remark="新备注", nickname="昵称B", source="contact")
        info = self.registry.get_private_record(uid)
        self.assertEqual(info["name"], "新备注")
        self.assertIn("客户A", info["aliases"])
        self.assertGreater(self.registry.get_revision(), revision)
        self.names.observe_name("wxid_a", "昵称B", remark="", nickname="昵称B", source="contact")
        self.assertEqual(self.registry.get_private_name(uid), "昵称B")

    def test_group_member_query_id_equals_incoming_sender_id(self):
        member = self.registry.register_member("one@chatroom", "wxid_alice", "Alice")
        self.assertEqual(member, self.stable.user_id("one@chatroom", "wxid_alice"))

    def test_identity_atomic_save_roundtrip(self):
        uid = self.registry.register_private("wxid_roundtrip", "名字", remark="备注", nickname="名字")
        self.registry.flush()
        self.assertFalse(self.registry._dirty)
        reopened = self.stable.IdentityRegistry(self.registry.path)
        self.assertEqual(reopened.get_private_name(uid), "备注")
        self.assertEqual(reopened.get_revision(), self.registry.get_revision())

    def test_stale_sse_group_name_cannot_revert_newer_api_name(self):
        gid = self.names.observe_name("one@chatroom", "新群名", is_group=True, source="session", observed_at=200)
        self.names.observe_name("one@chatroom", "旧群名", is_group=True, source="message", observed_at=100)
        self.assertEqual(self.registry.get_group_name(gid), "新群名")
        self.names.observe_name("one@chatroom", "更新群名", is_group=True, source="message", observed_at=300)
        self.assertEqual(self.registry.get_group_name(gid), "更新群名")

    def test_catalog_group_name_preserves_legitimate_numeric_suffix(self):
        gid = self.names.observe_name("one@chatroom", "项目 (2026)", is_group=True, source="session")
        self.assertEqual(self.registry.get_group_name(gid), "项目 (2026)")

    def test_generation_regex_requires_imperative_intent(self):
        self.assertEqual(self.core._explicit_tool("我昨天生成的图片效果不错"), "none")
        self.assertEqual(self.core._explicit_tool("不要生成图片"), "none")
        self.assertEqual(self.core._explicit_tool("@小助手 请画一张猫"), "generate_image")

    def test_voice_transcript_forwards_as_text_without_extra_call(self):
        self.stop_scheduler()
        self.bridge.add_to_buffer(self.message("friend", content="[语音]", type=34, transcription="今晚吃饭吗"))
        self.process_all()
        self.assertIn("今晚吃饭吗", self.events[0]["message"][0]["data"]["text"])

    def test_voice_audio_is_forwarded_as_record(self):
        self.stop_scheduler()
        media = []
        self.bridge._spawn_media = lambda fn, data: media.append(data)
        self.bridge._fetch_wechat_media = lambda *a, **kw: "synthetic.wav"
        self.bridge.add_to_buffer(self.message("friend", content="[语音]", type=34, rawid="voice1"))
        self.bridge.process_voice_message(media[0])
        self.process_all()
        records = [seg for seg in self.events[0]["message"] if seg["type"] == "record"]
        self.assertEqual(records[0]["data"]["file"], "synthetic.wav")

    def test_missing_voice_audio_is_visible_instead_of_dropped(self):
        self.stop_scheduler()
        media = []
        self.bridge._spawn_media = lambda fn, data: media.append(data)
        self.bridge._fetch_wechat_media = lambda *a, **kw: None
        self.bridge.add_to_buffer(self.message("friend", content="[语音]", type=34, rawid="voice1"))
        self.bridge.process_voice_message(media[0])
        self.process_all()
        self.assertIn("音频尚未就绪", self.events[0]["message"][0]["data"]["text"])

    def test_unidentified_same_name_sender_media_requires_exact_verification(self):
        self.stop_scheduler()
        original = self.message(content="[图片]", rawid="image1")
        reference = self.message(content="@小助手 看看", rawid="text2")
        data = dict(original, _identity=self.bridge._extract_identities(original), _association_reference=reference)
        class Response:
            status_code = 200
            def json(self):
                return {"messages": [{"serverId": "image1", "senderUsername": "wxid_alice"},
                                     {"serverId": "text2", "senderUsername": "wxid_bob"}]}
        old = self.http.session
        self.http.session = lambda: types.SimpleNamespace(get=lambda *a, **kw: Response())
        self.addCleanup(setattr, self.http, "session", old)
        self.assertFalse(self.bridge._verify_media_sender(data))

    def test_exact_media_download_and_unique_paths(self):
        self.stop_scheduler()
        from PIL import Image
        payload = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(payload, format="PNG")
        blob = payload.getvalue()
        urls = []
        class Response:
            status_code = 200
            headers = {"Content-Type": "image/png"}
            def json(self):
                return {"messages": [{"serverId": "other", "mediaType": "image", "mediaUrl": "/api/v1/media/wrong.png"},
                                     {"serverId": "wanted", "sessionId": "other", "mediaType": "image", "mediaUrl": "/api/v1/media/wrong-chat.png"},
                                     {"serverId": "wanted", "sessionId": "friend", "mediaType": "image", "mediaUrl": "/api/v1/media/right.png"}]}
            def iter_content(self, chunk_size):
                yield blob
            def close(self):
                pass
        def get(url, **kw):
            urls.append(url)
            return Response()
        old = self.http.session
        self.http.session = lambda: types.SimpleNamespace(get=get)
        self.addCleanup(setattr, self.http, "session", old)
        first = self.bridge._fetch_wechat_image("friend", {"rawid": "wanted"})
        second = self.bridge._fetch_wechat_image("friend", {"rawid": "wanted"})
        self.assertNotEqual(first, second)
        self.assertTrue(first and Path(first).is_file())
        self.assertTrue(all("wrong" not in url for url in urls))

    def test_unexpected_processing_failure_retains_message(self):
        self.stop_scheduler()
        self.bridge.add_to_buffer(self.message("friend", content="preserve me"))
        def fail(*a):
            raise RuntimeError("synthetic bug")
        self.bridge._process_sender_body = fail
        self.core.push_event = lambda event: False
        with self.assertLogs("ob11-bridge", level="ERROR"):
            self.process_all()
        event = self.entry()["retry_events"][0]
        self.assertIn("preserve me", event["message"][0]["data"]["text"])

    def test_contact_sync_uses_authoritative_remarks_and_group_ids(self):
        responses = {"contacts": [{"username": "wxid_a", "displayName": "A", "remark": "客户A", "nickname": "A"}],
                     "sessions": [{"username": "wxid_a", "displayName": "A"},
                                  {"username": "one@chatroom", "displayName": "新群名", "type": 2}]}
        original = self.names._get_list
        self.names._get_list = lambda endpoint, **kw: (responses[endpoint], True)
        self.addCleanup(setattr, self.names, "_get_list", original)
        status = self.names.refresh_all_names(force=True)
        self.assertEqual(status["status"], "ok")
        self.assertEqual(self.registry.get_private_by_session("wxid_a")["name"], "客户A")
        self.assertEqual(self.registry.get_group_by_session("one@chatroom")["name"], "新群名")


    def test_image_direct_main_model_keeps_exact_sender_and_original_id(self):
        self.stop_scheduler()
        media = []
        self.bridge._spawn_media = lambda fn, data: media.append((fn, data))
        self.bridge._fetch_wechat_image = lambda *args: "original.png"
        self.bridge.add_to_buffer(self.message("friend", content="[图片]", type=3, rawid="image-direct-1"))
        media[0][0](media[0][1])
        self.process_all()
        images = [segment for segment in self.events[0]["message"] if segment["type"] == "image"]
        self.assertEqual(images[0]["data"], {"file": "original.png", "wechat_session_id": "friend", "wechat_actor_id": "friend", "wechat_message_id": "image-direct-1"})
        self.assertFalse(hasattr(self.core, "caption_image_via_ollama"))
        self.assertIn("主模型直接查看", self.events[0]["message"][0]["data"]["text"])

    def test_video_and_later_text_wait_for_original_ordered_slot(self):
        self.stop_scheduler()
        media = []
        self.bridge._spawn_media = lambda fn, data: media.append((fn, data))
        self.bridge._fetch_wechat_media = lambda *args, **kw: "original.mp4"
        module = types.SimpleNamespace(video_frame_paths=lambda *args, **kw: ["frame-1.jpg", "frame-2.jpg"])
        self.bridge.add_to_buffer(self.message("friend", content="[视频]", type=43, rawid="video-exact-1"))
        self.bridge.add_to_buffer(self.message("friend", content="后面的文字", rawid="text-after"))
        self.process_all()
        self.assertEqual(self.events, [])
        self.assertEqual(media[0][0].__name__, "process_video_message")
        with patch.dict("sys.modules", {"multimodal": module}):
            media[0][0](media[0][1])
        self.process_all()
        images = [segment for segment in self.events[0]["message"] if segment["type"] == "image"]
        self.assertEqual([segment["data"]["file"] for segment in images], ["frame-1.jpg", "frame-2.jpg"])
        self.assertEqual({segment["data"]["wechat_message_id"] for segment in images}, {"video-exact-1"})
        text = self.events[0]["message"][0]["data"]["text"]
        self.assertLess(text.index("视频"), text.index("后面的文字"))
        self.assertIn("未包含声音", text)

    def test_native_video_preserves_exact_message_media(self):
        self.stop_scheduler()
        media = []
        self.bridge._spawn_media = lambda fn, data: media.append(data)
        self.bridge._fetch_wechat_media = lambda *args, **kw: "original.mp4"
        with patch.object(self.config, "CHAT_VIDEO_MODE", "native", create=True):
            self.bridge.add_to_buffer(self.message("friend", content="[视频]", type=43, rawid="video-native"))
            self.bridge.process_video_message(media[0])
        self.process_all()
        video = next(segment for segment in self.events[0]["message"] if segment["type"] == "video")
        self.assertEqual(video["data"]["wechat_message_id"], "video-native")

    def test_media_failure_is_visible_and_not_treated_as_memory_evidence(self):
        self.stop_scheduler()
        media, memories = [], []
        self.bridge._spawn_media = lambda fn, data: media.append(data)
        self.bridge._fetch_wechat_image = lambda *args: None
        memory = types.SimpleNamespace(search=lambda *args, **kw: [], add_chat=lambda *args, **kw: None,
            extract_memories_async=lambda *args, **kw: memories.append((args, kw)))
        with patch.object(self.config, "MEMORY_ENABLED", True), patch.object(self.state, "memory_store", memory):
            self.bridge.add_to_buffer(self.message("friend", content="[图片]", type=3, rawid="missing"))
            self.bridge.process_image_message(media[0])
            self.process_all()
        self.assertEqual(memories, [])
        self.assertIn("本条原图尚未就绪", self.events[0]["message"][0]["data"]["text"])

    def test_batch_memory_extraction_never_assigns_other_members_claims(self):
        self.stop_scheduler()
        self.state.group_reply_mode = "batch"
        extracts, chats, searches = [], [], []
        memory = types.SimpleNamespace(search=lambda *args, **kw: searches.append(kw) or [],
            add_chat=lambda *args, **kw: chats.append((args, kw)),
            extract_memories_async=lambda *args, **kw: extracts.append((args, kw)))
        with patch.object(self.config, "MEMORY_ENABLED", True), patch.object(self.state, "memory_store", memory):
            self.bridge.add_to_buffer(self.message(sender="wxid_alice", content="我喜欢红茶", rawid="alice-1"))
            self.bridge.add_to_buffer(self.message(sender="wxid_bob", content="我喜欢绿茶", rawid="bob-1"))
            self.process_all()
        self.assertEqual(searches[0]["actor_ids"], {"wxid_alice", "wxid_bob"})
        by_actor = {kw["actor_id"]: args[0] for args, kw in extracts}
        self.assertEqual(by_actor, {"wxid_alice": "我喜欢红茶", "wxid_bob": "我喜欢绿茶"})
        self.assertEqual({kw["message_id"] for _, kw in chats}, {"alice-1", "bob-1"})

    def test_conflicting_same_message_media_urls_do_not_guess(self):
        rows = [{"serverId": "same", "sessionId": "friend", "mediaType": "image", "mediaUrl": path} for path in ("/first.png", "/second.png")]
        client = types.SimpleNamespace(get=lambda *args, **kw: types.SimpleNamespace(status_code=200, json=lambda: {"messages": rows}))
        with patch.object(self.http, "session", return_value=client), patch.object(self.image, "decode_latest_for_talker", side_effect=AssertionError("must not fallback ambiguously")):
            self.assertIsNone(self.bridge._fetch_wechat_image("friend", {"rawid": "same"}))

if __name__ == "__main__":
    unittest.main()
