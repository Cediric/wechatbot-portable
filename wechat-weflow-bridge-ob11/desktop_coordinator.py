"""One desktop transaction at a time, with cancellable waits and clipboard leases.

The lock is shared by chat, voice and Moments senders. Clipboard restoration is
conditional: another application's newer clipboard always wins. Common HGLOBAL
formats (text, file lists and DIB images) are preserved; unsupported handle formats
are left alone instead of claiming that a lossy restoration was successful.
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("weflow-bridge")
DESKTOP_LOCK = threading.RLock()
_activity_lock = threading.RLock()
_activity = None
_external = {}


def desktop_progress():
    """Primitive progress only; never ask a UIA provider from the monitor."""
    current = _activity
    now = time.monotonic()
    if current and current["thread"] == threading.get_ident() and now-current["progress"] >= .25:
        with _activity_lock:
            if _activity is current:
                current["progress"] = now


def external_state(label, cleanup=None):
    token = object()
    with _activity_lock:
        _external[token] = (label, cleanup)
    return token


def release_external_state(token):
    with _activity_lock:
        _external.pop(token, None)


def restore_external_states():
    """Best effort rollback; an unconfirmed voice state is never guessed away."""
    with _activity_lock:
        pending = list(_external.items())
    for token, (_, cleanup) in pending:
        if cleanup is not None and cleanup() is True:
            release_external_state(token)
    with _activity_lock:
        return not _external


def desktop_snapshot():
    now = time.monotonic()
    with _activity_lock:
        current = dict(_activity) if _activity else None
        return {"active": current is not None,
                "elapsed_seconds": round(max(0, now-current["started"]), 2) if current else 0,
                "idle_seconds": round(max(0, now-current["progress"]), 2) if current else 0,
                "external_state": sorted({item[0] for item in _external.values()})}


class DesktopCancelled(RuntimeError):
    pass


class ClipboardChanged(OSError):
    """The conditional adapter write returned False without changing data."""


def check_cancel(cancelled=None):
    desktop_progress()
    if cancelled is not None and cancelled():
        raise DesktopCancelled("发送已取消或运行代次已变更")


def native_top_windows(auto, *, pid=0, user=None):
    """Enumerate native window facts before opening any UIA provider.

    Synthetic providers return None so offline fixtures stay independent of the
    actual desktop. An actual native lookup never falls back to asking every
    application's UIA provider for its children.
    """
    if user is None:
        if os.name != "nt" or not (getattr(auto, "__name__", "") == "uiautomation"
                or callable(getattr(auto, "GetForegroundWindow", None))):
            return None
        user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)
    user.EnumWindows.argtypes = [callback_type, ctypes.c_ssize_t]
    user.EnumWindows.restype = ctypes.c_int
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user.GetWindowThreadProcessId.restype = ctypes.c_uint32
    user.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user.GetClassNameW.restype = ctypes.c_int
    user.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user.IsWindowVisible.restype = ctypes.c_int
    windows = []
    def observe(hwnd, unused):
        owner = ctypes.c_uint32()
        if (user.IsWindowVisible(hwnd)
                and user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                and (not pid or owner.value == pid)):
            name = ctypes.create_unicode_buffer(256)
            user.GetClassNameW(hwnd, name, len(name))
            windows.append({"hwnd": int(hwnd), "pid": owner.value, "class": name.value})
        return 1
    user.EnumWindows(callback_type(observe), 0)
    return windows


@contextmanager
def desktop_operation(cancelled=None):
    global _activity
    while not DESKTOP_LOCK.acquire(timeout=0.05):
        check_cancel(cancelled)
    outer = False
    try:
        with _activity_lock:
            outer = _activity is None
            if outer:
                _activity = {"thread": threading.get_ident(), "started": time.monotonic(), "progress": time.monotonic()}
        check_cancel(cancelled)
        yield
    finally:
        with _activity_lock:
            if outer:
                _activity = None
        DESKTOP_LOCK.release()


def wait_until(predicate, timeout=2.5, cancelled=None, interval=0.06):
    """Poll observable UI state; do not pay a fixed delay after every operation."""
    deadline = time.monotonic() + timeout
    while True:
        check_cancel(cancelled)
        value = predicate()
        if value:
            return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(interval, remaining))


class NativeClipboard:
    """Small 64-bit-safe clipboard adapter; injectable in isolated tests."""
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows clipboard unavailable")
        self.user = ctypes.windll.user32
        self.kernel = ctypes.windll.kernel32
        self.user.CreateWindowExW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
                                             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.user.CreateWindowExW.restype = ctypes.c_void_p
        self.user.DestroyWindow.argtypes = [ctypes.c_void_p]
        self.user.OpenClipboard.argtypes = [ctypes.c_void_p]
        self.user.GetClipboardData.argtypes = [ctypes.c_uint]
        self.user.GetClipboardData.restype = ctypes.c_void_p
        self.user.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        self.user.SetClipboardData.restype = ctypes.c_void_p
        self.user.GetClipboardFormatNameW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int]
        self.kernel.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        self.kernel.GlobalAlloc.restype = ctypes.c_void_p
        self.kernel.GlobalLock.argtypes = [ctypes.c_void_p]
        self.kernel.GlobalLock.restype = ctypes.c_void_p
        self.kernel.GlobalUnlock.argtypes = [ctypes.c_void_p]
        self.kernel.GlobalFree.argtypes = [ctypes.c_void_p]
        self.kernel.GlobalSize.argtypes = [ctypes.c_void_p]
        self.kernel.GlobalSize.restype = ctypes.c_size_t

    @contextmanager
    def opened(self):
        # EmptyClipboard after OpenClipboard(NULL) leaves no valid owner and can
        # make SetClipboardData fail. This thread-owned message-only STATIC
        # window never appears on screen, takes focus or handles delayed data.
        hwnd = self.user.CreateWindowExW(0, "STATIC", "微信bot Clipboard", 0, 0, 0, 0, 0,
                                         ctypes.c_void_p(-3), None, None, None)
        if not hwnd:
            raise OSError("无法创建剪贴板所有者窗口")
        opened = False
        try:
            if not wait_until(lambda: self.user.OpenClipboard(hwnd), timeout=0.6):
                raise OSError("剪贴板被其他应用占用")
            opened = True
            yield
        finally:
            if opened:
                self.user.CloseClipboard()
            self.user.DestroyWindow(hwnd)

    def sequence(self):
        return int(self.user.GetClipboardSequenceNumber())

    def snapshot_with_sequence(self):
        formats, unreadable = {}, []
        # CF_BITMAP / CF_PALETTE / metafiles aren't HGLOBAL; never GlobalLock them.
        supported = {1, 7, 8, 13, 15, 16, 17}
        with self.opened():
            fmt = self.user.EnumClipboardFormats(0)
            while fmt:
                if fmt in supported or fmt >= 0xC000:
                    handle = self.user.GetClipboardData(fmt)
                    size = self.kernel.GlobalSize(handle) if handle else 0
                    ptr = self.kernel.GlobalLock(handle) if size and size <= 32 * 1024 * 1024 else None
                    if ptr:
                        try:
                            formats[fmt] = ctypes.string_at(ptr, size)
                        finally:
                            self.kernel.GlobalUnlock(handle)
                    else:
                        unreadable.append(fmt)
                else:
                    unreadable.append(fmt)
                fmt = self.user.EnumClipboardFormats(fmt)
            # A bitmap handle is a derived view when its actual DIB payload
            # was backed up. A declared but unreadable DIB is insufficient.
            if 8 in formats or 17 in formats:
                unreadable = [fmt for fmt in unreadable if fmt != 2]
            # OLE/clipboard-history markers describe a transfer, rather than
            # its payload. They must not block text, files or DIB preservation.
            auxiliary = {"DataObject", "Ole Private Data", "CanIncludeInClipboardHistory",
                         "CanUploadToCloudClipboard", "ExcludeClipboardContentFromMonitorProcessing"}
            if unreadable and any(fmt in formats for fmt in (8, 13, 15, 17)):
                for fmt in tuple(unreadable):
                    name = ctypes.create_unicode_buffer(256)
                    if fmt >= 0xC000 and self.user.GetClipboardFormatNameW(fmt, name, len(name)) and name.value in auxiliary:
                        unreadable.remove(fmt)
            # GetClipboardData can render delayed content and increment the
            # sequence itself. Read it after rendering, while still locked.
            sequence = self.sequence()
        return (formats if not unreadable else None), sequence

    def snapshot(self):
        return self.snapshot_with_sequence()[0]

    def _put_open(self, fmt, raw):
        handle = self.kernel.GlobalAlloc(0x0002, len(raw))
        if not handle:
            raise OSError("GlobalAlloc failed")
        ptr = self.kernel.GlobalLock(handle)
        if not ptr:
            self.kernel.GlobalFree(handle)
            raise OSError("GlobalLock failed")
        ctypes.memmove(ptr, raw, len(raw))
        self.kernel.GlobalUnlock(handle)
        if not self.user.SetClipboardData(fmt, handle):
            self.kernel.GlobalFree(handle)
            raise OSError("SetClipboardData failed")
        # Successful SetClipboardData transfers the handle to the OS.

    def replace(self, formats, expected_sequence=None):
        with self.opened():
            if expected_sequence is not None and self.sequence() != expected_sequence:
                return False
            if not self.user.EmptyClipboard():
                raise OSError("EmptyClipboard failed")
            for fmt, raw in formats.items():
                self._put_open(fmt, raw)
            owned_sequence = self.sequence()
        return owned_sequence

    def set_text(self, text, expected_sequence=None):
        return self.replace({13: (text + "\0").encode("utf-16-le")}, expected_sequence)

    def matching_text_sequence(self, text):
        # Remote clipboard synchronization may reannounce exactly the text we
        # wrote. Read while locked; the following write must still compare this
        # sequence while locked, so a genuinely newer copy cannot be replaced.
        with self.opened():
            handle = self.user.GetClipboardData(13)
            size = self.kernel.GlobalSize(handle) if handle else 0
            ptr = self.kernel.GlobalLock(handle) if 0 < size <= 32 * 1024 * 1024 else None
            if not ptr:
                return False
            try:
                raw = ctypes.string_at(ptr, size)
                current = raw.decode("utf-16-le").split("\0", 1)[0]
                return self.sequence() if current == text else False
            except UnicodeError:
                return False
            finally:
                self.kernel.GlobalUnlock(handle)

    def set_files(self, paths, expected_sequence=None):
        class DROPFILES(ctypes.Structure):
            _fields_ = [("pFiles", ctypes.c_uint32), ("x", ctypes.c_int32),
                        ("y", ctypes.c_int32), ("fNC", ctypes.c_int32), ("fWide", ctypes.c_int32)]
        payload = ("\0".join(os.path.abspath(p) for p in paths) + "\0\0").encode("utf-16-le")
        return self.replace({15: bytes(DROPFILES(ctypes.sizeof(DROPFILES), 0, 0, 0, 1)) + payload}, expected_sequence)

    def set_image(self, path, expected_sequence=None):
        from io import BytesIO
        from PIL import Image
        with Image.open(path) as source:
            output = BytesIO()
            source.convert("RGB").save(output, "BMP")
        return self.replace({8: output.getvalue()[14:]}, expected_sequence)


class ClipboardLease:
    def __init__(self, adapter=None):
        self._state_lock = threading.RLock()
        self.adapter = adapter or NativeClipboard()
        self.saved = None
        self.initial_sequence = None
        self.owned_sequence = None
        self.owned_text = None
        self.restored = False
        self._backup_error = None

    def __enter__(self):
        # Navigation and voice do not need to touch the clipboard at all.
        return self

    def _backup(self):
        if self.initial_sequence is not None:
            return
        if self._backup_error is not None:
            raise self._backup_error
        try:
            atomic_snapshot = getattr(self.adapter, "snapshot_with_sequence", None)
            if atomic_snapshot is not None:
                self.saved, after = atomic_snapshot()
            else:
                before = self.adapter.sequence()
                self.saved = self.adapter.snapshot()
                after = self.adapter.sequence()
                if before != after:
                    raise OSError("备份剪贴板时内容已变化，请稍后重试")
            if self.saved is None:
                raise OSError("剪贴板包含无法完整备份的格式，已保留原内容")
        except Exception as exc:
            self._backup_error = OSError("无法完整备份剪贴板，已保留原内容: " + str(exc))
            raise self._backup_error from exc
        self.initial_sequence = after

    def claim(self, sequence=None):
        self.owned_sequence = self.adapter.sequence() if sequence is None else sequence

    def _write(self, method, value):
        with self._state_lock:
            return self._write_locked(method, value)

    def _write_locked(self, method, value):
        if getattr(self, "_recovery_closed", False):
            raise DesktopCancelled("原发送任务已在恢复中取消")
        self._backup()
        if not getattr(self, "_external_token", None):
            self._external_token = external_state("clipboard", self._restore_for_recovery)
        expected = self.initial_sequence if self.owned_sequence is None else self.owned_sequence
        self._write_incomplete = True
        sequence = getattr(self.adapter, method)(value, expected_sequence=expected)
        if sequence is False:
            self._write_incomplete = False
        if sequence is False and self.owned_text is not None:
            matching = getattr(self.adapter, "matching_text_sequence", None)
            current = matching(self.owned_text) if matching is not None else False
            if current is not False:
                self._write_incomplete = True
                sequence = getattr(self.adapter, method)(value, expected_sequence=current)
                if sequence is False:
                    self._write_incomplete = False
        if sequence is False:
            raise ClipboardChanged("用户或其他应用已更新剪贴板，已保留新内容并取消发送")
        self.claim(sequence)
        self._write_incomplete = False
        self.owned_text = value if method == "set_text" else None

    def set_text(self, text):
        self._write("set_text", text)

    def set_files(self, paths):
        self._write("set_files", paths)

    def set_image(self, path):
        self._write("set_image", path)

    def __exit__(self, *unused):
        self._restore_for_recovery()

    def _restore_for_recovery(self):
        if not self._state_lock.acquire(blocking=False):
            return False
        try:
            return self._restore_locked()
        finally:
            self._state_lock.release()

    def _restore_locked(self):
        self._recovery_closed = True
        if getattr(self, "_write_incomplete", False):
            return False  # A partially failing native write has unknown ownership.
        if self.saved is None or self.owned_sequence is None:
            release_external_state(getattr(self, "_external_token", None))
            return True
        try:
            # Compare again while the clipboard is open to close the race.
            self.restored = self.adapter.replace(self.saved, expected_sequence=self.owned_sequence) is not False
            # False means the user's newer clipboard won; it must remain intact.
            self.owned_sequence = None
            release_external_state(getattr(self, "_external_token", None))
            return True
        except Exception as exc:
            log.debug("剪贴板恢复失败: %s", exc)
            return False
