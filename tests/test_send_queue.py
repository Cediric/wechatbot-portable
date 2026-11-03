"""Ordered send timing and cancelled queued media; no desktop or services."""
import importlib.util
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
spec = importlib.util.spec_from_file_location("timed_send_queue", SOURCE / "send_queue.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SendQueueTests(unittest.TestCase):
    def test_queue_wait_and_actual_send_have_separate_timings(self):
        started, release = threading.Event(), threading.Event()
        now = [10.0]
        first_called, second_called = [], []
        with patch.object(module.time, "monotonic", side_effect=lambda: now[0]):
            queue = module.SendQueue(4)
            self.addCleanup(queue.close)
            def first():
                first_called.append(True)
                started.set()
                release.wait(2)
                return True
            first_future = queue.submit(1, first, lambda: False)
            self.assertTrue(started.wait(2))
            second_future = queue.submit(2, lambda: second_called.append(True) or True, lambda: False)
            now[0] = 13.0
            release.set()
            self.assertTrue(first_future.result(2))
            self.assertTrue(second_future.result(2))
            queue.q.join()
            rows = {row["message_id"]: row for row in queue.stats()["recent"]}
            self.assertEqual(rows[1]["send_seconds"], 3.0)
            self.assertEqual(rows[2]["queue_wait_seconds"], 3.0)
            self.assertEqual(rows[2]["send_seconds"], 0.0)
            self.assertTrue(first_called and second_called)
            self.assertFalse(any(key.startswith("_") for row in rows.values() for key in row))

    def test_expired_queued_voice_never_starts_after_busy_send(self):
        started, release, expired = threading.Event(), threading.Event(), threading.Event()
        queue = module.SendQueue(4)
        self.addCleanup(queue.close)
        def busy():
            started.set()
            release.wait(2)
            return True
        busy_future = queue.submit(1, busy, lambda: False)
        self.assertTrue(started.wait(2))
        voice = queue.submit(2, lambda: self.fail("Expired voice must not start"), expired.is_set)
        expired.set()
        release.set()
        self.assertTrue(busy_future.result(2))
        self.assertFalse(voice.result(2))
        queue.q.join()
        row = queue.stats()["recent"][0]
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(row["send_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
