"""Single-message extractive compression keeps original quoted evidence."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'wechat-weflow-bridge-ob11'
sys.path.insert(0, str(SOURCE))
import memory_context as inputs


def profile(**values):
    return {'context_tokens': 4096, 'max_tokens': 256, 'prompt': '',
            'compression': {'enabled': True, 'threshold': .82}, **values}


class ExtractiveInputTests(unittest.TestCase):
    def prepare(self, text, selected=None, prompt='仅从资料抽取逐字原话。'):
        return inputs.prepare_memory_input(text, prompt, selected or profile(),
            session_id='native@chatroom', actor_id='custom_wechat_username', message_id='raw-message-1')

    def test_large_cap_normal_input_stays_original_without_a_summary(self):
        text = '我喜欢茶。' + '普通聊天内容' * 1000
        prepared = self.prepare(text, profile(context_tokens=256000, max_tokens=2048))
        self.assertFalse(prepared.compressed)
        payload = json.loads(prepared.messages[-1]['content'])
        self.assertEqual(payload['untrusted_message'], text)
        self.assertTrue(prepared.accepts_evidence('我喜欢茶。'))

    def test_low_budget_selects_intact_source_sentences_and_keeps_identity(self):
        text = ('与记忆无关的长内容' * 1000) + '。我喜欢红茶。下次周日再一起聊天。'
        prepared = self.prepare(text)
        self.assertTrue(prepared.compressed)
        payload = json.loads(prepared.messages[-1]['content'])
        self.assertEqual((payload['session_id'], payload['actor_id'], payload['message_id']),
            ('native@chatroom', 'custom_wechat_username', 'raw-message-1'))
        self.assertNotIn('untrusted_message', payload)
        for block in payload['untrusted_message_excerpts']:
            self.assertEqual(text[block['start']:block['end']], block['text'])
        self.assertTrue(prepared.accepts_evidence('我喜欢红茶。'))
        self.assertTrue(prepared.accepts_evidence('下次周日再一起聊天。'))
        self.assertFalse(prepared.accepts_evidence('用户爱喝红茶'))
        self.assertLessEqual(prepared.estimated_input_tokens + prepared.output_reserved + 128, prepared.context_cap)

    def test_adjacent_source_sentences_cannot_be_combined_into_one_excerpt_evidence(self):
        prepared = self.prepare('x' * 12000 + '。我喜欢红茶。我住在济南。')
        self.assertTrue(prepared.accepts_evidence('我喜欢红茶。'))
        self.assertTrue(prepared.accepts_evidence('我住在济南。'))
        self.assertFalse(prepared.accepts_evidence('我喜欢红茶。我住在济南。'))

    def test_json_escaping_and_configured_prompt_are_budgeted_before_request(self):
        text = ('"\\\n普通内容' * 1500) + '。我喜欢茶。'
        configured = '额外抽取提示词' * 10
        prepared = self.prepare(text, profile(prompt=configured))
        serialized = [dict(message) for message in prepared.messages]
        serialized[0]['content'] += '\n' + configured
        actual = sum(len(message['content'].encode('utf-8')) + 16 for message in serialized)
        self.assertEqual(actual, prepared.estimated_input_tokens)
        self.assertLessEqual(actual + prepared.output_reserved + 128, 4096)
        json.loads(prepared.messages[-1]['content'])

    def test_single_oversized_sentence_and_oversized_system_are_skipped(self):
        self.assertIsNone(self.prepare('没有句号的超长原句' * 5000))
        self.assertIsNone(self.prepare('我喜欢茶。', prompt='不能弱化的系统规则' * 1000))

    def test_disabled_compression_never_sends_over_budget_json(self):
        disabled = profile(compression={'enabled': False, 'threshold': .82})
        self.assertIsNone(self.prepare('长内容' * 5000, disabled))
        self.assertFalse(self.prepare('我喜欢茶。', disabled).compressed)

    def test_invalid_threshold_or_output_budget_fails_closed(self):
        for selected in (profile(compression={'enabled': True, 'threshold': float('nan')}),
                         profile(max_tokens=4096), profile(context_tokens=100)):
            with self.assertRaises(ValueError):
                self.prepare('我喜欢茶。', selected)

    def test_truncated_last_sentence_is_not_evidence_even_with_a_large_context(self):
        prepared = inputs.prepare_memory_input('我喜欢红茶。以前一直喜欢咖啡', '抽取原话。',
            profile(context_tokens=256000), session_id='room@chatroom', actor_id='wxid_actor',
            message_id='raw-1', source_truncated=True)
        self.assertFalse(prepared.compressed)
        self.assertTrue(prepared.accepts_evidence('我喜欢红茶。'))
        self.assertFalse(prepared.accepts_evidence('以前一直喜欢咖啡'))
        self.assertTrue(json.loads(prepared.messages[-1]['content'])['source_truncated'])


class MemoryStoreCompressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = types.SimpleNamespace(MEMORY_TOP_K=5, MEMORY_SCOPE='session', MEMORY_CONTEXT_CHARS=2400)
        specification = importlib.util.spec_from_file_location('memory_budget_test', SOURCE / 'memory_store.py')
        self.module = importlib.util.module_from_spec(specification)
        with patch.dict(sys.modules, {'config': self.cfg}):
            specification.loader.exec_module(self.module)
        self.store = self.module.MemoryStore(str(Path(self.directory.name) / 'memory.db'))
        self.addCleanup(self.store.close)

    def invoke(self, text, records, selected=None):
        provider = types.SimpleNamespace(profile_settings=lambda name: selected or profile(),
            request=Mock(return_value=json.dumps({'memories': records}, ensure_ascii=False)))
        with patch.dict(sys.modules, {'model_client': provider}):
            result = self.store.extract_memories(text, session_id='native@chatroom',
                actor_id='wxid_original_actor', message_id='raw-original-message')
        return result, provider

    def test_exact_selected_quote_saved_with_original_actor_message_and_one_request(self):
        text = '普通聊天内容' * 900 + '。我喜欢红茶。下次周日再聊天。'
        records = [{'content': '我喜欢红茶', 'evidence': '我喜欢红茶。', 'fact_key': 'tea'},
            {'content': '用户喜欢红茶', 'evidence': '用户喜欢红茶'},
            {'content': '下次周日再聊天', 'evidence': '下次周日再聊天。', 'fact_key': 'appointment'}]
        saved, provider = self.invoke(text, records)
        self.assertEqual(saved, ['我喜欢红茶', '下次周日再聊天'])
        provider.request.assert_called_once()
        self.assertEqual(provider.request.call_args.kwargs['timeout'], 30)
        rows = self.store.recent()
        self.assertEqual({(row['session_id'], row['actor_id'], row['evidence_message_id']) for row in rows},
            {('native@chatroom', 'wxid_original_actor', 'raw-original-message')})
        for row in rows:
            self.assertIn(row['evidence'], text)
            self.assertIn(row['content'], row['evidence'])

    def test_cross_excerpt_or_omitted_evidence_is_rejected_even_when_in_original(self):
        omitted = '这条非常长的普通内容' * 300 + '。'
        text = omitted + '我喜欢红茶。我住在济南。'
        records = [{'content': '我喜欢红茶', 'evidence': '我喜欢红茶。我住在济南。'},
            {'content': omitted[:30], 'evidence': omitted},
            {'content': '我住在济南', 'evidence': '我住在济南。'}]
        saved, provider = self.invoke(text, records)
        self.assertEqual(saved, ['我住在济南'])
        self.assertEqual(provider.request.call_count, 1)

    def test_no_original_block_fits_so_no_paid_request_occurs(self):
        saved, provider = self.invoke('不带标点的超长消息' * 1000, [])
        self.assertEqual(saved, [])
        provider.request.assert_not_called()

    def test_normal_input_never_extracts_more_than_five_facts(self):
        text = ''.join('我喜欢第%d种茶。' % number for number in range(8))
        records = [{'content': '我喜欢第%d种茶' % number, 'evidence': '我喜欢第%d种茶。' % number} for number in range(8)]
        saved, provider = self.invoke(text, records, profile(context_tokens=256000, max_tokens=2048))
        self.assertEqual(len(saved), 5)
        provider.request.assert_called_once()

    def test_long_evidence_cannot_be_truncated_away_from_its_content_in_database(self):
        evidence = 'a' * 8500 + '我喜欢茶'
        self.assertFalse(self.store.add_memory('我喜欢茶', session_id='wxid_original_actor',
            actor_id='wxid_original_actor', evidence=evidence, provenance='extracted'))
        self.assertEqual(self.store.count(), 0)

    @staticmethod
    def negation_after_limit():
        stable, incomplete = '我喜欢红茶。', '我以前一直喜欢咖啡'
        filler = '普通内容。' * ((500 - len(stable) - len(incomplete)) // len('普通内容。'))
        padding = ' ' * (500 - len(stable) - len(filler) - len(incomplete))
        head = stable + filler + padding + incomplete
        assert len(head) == 500
        return head + '，但现在已经不喜欢咖啡了。'

    def test_negation_cut_by_character_limit_does_not_become_a_preference(self):
        self.cfg.MEMORY_EXTRACT_MAX_CHARS = 500
        records = [{'content': '我喜欢红茶', 'evidence': '我喜欢红茶。'},
                   {'content': '我以前一直喜欢咖啡', 'evidence': '我以前一直喜欢咖啡'}]
        saved, provider = self.invoke(self.negation_after_limit(), records, profile(context_tokens=256000))
        self.assertEqual(saved, ['我喜欢红茶'])
        payload = json.loads(provider.request.call_args.args[1][-1]['content'])
        self.assertTrue(payload['source_truncated'])
        self.assertEqual(len(payload['untrusted_message']), 500)

    def test_background_queue_preserves_truncation_flag_and_rejects_partial_preference(self):
        self.cfg.MEMORY_EXTRACT_MAX_CHARS = 500
        records = [{'content': '我喜欢红茶', 'evidence': '我喜欢红茶。'},
                   {'content': '我以前一直喜欢咖啡', 'evidence': '我以前一直喜欢咖啡'}]
        provider = types.SimpleNamespace(profile_settings=lambda name: profile(context_tokens=256000),
            request=Mock(return_value=json.dumps({'memories': records}, ensure_ascii=False)))
        with patch.dict(sys.modules, {'model_client': provider}):
            self.assertTrue(self.store.extract_memories_async(self.negation_after_limit(),
                session_id='wxid_original_actor', actor_id='wxid_original_actor', message_id='raw-background'))
            self.store._extract_q.join()
            self.assertEqual([row['content'] for row in self.store.recent()], ['我喜欢红茶'])
            provider.request.assert_called_once()
            self.store.close()
            self.store._extract_thread.join(1)
            self.assertFalse(self.store._extract_thread.is_alive())


if __name__ == '__main__':
    unittest.main()
