"""共享 HTTP Session：连接复用，减少每次请求的 TCP/TLS 握手。"""

from __future__ import annotations

import atexit
import threading
import weakref

import requests
from requests.adapters import HTTPAdapter

_lock = threading.Lock()
_local = threading.local()
_sessions = weakref.WeakSet()


def _make_session(pool: int) -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=0)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def session() -> requests.Session:
    """Reuse connections per worker, isolating mutable cookie/session state."""
    current = getattr(_local, "session", None)
    if current is None:
        current = _make_session(6)
        _local.session = current
        with _lock:
            _sessions.add(current)
    return current


def new_session(pool: int = 2) -> requests.Session:
    """独立 Session，给 SSE 这类长连接用，避免占满共享连接池。"""
    return _make_session(pool)


def _close():
    with _lock:
        current = list(_sessions)
        _sessions.clear()
    for item in current:
        try:
            item.close()
        except Exception:
            pass
    if hasattr(_local, "session"):
        del _local.session


atexit.register(_close)
