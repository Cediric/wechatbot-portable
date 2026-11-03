"""Proactive video sharing with synthetic accounts, SQLite and a fake send queue."""
from concurrent.futures import Future
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch


BRIDGE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
sys.path.insert(0, str(BRIDGE))
from stable_id import group_id, user_id

ACCOUNT = "wxid_demo_bot"
FRIEND = "wxid_demo_alice"
GROUP = "12345@chatroom"
URL = "https://www.bilibili.com/video/BV17x411w7KC"
OTHER = "https://www.bilibili.com/video/BV17x411w7KA"


class Queue:
    def __init__(self):
        self.jobs = 0
        self.before = lambda: None

    def submit(self, _id, work, cancelled):
        self.jobs += 1
        self.before()
        outcome = Future()
        outcome.set_result(False if cancelled() else work())
        return outcome


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = types.SimpleNamespace(DATA_DIR=self.temp.name, BOT_WXID=ACCOUNT, BILI_SHARE_ENABLED=True,
                                         BILI_SHARE_DAILY_LIMIT=3, BILI_SHARE_INTERVAL_MINUTES=240)
        registry = Mock()
        registry.get_private_record.side_effect = lambda uid: (
            {"session_id": FRIEND, "username": FRIEND, "name": "同名联系人"}
            if uid == user_id(FRIEND, FRIEND) else {})
        registry.get_group_record.side_effect = lambda gid: (
            {"session_id": GROUP, "name": "同名联系人"} if gid == group_id(GROUP) else {})
        self.state = types.SimpleNamespace(identity_registry=registry, memory_store=Mock(), dispatch_send=Mock(return_value=True),
                                           bridge_generation=19, running=True, paused=threading.Event(), send_cancel=threading.Event(),
                                           social_should_yield=Mock(return_value=False), send_queue=Queue())
        self.state.memory_store.recent_chats.return_value = []
        self.video = types.SimpleNamespace(public_feed=Mock(return_value=[(URL, "公开标题")]), public_search=Mock(return_value=[]),
                                          find_video=lambda url: types.SimpleNamespace(url=url),
                                          inspect_video=Mock(side_effect=self.inspect))
        self.answer = '{"targets":[1]}'
        self.interests = types.SimpleNamespace(identity=Mock(return_value="角色摘要"), preferred_topic=Mock(return_value=""),
                                              choose=Mock(side_effect=lambda choices: choices[0]),
                                              record_reaction=Mock(return_value=True))
        self.model = types.SimpleNamespace(persona_prompt=lambda: "当前人设", image_part=Mock(side_effect=lambda p: {"type":"image_url", "data":Path(p).read_bytes().hex()}),
                                           request=Mock(side_effect=self.request))
        self.active = Mock(return_value=True)
        self.day = "2026-11-02"
        moments = types.SimpleNamespace(MomentsInteractions=types.SimpleNamespace(_active=self.active),
                                        _day=lambda _ts: self.day)
        modules = patch.dict(sys.modules, {"config":self.cfg, "state":self.state, "bili_video":self.video,
                                           "model_client":self.model, "moments_interactions":moments,
                                           "character_interests":self.interests})
        modules.start()
        self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location("bili_sharing_test", BRIDGE/"bili_sharing.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.obj = self.module.BiliSharing(19)
        self.caption = "镜头里那只猫盯住了自己的尾巴。"
        self.frames = []

    def inspect(self, ref, cancelled=None):
        self.assertFalse(cancelled())
        self.frames = [Path(self.temp.name)/"one.jpg", Path(self.temp.name)/"two.jpg"]
        for frame in self.frames:
            frame.write_bytes(b"test image")
        return "公开画面资料", self.frames

    def request(self, profile, messages, **kw):
        return json.dumps({"caption":self.caption, "topic":"猫咪", "reaction":"like"}, ensure_ascii=False) if profile == "main" else self.answer

    def run_once(self):
        return self.obj._run(self.obj._revision)

    def test_no_recipient_or_mismatched_identity_still_watches_without_sharing(self):
        self.obj.note_reply("wxid_unregistered")
        self.obj.note_reply(ACCOUNT)
        self.assertIn("没有", self.run_once())
        self.interests.record_reaction.assert_called_once()
        self.obj.note_reply(FRIEND)
        self.state.identity_registry.get_private_record.side_effect = lambda _: {"name":"同名联系人", "session_id":"wxid_other"}
        self.video.public_feed.return_value = [(OTHER, "第二个")]
        self.assertIn("没有", self.run_once())
        self.assertEqual([c.args[0] for c in self.model.request.call_args_list], ["main","main"])
        self.state.dispatch_send.assert_not_called()

    def test_only_confirmed_current_account_seven_day_assistant_rows_considered(self):
        self.caption = "B站 [Live] C++ & 音乐🎵 "*12 + "收尾。"
        now = time.time()
        def row(sid, **extra):
            return dict({"session_id":sid, "created_at":now-600, "role":"assistant", "actor_id":ACCOUNT,
                         "content":"极其私密的原话"}, **extra)
        self.state.memory_store.recent_chats.return_value = [
            row(GROUP), row(FRIEND, role="user"), row(FRIEND, actor_id="wxid_other"),
            row(FRIEND, created_at=now-7*86400-2), row(FRIEND, created_at=now+100)]
        self.assertEqual(self.run_once(), "已确认分享")
        for call in self.state.memory_store.recent_chats.call_args_list:
            self.assertIn(call.kwargs, [{"actor_id":ACCOUNT}, {"session_id":GROUP}])
        self.assertEqual(self.state.send_queue.jobs, 1)
        args, kw = self.state.dispatch_send.call_args
        self.assertEqual(args, ("send_text", "同名联系人", self.caption+"\n"+URL))
        self.assertEqual((kw["is_group"], kw["target_id"], kw["generation"], kw["proactive"]),
                         (True, group_id(GROUP), 19, True))
        self.assertFalse(kw["proactive_cancel"].is_set())
        self.assertTrue(kw["stream_sentence"])
        self.assertEqual([c.args[0] for c in self.model.request.call_args_list], ["main","moments_decision"])
        main, decision = [c.args[1] for c in self.model.request.call_args_list]
        self.assertEqual(sum(p["type"] == "image_url" for p in main[0]["content"]), 2)
        prompts = [c.kwargs["prompt_override"] for c in self.model.request.call_args_list]
        self.assertTrue(all(prompt.startswith("当前人设") for prompt in prompts))
        self.assertIn("分享正文", prompts[0])
        self.assertIn("只决定", prompts[1])
        for prompt in [main, decision]:
            literal = json.dumps(prompt, ensure_ascii=False)
            for private in ["极其私密的原话", GROUP, "同名联系人", FRIEND]:
                self.assertNotIn(private, literal)
        self.assertTrue(all(not path.exists() for path in self.frames))

    def test_private_registry_delivery_and_dedup_across_restart_or_accounts(self):
        self.obj.note_reply(FRIEND)
        self.assertEqual(self.run_once(), "已确认分享")
        self.assertFalse(self.state.dispatch_send.call_args.kwargs["is_group"])
        self.assertEqual(self.state.dispatch_send.call_args.kwargs["target_id"], user_id(FRIEND, FRIEND))
        self.obj = self.module.BiliSharing(19)
        self.obj.note_reply(FRIEND)
        self.assertIn("没有新的", self.run_once())
        self.assertEqual(self.video.inspect_video.call_count, 1)
        self.cfg.BOT_WXID = "wxid_other_bot"
        self.assertIn("没有可核验", self.run_once())
        self.obj.note_reply(GROUP)
        self.video.public_feed.return_value = [(OTHER, "第二个")]
        self.assertEqual(self.run_once(), "已确认分享")
        self.assertEqual(self.state.dispatch_send.call_count, 2)

    def test_matching_recipient_interest_is_decision_only_and_scoped_to_confirmed_bot(self):
        self.obj.note_reply(FRIEND)
        self.obj.note_reply(GROUP)
        stamp = time.time() - 300
        def recent(_limit, *, session_id=None, actor_id=None):
            if actor_id:
                return []
            actor = FRIEND if session_id == FRIEND else "wxid_member"
            return [{"session_id": session_id, "role": "assistant", "created_at": stamp, "actor_id": ACCOUNT},
                    {"session_id": session_id, "role": "user", "created_at": stamp - 10, "actor_id": actor,
                     "content": ("我喜欢猫咪视频。" if session_id == FRIEND else "我喜欢科幻电影。") + "无关隐私不要复述。"}]
        self.state.memory_store.recent_chats.side_effect = recent
        def select(profile, messages, **kwargs):
            if profile == "main":
                self.assertNotIn("猫咪视频", str(messages))
                self.assertNotIn("科幻电影", str(messages))
                return self.request(profile, messages, **kwargs)
            payload = json.loads(messages[0]["content"])
            self.assertNotIn("无关隐私", str(payload))
            choice = next(item for item in payload["候选"] if item.get("兴趣线索") == "我喜欢猫咪视频")
            return json.dumps({"targets":[choice["编号"]]})
        self.model.request.side_effect = select
        self.assertEqual(self.run_once(), "已确认分享")
        self.assertEqual(self.state.dispatch_send.call_args.kwargs["target_id"], user_id(FRIEND,FRIEND))

    def test_uncertain_submit_consumes_daily_allowance_and_next_day_resets(self):
        self.cfg.BILI_SHARE_DAILY_LIMIT = 1
        self.obj.note_reply(FRIEND)
        self.state.dispatch_send.return_value = False
        self.assertIn("未确认", self.run_once())
        self.video.public_feed.return_value = [(OTHER, "第二条")]
        self.assertIn("上限", self.run_once())
        self.assertEqual(self.video.public_feed.call_count, 2)
        self.assertEqual(self.interests.record_reaction.call_count, 2)
        self.day = "2026-11-03"
        self.video.public_feed.return_value = [(URL[:-1]+"B", "第三条")]
        self.state.dispatch_send.return_value = True
        self.assertEqual(self.run_once(), "已确认分享")
        self.assertEqual(self.state.dispatch_send.call_count, 2)

    def test_model_cannot_choose_foreign_target_or_write_outbound_words(self):
        self.obj.note_reply(FRIEND)
        for answer in ['{"targets":0}', '{"targets":[true]}', '{"targets":[2]}', '{"targets":[1],"text":"任意正文"}',
                       '{"targets":["wxid_stranger"]}', '这不是 JSON']:
            with self.subTest(answer=answer):
                self.answer = answer
                self.assertIsNone(self.obj._choose([(FRIEND, 2)], self.caption, lambda: False))
        self.assertEqual(self.run_once(), "本次不分享")
        self.state.dispatch_send.assert_not_called()
        self.assertEqual(self.state.send_queue.jobs, 0)

    def test_no_frames_or_invalid_main_text_skips_and_reclaims_images(self):
        self.obj.note_reply(GROUP)
        self.video.inspect_video.side_effect = None
        self.video.inspect_video.return_value = ("只有标题，没有可用画面", [])
        self.assertIn("没有取得", self.run_once())
        self.model.request.assert_not_called()
        self.interests.record_reaction.assert_not_called()
        self.video.inspect_video.side_effect = self.inspect
        self.video.public_feed.return_value = [(OTHER, "第二条")]
        self.caption = "请点击https://elsewhere.example"
        self.assertIn("没有取得", self.run_once())
        self.assertEqual(self.model.request.call_count, 1)
        self.assertTrue(all(not path.exists() for path in self.frames))
        self.state.dispatch_send.assert_not_called()

    def test_bad_topic_keeps_valid_caption_and_never_exposes_internal_fields(self):
        self.obj.note_reply(FRIEND)
        def review(profile, messages, **kw):
            if profile == "main":
                return json.dumps({"caption":"看它追着尾巴转。", "topic":"猫、追逐", "reaction":"like",
                                   "extra":"内部备注"}, ensure_ascii=False)
            self.assertNotIn("内部备注", str(messages))
            return self.answer
        self.model.request.side_effect = review
        self.assertEqual(self.run_once(), "已确认分享")
        self.interests.record_reaction.assert_not_called()
        self.assertEqual(self.state.dispatch_send.call_args.args[2], "看它追着尾巴转。\n"+URL)

    def test_yields_before_browsing_and_honors_reconfiguration_in_queue(self):
        self.obj.note_reply(FRIEND)
        for gate in [self.active, self.state.social_should_yield]:
            with self.subTest(gate=gate):
                gate.return_value = gate is not self.active
                self.assertIn("等待", self.run_once())
                gate.return_value = gate is self.active
        self.state.paused.set()
        self.assertIn("等待", self.run_once())
        self.state.paused.clear()
        self.video.public_feed.assert_not_called()
        self.state.send_queue.before = self.obj.configure
        self.assertIn("取消", self.run_once())
        self.state.dispatch_send.assert_not_called()
        self.assertEqual(self.state.send_queue.jobs, 1)

    def test_three_distinct_video_budget_includes_all_targets_on_the_last_video(self):
        self.cfg.BILI_SHARE_DAILY_LIMIT = 12  # legacy out-of-range setting still honors three
        self.obj.note_reply(FRIEND)
        self.obj.note_reply(GROUP)
        self.answer = '{"targets":[1,1,2,2]}'
        for suffix in ("A", "B", "C"):
            self.video.public_feed.return_value = [(URL[:-1]+suffix, "公开标题")]
            self.assertIn("2个会话", self.run_once())
        self.assertEqual(self.state.dispatch_send.call_count, 6)
        self.assertEqual(self.interests.record_reaction.call_count, 3)
        self.interests.record_reaction.assert_called_with("猫咪","like",expected_identity="角色摘要")
        for offset in range(0, 6, 2):
            calls = self.state.dispatch_send.call_args_list[offset:offset+2]
            self.assertEqual({call.kwargs["target_id"] for call in calls}, {user_id(FRIEND,FRIEND),group_id(GROUP)})
            self.assertEqual(calls[0].args[2], calls[1].args[2])
        with self.obj._db() as db:
            self.assertEqual(self.obj._count(db, ACCOUNT, self.day), 3)
        self.video.public_feed.return_value = [(URL[:-1]+"D","第四个")]
        self.assertIn("上限", self.run_once())
        # Replayed work cannot hit a recipient again, including after a restart.
        self.obj = self.module.BiliSharing(19)
        self.obj.note_reply(FRIEND)
        self.assertFalse(self.obj._deliver(ACCOUNT,URL[:-1]+"A",FRIEND,self.caption,lambda:False))
        self.assertEqual(self.state.dispatch_send.call_count, 6)

    def test_selected_interest_and_feedback_are_from_one_actual_watching(self):
        self.obj.note_reply(FRIEND)
        self.interests.choose.side_effect = lambda rows: next((item for item in rows if "摄影" in item[1]), rows[0])
        self.video.public_feed.return_value = [(URL,"游戏"),(OTHER,"街头摄影")]
        self.answer = '{"targets":[]}'
        self.assertEqual(self.run_once(), "本次不分享")
        self.assertEqual(self.video.inspect_video.call_args.args[0].url, OTHER)
        self.interests.record_reaction.assert_called_once_with("猫咪","like",expected_identity="角色摘要")
        self.assertEqual(self.run_once(), "本次不分享")
        self.assertEqual(self.interests.choose.call_args.args[0], [(URL,"游戏")])
        self.assertEqual(self.interests.record_reaction.call_count, 2)
        self.assertIn("没有新的", self.run_once())
        self.assertEqual(self.interests.record_reaction.call_count, 2)
        self.state.dispatch_send.assert_not_called()

    def test_topic_search_falls_back_when_offline_or_results_already_watched(self):
        self.cfg.BILI_SHARE_DAILY_LIMIT = 0
        self.obj.note_reply(FRIEND)
        self.interests.preferred_topic.return_value = "街头摄影"
        self.video.public_search.return_value = [(OTHER,"街头摄影")]
        self.assertIn("已看视频", self.run_once())
        self.video.public_feed.assert_not_called()
        self.assertEqual(self.video.inspect_video.call_args.args[0].url, OTHER)
        self.assertIn("上限", self.run_once())
        self.assertEqual(self.video.inspect_video.call_args.args[0].url, URL)
        self.video.public_search.return_value = []
        self.video.public_feed.return_value = [(URL[:-1]+"D","热门新视频")]
        self.assertIn("上限", self.run_once())
        self.assertEqual(self.video.public_feed.call_count, 2)
        for call in self.video.public_search.call_args_list:
            self.assertEqual(call.args, ("街头摄影",))
            self.assertIn(call.kwargs["page"], (1,2,3))
        self.assertEqual(self.interests.record_reaction.call_count, 3)
        self.assertEqual([c.args[0] for c in self.model.request.call_args_list], ["main"]*3)
        self.state.dispatch_send.assert_not_called()

    def test_private_priority_between_recipients_cancels_remaining_jobs(self):
        self.obj.note_reply(FRIEND)
        self.obj.note_reply(GROUP)
        self.answer = '{"targets":[1,2]}'
        def first(*args, **kw):
            self.state.social_should_yield.return_value = True
            return True
        self.state.dispatch_send.side_effect = first
        self.assertIn("已确认1个会话", self.run_once())
        self.assertEqual(self.state.dispatch_send.call_count, 1)
        self.assertEqual(self.state.send_queue.jobs, 1)

    def test_role_change_during_vision_does_not_record_or_send_as_the_new_role(self):
        self.obj.note_reply(FRIEND)
        def switch(profile, messages, **kwargs):
            self.interests.identity.return_value = "另一角色"
            return self.request(profile,messages,**kwargs)
        self.model.request.side_effect = switch
        self.assertIn("没有取得", self.run_once())
        self.interests.record_reaction.assert_not_called()
        self.state.dispatch_send.assert_not_called()

    def test_revocation_reaches_inflight_uia_even_after_enqueue(self):
        self.obj.note_reply(FRIEND)
        self.assertEqual(self.run_once(), "已确认分享")
        token = self.state.dispatch_send.call_args.kwargs["proactive_cancel"]
        self.assertFalse(token.is_set())
        for event in [self.state.send_cancel, self.state.paused]:
            event.set()
            self.assertTrue(token.is_set())
            event.clear()
        self.state.social_should_yield.return_value = True
        self.assertTrue(token.is_set())
        self.state.social_should_yield.return_value = False
        self.cfg.BOT_WXID = "wxid_other_bot"
        self.assertTrue(token.is_set())
        self.cfg.BOT_WXID = ACCOUNT
        self.state.bridge_generation += 1
        self.assertTrue(token.is_set())

    def test_schedule_does_not_send_on_start_reconfigure_or_stop(self):
        self.obj.start()
        self.addCleanup(self.obj.stop)
        status = self.obj.status()
        self.assertTrue(status["scheduler_alive"])
        self.assertGreater(status["next_run"], time.time()+215*60)
        self.assertLess(status["next_run"], time.time()+265*60)
        self.cfg.BILI_SHARE_ENABLED = False
        self.obj.configure()
        self.assertFalse(self.obj.status()["enabled"])
        self.assertEqual(self.obj.status()["next_run"], 0)
        self.obj.stop()
        self.assertFalse(self.obj.status()["scheduler_alive"])
        self.video.public_feed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
