"""OneBot readiness and forwarding checks use only in-memory transports."""
import asyncio
from contextlib import ExitStack
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

BRIDGE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"


def run_inline(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError("In-memory test unexpectedly waited for asynchronous I/O")


class FakeTask:
    def __init__(self, coroutine):
        self.coroutine = coroutine
    def cancel(self):
        self.coroutine.close()


class OneBotClientTests(unittest.TestCase):
    def setUp(self):
        self.state = types.ModuleType("state")
        self.state.__dict__.update(running=True, client_generation=7, bridge_generation=3,
            run_lock=threading.RLock(), paused=threading.Event(), _ob_ws=None,
            _ob_ws_ready=threading.Event(), _self_id_int=123, outbox=None,
            record_activity=Mock(), increment=Mock(), prepare_outbox_reply=Mock(return_value=True))
        config = types.ModuleType("config")
        config.ASTRBOT_OB_URL = "ws://127.0.0.1:11229/?PRIVATE_URL_CANARY"
        config.MEDIA_MAX_MB = 5
        protocol = types.ModuleType("ob_protocol")
        async def api(*args, **kwargs):
            pass
        protocol._handle_ob_api = api
        self.ws_module = types.ModuleType("websockets")
        self.ws_module.connect = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"config": config, "state": self.state, "ob_protocol": protocol,
                                      "websockets": self.ws_module}):
            spec = importlib.util.spec_from_file_location("isolated_ob_client", BRIDGE / "ob_client.py")
            self.client = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.client)
        self.tasks, self.sleeps = [], []
        self.send_error = None
        self.change_generation = False
        self.observations = []
        test = self
        class FakeWS:
            async def send(self, raw):
                test.observations.append((json.loads(raw), test.state._ob_ws_ready.is_set(), len(test.tasks)))
                if test.change_generation:
                    test.state.client_generation += 1
                if test.send_error:
                    raise test.send_error
            def __aiter__(self):
                return self
            async def __anext__(self):
                test.observations.append(("receive", test.state._ob_ws_ready.is_set(), len(test.tasks)))
                test.state.running = False
                raise StopAsyncIteration
            async def close(self):
                pass
        self.ws = FakeWS()

    def run_client(self, refused_first=False):
        test = self
        entered = [0]
        class Connection:
            async def __aenter__(self):
                entered[0] += 1
                if refused_first and entered[0] == 1:
                    raise ConnectionRefusedError("synthetic cold start")
                return test.ws
            async def __aexit__(self, *args):
                pass
        def connect(url, extra_headers=None, **kwargs):
            return Connection()
        def task(coroutine):
            value = FakeTask(coroutine)
            self.tasks.append(value)
            return value
        async def gather(*args, **kwargs):
            return []
        async def sleep(seconds):
            self.sleeps.append(seconds)
            if not refused_first or entered[0] > 1:
                self.state.running = False
        with patch.object(self.ws_module, "connect", connect), \
             patch.object(self.client.asyncio, "create_task", task), \
             patch.object(self.client.asyncio, "gather", gather), \
             patch.object(self.client.asyncio, "sleep", sleep), \
             patch.object(self.client.log, "info") as info:
            run_inline(self.client._ob_client_main(7))
        return info, entered[0]

    def test_ready_flag_and_workers_wait_for_successful_lifecycle_frame(self):
        info, _ = self.run_client()
        frame, ready_during_send, tasks_during_send = self.observations[0]
        self.assertEqual((frame["post_type"], frame["meta_event_type"], frame["sub_type"]),
                         ("meta_event", "lifecycle", "connect"))
        self.assertFalse(ready_during_send)
        self.assertEqual(tasks_during_send, 0)
        self.assertEqual(self.observations[1], ("receive", True, 6))
        self.assertFalse(self.state._ob_ws_ready.is_set())
        self.assertIsNone(self.state._ob_ws)
        info.assert_called_once_with("AstrBot OneBot 已连接，生命周期握手已发送。")
        self.assertNotIn("PRIVATE_URL_CANARY", str(info.call_args_list))

    def test_failed_lifecycle_frame_never_marks_ready_or_starts_writer(self):
        self.send_error = OSError("synthetic frame failure")
        info, _ = self.run_client()
        self.assertEqual(len(self.observations), 1)
        self.assertEqual(self.tasks, [])
        self.assertFalse(self.state._ob_ws_ready.is_set())
        self.state.record_activity.assert_not_called()
        info.assert_not_called()
        self.assertEqual(self.sleeps, [0.5])

    def test_generation_change_during_lifecycle_send_does_not_mark_new_client_ready(self):
        self.change_generation = True
        info, _ = self.run_client()
        self.assertEqual(self.state.client_generation, 8)
        self.assertFalse(self.state._ob_ws_ready.is_set())
        self.assertEqual(self.tasks, [])
        info.assert_not_called()

    def test_cold_connection_refusal_retries_then_records_single_fixed_success(self):
        info, attempts = self.run_client(refused_first=True)
        self.assertEqual(attempts, 2)
        self.assertEqual(self.sleeps, [0.5])
        info.assert_called_once_with("AstrBot OneBot 已连接，生命周期握手已发送。")
        self.assertEqual(self.observations[1], ("receive", True, 6))

    def run_writer(self, failure=False, prepare=True, ticket=True):
        event = {"message": "PRIVATE_BODY_CANARY", "user_id": "PRIVATE_ID_CANARY"}
        if ticket:
            event["wechat_reply_request"] = {"request_id": "PRIVATE_TICKET_CANARY"}
        outbox = Mock()
        def peek(wait_seconds=0):
            if outbox.peek.call_count == 1:
                return 1, event
            self.state.running = False
            return None
        outbox.peek.side_effect = peek
        self.state.outbox = outbox
        self.state.prepare_outbox_reply.return_value = prepare
        self.state.running = True
        self.send_error = OSError("synthetic send failure") if failure else None
        async def threaded(function, *args):
            return function(*args)
        async def sleep(seconds):
            self.state.running = False
        with patch.object(self.client.asyncio, "to_thread", threaded), \
             patch.object(self.client.asyncio, "sleep", sleep), \
             patch.object(self.client.time, "monotonic", side_effect=[10.0, 10.25]), \
             patch.object(self.client.log, "info") as info:
            if failure:
                with self.assertRaisesRegex(OSError, "send failure"):
                    run_inline(self.client._outbox_writer(self.ws, 7))
            else:
                run_inline(self.client._outbox_writer(self.ws, 7))
        return outbox, info

    def test_forward_timing_is_recorded_after_ws_send_with_only_ticket_hash(self):
        outbox, info = self.run_writer()
        outbox.acknowledge.assert_called_once_with(1)
        self.state.increment.assert_called_once_with("forwarded")
        turn = hashlib.sha256(b"PRIVATE_TICKET_CANARY").hexdigest()[:12]
        info.assert_called_once_with("[耗时] turn=%s stage=astrbot_forward seconds=%.3f result=websocket_sent", turn, 0.25)
        for private in ("PRIVATE_BODY_CANARY", "PRIVATE_ID_CANARY", "PRIVATE_TICKET_CANARY", "PRIVATE_URL_CANARY"):
            self.assertNotIn(private, str(info.call_args_list))

    def test_failed_ws_send_keeps_outbox_and_has_no_success_timing(self):
        outbox, info = self.run_writer(failure=True)
        outbox.acknowledge.assert_not_called()
        self.state.increment.assert_not_called()
        info.assert_not_called()

    def test_cancelled_ticket_is_removed_without_frame_or_forward_timing(self):
        outbox, info = self.run_writer(prepare=False)
        self.assertEqual(self.observations, [])
        outbox.acknowledge.assert_called_once_with(1)
        self.state.increment.assert_called_once_with("cancelled")
        info.assert_not_called()

    def test_legacy_event_is_forwarded_without_inventing_ticket(self):
        outbox, info = self.run_writer(ticket=False)
        outbox.acknowledge.assert_called_once_with(1)
        self.state.increment.assert_called_once_with("forwarded")
        info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
