"""Monotonic reply phases with synthetic models, tickets and senders only."""
import ast
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import re
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

import test_stream_runtime as stream_cases

BRIDGE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def monotonic(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def fields(line):
    return dict(re.findall(r"([a-z_]+)=([^ ]+)", line))


class BridgePreparationTimingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.active = True
        self.config = types.SimpleNamespace(GROUP_REPLY_JUDGE_ENABLED=True, HUMANLIKE_DECISION_ENABLED=True,
            VOICE_ENABLED=False, TOOLS_ENABLED=False, MEMORY_ENABLED=True, MEMORY_TOP_K=3, BOT_NICKNAMES=[])
        self.store = types.SimpleNamespace(search=self.search, format_context=lambda values: "",
            add_chat=Mock(), extract_memories_async=Mock())
        self.state = types.SimpleNamespace(reply_state_lock=threading.RLock(), suppress_text_until={},
            pending_reply_modes={}, prune_suppress=lambda: None, mark_response_started=Mock(),
            reply_revision_active=lambda *args: self.active, memory_store=self.store,
            ob_map_lock=threading.RLock(), _ob_id_to_contact={}, _self_id_int=9, group_reply_mode="single")
        self.judge = types.SimpleNamespace(should_reply_group=self.group_decision)
        tree = ast.parse((BRIDGE / "bridge_core.py").read_text(encoding="utf-8-sig"))
        klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WeFlowBridge")
        methods = [node for node in klass.body if isinstance(node, ast.FunctionDef)
                   and node.name in {"_process_sender_body", "_attach_event_identity", "_record_incoming", "_attach_recent_media"}]
        methods.extend(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_conversation_command")
        namespace = dict(time=self.clock, hashlib=hashlib, state=self.state, config=self.config,
            stable_id=types.SimpleNamespace(user_id=lambda *args: 1, group_id=lambda *args: 2),
            reply_judge=self.judge, log=logging.getLogger("ob11-bridge"), _strip_author_prefix=lambda value: value,
            _explicit_tool=lambda value: "none", _explicit_voice=lambda value: False,
            humanize=types.SimpleNamespace(style_prefix=lambda: ""),
            make_message_event=lambda kind, uid, message, **kwargs: dict(message_type=kind, user_id=uid,
                message=message, **kwargs), json=__import__("json"))
        for method in methods:
            if method.decorator_list:
                method.decorator_list = []
            exec(compile(ast.Module(body=[method], type_ignores=[]), str(BRIDGE / "bridge_core.py"), "exec"), namespace)
        self.bridge = types.SimpleNamespace(generation=123, _active=lambda: True, _publish_event=self.publish)
        self.bridge._attach_event_identity = namespace["_attach_event_identity"]
        self.bridge._record_incoming = namespace["_record_incoming"]
        self.bridge._attach_recent_media = types.MethodType(namespace["_attach_recent_media"], self.bridge)
        self.process = types.MethodType(namespace["_process_sender_body"], self.bridge)
        self.events = []
        self.context = types.SimpleNamespace(chat_context=self.chat_context)
        self.humanlike = types.SimpleNamespace(choose=self.private_decision)
        modules = patch.dict(sys.modules, {"interaction_context": self.context, "humanlike_decision": self.humanlike})
        modules.start(); self.addCleanup(modules.stop)

    def chat_context(self, *args, **kwargs):
        self.clock.advance(1)
        return {"current_message": "私密正文"}

    def search(self, *args, **kwargs):
        self.clock.advance(.5)
        return []

    def private_decision(self, *args, **kwargs):
        self.clock.advance(2)
        return True, "text", "私密原因", "", ""

    def group_decision(self, *args, **kwargs):
        self.clock.advance(3)
        return True, "text", "私密原因", "", "none"

    def publish(self, event, mode):
        self.clock.advance(.2)
        event["wechat_reply_request"] = {"request_id": "synthetic-ticket", "revision": 1}
        self.events.append(event)
        return True

    def entry(self, group=False):
        return dict(contact="私密昵称", is_group=group, session_id_data="wxid_private",
            sender_username="wxid_private", source_name="私密昵称", group_name="私密群名",
            memory_records=[dict(at=98.0, reply_revision=1, sender_username="wxid_private", text="私密正文")],
            identity_records=[dict(username="wxid_private")])

    def test_private_and_group_phases_use_receive_time_and_hashed_ticket(self):
        for group, decision in ((False, 2), (True, 3)):
            with self.subTest(group=group):
                self.clock.value = 100
                with self.assertLogs("ob11-bridge", level="INFO") as logs:
                    self.process("wxid_private", self.entry(group), ["私密正文"])
                line = next(line for line in logs.output if "stage=bridge_prepare" in line)
                measured = fields(line)
                self.assertEqual(measured["turn"], hashlib.sha256(b"synthetic-ticket").hexdigest()[:12])
                self.assertEqual(measured["chat"], "group" if group else "private")
                self.assertEqual(measured["decision_role"], "group_reply_judge" if group else "moments_decision")
                for name, expected in (("buffer_seconds", 2), ("decision_seconds", decision),
                                       ("context_seconds", 1.5), ("push_seconds", .2), ("seconds", 3.7 + decision)):
                    self.assertAlmostEqual(float(measured[name]), expected)
                self.assertEqual(self.events[-1]["wechat_received_monotonic"], 98)
                self.state.mark_response_started.assert_called_with(1 if not group else 2, 123, 98)
                for private in ("wxid_private", "私密正文", "私密昵称", "私密群名", "synthetic-ticket"):
                    self.assertNotIn(private, line)

    def test_new_revision_during_decision_does_not_publish_success_timing(self):
        def cancel_during_decision(*args, **kwargs):
            self.clock.advance(2)
            self.active = False
            return True, "text", "", "", ""
        self.humanlike.choose = cancel_during_decision
        with patch("logging.Logger.info") as info:
            self.process("wxid_private", self.entry(), ["私密正文"])
        self.assertEqual(self.events, [])
        self.assertFalse(any("stage=bridge_prepare" in call.args[0] for call in info.call_args_list))


class ReplySendTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stream_cases.StreamProtocolTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        stream_cases.StreamProtocolTests.tearDownClass.__func__(cls)

    def setUp(self):
        stream_cases.StreamProtocolTests.setUp(self)
        self.clock = Clock()
        self.time_patch = patch.object(self.protocol, "time", self.clock)
        self.time_patch.start(); self.addCleanup(self.time_patch.stop)
        self.record = self.ticket_state._reply_requests[self.ticket["request_id"]]
        self.record["started"] = 90.0
        self.state.dispatch_send.side_effect = lambda *args, **kwargs: self.clock.advance(.4) or True

    send_packet = stream_cases.StreamProtocolTests.send_packet

    def test_only_first_confirmed_text_logs_receive_to_completion_and_retries_do_not_log_again(self):
        with self.assertLogs("ob11-bridge", level="INFO") as logs:
            self.assertEqual(self.send_packet(text="私密正文。")['status'], 'ok')
            self.assertEqual(self.send_packet(text="私密正文。")['status'], 'ok')
            self.clock.value = 106
            self.assertEqual(self.send_packet(text="后续句子。", seq=1)['status'], 'ok')
        timing = [fields(line) for line in logs.output if "[耗时]" in line]
        self.assertEqual([row['stage'] for row in timing], ['reply_first_text_done', 'reply_text_done'])
        self.assertAlmostEqual(float(timing[0]['seconds']), 10.4)
        self.assertAlmostEqual(float(timing[0]['received_to_send_seconds']), 10)
        self.assertAlmostEqual(float(timing[1]['seconds']), .4)
        self.assertTrue(all(float(row['send_seconds']) == .4 for row in timing))
        turn = hashlib.sha256(self.ticket['request_id'].encode()).hexdigest()[:12]
        self.assertTrue(all(row['turn'] == turn for row in timing))
        for line in logs.output:
            for private in (self.ticket['request_id'], 'wxid_a', '私密正文', '后续句子'):
                self.assertNotIn(private, line)

    def test_cancelled_revision_and_failed_dispatch_have_no_success_timing(self):
        self.ticket_state.advance_reply_revision(1, False, 123)
        with patch.object(self.protocol.log, 'info') as info:
            self.assertEqual(self.send_packet()['status'], 'failed')
        info.assert_not_called()
        self.state.dispatch_send.assert_not_called()
        self.record['revision'] += 1
        self.ticket['revision'] += 1
        self.state.dispatch_send.side_effect = lambda *args, **kwargs: False
        with patch.object(self.protocol.log, 'info') as info:
            self.assertEqual(self.send_packet()['status'], 'failed')
        info.assert_not_called()
        self.assertNotIn('_first_text_timing_logged', self.record)

    def test_new_turn_uses_its_own_receive_baseline(self):
        self.send_packet()
        self.clock.value = 110
        event = stream_cases.event_value()
        event['wechat_received_monotonic'] = 109
        revision = self.ticket_state.advance_reply_revision(1, False, 123)
        self.ticket = self.ticket_state.register_reply_request(event, 123, {'reply_type': 'text'}, revision)
        with self.assertLogs('ob11-bridge', level='INFO') as logs:
            self.send_packet()
        measured = fields(next(line for line in logs.output if 'reply_first_text_done' in line))
        self.assertAlmostEqual(float(measured['seconds']), 1.4)

    def test_revision_cancelled_during_dispatch_is_not_logged_as_current_success(self):
        def superseded_dispatch(*args, **kwargs):
            self.clock.advance(.4)
            self.ticket_state.advance_reply_revision(1, False, 123)
            return True
        self.state.dispatch_send.side_effect = superseded_dispatch
        with patch.object(self.protocol.log, 'info') as info:
            self.send_packet()
        info.assert_not_called()
        self.assertNotIn('_first_text_timing_logged', self.record)

    def test_group_first_text_has_group_scope_and_nonnegative_elapsed(self):
        event = stream_cases.event_value(group=True)
        event['wechat_received_monotonic'] = 101  # A malformed future baseline cannot produce a negative duration.
        revision = self.ticket_state.advance_reply_revision(1, True, 123)
        ticket = self.ticket_state.register_reply_request(event, 123, {'reply_type': 'text'}, revision)
        with self.assertLogs('ob11-bridge', level='INFO') as logs:
            result = __import__('asyncio').run(self.protocol._handle_ob_api({'action': 'send_group_msg', 'params': {
                'group_id': 1, 'message': [{'type': 'text', 'data': {'text': '群里一句。'}}],
                'wechat_stream': {**ticket, 'stream_id': 's', 'seq': 0, 'final': False}}}, generation=456))
        self.assertEqual(result['status'], 'ok')
        measured = fields(next(line for line in logs.output if 'reply_first_text_done' in line))
        self.assertEqual(measured['chat'], 'group')
        self.assertEqual(float(measured['seconds']), 0)
        self.assertGreaterEqual(float(measured['send_seconds']), 0)


class PrivateDecisionTimingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cancel = threading.Event()
        self.response = '{"should_reply":true,"reply_type":"text","reason":"私密判断原因"}'
        model = types.SimpleNamespace(persona_prompt=lambda: '私密角色卡', request=self.request)
        modules = patch.dict(sys.modules, {'config': types.SimpleNamespace(HUMANLIKE_DECISION_ENABLED=True),
            'interaction_context': types.SimpleNamespace(compact_context=lambda *args, **kwargs: '私密输入'),
            'model_client': model})
        modules.start(); self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location('_timing_humanlike', BRIDGE / 'humanlike_decision.py')
        self.decision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.decision)
        self.decision.time = self.clock

    def request(self, *args, **kwargs):
        self.clock.advance(3)
        return self.response

    def test_decision_duration_and_fixed_outcome_without_input_output_text(self):
        with self.assertLogs('ob11-bridge', level='INFO') as logs:
            result = self.decision.choose({'current_message': '私密正文'}, cancel=self.cancel)
        self.assertTrue(result[0])
        measured = fields(logs.output[0])
        self.assertEqual(measured['model'], 'moments_decision')
        self.assertEqual(float(measured['seconds']), 3)
        self.assertEqual(measured['result'], 'reply')
        self.assertEqual(int(measured['response_chars']), len(self.response))
        self.assertNotIn('私密', logs.output[0])

    def test_cancel_during_request_is_marked_cancelled(self):
        original = self.request
        def cancel_request(*args, **kwargs):
            text = original(*args, **kwargs)
            self.cancel.set()
            return text
        with patch.dict(sys.modules, {'model_client': types.SimpleNamespace(persona_prompt=lambda: '', request=cancel_request)}):
            with self.assertLogs('ob11-bridge', level='INFO') as logs:
                result = self.decision.choose({}, cancel=self.cancel)
        self.assertFalse(result[0])
        self.assertEqual(result[2], 'cancelled')
        self.assertEqual(fields(logs.output[-1])['result'], 'cancelled')

    def test_decision_drafts_are_ignored_and_legacy_mixed_mode_is_voice_only(self):
        self.response = json.dumps({'should_reply': True, 'reply_type': 'text_voice',
            'text': 'DECISION_TEXT_CANARY', 'voice_text': 'DECISION_VOICE_CANARY',
            'image_prompt': 'DECISION_IMAGE_CANARY', 'tone': 'warm'})
        result = self.decision.choose({'current_message': '合成请求'}, cancel=self.cancel)
        self.assertEqual(result[:2], (True, 'voice'))
        self.assertNotIn('CANARY', str(result))


if __name__ == '__main__':
    unittest.main()
