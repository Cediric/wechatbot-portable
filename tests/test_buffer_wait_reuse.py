"""Execute real release methods with a frozen clock, no scheduler threads."""
import ast
from collections import deque
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1] / 'wechat-weflow-bridge-ob11/bridge_core.py'


class BufferWaitReuseTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.config = types.SimpleNamespace(BUFFER_SECONDS=2, SMART_BUFFER_ENABLED=True,
            PRIVATE_BUFFER_SECONDS=.35, GROUP_BUFFER_SECONDS=.7, BUFFER_MAX_WAIT=2)
        namespace = {'config': self.config, 'time': types.SimpleNamespace(monotonic=lambda: self.now)}
        tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'WeFlowBridge')
        for method in owner.body:
            if isinstance(method, ast.FunctionDef) and method.name in {'_buffer_delay', '_release_buffer'}:
                exec(compile(ast.Module(body=[method], type_ignores=[]), str(SOURCE), 'exec'), namespace)
        self.bridge = types.SimpleNamespace(buffer_lock=threading.RLock(), pending_buffers={},
            _active=Mock(return_value=True), _arm=Mock(), _sched=types.SimpleNamespace(cancel=Mock()), _discard_entry_media=Mock())
        self.bridge._buffer_delay = types.MethodType(namespace['_buffer_delay'], self.bridge)
        self.release = types.MethodType(namespace['_release_buffer'], self.bridge)

    def entry(self, *, group=False, first=999.1, last=999.4, ready=True):
        value = {'processing': True, 'messages': [{'at': last, 'ready': ready}], 'retry_events': deque(),
                 'first_at': first, 'is_group': group}
        self.bridge.pending_buffers['native_session'] = value
        return value

    def delay(self):
        self.release('native_session')
        return self.bridge._arm.call_args.args[2]

    def test_private_and_group_already_quiet_bursts_do_not_wait_another_window(self):
        for group in (False, True):
            self.entry(group=group, last=999.1)
            self.assertEqual(self.delay(), .05)

    def test_recent_private_message_waits_only_unspent_debounce(self):
        self.entry(first=999.7, last=999.9)
        self.assertAlmostEqual(self.delay(), .25)

    def test_recent_group_message_keeps_group_debounce(self):
        self.entry(group=True, first=999.7, last=999.9)
        self.assertAlmostEqual(self.delay(), .6)

    def test_max_wait_deadline_is_not_subtracted_twice(self):
        self.entry(first=998.2, last=999.9)
        self.assertAlmostEqual(self.delay(), .2)

    def test_smart_buffer_disabled_keeps_legacy_cap(self):
        self.config.SMART_BUFFER_ENABLED = False
        self.entry(last=990)
        self.assertEqual(self.delay(), 2)

    def test_unready_media_at_front_keeps_following_text_in_order(self):
        entry = self.entry(ready=False)
        entry['messages'].append({'at': 999, 'ready': True})
        self.release('native_session')
        self.bridge._arm.assert_not_called()

    def test_retry_event_stays_at_existing_retry_interval(self):
        self.entry()['retry_events'].append({'message_id': 'durable-existing-event'})
        self.assertEqual(self.delay(), 1)


if __name__ == '__main__':
    unittest.main()
