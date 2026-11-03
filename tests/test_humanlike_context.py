"""Offline identity/context and private-priority regressions; no HTTP/desktop."""
from concurrent.futures import Future
import importlib.util
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SOURCE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InteractionContextTests(unittest.TestCase):
    def setUp(self):
        self.config = types.SimpleNamespace(BOT_WXID="wxid_bot", DECISION_CONTEXT_RECENT_CHATS=20)
        self.modules = patch.dict(sys.modules, {"config": self.config})
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.context = load("synthetic_interaction_context", "interaction_context.py")

    def test_fake_image_prompt_profile_preserves_current_native_identity(self):
        with patch.dict(sys.modules, {"model_client": types.SimpleNamespace(request=Mock())}):
            context = json.loads(self.context.compact_context({"actor_id": "wxid_friend", "task": "image_prompt"}, "image_prompt"))
        self.assertEqual(context["actor_id"], "wxid_friend")
        self.assertEqual(context["context_compaction"]["context_tokens"], 8192)

    def test_main_writing_fallback_uses_main_context_and_keeps_task_boundary(self):
        self.config.MAIN_LLM_CONTEXT_TOKENS = 256000
        with patch.dict(sys.modules, {"model_client": types.SimpleNamespace(request=Mock())}):
            context = json.loads(self.context.compact_context({"actor_id": "wxid_friend", "task": "image_prompt"}, "main"))
        self.assertEqual(context["task"], "image_prompt")
        self.assertEqual(context["actor_id"], "wxid_friend")
        self.assertEqual(context["context_compaction"]["context_tokens"], 256000)

    def test_private_assistant_and_scoped_group_statement_do_not_import_other_people(self):
        def chat(actor, session, text, role="user"):
            return dict(actor_id=actor, session_id=session, content=text, role=role, created_at=1)
        store = Mock()
        store.recent_chats.side_effect = [
            [chat("wxid_bot", "wxid_friend", "机器人本人的回复", "assistant"),
             chat("wxid_friend", "wxid_friend", "这位朋友的话"), chat("wxid_other", "wxid_other", "其他人的秘密")],
            [chat("wxid_friend", "room@chatroom", "此人的群聊发言"),
             chat("wxid_other", "room@chatroom", "他人的群聊发言"), chat("wxid_friend", "wxid_other", "错误私聊目标")]]
        rows = self.context.scoped_chats(store, {"wxid_friend"})
        self.assertEqual({row["content"] for row in rows}, {"机器人本人的回复", "这位朋友的话", "此人的群聊发言"})
        self.assertTrue(all(call.kwargs for call in store.recent_chats.call_args_list))
        self.assertEqual(rows[-1]["provenance"], "related_group_actor_statement")

    def test_local_compaction_drops_whole_history_records_and_keeps_current_ids(self):
        values = dict(context_tokens=3072, max_tokens=768, prompt="真实身份必须保留")
        model = types.SimpleNamespace(profile_settings=lambda role: values)
        payload = {"post_id": "10001", "comment_id": "20002", "current_message": "新的完整消息",
            "related_chats": [{"actor_id": "wxid_friend", "content": "旧的完整历史" * 300}]}
        with patch.dict(sys.modules, {"model_client": model}):
            result = json.loads(self.context.compact_context(payload, "moments_decision"))
        self.assertEqual(result["related_chats"], [])
        self.assertEqual(result["post_id"], "10001")
        self.assertEqual(result["comment_id"], "20002")
        self.assertEqual(result["current_message"], payload["current_message"])


class PrivatePriorityTests(unittest.TestCase):
    def setUp(self):
        stable = load("priority_synthetic_stable", "stable_id.py")
        stable.IdentityRegistry = lambda: types.SimpleNamespace(flush=lambda: None)
        modules = patch.dict(sys.modules, {"stable_id": stable, "config": types.SimpleNamespace(MOMENTS_PRIVATE_QUIET_SECONDS=60)})
        modules.start(); self.addCleanup(modules.stop)
        self.state = load("synthetic_private_priority_state", "state.py")
        self.state.bridge_generation = 7
        self.clock = patch.object(self.state.time, "monotonic", return_value=200)
        self.clock.start(); self.addCleanup(self.clock.stop)

    def request(self, voice_state, *, final_done=True):
        head, tail = Future(), Future()
        head.set_result(True)
        if final_done:
            tail.set_result(True)
        record = {"request_id": "test", "target_id": 1, "is_group": False, "generation": 7,
            "revision": 1, "created": 100, "closed": True,
            "segments": {0: {"future": head, "delivery": {"voice_pending": True, "voice_state": "collecting"}},
                         1: {"future": tail, "delivery": {"voice_pending": False, "voice_state": voice_state}}}}
        self.state._reply_requests["test"] = record
        self.state._reply_revisions[(1, False, 7)] = 1
        return record

    def test_historical_collecting_head_does_not_block_after_voice_terminal_state(self):
        for terminal in ("sent", "failed", "queue_rejected", "cancelled", "unconfirmed", "failed_text_preserved", "text_fallback_sent", "style_rejected"):
            with self.subTest(terminal=terminal):
                self.request(terminal)
                self.assertFalse(self.state.private_priority_snapshot()["private_inflight"])

    def test_text_voice_synthesis_or_queue_still_has_private_priority(self):
        for ongoing in ("synthesizing", "queued"):
            with self.subTest(ongoing=ongoing):
                self.request(ongoing)
                self.assertTrue(self.state.private_priority_snapshot()["private_inflight"])

    def test_unfinished_final_send_and_private_processing_both_block_social(self):
        self.request("sent", final_done=False)
        self.assertTrue(self.state.social_should_yield())
        self.state._reply_requests.clear()
        self.state.private_processing("wxid_friend", True)
        self.assertTrue(self.state.social_should_yield())
        self.state.private_processing("wxid_friend", False)
        self.assertFalse(self.state.social_should_yield())

    def test_private_quiet_window_expires_without_inflight_work(self):
        self.state._private_activity_at = 150
        self.assertTrue(self.state.social_should_yield())
        self.state._private_activity_at = 140
        self.assertFalse(self.state.social_should_yield())


if __name__ == "__main__":
    unittest.main()
