"""Measured performance summaries only: synthetic clocks, senders, and receipts."""
import json
import types
import unittest
from unittest.mock import Mock, call, patch

import test_stream_runtime as stream_cases


class ResponsePerformanceTests(unittest.TestCase):
    def setUp(self):
        self.state = stream_cases.fresh_state()

    def test_stage_summary_uses_nearest_rank_p95_and_keeps_end_to_end_separate(self):
        for value in range(1, 21):
            self.state.note_performance_sample("first_text", value)
        self.state.note_performance_sample("wechat_send", .25)
        summary = self.state.response_performance_snapshot()
        self.assertEqual(summary["stages"]["first_text"]["sample_count"], 20)
        self.assertEqual(summary["stages"]["first_text"]["latest_seconds"], 20)
        self.assertEqual(summary["stages"]["first_text"]["average_seconds"], 10.5)
        self.assertEqual(summary["stages"]["first_text"]["p95_seconds"], 19)
        self.assertEqual(summary["stages"]["wechat_send"]["average_seconds"], .25)
        self.assertIsNone(summary["stages"]["first_voice"]["latest_seconds"])
        self.assertEqual(summary["verification"], "local_ui_accepted")

    def test_only_recent_two_hundred_valid_measurements_are_kept(self):
        for bad in (-1, float("nan"), float("inf"), True, "3"):
            self.assertFalse(self.state.note_performance_sample("first_text", bad))
        self.assertFalse(self.state.note_performance_sample("private-body-canary", 2))
        for value in range(1, 301):
            self.state.note_performance_sample("first_text", value)
        summary = self.state.response_performance_snapshot()
        self.assertEqual(summary["stages"]["first_text"]["sample_count"], 200)
        self.assertEqual(summary["stages"]["first_text"]["average_seconds"], 200.5)
        self.assertEqual(summary["stages"]["first_text"]["p95_seconds"], 290)
        self.assertNotIn("private-body-canary", json.dumps(summary))

    def test_queue_snapshot_polling_does_not_resample_or_invent_end_to_end_latency(self):
        queue = {"recent": [{"queue_wait_seconds": 2, "state": "failed", "started": 20},
                            {"queue_wait_seconds": 1, "state": "sent", "started": 10},
                            {"state": "queued", "created": 30}]}
        first = self.state.response_performance_snapshot(queue)
        second = self.state.response_performance_snapshot(queue)
        self.assertEqual(first, second)
        self.assertEqual(first["stages"]["queue_wait"]["sample_count"], 2)
        self.assertEqual(first["stages"]["queue_wait"]["latest_seconds"], 2)
        self.assertEqual(first["stages"]["queue_wait"]["average_seconds"], 1.5)
        self.assertEqual(first["stages"]["first_text"]["sample_count"], 0)

    def test_unconfirmed_send_does_not_enter_confirmed_ui_summary(self):
        self.state.running = True
        self.state.sender_instance = types.SimpleNamespace(send_image=Mock(return_value=False))
        self.assertFalse(self.state.dispatch_send("send_image", "private-name-canary", "synthetic.png", generation=123))
        self.assertEqual(self.state.response_performance_snapshot()["stages"]["wechat_send"]["sample_count"], 0)
        self.state.sender_instance.send_image.return_value = True
        self.assertTrue(self.state.dispatch_send("send_image", "private-name-canary", "synthetic.png", generation=123))
        result = self.state.response_performance_snapshot()
        self.assertEqual(result["stages"]["wechat_send"]["sample_count"], 1)
        self.assertNotIn("private-name-canary", json.dumps(result))


class PerformanceHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stream_cases.StreamProtocolTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        stream_cases.StreamProtocolTests.tearDownClass.__func__(cls)

    setUp = stream_cases.StreamProtocolTests.setUp

    def test_hooks_sample_real_receive_baseline_once_and_ready_voice_only(self):
        sample = Mock()
        request = {"request_id": "private-ticket-canary", "started": 10}
        with patch.object(self.state, "note_performance_sample", sample, create=True):
            self.protocol._note_text_timing(request, False, "private-body-canary", 19, 20)
            self.protocol._note_text_timing(request, False, "private-body-canary", 21, 22)
            self.protocol._note_voice_prepare(request, False, "private-body-canary", 22, 24, "failed")
            self.protocol._note_voice_prepare(request, False, "private-body-canary", 22, 24, "ready")
            self.protocol._note_voice_send(request, False, "private-body-canary", 25, 28, 30)
            self.protocol._note_text_timing({"request_id": "future", "started": 100}, False, "x", 20, 21)
        self.assertEqual(sample.call_args_list, [call("first_text", 10), call("voice_prepare", 2),
                                               call("first_voice", 20), call("voice_queue", 3)])


if __name__ == "__main__":
    unittest.main()
