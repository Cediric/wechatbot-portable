"""Send one synthetic private turn from WeFlow through the deployed AstrBot hooks."""
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock

import test_core as core
import test_copied_profile_plugin as deployed
from test_context_summary import request

sys.path.insert(0, str(core.SOURCE))


class ChatInterestContextTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        core.CoreTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        core.CoreTests.tearDownClass.__func__(cls)

    async def test_character_interest_reaches_only_the_owned_chat_system_prompt(self):
        core.CoreTests.setUp(self)
        self.bridge._sched.stop()
        topic, question, answer = "湿地观鸟", "今天过得怎么样？", "今天还挺开心的。"
        self.config.CHARACTER_INTERESTS_ENABLED = self.config.MEMORY_ENABLED = True
        self.config.CHARACTER_INTEREST_SEEDS = [topic]
        memory = self.state.memory_store = Mock()
        memory.search.return_value = []
        self.bridge.add_to_buffer(core.CoreTests.message("wxid_friend", content=question, rawid="311"))
        self.bridge.process_sender("wxid_friend")
        raw, = self.events
        note = raw["wechat_character_interests"]
        self.assertIn(topic, note)
        user_text = raw["message"][0]["data"]["text"]
        self.assertIn(question, user_text)
        self.assertNotIn(topic, user_text)
        self.assertEqual(memory.add_chat.call_args.args[0], question)
        self.assertEqual(memory.extract_memories_async.call_args.args[0], question)
        self.assertNotIn(topic, str(memory.mock_calls))

        main, _ = deployed.load_copied_plugin(deployed.copy_plugin(self.temp.name))
        manager = types.SimpleNamespace(load_provider=AsyncMock(), get_provider_by_id=AsyncMock(return_value=object()))
        plugin = main.WechatbotProfile(types.SimpleNamespace(provider_manager=manager))
        settings = {"main_llm_api_base": "http://127.0.0.1:11434/v1", "main_llm_model": "synthetic"}
        profile = main.main_profile(settings, deployed.ROOT)
        plugin._location = {"root": deployed.ROOT, "platform": "wechatbot-portable", "status": Path(self.temp.name) / "status.json"}
        plugin._read = lambda: (plugin._location, (settings, profile))
        event = deployed.OwnedEvent()
        ticket = dict(event.message_obj.raw_message["wechat_reply_request"], session_id="wxid_friend", target_id=raw["user_id"])
        event.message_obj.raw_message = dict(raw, wechat_reply_request=ticket)
        event.get_extra = event.extra.get
        await plugin.select_profile(event)
        req = request()
        req.prompt = user_text
        await plugin.configure_request(event, req)
        self.assertFalse(event.stopped)
        self.assertIn(note, req.system_prompt)
        self.assertNotIn(topic, req.prompt)
        profile_after = event.get_extra("wechatbot_main_profile")
        self.assertNotIn(topic, json.dumps(profile_after, ensure_ascii=False))
        self.assertEqual(event.get_extra("selected_provider"), "wechatbot-main-" + main.profile_digest(profile_after))
        self.assertNotIn(topic, str(manager.load_provider.await_args))

        async def chunks():
            yield deployed.streams.Chain(answer)
        await event.send_streaming(chunks())
        sent = [seg["data"]["text"] for _, params in event.calls for seg in params["message"] if seg["type"] == "text"]
        self.assertEqual("".join(sent), answer)
