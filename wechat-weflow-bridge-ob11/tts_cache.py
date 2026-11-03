"""Completed speech content cache and per-key, process-local single flight."""
from collections import deque
from concurrent.futures import Future, TimeoutError
import hashlib
import json
import math
import os
import threading
import time


def _cache_path(identity, directory):
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return os.path.join(directory, "tts_0_" + digest[:32] + ".wav")


def _read_fresh(path, retention_seconds, validate, max_bytes):
    try:
        if not os.path.islink(path) and time.time() - os.path.getmtime(path) < retention_seconds:
            with open(path, "rb") as stream:
                return validate(stream.read(max_bytes + 1))
    except (OSError, ValueError):
        pass  # Missing, expired or damaged files are not cache hits.
    return None


class SpeechCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._flights = {}
        self._hits = self._misses = self._waits = self._failures = self._count = 0
        self._total = self._last = 0.0
        self._samples = deque(maxlen=200)

    def get_or_create(self, identity, directory, retention_seconds, render, validate, write, max_bytes, *,
                      cancelled=None, previous_identity=None):
        if cancelled is not None and cancelled():
            return None
        directory = os.path.realpath(directory)
        # Preserve the existing preview's managed tts_<number>_<32hex>.wav ID.
        path = _cache_path(identity, directory)
        key = path
        with self._lock:
            future = self._flights.get(key)
            owner = future is None
            if owner:
                future = self._flights[key] = Future()
            else:
                self._waits += 1
        if not owner:
            # A cancelled follower can release its reply pipeline immediately;
            # the owner still finishes the one shared synthesis and caches it.
            if cancelled is None:
                result = future.result()
            else:
                while True:
                    if cancelled():
                        return None
                    try:
                        result = future.result(timeout=.1)
                        break
                    except TimeoutError:
                        if future.done():
                            # Completion may race the wait timeout; preserve
                            # the actual result or synthesis exception.
                            result = future.result()
                            break
                if cancelled():
                    return None
            if result:
                with self._lock:
                    self._hits += 1
            return result
        try:
            cached = _read_fresh(path, retention_seconds, validate, max_bytes)
            if cached is not None:
                try:
                    os.utime(path, None)
                except OSError:
                    pass  # A readable valid cache hit does not need another paid call.
            elif previous_identity is not None:
                old_path = _cache_path(previous_identity, directory)
                cached = _read_fresh(old_path, retention_seconds, validate, max_bytes)
                if cached is not None:
                    write(path, cached)  # Apply the current WAV processing once.
            if cached is not None:
                with self._lock:
                    self._hits += 1
                future.set_result(path)
                return path
            with self._lock:
                self._misses += 1
            started = time.monotonic()
            try:
                audio = render()
                result = write(path, validate(audio)) if audio else None
            except BaseException:
                with self._lock:
                    self._failures += 1
                raise
            elapsed = max(0.0, time.monotonic()-started)
            with self._lock:
                if result:
                    self._count += 1
                    self._total += elapsed
                    self._last = elapsed
                    self._samples.append(elapsed)
                else:
                    self._failures += 1
            future.set_result(result)
            return result
        except BaseException as error:
            future.set_exception(error)
            raise
        finally:
            with self._lock:
                self._flights.pop(key, None)

    def stats(self):
        with self._lock:
            samples = sorted(self._samples)
            return {"hits": self._hits, "misses": self._misses, "coalesced_waits": self._waits,
                "synthesis_count": self._count, "failures": self._failures,
                "synthesis_seconds_total": round(self._total, 3),
                "synthesis_seconds_average": round(self._total/self._count, 3) if self._count else 0.0,
                "synthesis_window_average_seconds": round(sum(samples)/len(samples), 3) if samples else 0.0,
                "last_synthesis_seconds": round(self._last, 3),
                "synthesis_p95_seconds": round(samples[math.ceil(.95*len(samples))-1], 3) if samples else 0.0,
                "synthesis_samples": len(samples),
                "hit_rate": round(self._hits/(self._hits+self._misses), 4) if self._hits+self._misses else 0.0,
                "inflight": len(self._flights)}


cache = SpeechCache()


def stats():
    """Small metadata-only snapshot; never scan files or expose cache identity."""
    return cache.stats()
