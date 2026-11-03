"""Count avoided calls without timing-dependent assertions or real services."""
import types
import unittest
import tempfile
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import test_core as core_fixture


class OutboxWakeTests(unittest.TestCase):
    def setUp(self):
        from outbox import Outbox
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.box = Outbox(str(Path(self.folder.name) / 'outbox.db'))
        self.addCleanup(self.box.close)

    def test_commit_wakes_waiter_without_poll_interval(self):
        entered = threading.Event()
        real_wait = self.box.ready.wait
        def waiting(timeout):
            entered.set()
            return real_wait(timeout)
        event = {'self_id': 1, 'message_id': 1, 'time': 1}
        with patch.object(self.box.ready, 'wait', side_effect=waiting), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.box.peek, 2)
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.box.put(event))
            self.assertEqual(future.result(1)[1], event)
        self.assertEqual(self.box.stats()['pending'], 1)

    def test_put_between_empty_read_and_wait_cannot_lose_wakeup(self):
        event = {'self_id': 1, 'message_id': 2, 'time': 1}
        real_wait = self.box.ready.wait
        def inserted_before_wait(timeout):
            self.box.put(event)
            return real_wait(0)
        with patch.object(self.box.ready, 'wait', side_effect=inserted_before_wait):
            self.assertEqual(self.box.peek(.5)[1], event)

    def test_existing_event_does_not_wait_and_empty_wait_is_bounded(self):
        self.box.put({'self_id': 1, 'message_id': 3, 'time': 1})
        with patch.object(self.box.ready, 'wait', return_value=False) as wait:
            row_id, _ = self.box.peek(.5)
            wait.assert_not_called()
            self.box.acknowledge(row_id)
            self.assertIsNone(self.box.peek(.5))
            wait.assert_called_once_with(.5)


