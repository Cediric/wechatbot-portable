"""OneBot group metadata and routing against synthetic registry records."""
import asyncio
import threading
import types
import unittest

import test_protocol_validation as validation


class GroupProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        validation.ProtocolValidationTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        validation.ProtocolValidationTests.tearDownClass.__func__(cls)

    def setUp(self):
        validation.ProtocolValidationTests.setUp(self)
        self.state._self_id_int = 77
        self.config.BOT_NICKNAMES = ["微信助手"]
        self.config.REPLY_STYLE_ENABLED = False
        self.groups = {1: {"name": "群甲", "session_id": "111@chatroom"},
                       2: {"name": "群乙", "session_id": "222@chatroom"}}
        self.members = {"11": {"name": "群甲成员", "username": "wxid_person", "group_session": "111@chatroom"},
                        "12": {"name": "群乙成员", "username": "wxid_person", "group_session": "222@chatroom"}}
        self.private = {"21": {"name": "私聊备注", "nickname": "私聊昵称", "username": "wxid_person", "session_id": "wxid_person"}}
        self.state.identity_registry = types.SimpleNamespace(_lock=threading.RLock(), members=self.members,
            private=self.private, groups={str(key): value for key, value in self.groups.items()},
            get_group_record=lambda key: self.groups.get(key, {}),
            get_private_record=lambda key: self.private.get(str(key), {}), get_member_name=lambda key: self.members[str(key)]["name"])

    def api(self, action, **params):
        return asyncio.run(self.protocol._handle_ob_api({"action": action, "params": params}, generation=456))

    def test_incoming_self_mention_resolves_without_a_registered_self_member(self):
        event = self.protocol.make_message_event("group", 11,
            [{"type": "at", "data": {"qq": "77"}}], group_id=1, nickname="群甲成员")
        result = self.api("get_group_member_info", group_id=event["group_id"], user_id=int(event["message"][0]["data"]["qq"]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"], {"group_id": 1, "user_id": 77, "nickname": "微信助手", "card": "微信助手", "role": "member"})
        self.assertEqual(self.api("get_stranger_info", user_id=77)["data"]["nickname"], "微信助手")

    def test_group_scoped_sender_lookup_has_card_and_same_event_user_id(self):
        result = self.api("get_group_member_info", group_id=1, user_id=11)
        self.assertEqual(result["status"], "ok")
        self.assertEqual((result["data"]["user_id"], result["data"]["card"]), (11, "群甲成员"))
        stranger = self.api("get_stranger_info", user_id=11)
        self.assertEqual((stranger["status"], stranger["data"]["user_id"], stranger["data"]["nickname"]), ("ok", 11, "群甲成员"))
        self.assertEqual(self.api("get_stranger_info", user_id=21)["data"]["nickname"], "私聊昵称")

    def test_same_person_in_another_group_cannot_resolve_as_this_groups_member(self):
        self.assertEqual(self.api("get_group_member_info", group_id=2, user_id=11)["status"], "failed")
        result = self.api("get_group_member_info", group_id=2, user_id=12)
        self.assertEqual((result["status"], result["data"]["nickname"]), ("ok", "群乙成员"))
        self.state.dispatch_send.assert_not_called()

    def test_unknown_group_cannot_authorize_self_or_list_query(self):
        self.assertEqual(self.api("get_group_member_info", group_id=999, user_id=77)["status"], "failed")
        self.assertEqual(self.api("get_group_member_list", group_id=999)["status"], "failed")

    def test_member_list_contains_only_requested_verified_group(self):
        result = self.api("get_group_member_list", group_id="1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"], [{"group_id": 1, "user_id": 11, "nickname": "群甲成员", "card": "群甲成员", "role": "member"}])

    def test_invalid_lookup_ids_are_not_truncated_or_reinterpreted(self):
        for bad in (True, 1.9, "bad"):
            with self.subTest(value=bad):
                self.assertEqual(self.api("get_group_member_info", group_id=bad, user_id=77)["status"], "failed")
                self.assertEqual(self.api("get_stranger_info", user_id=bad)["status"], "failed")

    def test_group_reply_uses_group_target_even_when_sender_private_id_is_present(self):
        response = self.api("send_msg", message_type="group", group_id=1, user_id=21,
                            message=[{"type": "text", "data": {"text": "一起聊聊。"}}])
        self.assertEqual(response["status"], "ok")
        call = self.state.dispatch_send.call_args
        self.assertEqual(call.args[:3], ("send_text", "群甲", "一起聊聊。"))
        self.assertEqual((call.kwargs["target_id"], call.kwargs["is_group"]), (1, True))

    def test_group_id_is_not_a_private_recipient(self):
        result = self.api("send_private_msg", user_id=1, message=[{"type": "text", "data": {"text": "一句。"}}])
        self.assertEqual(result["status"], "failed")
        self.state.dispatch_send.assert_not_called()

    def test_api_failure_log_identifies_action_without_ids_or_body(self):
        with self.assertLogs("ob11-bridge", level="WARNING") as logs:
            self.api("get_group_member_info", group_id=2, user_id=11)
        self.assertIn("action=get_group_member_info", logs.output[-1])
        self.assertNotIn("wxid_person", logs.output[-1])


class ImageVoicePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        validation.ProtocolValidationTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        validation.ProtocolValidationTests.tearDownClass.__func__(cls)

    def setUp(self):
        validation.ProtocolValidationTests.setUp(self)
        self.config.AUTO_VOICE_ENABLED = True
        self.config.AUTO_STICKER_ENABLED = False
        self.config.VOICE_MIN_CHARS = 1
        self.protocol._reply_media_policy = None

    def plan(self, mode):
        return self.protocol._decide_tail_media({"mode": mode, "session_id": "synthetic-session", "request_id": "synthetic-ticket"}, "这张给你，看看喜欢不？")

    def test_picture_request_success_and_failure_both_suppress_random_voice(self):
        for image_status in ("sent", "generation_failed", "send_unconfirmed"):
            self.protocol._reply_media_policy = None
            result = self.plan({"reply_type": "text_voice", "image_requested": True, "image_status": image_status,
                                "user_text": "发张照片看看", "explicit_voice": False})
            self.assertEqual(result["reply_type"], "text")
            self.tts.synthesize_voice.assert_not_called()

    def test_user_requesting_picture_and_voice_can_still_receive_voice(self):
        result = self.plan({"reply_type": "voice", "image_requested": True, "explicit_voice": True,
                            "user_text": "发张照片，也发个语音"})
        self.assertEqual(result["reply_type"], "voice")

    def test_ordinary_chat_keeps_configured_optional_voice(self):
        self.assertEqual(self.plan({"reply_type": "text", "user_text": "咋啦"})["reply_type"], "voice")

    def test_already_delivered_text_cannot_become_voice_again(self):
        request = {"mode": {"reply_type": "voice", "explicit_voice": True}, "text_delivered": True,
                   "session_id": "synthetic-session", "request_id": "synthetic-ticket"}
        self.assertEqual(self.protocol._decide_tail_media(request, "我在呢。")["reply_type"], "text")


if __name__ == "__main__":
    unittest.main()
