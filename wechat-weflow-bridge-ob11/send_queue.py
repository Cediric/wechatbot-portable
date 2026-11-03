"""Bounded ordered send jobs with truthful delivery states and cancellation."""
from concurrent.futures import Future
from collections import deque
import logging
import queue
import threading
import time

log = logging.getLogger("weflow-bridge")

class SendQueue:
    def __init__(self, limit=128):
        self.q = queue.Queue(maxsize=limit)
        self.lock = threading.RLock()
        self.history = deque(maxlen=200)
        self.closed = threading.Event()
        self.worker = threading.Thread(target=self._work, daemon=True, name="outgoing-send")
        self.worker.start()

    def submit(self, message_id, func, cancel_check):
        future = Future()
        job = {"message_id": message_id, "state": "queued", "created": time.time(), "future": future,
               "_queued_at": time.monotonic()}
        with self.lock:
            self.history.appendleft(job)
            try:
                if self.closed.is_set():
                    raise RuntimeError("发送队列已关闭")
                self.q.put_nowait((job, func, cancel_check))
            except (queue.Full, RuntimeError):
                job["state"] = "rejected"
                future.set_exception(RuntimeError("发送队列已满或关闭"))
        return future

    def _work(self):
        while not self.closed.is_set():
            try:
                job, func, cancel_check = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                with self.lock:
                    job["queue_wait_seconds"] = round(time.monotonic() - job["_queued_at"], 3)
                if not job["future"].set_running_or_notify_cancel():
                    with self.lock:
                        job["state"] = "cancelled"
                    continue
                if self.closed.is_set() or cancel_check():
                    with self.lock:
                        job["state"] = "cancelled"
                    job["future"].set_result(False)
                else:
                    with self.lock:
                        job["state"] = "sending"
                        job["started"] = time.time()
                        job["_started_at"] = time.monotonic()
                    result = func()
                    ok = bool(result)
                    with self.lock:
                        job["state"] = getattr(result,"delivery_state","sent") if ok else "failed"
                    job["future"].set_result(ok)
            except Exception as exc:
                with self.lock:
                    job["state"] = "failed"
                    job["error"] = str(exc)
                job["future"].set_exception(exc)
            finally:
                with self.lock:
                    job["completed"] = time.time()
                    job["send_seconds"] = round(time.monotonic() - job["_started_at"], 3) if "_started_at" in job else 0.0
                log.info("[发送队列耗时] message_id=%s queue_wait_seconds=%.3f send_seconds=%.3f status=%s",
                         job["message_id"], job["queue_wait_seconds"], job["send_seconds"], job["state"])
                self.q.task_done()

    def stats(self):
        with self.lock:
            active = next((job for job in self.history if job["state"] == "sending"), None)
            return {"pending": self.q.qsize(), "limit": self.q.maxsize,
                    "worker_alive": self.worker.is_alive(),
                    "active": {"message_id": active["message_id"], "state": active["state"],
                               "elapsed_seconds": round(max(0, time.monotonic()-active["_started_at"]), 2)} if active else None,
                    "recent": [{k:v for k,v in job.items() if k != "future" and not k.startswith("_")} for job in self.history]}

    def close(self):
        with self.lock:
            self.closed.set()
            while True:
                try:
                    job, _, _ = self.q.get_nowait()
                except queue.Empty:
                    break
                job["state"] = "cancelled"
                if not job["future"].done():
                    job["future"].set_result(False)
                self.q.task_done()
