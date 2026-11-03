"""Independent integration review: no network, paid model or desktop access."""
import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from test_core import CoreTests as CoreFixture
from test_protocol_validation import ProtocolValidationTests as ProtocolFixture, text

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
sys.path.insert(0, str(SOURCE))

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class CrossMediaTests(unittest.TestCase):
    def test_real_moments_sampler_accepts_current_event_style_cancel_token(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            cfg = types.SimpleNamespace(DATA_DIR=folder, MEDIA_MAX_MB=30)
            encoder = Mock(return_value={"type": "image_url", "image_url": {"url": "data:image/png;base64,test"}})
            with patch.dict(sys.modules, {"config": cfg, "media_util": types.SimpleNamespace(), "model_client": types.SimpleNamespace(image_part=encoder)}):
                sampler = load("cross_real_multimodal", "multimodal.py")
                with patch.dict(sys.modules, {"multimodal": sampler}):
                    moments = load("cross_real_moments_media", "moments_media.py")
                    class CancelCheck:
                        def is_set(self):
                            return False
                    def download(url, path, cancelled, *, deadline):
                        Image.new("RGB", (8, 8), "red").save(path, format="PNG")
                        return os.path.getsize(path)
                    with patch.object(moments, "_download", side_effect=download):
                        parts = moments.post_parts({"identity_verified": True, "has_media": True,
                            "media": [{"url": "https://mmbiz.qpic.cn/test", "type": "image"}]}, cancelled=CancelCheck())
                    self.assertEqual(sum(p["type"] == "image_url" for p in parts), 1)
                    encoder.assert_called_once()
                    self.assertEqual(list((Path(folder) / "moments_media").iterdir()), [])

    def test_event_cancellation_stops_before_any_encoder_call(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "image.png"
            source.write_bytes(b"test file")
            event = threading.Event(); event.set()
            encoder = Mock()
            with patch.dict(sys.modules, {"config": types.SimpleNamespace(DATA_DIR=folder),
                    "model_client": types.SimpleNamespace(image_part=encoder)}):
                sampler = load("cross_event_multimodal", "multimodal.py")
                with self.assertRaises(sampler.MediaError):
                    sampler.media_parts(source, cancelled=event)
            encoder.assert_not_called()

class CrossInboundLeaseTests(CoreFixture):
    def test_later_batch_actor_while_previous_batch_is_processing_owns_event_user_id(self):
        self.stop_scheduler()
        original = self.bridge._process_sender_body
        added = []
        def process(key, entry, messages):
            if not added:
                added.append(True)
                self.bridge.add_to_buffer(self.message(sender="wxid_bob", content="只有 Bob 的后续发言", rawid="bob-later"))
            return original(key, entry, messages)
        self.bridge._process_sender_body = process
        self.bridge.add_to_buffer(self.message(sender="wxid_alice", content="只有 Alice 的第一轮", rawid="alice-first"))
        self.process_all()
        self.process_all()
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.events[1]["wechat_identity"]["messages"][0]["username"], "wxid_bob")
        self.assertEqual(self.events[1]["user_id"], self.stable.user_id("one@chatroom", "wxid_bob"),
            "第二轮仅 Bob 发言，不能沿用第一轮 Alice 的 OneBot user_id")

    def test_ready_buffered_image_remains_referenced_before_outbox_acceptance(self):
        self.stop_scheduler()
        jobs = []
        self.bridge._spawn_media = lambda fn, data: jobs.append(data)
        media = load("cross_buffered_media_util", "media_util.py")
        directory = Path(self.temp.name) / "pending-buffer-cache"
        directory.mkdir(exist_ok=True)
        paths = [directory / f"{index}.png" for index in range(3)]
        for index, path in enumerate(paths):
            path.write_bytes(b"cached image")
            os.utime(path, (100 + index, 100 + index))
        with patch.dict(sys.modules, {"media_util": media}), patch.object(self.state, "bridge_instance", self.bridge, create=True):
            self.bridge.add_to_buffer(self.message("friend", content="[图片]", type=3, rawid="image-one"))
            self.bridge._finish_media(jobs[0], "真实原图", segments=[{"type": "image", "data": {"file": str(paths[0])}}])
            self.assertTrue(self.entry()["messages"][0]["ready"])
            self.core._prune_dir(str(directory), keep=2)
            self.assertTrue(paths[0].exists(), "缓冲中仍被主模型引用的原图不能被计数清理删除")
            self.assertFalse(paths[1].exists())

    def test_main_receives_original_image_without_caption_model_request(self):
        self.stop_scheduler()
        jobs = []
        self.bridge._spawn_media = lambda fn, data: jobs.append(data)
        self.bridge._fetch_wechat_image = lambda *args: "original-exact.png"
        external_model = Mock(side_effect=AssertionError("No caption or main call belongs in the bridge"))
        with patch.dict(sys.modules, {"model_client": types.SimpleNamespace(request=external_model),
                "image_caption": types.SimpleNamespace(caption=external_model)}):
            self.bridge.add_to_buffer(self.message("friend", content="[图片]", type=3, rawid="image-main-only"))
            self.bridge.process_image_message(jobs[0])
            self.process_all()
        external_model.assert_not_called()
        images = [s for s in self.events[0]["message"] if s["type"] == "image"]
        self.assertEqual(images[0]["data"]["file"], "original-exact.png")
        self.assertEqual(images[0]["data"]["wechat_actor_id"], "friend")
        self.assertEqual(images[0]["data"]["wechat_message_id"], "image-main-only")

class CrossAssistantLogTests(ProtocolFixture):
    def _memory(self):
        self.state.memory_store = Mock()
        self.state.identity_registry.get_private_record = lambda _: {"name": "同名好友", "session_id": "wxid_real_friend"}
        return self.state.memory_store

    def test_partial_text_log_contains_confirmed_head_without_unsent_tail(self):
        memory = self._memory()
        self.state.dispatch_send.side_effect = [True, False]
        with self.assertRaises(self.protocol.SendFailure):
            self.send([text("confirmed head"), {"type": "reply", "data": {"id": "source"}}, text("unsent tail")])
        memory.add_chat.assert_called_once()
        self.assertEqual(memory.add_chat.call_args.args, ("confirmed head", "wxid_real_friend"))
        self.assertEqual(memory.add_chat.call_args.kwargs["role"], "assistant")
        memory.extract_memories_async.assert_not_called()

    def test_partial_voice_log_contains_confirmed_transcript_without_unsent_tail(self):
        memory = self._memory()
        self.tts.synthesize_voice.return_value = self.audio()
        self.state.dispatch_send.side_effect = [True, False]
        with self.assertRaises(self.protocol.SendFailure):
            self.send([{"type": "record", "data": {"text": "confirmed voice"}}, text("unsent tail")])
        memory.add_chat.assert_called_once()
        self.assertEqual(memory.add_chat.call_args.args, ("confirmed voice", "wxid_real_friend"))
        self.assertEqual(memory.add_chat.call_args.kwargs["role"], "assistant")
        memory.extract_memories_async.assert_not_called()

    def test_partial_bubbles_log_exact_confirmed_head_from_current_thread_result(self):
        memory = self._memory()
        self.state.sender_instance._last_send = {"status": "accepted_by_ui", "result_sequence": 10}
        def partial(*args, **kwargs):
            self.state.sender_instance._last_send = {"status": "partial", "result_sequence": 11,
                "sent_parts": 1, "attempted_parts": 2, "confirmed_texts": ["实际已发头部"]}
            return False
        self.state.dispatch_send.side_effect = partial
        with self.assertRaises(self.protocol.SendFailure):
            self.send([text("发送器拟真转换前的全文，含未发尾部")])
        memory.add_chat.assert_called_once()
        self.assertEqual(memory.add_chat.call_args.args, ("实际已发头部", "wxid_real_friend"))
        memory.extract_memories_async.assert_not_called()

    def test_partial_bubbles_stale_result_cannot_create_assistant_log(self):
        memory = self._memory()
        self.state.sender_instance._last_send = {"status": "partial", "result_sequence": 10,
            "sent_parts": 1, "attempted_parts": 2, "confirmed_texts": ["另一次请求的旧文字"]}
        self.state.dispatch_send.side_effect = lambda *args, **kwargs: False
        self.assertFalse(self.send([text("本条请求未发送")]))
        memory.add_chat.assert_not_called()
        memory.extract_memories_async.assert_not_called()

class CrossMemoryQueueTests(unittest.TestCase):
    def test_real_background_extraction_cannot_reopen_or_write_after_close(self):
        with tempfile.TemporaryDirectory() as folder:
            entered, release = threading.Event(), threading.Event()
            db_path = str(Path(folder) / "memory.db")
            cfg = types.SimpleNamespace(MEMORY_TOP_K=3)
            checks = []
            def request(*args, **kwargs):
                checks.append(kwargs["cancelled"])
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release synthetic model")
                return json.dumps({"memories": [{"content": "喜欢红茶", "evidence": "喜欢红茶"}]})
            with patch.dict(sys.modules, {"config": cfg, "model_client": types.SimpleNamespace(request=request)}):
                module = load("cross_close_memory", "memory_store.py")
                store = module.MemoryStore(db_path)
                try:
                    self.assertTrue(store.extract_memories_async("喜欢红茶", session_id="wxid_alice", message_id="one"))
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(store.extract_memories_async("喜欢绿茶", session_id="wxid_bob", message_id="two"))
                    store.close()
                    self.assertEqual(store._extract_q.qsize(), 0)
                    self.assertTrue(checks[0]())
                    self.assertFalse(store.extract_memories_async("喜欢白茶", session_id="wxid_alice", message_id="three"))
                    release.set()
                    store._extract_thread.join(2)
                    self.assertFalse(store._extract_thread.is_alive())
                    self.assertIsNone(store._conn)
                    with closing(sqlite3.connect(db_path)) as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM memories").fetchone()[0], 0)
                finally:
                    release.set()
                    store.close()

    def test_real_send_queue_close_drains_and_running_job_observes_original_cancel(self):
        module = load("cross_close_send_queue", "send_queue.py")
        queue = module.SendQueue(4)
        entered, cancelled = threading.Event(), threading.Event()
        called = []
        def running():
            entered.set()
            cancelled.wait(2)
            return not cancelled.is_set()
        try:
            first = queue.submit(1, running, cancelled.is_set)
            self.assertTrue(entered.wait(1))
            second = queue.submit(2, lambda: called.append("stale send") or True, cancelled.is_set)
            cancelled.set()  # main.stop invalidates the captured token before closing.
            queue.close()
            self.assertFalse(first.result(2))
            self.assertFalse(second.result(2))
            self.assertEqual(called, [])
            queue.worker.join(2)
            self.assertFalse(queue.worker.is_alive())
            third = queue.submit(3, lambda: self.fail("closed queue must not send"), cancelled.is_set)
            with self.assertRaises(RuntimeError):
                third.result(1)
        finally:
            cancelled.set()
            queue.close()

def load_tests(loader, tests, pattern):
    # Imported fixtures have their own full suites; run only independent cases.
    suite = unittest.TestSuite()
    for cls in (CrossMediaTests, CrossInboundLeaseTests, CrossAssistantLogTests, CrossMemoryQueueTests):
        for name in cls.__dict__:
            if name.startswith("test_"):
                suite.addTest(cls(name))
    return suite

if __name__ == "__main__":
    unittest.main()