class DirectMediaLatencyTests(unittest.TestCase):
    setUpClass = classmethod(core_fixture.CoreTests.setUpClass.__func__)
    tearDownClass = classmethod(core_fixture.CoreTests.tearDownClass.__func__)
    message = staticmethod(core_fixture.CoreTests.message)
    stop_scheduler = core_fixture.CoreTests.stop_scheduler
    process_all = core_fixture.CoreTests.process_all
    entry = core_fixture.CoreTests.entry

    def setUp(self):
        core_fixture.CoreTests.setUp(self)
        self.stop_scheduler()
        settings = patch.object(self.config, 'HUMANLIKE_DECISION_ENABLED', True, create=True)
        settings.start()
        self.addCleanup(settings.stop)
        self.choose = Mock(return_value=(True, 'text', 'ordinary', '', ''))
        self.context = core_fixture.load_module('latency623_context', 'interaction_context.py')
        modules = patch.dict('sys.modules', {'humanlike_decision': types.SimpleNamespace(choose=self.choose),
                                           'interaction_context': self.context})
        modules.start()
        self.addCleanup(modules.stop)

    def test_direct_voice_omits_decision_and_keeps_voice_route(self):
        self.bridge.add_to_buffer(self.message('friend', content='给我发语音', rawid='v1'))
        self.process_all()
        self.choose.assert_not_called()
        self.assertEqual(self.events[0]['wechat_reply_plan']['reply_type'], 'voice')
        self.assertEqual(len(self.events), 1)

    def test_direct_image_omits_decision_but_preserves_main_writer_context(self):
        rows = [dict(session_id='friend', actor_id='friend', content='之前想看海', role='user', message_id='old', created_at=1)]
        self.state.memory_store = types.SimpleNamespace(recent_chats=lambda *a, **k: rows, recent=lambda *a: [])
        self.tools.generate_image = Mock(return_value='synthetic.png')
        self.bridge.add_to_buffer(self.message('friend', content='请画一张海边照片', rawid='i1'))
        self.process_all()
        self.choose.assert_not_called()
        self.tools.generate_image.assert_called_once()
        context = self.tools.generate_image.call_args.kwargs['context']
        self.assertEqual(context['related_chats'][0]['content'], '之前想看海')
        self.assertEqual(context['session_id'], 'friend')
        self.assertEqual(context['humanlike_plan']['source'], 'explicit_user_request_default')
        self.assertEqual(self.sent[0][0], 'send_image')

    def test_ordinary_or_negative_media_chat_still_uses_decision(self):
        for text in ('今天过得怎么样', '不要语音，直接说'):
            with self.subTest(text=text):
                self.choose.reset_mock()
                self.bridge.add_to_buffer(self.message('friend', content=text))
                self.process_all()
                self.choose.assert_called_once()

    def test_disabled_image_tool_does_not_skip_normal_decision(self):
        self.config.TOOLS_ENABLED = False
        self.bridge.add_to_buffer(self.message('friend', content='请画一只猫'))
        self.process_all()
        self.choose.assert_called_once()
        self.assertFalse(self.sent)

    def test_group_direct_voice_keeps_participation_judge(self):
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        self.judge.should_reply_group = Mock(return_value=(False, 'text', 'not_addressed', '', 'none'))
        self.bridge.add_to_buffer(self.message(content='给我发语音'))
        self.process_all()
        self.judge.should_reply_group.assert_called_once()
        self.choose.assert_not_called()
        self.assertFalse(self.events)

    def test_full_valid_current_history_avoids_discarded_related_queries(self):
        self.config.DECISION_CONTEXT_RECENT_CHATS = 2
        self.addCleanup(delattr, self.config, 'DECISION_CONTEXT_RECENT_CHATS')
        rows = [dict(session_id='room@chatroom', actor_id='alice', content=text, role='user', message_id=str(i))
                for i, text in enumerate(('最近一句', '前一句'))]
        store = types.SimpleNamespace(recent_chats=Mock(return_value=rows))
        selected = self.context.scoped_chats(store, {'alice'}, session_id='room@chatroom', is_group=True)
        self.assertEqual(len(selected), 2)
        store.recent_chats.assert_called_once_with(2, session_id='room@chatroom')

    def test_active_chat_does_not_fill_history_budget_from_other_groups(self):
        self.config.DECISION_CONTEXT_RECENT_CHATS = 2
        self.addCleanup(delattr, self.config, 'DECISION_CONTEXT_RECENT_CHATS')
        bad = dict(session_id='other', actor_id='other', content='不能混入', role='user', message_id='bad')
        related = dict(session_id='room@chatroom', actor_id='friend', content='他的群发言', role='user', message_id='g')
        store = types.SimpleNamespace(recent_chats=Mock(side_effect=[[bad, bad], [related]]))
        selected = self.context.scoped_chats(store, {'friend'}, session_id='friend')
        self.assertEqual(selected, [])
        store.recent_chats.assert_called_once_with(2, session_id='friend')

    def test_main_chat_event_uses_shared_profile_by_native_id_after_rename(self):
        memory = core_fixture.load_module('caller_shared_memory', 'memory_store.py')
        with tempfile.TemporaryDirectory() as directory:
            store = memory.MemoryStore(str(Path(directory)/'facts.db'), extraction_paused=True)
            try:
                store.add_memory('我喜欢喝红茶', source='旧备注', session_id='wxid_friend', actor_id='wxid_friend')
                store.add_memory('我喜欢听摇滚', source='新昵称', session_id='other@chatroom', actor_id='wxid_other')
                event = self.message('first@chatroom', sender='wxid_friend', content='你好', rawid='recall1')
                event['senderName'] = '新昵称'
                with patch.object(self.config, 'MEMORY_ENABLED', True), patch.object(self.config, 'MEMORY_SCOPE', 'shared', create=True), patch.object(self.state, 'memory_store', store):
                    self.bridge.add_to_buffer(event)
                    self.process_all()
                text = ''.join(s['data']['text'] for s in self.events[0]['message'] if s['type']=='text')
                self.assertIn('我喜欢喝红茶', text)
                self.assertNotIn('我喜欢听摇滚', text)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
