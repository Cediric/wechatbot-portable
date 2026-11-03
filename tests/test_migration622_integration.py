"""Native-ID bridge integration; no desktop, external service or paid model."""
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

import test_core as core_fixture
from test_stream_runtime import streaming
import delivery_context


class MigrationIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(core_fixture.CoreTests.setUpClass.__func__)
    tearDownClass = classmethod(core_fixture.CoreTests.tearDownClass.__func__)
    message = staticmethod(core_fixture.CoreTests.message)
    stop_scheduler = core_fixture.CoreTests.stop_scheduler
    process_all = core_fixture.CoreTests.process_all
    entry = core_fixture.CoreTests.entry

    def setUp(self):
        core_fixture.CoreTests.setUp(self)
        self.stop_scheduler()
        delivery_context.clear()
        self.addCleanup(delivery_context.clear)
        self.addCleanup(setattr, self.config, 'MEMORY_ENABLED', False)

    def memory(self):
        self.config.MEMORY_ENABLED = True
        store = types.SimpleNamespace(add_chat=Mock(), remember_explicit=Mock(),
            checkpoint_session=Mock(return_value=1), extract_memories_async=Mock(),
            search=Mock(return_value=[]), recent_chats=Mock(return_value=[]))
        self.state.memory_store = store
        return store

    def test_silent_group_turn_persists_original_actor_evidence_before_decision(self):
        store = self.memory()
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        def decide(*args, **kwargs):
            store.add_chat.assert_called_once()
            self.assertEqual(store.add_chat.call_args.kwargs['actor_id'], 'wxid_alice')
            return False, 'text', 'silent', '', 'none'
        self.judge.should_reply_group = decide
        context = types.SimpleNamespace(chat_context=lambda *a, **k: {})
        with patch.dict('sys.modules', {'interaction_context': context}):
            self.bridge.add_to_buffer(self.message(sender='wxid_alice', content='记住我喜欢红茶', rawid='claim1'))
            self.process_all()
        store.remember_explicit.assert_called_once()
        self.assertEqual(store.remember_explicit.call_args.args[0], '记住我喜欢红茶')
        self.assertFalse(self.events)

    def test_reset_checkpoints_before_exact_command_without_judge_or_style_prefix(self):
        store = self.memory()
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        self.judge.should_reply_group = Mock(side_effect=AssertionError('reset is not a model decision'))
        store.checkpoint_session.side_effect = lambda session: self.assertFalse(self.events)
        self.bridge.add_to_buffer(self.message(sender='wxid_alice', content='/reset', rawid='reset1'))
        self.process_all()
        store.checkpoint_session.assert_called_once_with('one@chatroom')
        texts = [part['data']['text'] for part in self.events[0]['message'] if part['type'] == 'text']
        self.assertEqual(texts, ['/reset'])
        self.judge.should_reply_group.assert_not_called()

    def test_checkpoint_failure_preserves_conversation(self):
        store = self.memory()
        store.checkpoint_session.side_effect = OSError('synthetic disk failure')
        self.bridge.add_to_buffer(self.message('friend', content='/new', rawid='new1'))
        with self.assertLogs('ob11-bridge', level='ERROR'):
            self.process_all()
        self.assertFalse(self.events)

    def test_reset_inside_group_burst_preserves_command_author_and_next_turn(self):
        store = self.memory()
        self.bridge.add_to_buffer(self.message(sender='wxid_alice', content='我喜欢红茶', rawid='a1'))
        self.bridge.add_to_buffer(self.message(sender='wxid_bob', content='/reset', rawid='b1'))
        self.bridge.add_to_buffer(self.message(sender='wxid_alice', content='新话题', rawid='a2'))
        self.process_all()
        self.assertEqual(store.add_chat.call_count, 2)
        self.assertEqual(self.events[0]['user_id'], self.stable.user_id('one@chatroom', 'wxid_bob'))
        self.assertEqual([p['data']['text'] for p in self.events[0]['message'] if p['type']=='text'], ['/reset'])
        self.process_all()
        self.assertEqual(len(self.events), 2)
        self.assertIn('新话题', self.events[1]['message'][1]['data']['text'])

    def test_skipped_group_image_is_available_to_same_author_followup_only(self):
        jobs = []
        self.bridge._spawn_media = lambda fn, data: jobs.append(data)
        source = Path(self.temp.name) / 'followup-source.png'
        source.write_bytes(b'synthetic media bytes')
        self.bridge.add_to_buffer(self.message(sender='wxid_alice', content='[图片]', type=3, rawid='picture1'))
        self.bridge._finish_media(jobs[0], '原图', segments=[{'type':'image','data':{'file':str(source)}}])
        self.config.GROUP_REPLY_JUDGE_ENABLED = True
        self.judge.should_reply_group = lambda *a, **kw: (False, 'text', 'silent', '', 'none')
        with patch.dict('sys.modules', {'interaction_context': types.SimpleNamespace(chat_context=lambda *a, **k: {})}):
            self.process_all()
        self.assertFalse(self.events)
        self.config.GROUP_REPLY_JUDGE_ENABLED = False
        self.bridge.add_to_buffer(self.message(sender='wxid_bob', content='这是什么？', rawid='bob1'))
        self.process_all()
        self.assertFalse(any(p['type']=='image' for p in self.events[-1]['message']))
        self.bridge.add_to_buffer(self.message(sender='wxid_alice', content='刚才的图片好看吗？', rawid='alice2'))
        self.process_all()
        image = next(p for p in self.events[-1]['message'] if p['type']=='image')
        self.assertEqual(image['data']['wechat_message_id'], 'picture1')
        self.assertEqual(image['data']['wechat_actor_id'], 'wxid_alice')
        self.assertIn('历史媒体引用', self.events[-1]['message'][1]['data']['text'])

    def test_delivery_feedback_crosses_event_boundary_with_session_check(self):
        delivery_context.record('friend', 'voice', 'confirmed', text='好呀', operation_id='voice1')
        delivery_context.record('someone_else', 'text', 'confirmed', text='不应混入', operation_id='other1')
        self.bridge.add_to_buffer(self.message('friend', content='听到了', rawid='reply1'))
        self.process_all()
        event = self.events[0]
        event['wechat_reply_request'] = {'request_id': 'synthetic-request'}
        note = streaming.verified_delivery_note(event)
        self.assertIn('好呀', note)
        self.assertNotIn('不应混入', note)
        event['wechat_delivery_feedback']['session_id'] = 'someone_else'
        self.assertEqual(streaming.verified_delivery_note(event), '')

    def test_confirmed_image_receipt_survives_cancellation_after_send(self):
        self.tools.generate_image = lambda *a, **k: 'synthetic-image.png'
        context = types.SimpleNamespace(chat_context=lambda *a, **k: {})
        def sent_then_cancel(*a, **kw):
            self.state.send_cancel.set()
            return True
        self.state.dispatch_send = sent_then_cancel
        with patch.dict('sys.modules', {'interaction_context': context}):
            self.bridge.add_to_buffer(self.message('friend', content='给我生成一张图片', rawid='draw1'))
            self.process_all()
        rows = delivery_context.snapshot('friend')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['kind'], rows[0]['status']), ('image', 'confirmed'))
        self.assertFalse(self.events)


if __name__ == '__main__':
    unittest.main()
