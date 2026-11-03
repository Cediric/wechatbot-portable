"""Exercise WeFlow ingress through the outbound OneBot/desktop boundaries."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_core as core_cases

sys.path.insert(0, str(core_cases.SOURCE))

WATCH = "https://www.bilibili.com/video/BV17x411w7KC?p=2"
CARD = f"<msg><appmsg><type>5</type><url><![CDATA[{WATCH}]]></url></appmsg></msg>"
SONG = "晴天 — 周杰伦\nhttps://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV"


class ChatMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core_cases.CoreTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        core_cases.CoreTests.tearDownClass.__func__(cls)

    message = staticmethod(core_cases.CoreTests.message)
    stop_scheduler = core_cases.CoreTests.stop_scheduler
    process_all = core_cases.CoreTests.process_all

    def setUp(self):
        core_cases.CoreTests.setUp(self)
        self.stop_scheduler()
        self.config.BILI_VIDEO_ENABLED = self.config.QQ_MUSIC_ENABLED = True
        self.config.HUMANLIKE_DECISION_ENABLED = False
        self.frame = Path(self.temp.name) / "frame.jpg"
        self.frame.write_bytes(b"synthetic frame")
        self.watch = self.enterContext(patch("bili_video.inspect_video", return_value=("只包含本分P抽帧，无声音", [str(self.frame)])))
        self.song = self.enterContext(patch("qq_music.lookup_song_reply", return_value=SONG))
        self.http_get = Mock()
        self.enterContext(patch.object(self.http, "session", return_value=types.SimpleNamespace(get=self.http_get)))
        ctx = types.SimpleNamespace(chat_context=lambda store, **values: dict(values))
        self.enterContext(patch.dict(sys.modules, {"interaction_context": ctx}))

    def test_private_query_owns_only_its_message_and_preserves_literal_url(self):
        self.config.TOOLS_ENABLED = False
        for i, text in enumerate(("上午好", "点歌 周杰伦的《晴天》", "下午好")):
            self.bridge.add_to_buffer(self.message("friend", content=text, rawid=str(i+1)))
        for _ in range(3):
            self.process_all()
        self.song.assert_called_once()
        self.assertEqual([e["wechat_identity"]["messages"][0]["original_message_id"] for e in self.events], ["1", "3"])
        self.assertEqual(len(self.sent), 1)
        method, _, args, kw = self.sent[0]
        self.assertEqual((method, args, kw["target_id"], kw["is_group"], kw["stream_sentence"]),
                         ("send_text", (SONG,), self.stable.user_id("friend", "friend"), False, True))

    def test_group_judge_controls_both_tools_before_any_lookup(self):
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        self.judge.should_reply_group = Mock(return_value=(False, "voice", "群友之间交流", "", "none"))
        self.bridge.add_to_buffer(self.message(content="点歌 晴天", rawid="10"))
        self.bridge.add_to_buffer(self.message(content="分享标题", localType=49, rawid="11"))
        self.process_all()
        self.process_all()
        self.assertEqual(self.judge.should_reply_group.call_count, 2)
        self.http_get.assert_not_called()
        self.watch.assert_not_called()
        self.song.assert_not_called()
        self.assertEqual((self.sent, self.events), ([], []))

    def test_mention_only_song_one_group_message_even_when_judge_chooses_voice(self):
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        self.state.group_reply_mode = "mention"
        self.judge.should_reply_group = Mock(return_value=(True, "voice", "对我点歌", "", "none"))
        self.bridge.add_to_buffer(self.message("other@chatroom", content="点歌 晴天", rawid="21"))
        self.bridge.add_to_buffer(self.message(content=WATCH, rawid="90"))
        self.bridge.add_to_buffer(self.message(content="@小助手，点歌 晴天", rawid="22"))
        self.process_all()
        self.process_all()
        self.watch.assert_not_called()
        self.song.assert_called_once()
        self.assertEqual((len(self.sent), self.events), (1, []))
        self.assertEqual(self.sent[0][3]["target_id"], self.stable.group_id("one@chatroom"))
        self.assertTrue(self.sent[0][3]["is_group"])

    def test_group_share_card_retains_original_author_and_same_turn_question(self):
        self.config.TOOLS_ENABLED = False
        self.state.group_reply_mode = "mention"
        self.bridge.add_to_buffer(self.message(sender="owner", content="", localType=49, rawXml=CARD, rawid="40"))
        self.bridge.add_to_buffer(self.message(sender="other", content="@小助手 这是谁", rawid="41"))
        self.process_all()
        self.watch.assert_not_called()
        self.bridge.add_to_buffer(self.message(sender="owner", content="@小助手 看看这个分P开头是什么", rawid="42"))
        self.process_all()
        self.assertEqual(self.watch.call_args.args[0].url, WATCH)
        event = self.events[-1]
        self.assertEqual([i["original_message_id"] for i in event["wechat_identity"]["messages"]], ["40", "42"])
        self.assertIn("这个分P开头是什么", event["message"][1]["data"]["text"])
        self.assertIn(WATCH, event["message"][1]["data"]["text"])
        self.assertNotIn("<appmsg>", event["message"][1]["data"]["text"])
        self.assertEqual(event["message"][-1], {"type": "image", "data": {"file": str(self.frame),
                          "wechat_session_id": "one@chatroom", "wechat_actor_id": "owner", "wechat_message_id": "40"}})
        self.http_get.assert_not_called()

    def test_native_group_card_name_and_sole_bot_target(self):
        self.state.group_reply_mode = "mention"
        for seq, metadata in enumerate(({"atMe": True}, {"atUserList": ["bot"]},
                {"mentions": [{"username": "bot", "displayName": "群内的新名片"}]}), start=80):
            self.bridge.add_to_buffer(self.message(content="@群内的新名片\u2005点歌 晴天", rawid=str(seq), **metadata))
            self.process_all()
        self.assertEqual(self.song.call_count, 3)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.events, [])
        for seq, (text, metadata) in enumerate((
                ("@另一个人\u2005点歌 晴天", {"mentions": ["other"]}),
                ("@我和别人同名\u2005点歌 晴天", {"mentions": ["bot", "other"]}),
                ("@群内的新名片\u2005点歌 晴天 @别人", {"atMe": True}),
                ("@另一个人\u2005点歌 晴天", {"atMe": True, "mentions": ["other"]})), start=84):
            self.bridge.add_to_buffer(self.message(content=text, rawid=str(seq), **metadata))
            self.process_all()
        self.assertEqual(self.song.call_count, 3)

    def test_native_card_title_is_not_a_command_or_a_textual_mention(self):
        self.state.group_reply_mode = "mention"
        self.bridge.add_to_buffer(self.message(content={"title": "@小助手 点歌 晴天", "url": WATCH},
                                              type="link", rawid="96"))
        self.process_all()
        self.assertEqual(self.events, [])
        self.watch.assert_not_called()
        self.bridge.add_to_buffer(self.message("friend", content="请生成一张猫图", linkUrl=WATCH, rawid="97"))
        self.tools.generate_image = Mock()
        self.process_all()
        self.tools.generate_image.assert_not_called()
        self.watch.assert_called_once()
        self.song.assert_not_called()

    def test_title_only_uses_verified_original_weflow_row_and_never_other_sender(self):
        self.http_get.return_value.status_code = 200
        row = {"serverId": "90071992547409931", "senderUsername": "other", "isSend": 0, "localType": 49,
               "rawContent": CARD, "sessionId": "one@chatroom"}
        self.http_get.return_value.json.return_value = {"talker": "one@chatroom", "messages": [row]}
        self.bridge.add_to_buffer(self.message(content="标题", localType=49, rawid=row["serverId"]))
        self.process_all()
        self.watch.assert_not_called()
        self.assertNotIn("image", [s["type"] for s in self.events[-1]["message"]])
        row["senderUsername"] = "alice"
        self.bridge.add_to_buffer(self.message(content="标题", localType=49, rawid=row["serverId"]))
        self.process_all()
        self.watch.assert_called_once()
        self.assertEqual(self.http_get.call_args.kwargs["params"]["talker"], "one@chatroom")
        self.assertEqual(self.events[-1]["message"][-1]["data"]["wechat_message_id"], row["serverId"])

    def test_plain_link_and_failure_reach_model_with_own_text_without_fake_frames(self):
        self.watch.return_value = ("未取得视频画面，无法判断。", [])
        question = "帮我看看这一段谁在说话 " + WATCH
        self.bridge.add_to_buffer(self.message("friend", content=question, rawid="60"))
        self.process_all()
        segments = self.events[0]["message"]
        self.assertEqual([s["type"] for s in segments], ["text"])
        self.assertIn(question, segments[0]["data"]["text"])
        self.assertIn("未取得视频画面", segments[0]["data"]["text"])
        self.http_get.assert_not_called()

    def test_structured_bili_miniapp_and_unsupported_native_page(self):
        import bili_video
        card = {"appmsg": {"title": "一起看看", "weappinfo": {"appid": bili_video.BILI_APP_ID,
                "pagepath": "pages/video/video?avid=1234&p=2"}}}
        self.bridge.add_to_buffer(self.message("friend", content=card, type=49, rawid="941"))
        self.process_all()
        self.assertEqual(self.watch.call_args.args[0].url, "https://www.bilibili.com/video/av1234?p=2")
        self.assertIn("一起看看", self.events[-1]["message"][0]["data"]["text"])
        card["appmsg"]["weappinfo"]["appid"] = "other-app"
        self.bridge.add_to_buffer(self.message("friend", content=card, type=49, rawid="942"))
        self.process_all()
        self.assertEqual([s["type"] for s in self.events[-1]["message"]], ["text"])
        card["appmsg"]["weappinfo"].update(appid=bili_video.BILI_APP_ID, pagepath="pages/home")
        self.bridge.add_to_buffer(self.message("friend", content=card, type=49, rawid="943"))
        self.process_all()
        self.assertIn("未取得画面", self.events[-1]["message"][0]["data"]["text"])
        self.watch.assert_called_once()
        self.http_get.assert_not_called()

    def test_multiple_group_shares_only_latest_is_viewed_and_bound(self):
        self.bridge.add_to_buffer(self.message(sender="alice", content=WATCH, rawid="951"))
        self.bridge.add_to_buffer(self.message(sender="bob", content="https://www.bilibili.com/video/av1234", rawid="952"))
        self.process_all()
        self.watch.assert_called_once()
        self.assertTrue(self.watch.call_args.args[0].url.endswith("av1234"))
        event = self.events[-1]
        self.assertIn("前面的 1 条未观看", event["message"][1]["data"]["text"])
        self.assertEqual(event["message"][-1]["data"]["wechat_actor_id"], "bob")
        self.assertEqual(event["message"][-1]["data"]["wechat_message_id"], "952")

    def test_discussion_of_music_and_bilibili_or_disabled_tools_do_no_io(self):
        self.bridge.add_to_buffer(self.message("friend", content="点歌功能怎么用？B站最近怎样？"))
        self.process_all()
        self.config.BILI_VIDEO_ENABLED = self.config.QQ_MUSIC_ENABLED = False
        self.bridge.add_to_buffer(self.message("friend", content=WATCH))
        self.bridge.add_to_buffer(self.message("friend", content="点歌 晴天"))
        self.process_all()
        self.assertEqual(len(self.events), 2)
        self.watch.assert_not_called()
        self.song.assert_not_called()
        self.http_get.assert_not_called()
