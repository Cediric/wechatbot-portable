"""Time-based ranking uses original scoped evidence and never forgets by deletion."""
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'wechat-weflow-bridge-ob11'
sys.path.insert(0, str(SOURCE))
DAY = 86400
NOW = 2_000_000_000


class MemoryForgettingTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = str(Path(folder) / 'memory.db')
        self.cfg = types.SimpleNamespace(MEMORY_TOP_K=5, MEMORY_SCOPE='session', MEMORY_CONTEXT_CHARS=4000)
        self.provider = types.SimpleNamespace(request=Mock())
        self.enterContext(patch.dict(sys.modules, {'config': self.cfg, 'model_client': self.provider}))
        spec = importlib.util.spec_from_file_location('memory_age_test', SOURCE / 'memory_store.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.store = self.module.MemoryStore(self.path, extraction_paused=True)
        self.addCleanup(self.store.close)

    def claim(self, content, stamp, message, actor='alice', session='group@chatroom'):
        return self.store.add_memory(content, '聊天', session, actor_id=actor, message_id=message,
            provenance='extracted', evidence='原话：' + content, evidence_at=stamp)

    def ranked(self, now, query='我喜欢红茶'):
        with patch.object(self.module.time, 'time', return_value=now):
            rows = self.store.search(query, session_id='group@chatroom', actor_ids={'alice'})
        return [row['content'] for row in rows]

    def test_curve_changes_rank_over_time_and_can_be_disabled(self):
        self.claim('我喜欢红茶', NOW, 'old')
        self.claim('我喜欢绿茶', NOW + 60 * DAY, 'new')
        self.assertEqual(self.ranked(NOW + DAY)[0], '我喜欢红茶')
        self.assertEqual(self.ranked(NOW + 60 * DAY), ['我喜欢绿茶', '我喜欢红茶'])
        self.cfg.MEMORY_HALF_LIFE_DAYS = 365
        self.assertEqual(self.ranked(NOW + 60 * DAY)[0], '我喜欢红茶')
        self.cfg.MEMORY_HALF_LIFE_DAYS = 1
        self.cfg.MEMORY_FORGETTING_ENABLED = False
        self.assertEqual(self.ranked(NOW + 60 * DAY)[0], '我喜欢红茶')
        self.assertEqual(self.store.count(), 2)
        self.provider.request.assert_not_called()

    def test_verified_person_recalls_recent_profile_without_overlap_across_chats(self):
        self.cfg.MEMORY_SCOPE = 'shared'
        for i, content in enumerate(('我喜欢读科幻', '我喜欢喝乌龙茶', '我喜欢听爵士乐')):
            self.claim(content, NOW + i, str(i), actor='wxid_a', session='wxid_a')
        self.claim('我喜欢吃西瓜', NOW+4, 'other', actor='wxid_b', session='other@chatroom')
        self.store.add_chat('只有原始聊天包含这句话', 'wxid_a', actor_id='wxid_a', message_id='origin')
        def recall(when):
            with patch.object(self.module.time, 'time', return_value=when):
                return self.store.search('你好', session_id='new@chatroom', actor_ids={'wxid_a'})
        self.assertEqual([r['content'] for r in recall(NOW+10)], ['我喜欢听爵士乐', '我喜欢喝乌龙茶'])
        self.assertEqual(recall(NOW+100*DAY), [])
        self.claim('我喜欢读科幻', NOW+101*DAY, 'rementioned', actor='wxid_a', session='third@chatroom')
        rows = recall(NOW+102*DAY)
        self.assertEqual([r['content'] for r in rows], ['我喜欢读科幻'])
        context = self.store.format_context(rows)
        self.assertNotIn('third@chatroom', context)
        self.assertNotIn('原始聊天', context)
        with patch.object(self.module.time, 'time', return_value=NOW+10):
            other = self.store.search('乌龙茶', session_id='wxid_b')
        self.assertEqual([(r['actor_id'], r['content']) for r in other], [('wxid_b','我喜欢吃西瓜')])
        self.provider.request.assert_not_called()

    def test_shared_correction_suppresses_old_value_even_when_only_old_label_matches(self):
        self.cfg.MEMORY_SCOPE = 'shared'
        for source, stamp, text in [('wxid_a', NOW+10, '我喜欢喝奶茶'), ('other@chatroom', NOW, '我喜欢喝红茶')]:
            self.store.add_memory(text, session_id=source, actor_id='wxid_a', evidence=text,
                provenance='extracted', audience='profile', fact_key='fav_drink', message_id=source, evidence_at=stamp)
        with patch.object(self.module.time, 'time', return_value=NOW+100):
            rows = self.store.search('红茶', session_id='here@chatroom', actor_ids={'wxid_a'})
        self.assertEqual([r['content'] for r in rows], ['我喜欢喝奶茶'])

    def test_retrieval_and_replay_do_not_refresh_and_new_scoped_evidence_does(self):
        self.claim('我喜欢红茶', NOW, 'first')
        self.ranked(NOW + 90 * DAY)
        self.claim('我喜欢红茶', NOW + DAY, 'first')
        self.claim('我喜欢红茶', NOW + DAY, 'other', actor='bob')
        self.claim('我喜欢红茶', NOW + DAY, 'other', session='another@chatroom')
        def record():
            rows = self.store.search('红茶', session_id='group@chatroom', actor_ids={'alice'})
            self.assertEqual(len(rows), 1)
            return (rows[0]['evidence_at'], rows[0]['evidence_message_id'])
        self.assertEqual(record(), (NOW, 'first'))
        self.claim('我喜欢红茶', NOW + DAY, 'second')
        self.claim('我喜欢红茶', NOW, 'first')
        self.assertEqual(record(), (NOW + DAY, 'second'))

    def test_explicit_or_manual_confirmation_pins_existing_extraction(self):
        for method in ('manual', 'explicit'):
            content = '我喜欢红茶' + method
            self.claim(content, NOW - 300 * DAY, 'old')
            with patch.object(self.module.time, 'time', return_value=NOW):
                if method == 'manual':
                    self.assertTrue(self.store.add_memory(content, session_id='group@chatroom', actor_id='alice'))
                else:
                    self.assertTrue(self.store.remember_explicit('记住：' + content, session_id='group@chatroom',
                        actor_id='alice', message_id='pin'))
            self.claim(content, NOW + 600 * DAY, 'auto')
            row = next(row for row in self.store.recent() if row['content'] == content)
            self.assertEqual(row['provenance'], method)
            self.assertEqual(row['evidence_at'], NOW)
        self.claim('我喜欢绿茶', NOW + 900 * DAY, 'other')
        for method in ('manual', 'explicit'):
            query = '我喜欢红茶' + method
            self.assertEqual(self.ranked(NOW + 900 * DAY, query)[0], query)
        self.provider.request.assert_not_called()

    def test_delayed_batch_uses_each_quoted_message_after_restart(self):
        old, new = '我喜欢红茶', '我的家乡是武汉'
        for stamp, msg, content in ((NOW, 'old', old), (NOW + 90 * DAY, 'new', new)):
            with patch.object(self.module.time, 'time', return_value=stamp):
                self.store.add_chat(content, 'group@chatroom', actor_id='alice', message_id=msg)
        self.assertEqual(self.store.checkpoint_session('group@chatroom'), 1)
        # Queued jobs must own their evidence even after rolling chat history is pruned.
        self.store._connect().execute('DELETE FROM chat_log')
        self.store._connect().commit()
        self.store.close()
        self.provider.request.return_value = json.dumps({'memories': [
            {'content': text, 'evidence': text} for text in (old, new)]})
        reopened = self.module.MemoryStore(self.path)
        try:
            deadline = time.monotonic() + 4
            while reopened.count() < 2 and time.monotonic() < deadline:
                time.sleep(.02)
            found = {r['content']: (r['evidence_at'], r['evidence_message_id']) for r in reopened.recent()}
            self.assertEqual(found, {old: (NOW, 'old'), new: (NOW + 90 * DAY, 'new')})
            self.provider.request.assert_called_once()
        finally:
            reopened.close()
            if reopened._extract_thread:
                reopened._extract_thread.join(2)


if __name__ == '__main__':
    unittest.main()
