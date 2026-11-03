"""Verified and cancellable WeChat desktop sends.

Success confirms local UI acceptance, not server/recipient delivery. Each task
discovers fresh controls in its calling thread's COM apartment. Target selection,
draft content and focus must be observed before any irreversible operation.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import threading
import time
from collections import deque
from contextlib import contextmanager

import audio_helper
import config
from desktop_coordinator import (ClipboardLease, check_cancel, desktop_operation,
    native_top_windows, wait_until, external_state, release_external_state)

log = logging.getLogger("weflow-bridge")


class BaseSender:
    def send_text(self, contact, text, **kwargs):
        raise NotImplementedError

    def send_image(self, contact, image_path, **kwargs):
        raise NotImplementedError

    def send_sticker(self, contact, sticker_path="", text="", **kwargs):
        raise NotImplementedError

    def send_voice(self, contact, audio_path, **kwargs):
        raise NotImplementedError


def _prop(control, key, default=None):
    try:
        return getattr(control, key)
    except Exception:
        return default


def _name(control):
    return str(_prop(control, "Name", "") or "").strip()


def _aid_segment(control, *names):
    """Match full ID components, including newer MainView.* Qt paths."""
    components = str(_prop(control, "AutomationId", "") or "").casefold().split(".")
    return any(name.casefold() in components for name in names)


def _aid_segment_prefix(control, prefix):
    components = str(_prop(control, "AutomationId", "") or "").casefold().split(".")
    prefix = prefix.casefold()
    return any(component.startswith(prefix) and len(component) > len(prefix) for component in components)


def _visible(control):
    return not _prop(control, "IsOffscreen", False) and _prop(control, "IsEnabled", True)


def _runtime_id(control):
    try:
        return tuple(control.GetRuntimeId())
    except Exception:
        return (id(control),)


def _parent(control):
    try:
        return control.GetParentControl() if control is not None else None
    except Exception:
        return None


def _walk(root, max_depth=32, max_nodes=700, cancelled=None, skip_children=None):
    queue = deque([(root, 0)] if root is not None else [])
    count = 0
    while queue and count < max_nodes:
        check_cancel(cancelled)
        control, depth = queue.popleft()
        count += 1
        yield control, depth
        if depth < max_depth and not (skip_children and skip_children(control)):
            try:
                queue.extend((child, depth + 1) for child in control.GetChildren())
            except Exception:
                continue
            check_cancel(cancelled)


def process_name(pid):
    if os.name != "nt" or not pid:
        return ""
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                 ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_uint(len(buffer))
        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value).casefold()
        return ""
    finally:
        kernel.CloseHandle(handle)


class UiaSender(BaseSender):
    PROCESS_NAMES = {"wechat.exe", "weixin.exe"}
    MAIN_CLASSES = {"WeChatMainWndForPC", "Qt51514QWindowIcon", "Qt6QWindowIcon", "mmui::MainWindow"}
    SEARCH_NAMES = {"搜索", "搜索联系人", "搜索联系人和群聊", "Search", "搜索聊天"}
    EDITOR_NAMES = {"", "输入", "输入消息", "消息", "发送消息", "编辑", "Message", "消息输入框"}

    def __init__(self, search_enabled=True):
        self.search_enabled = bool(search_enabled)
        self._auto = None
        self._window = None
        self._tree_nodes = None
        self._tree_window = None
        self._header_nodes = None
        self._header_bounds = None
        self._editor_node = None
        self._target_proof = None  # Primitive facts only; never a cross-thread Control.
        self._proof_restored = False
        self._send_window = None
        self._known_window = None  # Native HWND/PID only; never a retained COM control.
        self._observed_editor_info = None
        self._observed_headers = []
        self._observed_controls_available = False
        self._ready = False
        self._last_contact = ""  # Information only; never used to skip verification.
        self._last_error = ""
        self._last_send = {"status": "idle", "verification": "not_attempted"}
        self._snapshot = {"ready": False, "window": None, "editor": None, "current_chat": ""}
        self._metadata_lock = threading.Lock()
        self._thread_results = threading.local()
        self._cancelled = None
        self._identity = {}
        self._require_stable_header = False
        self._lease = None
        self._closed = False
        from uia_recovery import shared_gate_manager
        self._gate_manager = shared_gate_manager()
        self._init()

    def _set_error(self, error):
        message = str(error)
        self._thread_results.operation_error = message
        with self._metadata_lock:
            self._last_error = message

    def _init(self):
        try:
            import uiautomation as auto
            self._auto = auto
        except ImportError:
            self._set_error("缺少 uiautomation 依赖")

    @contextmanager
    def _uia_context(self):
        if self._auto is None:
            raise RuntimeError("缺少 uiautomation 依赖")
        with self._auto.UIAutomationInitializerInThread():
            self._window = None
            self._tree_nodes = None
            self._header_nodes = None
            self._send_window = None
            self._editor_node = None
            self._proof_restored = False
            self._voice_watch = None
            self._observed_editor_info = None
            self._observed_headers = []
            self._observed_controls_available = False
            try:
                yield
            finally:
                self._window = None  # Never retain a Control from another COM apartment.
                self._tree_nodes = None
                self._header_nodes = None
                self._send_window = None
                self._editor_node = None
                self._proof_restored = False
                self._voice_watch = None
                self._observed_editor_info = None
                self._observed_headers = []
                self._observed_controls_available = False

    def _control_nodes(self, *, refresh=False):
        """Reuse one observed tree inside this desktop/COM operation only."""
        window = (_runtime_id(self._window), _prop(self._window, "NativeWindowHandle", 0), _prop(self._window, "ProcessId", 0))
        if refresh or self._tree_nodes is None or window != self._tree_window:
            self._tree_nodes = list(_walk(self._window, cancelled=self._cancelled,
                                          skip_children=self._chat_tree_skip))
            self._tree_window = window
            self._header_nodes = None
            self._header_bounds = None
        return self._tree_nodes

    @staticmethod
    def _chat_tree_skip(control):
        """Chat controls and search results live outside history and the recent-chat list.

        Search lists remain dynamic and fully traversable. Do not prune an
        anonymous List/Document role: older providers may use it for a composer.
        """
        return (_aid_segment(control, "session_list", "chat_message_list", "chat_history_list", "message_list", "history_list")
                or any(word in str(_prop(control, "ClassName", "")).casefold()
                       for word in ("messagelist", "historylist")))

    @contextmanager
    def _send_phase(self, name):
        started = time.monotonic()
        try:
            yield
        finally:
            timings = getattr(self._thread_results, "timings", {})
            timings[name] = round(timings.get(name, 0) + time.monotonic() - started, 3)
            self._thread_results.timings = timings

    def _same_send_window(self):
        return bool(self._window is not None and self._send_window ==
                    (_prop(self._window, "NativeWindowHandle", 0), _prop(self._window, "ProcessId", 0)))

    def _control_in_window(self, control):
        expected = _runtime_id(self._window)
        for _ in range(33):
            if control is None:
                return False
            if _runtime_id(control) == expected:
                return True
            control = _parent(control)
        return False

    def _timeout(self):
        return max(0.3, min(float(getattr(config, "UIA_TIMEOUT", 2.8)), 10.0))

    def _is_wechat(self, window, main=True):
        if process_name(_prop(window, "ProcessId", 0)) not in self.PROCESS_NAMES:
            return False
        if not main:
            return True
        klass = _prop(window, "ClassName", "")
        known = klass in self.MAIN_CLASSES or bool(re.fullmatch(r"Qt\d+QWindowIcon", klass))
        # mmui exposes the account nickname as window name on some 4.x builds.
        return known and (klass in {"WeChatMainWndForPC", "mmui::MainWindow"}
                          or bool(re.fullmatch(r"(?:微信|WeChat|Weixin)(?:\s*[（(]\d+[）)])?", _name(window))))

    def _find_window(self):
        self._window, self._ready = None, False
        self._known_window = None
        self._tree_nodes = None
        configured_pid = int(getattr(config, "UIA_PROCESS_ID", 0) or 0)
        native = native_top_windows(self._auto, pid=configured_pid)
        if native is None:
            candidates = self._auto.GetRootControl().GetChildren()
        else:
            candidates = []
            names = {}
            for item in native:
                check_cancel(self._cancelled)
                pid = item["pid"]
                if pid not in names:
                    names[pid] = process_name(pid)
                # Native Qt class names and accessible ClassName need not
                # agree. Only open this process, then let _is_wechat identify
                # its accessible main window as before.
                if names[pid] in self.PROCESS_NAMES:
                    candidates.append(self._auto.ControlFromHandle(item["hwnd"]))
        windows = [w for w in candidates if self._is_wechat(w)]
        if configured_pid:
            windows = [w for w in windows if _prop(w, "ProcessId", 0) == configured_pid]
        if len(windows) != 1:
            self._set_error("微信主窗口未找到" if not windows else "发现多个微信主窗口，请配置明确的进程 ID")
            return False
        self._window, self._ready = windows[0], True
        self._send_window = (_prop(self._window, "NativeWindowHandle", 0), _prop(self._window, "ProcessId", 0))
        self._known_window = self._send_window
        return True

    def _ensure_window(self):
        # Reacquire only our known native window in this thread's COM apartment.
        # Enumerating every desktop UIA provider can block on an unrelated app.
        known = self._known_window
        getter = getattr(self._auto, "ControlFromHandle", None)
        configured_pid = int(getattr(config, "UIA_PROCESS_ID", 0) or 0)
        if known and callable(getter) and (not configured_pid or configured_pid == known[1]):
            check_cancel(self._cancelled)
            try:
                user = self._native_user32()
                native = self._native_owner(user, known[0]) if user is not None else (1, known[1])
                if native[0] and native[1] == known[1]:
                    window = getter(known[0])
                    if self._window_identity(window) == known and self._is_wechat(window):
                        self._window, self._send_window, self._ready = window, known, True
                        return True
            except Exception:
                pass  # A closed/restarted window is rediscovered below.
        check_cancel(self._cancelled)
        return self._find_window()

    def _ensure_recovery_gate(self, hwnd, pid):
        """Finish only a safely expired lease before one fresh pre-input ensure."""
        before = self._gate_manager.snapshot()
        check_cancel(self._cancelled)
        if self._gate_manager.ensure(hwnd, pid):
            return True
        after = self._gate_manager.snapshot()
        old = before.get("lease")
        if not isinstance(old, dict):
            return False
        remaining = old.get("remaining_seconds")
        expired = (before.get("status") == "lease_expired" and before.get("active") is False
                   and old.get("expired") is True and old.get("restored") is True)
        due = (before.get("status") == "active_pending_control_check" and before.get("active") is True
               and old.get("expired") is False and old.get("restored") is False
               and type(remaining) in (int, float) and remaining == 0)
        restored = after.get("restore_result")
        if (not (expired or due) or before.get("enabled") is not True or before.get("busy") is not False
                or old.get("watchdog_error") != "" or before.get("build_check", {}).get("status") != "matched"
                or after.get("enabled") is not True or after.get("busy") is not False
                or after.get("active") is not False or after.get("lease") is not None
                or after.get("status") != "lease_expired" or after.get("build_check", {}).get("status") != "matched"
                or not isinstance(restored, dict) or restored.get("ok") is not True
                or restored.get("status") not in {"restored", "already_restored"}):
            return False
        # This is still before target selection, clipboard input or a draft.
        # Reacquire exactly the original main window, then honor cancellation.
        getter = getattr(self._auto, "ControlFromHandle", None)
        window = getter(hwnd) if callable(getter) else None
        if window is None:
            self._find_window()
            window = self._window
        if (window is None or not self._is_wechat(window)
                or _prop(window, "NativeWindowHandle", 0) != hwnd or _prop(window, "ProcessId", 0) != pid):
            return False
        self._window = window
        check_cancel(self._cancelled)
        if self._gate_manager.snapshot() != after or not self._gate_manager.ensure(hwnd, pid):
            return False
        fresh = self._gate_manager.snapshot()
        lease = fresh.get("lease")
        # An external activation or another expiry never licenses this retry.
        return bool(fresh.get("enabled") is True and fresh.get("busy") is False
                    and fresh.get("active") is True and fresh.get("status") == "active_pending_control_check"
                    and fresh.get("build_check", {}).get("status") == "matched"
                    and isinstance(lease, dict) and lease.get("expired") is False
                    and lease.get("restored") is False and lease.get("watchdog_error") == "")

    def _recover_current_window(self, *, controls_ready=None):
        """Only an already verified main HWND, never a conversation popout."""
        hwnd, pid = _prop(self._window, "NativeWindowHandle", 0), _prop(self._window, "ProcessId", 0)
        check_cancel(self._cancelled)
        if not hwnd or not self._ensure_recovery_gate(hwnd, pid):
            return False
        def fresh():
            check_cancel(self._cancelled)
            getter = getattr(self._auto, "ControlFromHandle", None)
            window = getter(hwnd) if callable(getter) else None
            if window is None:
                self._find_window()
                window = self._window
            if window is None or not self._is_wechat(window) or _prop(window, "NativeWindowHandle", 0) != hwnd or _prop(window, "ProcessId", 0) != pid:
                return None  # Identity changed; do not refresh the old handle.
            self._window = window
            self._tree_nodes = None
            return bool(controls_ready()) if controls_ready is not None else self._controls_available()
        available = fresh()
        if available is None:
            return False
        if available:
            return True
        # Tested on this exact build: one main-window Tab initializes the fresh
        # provider. It inserts no text and selects no search/contact result.
        if not self._activate() or not self._foreground():
            return False
        check_cancel(self._cancelled)
        self._keys("{Tab}")
        return bool(wait_until(fresh, self._timeout(), self._cancelled))

    def prepare(self, *, cancel_event=None, generation=None, cancelled=None):
        """Explicit startup preparation may activate the configured known build."""
        if self._closed:
            return False
        base_cancelled = self._cancel_test(cancel_event, generation)
        is_cancelled = lambda: base_cancelled() or bool(cancelled and cancelled())
        try:
            with desktop_operation(is_cancelled), self._uia_context():
                self._cancelled = is_cancelled
                try:
                    ready = self._ensure_accessibility()
                    self._update_snapshot()
                    return bool(ready)
                finally:
                    self._cancelled = None
        except Exception as error:
            self._set_error(error)
            return False

    def close(self):
        self._closed = True
        result = self._gate_manager.close()
        if not result.get("ok"):
            self._set_error("UIA 恢复状态未能还原: " + str(result))
        return result

    def _ensure_accessibility(self, *, current_target=None):
        check_cancel(self._cancelled)
        recovery = self._gate_manager.snapshot()
        if recovery["active"] and not recovery["enabled"]:
            if not self._gate_manager.close().get("ok"):
                return False
        with self._send_phase("window_discovery_seconds"):
            if not self._ensure_window():
                return False
        if recovery["active"] and recovery["enabled"]:
            check_cancel(self._cancelled)
            if not self._ensure_recovery_gate(_prop(self._window, "NativeWindowHandle", 0), _prop(self._window, "ProcessId", 0)):
                return False
        if current_target is not None:
            with self._send_phase("target_reuse_seconds"):
                if self._restore_target_proof(*current_target):
                    return True
        with self._send_phase("controls_discovery_seconds"):
            return self._controls_available() or self._recover_current_window()

    def _native_user32(self):
        # Headless/injected providers expose only the UIA API. Do not consult
        # the real desktop behind a synthetic provider used by offline tests.
        if os.name != "nt" or not (getattr(self._auto, "__name__", "") == "uiautomation"
                                   or callable(getattr(self._auto, "GetForegroundWindow", None))):
            return None
        user = ctypes.windll.user32
        signatures = {
            "GetForegroundWindow": ([], ctypes.c_void_p),
            "GetWindowThreadProcessId": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_uint32),
            "IsChild": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
            "GetAncestor": ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_void_p),
            "IsIconic": ([ctypes.c_void_p], ctypes.c_int),
            "ShowWindow": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
            "SetForegroundWindow": ([ctypes.c_void_p], ctypes.c_int),
            "BringWindowToTop": ([ctypes.c_void_p], ctypes.c_int),
            "AttachThreadInput": ([ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            method = getattr(user, name)
            method.argtypes, method.restype = arguments, result
        return user

    @staticmethod
    def _native_owner(user, hwnd):
        pid = ctypes.c_uint32()
        thread = user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) if hwnd else 0
        return int(thread or 0), pid.value

    @staticmethod
    def _window_identity(control):
        handle = int(_prop(control, "NativeWindowHandle", 0) or 0)
        return (int(ctypes.c_void_p(handle).value or 0),
                int(_prop(control, "ProcessId", 0) or 0))

    def _activation_target_valid(self, target, identity, user=None):
        if not all(identity):
            self._activation_failure = "window_identity_changed"
            return False
        if user is not None:
            thread, pid = self._native_owner(user, identity[0])
            if not thread or pid != identity[1]:
                self._activation_failure = "native_window_identity_mismatch"
                return False
        # Check the live UIA identity once, after the native owner query. The
        # former pre-query read repeated the same COM calls and could not
        # replace this final check if a provider changed during the query.
        if self._window_identity(target) != identity:
            self._activation_failure = "window_identity_changed"
            return False
        return True

    def _foreground(self, root=None):
        expected = root or self._window
        try:
            identity = self._window_identity(expected)
            if not all(identity):
                self._activation_failure = "window_identity_unavailable"
                return False
            user = self._native_user32()
            if user is not None:
                if not self._activation_target_valid(expected, identity, user):
                    return False
                if user.IsIconic(identity[0]):
                    self._activation_failure = "window_minimized"
                    return False
                front = int(user.GetForegroundWindow() or 0)
                if not front:
                    self._activation_failure = "native_foreground_unavailable"
                    return False
                if self._native_owner(user, front)[1] != identity[1]:
                    self._activation_failure = "foreground_other_process"
                    return False
                # An owned top-level popup/settings window is not a child.
                # Never broaden a verified main/composer to every same-PID HWND.
                matches = front == identity[0] or bool(user.IsChild(identity[0], front)
                    and int(user.GetAncestor(front, 2) or 0) == identity[0])  # GA_ROOT
            else:
                front = self._auto.GetForegroundControl()
                matches = self._window_identity(front) == identity
            if not matches or not self._activation_target_valid(expected, identity, user):
                self._activation_failure = "foreground_window_mismatch"
                return False
            self._activation_failure = ""
            return True
        except Exception:
            self._activation_failure = "foreground_query_failed"
            return False

    @staticmethod
    def _activate_control(target, name):
        method = getattr(target, name)
        try:
            return method(waitTime=0)
        except TypeError:
            return method()  # Injected/older providers may not accept waitTime.

    def _activate(self, root=None):
        target = root or self._window
        self._activation_failure = ""
        check_cancel(self._cancelled)
        try:
            identity = self._window_identity(target)
            user = self._native_user32()
            if not self._activation_target_valid(target, identity, user):
                return False
            if user is not None and self._foreground(target):
                confirmed = self._activation_target_valid(target, identity, user)
                check_cancel(self._cancelled)
                return confirmed
            if user is not None and user.IsIconic(identity[0]):
                user.ShowWindow(identity[0], 9)  # SW_RESTORE only when minimized.
            check_cancel(self._cancelled)
            if not self._activation_target_valid(target, identity, user):
                return False
            try:
                self._activate_control(target, "SetActive")
            except Exception:
                try:
                    self._activate_control(target, "SwitchToThisWindow")
                except Exception:
                    pass  # A native verified HWND can outlive a Qt UIA wrapper.
            check_cancel(self._cancelled)
            if not self._activation_target_valid(target, identity, user):
                return False
            if self._foreground(target):
                confirmed = self._activation_target_valid(target, identity, user)
                check_cancel(self._cancelled)
                return confirmed
            if user is not None:
                foreground = int(user.GetForegroundWindow() or 0)
                foreground_tid = self._native_owner(user, foreground)[0]
                target_tid = self._native_owner(user, identity[0])[0]
                if not foreground or not foreground_tid or not target_tid:
                    self._activation_failure = "native_foreground_unavailable"
                    return False
                kernel = ctypes.windll.kernel32
                kernel.GetCurrentThreadId.restype = ctypes.c_uint32
                current_tid = int(kernel.GetCurrentThreadId())
                attached, seen, detach_failed = [], set(), False
                try:
                    for pair in ((current_tid, target_tid), (foreground_tid, target_tid)):
                        check_cancel(self._cancelled)
                        if not self._activation_target_valid(target, identity, user):
                            return False
                        if not all(pair) or pair[0] == pair[1] or frozenset(pair) in seen:
                            continue
                        seen.add(frozenset(pair))
                        if user.AttachThreadInput(*pair, True):
                            attached.append(pair)
                    check_cancel(self._cancelled)
                    if not self._activation_target_valid(target, identity, user):
                        return False
                    # Do not pursue a different foreground acquired by the user
                    # while the temporary input relationships were being set up.
                    if int(user.GetForegroundWindow() or 0) != foreground and not self._foreground(target):
                        self._activation_failure = "foreground_changed_during_activation"
                        return False
                    if not self._foreground(target):
                        user.SetForegroundWindow(identity[0])
                        user.BringWindowToTop(identity[0])
                finally:
                    for pair in reversed(attached):
                        try:
                            if not user.AttachThreadInput(*pair, False):
                                detach_failed = True
                        except Exception:
                            detach_failed = True
                if detach_failed:
                    self._activation_failure = "input_detach_failed"
                    return False
            confirmed = wait_until(lambda: self._activation_target_valid(target, identity, user)
                                   and self._foreground(target), self._timeout(), self._cancelled)
            # A polling predicate is an observation, not a reusable licence.
            confirmed = bool(confirmed and self._activation_target_valid(target, identity, user)
                             and self._foreground(target) and self._window_identity(target) == identity)
            check_cancel(self._cancelled)
            if not confirmed:
                self._activation_failure = self._activation_failure or "foreground_not_acquired"
            return bool(confirmed)
        except Exception:
            check_cancel(self._cancelled)
            self._activation_failure = "native_activation_failed"
            return False

    def _keys(self, text):
        check_cancel(self._cancelled)
        self._auto.SendKeys(text, interval=0.003, waitTime=0)

    def _focus(self, control):
        if not self._foreground():
            return False
        if self._focused(control):
            return True
        try:
            control.SetFocus()
        except Exception:
            return False
        if self._focused(control):
            return True
        try:
            control.Click(simulateMove=False, waitTime=0)
        except Exception:
            return False
        return bool(wait_until(lambda: self._focused(control), self._timeout(), self._cancelled))

    def _focused(self, control):
        if not self._foreground():
            return False
        try:
            focused = self._auto.GetFocusedControl()
            expected = _runtime_id(control)
            for _ in range(33):
                if focused is None:
                    return False
                if _runtime_id(focused) == expected:
                    return True
                focused = _parent(focused)
            return False
        except Exception:
            return False

    def _editor_text(self, editor):
        observed_empty = False
        for method in ("GetValuePattern", "GetTextPattern", "GetLegacyIAccessiblePattern"):
            try:
                pattern = getattr(editor, method)()
                if pattern is not None:
                    value = pattern.DocumentRange.GetText(-1) if method == "GetTextPattern" else pattern.Value
                    if value is None:
                        continue
                    value = str(value).replace("\r\n", "\n").replace("\r", "\n")
                    if value:
                        return value
                    # Some Qt editors expose an empty Value alongside a live
                    # TextPattern. An empty placeholder must not hide a draft.
                    observed_empty = True
            except Exception:
                pass
        if observed_empty:
            return ""
        # Qt's accessible Name can be the chat title, even with a draft present.
        # A title/placeholder is never proof of an empty native composer.
        if self._chat_editor_profile(editor):
            return None
        name = _name(editor)
        if name not in self.EDITOR_NAMES and name not in self.SEARCH_NAMES:
            return name
        return None

    @staticmethod
    def _chat_editor_profile(control):
        return (_aid_segment(control, "chat_input_field")
                or _prop(control, "ClassName", "") == "mmui::ChatInputField")

    def _search_editor_profile(self, control):
        if (_name(control) in self.SEARCH_NAMES
                or any("search" in part for part in str(_prop(control, "AutomationId", "")).casefold().split("."))):
            return True
        # The search Name may become its query. Confirm the native class and
        # left header region instead of confusing another rich text editor.
        if _prop(control, "ClassName", "") != "mmui::XValidatorTextEdit":
            return False
        rect, win = _prop(control, "BoundingRectangle"), _prop(self._window, "BoundingRectangle")
        return bool(rect and win and rect.bottom <= win.top + (win.bottom - win.top) * .30
                    and rect.right <= win.left + (win.right - win.left) * .45)

    def _find_editor(self, *, refresh=False):
        self._observed_editor_info = None
        if refresh:
            self._editor_node = None
            self._tree_nodes = None
        cached = self._editor_node
        if (cached is not None and _visible(cached) and _prop(cached, "ControlTypeName", "") == "EditControl"
                and self._control_in_window(cached)):
            self._observed_editor_info = {"name":_name(cached),"type":"EditControl",
                                          "automation_id":str(_prop(cached,"AutomationId","")).casefold()}
            return cached
        for refresh in (False, True):
            edits, profile_edits, observations = [], [], {}
            for control, _ in self._control_nodes(refresh=refresh):
                if _prop(control, "ControlTypeName", "") != "EditControl" or not _visible(control):
                    continue
                name, aid = _name(control), str(_prop(control, "AutomationId", "")).casefold()
                if self._search_editor_profile(control):
                    continue
                rect, wr = _prop(control, "BoundingRectangle"), _prop(self._window, "BoundingRectangle")
                geometric = rect is not None and wr is not None and rect.top >= wr.top + (wr.bottom - wr.top) * 0.40
                semantic = name in self.EDITOR_NAMES or any(x in aid for x in ("chat_input_field", "chatinput", "messageinput", "inputbox"))
                if geometric and (semantic or self._editor_text(control) is not None):
                    edits.append(control)
                    observations[id(control)] = {"name": name, "type": "EditControl", "automation_id": aid}
                    if self._chat_editor_profile(control):
                        profile_edits.append(control)
            candidates = profile_edits or edits
            if len(candidates) != 1:
                return None
            if self._control_in_window(candidates[0]):
                self._observed_editor_info = observations[id(candidates[0])]
                self._editor_node = candidates[0]
                return candidates[0]
        return None

    def _header_names(self, editor):
        self._observed_headers = []
        rect, win = _prop(editor, "BoundingRectangle"), _prop(self._window, "BoundingRectangle")
        if rect is None or win is None:
            return []
        nodes = self._control_nodes()
        bounds = (rect.left, rect.top, rect.right, rect.bottom, win.left, win.top, win.right, win.bottom)
        if self._header_nodes is not None and any(not self._control_in_window(c) for c in self._header_nodes):
            # A replaced title/ID is not proof of the former chat. Refresh the
            # operation tree instead of continuing with detached header nodes.
            nodes = self._control_nodes(refresh=True)
        if self._header_nodes is None or self._header_bounds != bounds:
            candidates = []
            for control, _ in nodes:
                check_cancel(self._cancelled)
                cr = _prop(control, "BoundingRectangle")
                if (_aid_segment(control, "current_chat_name_label")
                        or (cr is not None and _prop(control, "ControlTypeName", "") in {"TextControl", "ButtonControl", "PaneControl"}
                            and cr.left >= rect.left - 40 and cr.top >= win.top
                            and cr.bottom <= min(rect.top, win.top + max(145, (win.bottom - win.top) * .22)))):
                    candidates.append(control)
            # Cache candidates only, within this COM operation. Names, native
            # IDs, geometry, visibility and parent ownership stay live below.
            self._header_nodes, self._header_bounds = candidates, bounds
        names, native_titles = [], {}
        for control in self._header_nodes:
            check_cancel(self._cancelled)
            cr = _prop(control, "BoundingRectangle")
            visible = _visible(control)
            native = visible and _aid_segment(control, "current_chat_name_label")
            geometric = bool(cr is not None and visible and _prop(control, "ControlTypeName", "") in {"TextControl", "ButtonControl", "PaneControl"}
                             and cr.left >= rect.left - 40 and cr.top >= win.top
                             and cr.bottom <= min(rect.top, win.top + max(145, (win.bottom - win.top) * 0.22)))
            if not (native or geometric) or self._in_message_or_result_list(control) or not self._control_in_window(control):
                continue
            name = _name(control)
            if native:
                native_titles[_runtime_id(control)] = name
            if geometric and name:
                names.append(name)
        if native_titles:
            # A message sender near the top of the history can share a name
            # with the requested contact. Prefer the actual 4.x chat title.
            titles = list(native_titles.values())
            if len(titles) != 1 or not titles[0]:
                return []
            names = titles + [n for n in names if n.startswith(("微信号：", "微信号:", "WeChat ID:"))]
        self._observed_headers = list(names)  # Diagnostic observations only; never target proof.
        return names

    @staticmethod
    def _in_message_or_result_list(control):
        current = control
        for _ in range(33):
            if current is None:
                return False
            if (_prop(current, "ControlTypeName", "") in {"ListControl", "ListItemControl", "TreeControl", "TreeItemControl", "DocumentControl"}
                    or _aid_segment(current, "chat_message_list", "chat_history_list", "message_list", "history_list", "message_item")
                    or any(word in str(_prop(current, "ClassName", "")).casefold() for word in ("messagelist", "historylist", "messageitem"))):
                return True
            current = _parent(current)
        return True  # An unbounded or cyclic parent chain is not header proof.

    def _matches_header(self, name, contact):
        if name == contact:
            return True
        return bool(self._identity.get("is_group") and re.sub(r"\s*[（(]\d+[）)]$", "", name) == contact)

    def _chat_verified(self, contact, editor=None):
        editor = editor or self._find_editor()
        names = self._header_names(editor) if editor else []
        if not any(self._matches_header(name, contact) for name in names):
            return False
        username = str(self._identity.get("username") or self._identity.get("session_id") or "")
        if self._require_stable_header or username and any(name.startswith(("微信号：", "微信号:", "WeChat ID:")) for name in names):
            return bool(username and any(name in {username, "微信号：" + username, "微信号: " + username,
                                                 "WeChat ID: " + username} for name in names))
        return True

    def _search_editor(self):
        edits = [c for c, _ in self._control_nodes(refresh=True) if _prop(c, "ControlTypeName", "") == "EditControl"
                 and _visible(c) and self._search_editor_profile(c)]
        edits = list({(_runtime_id(control) or ("object", id(control))): control for control in edits}.values())
        return edits[0] if len(edits) == 1 and self._control_in_window(edits[0]) else None

    def _return_to_chats(self):
        """SNS may consume Ctrl+F; select the main sidebar before searching."""
        if not self._same_send_window() or not self._foreground():
            return False
        bounds = _prop(self._window, "BoundingRectangle")
        if bounds is None:
            return False
        entries = {}
        for control, _ in self._control_nodes(refresh=True):
            rect = _prop(control, "BoundingRectangle")
            klass = str(_prop(control, "ClassName", "")).casefold()
            if (_visible(control) and _prop(control, "ControlTypeName", "") in {"EditControl", "DocumentControl"}
                    and "comment" in klass and any(part in klass for part in ("editor", "input", "edit"))
                    and self._editor_text(control) not in (None, "")):
                return False  # Leaving SNS can discard an existing comment draft.
            if (_prop(control, "ControlTypeName", "") in {"ButtonControl", "TabItemControl", "RadioButtonControl"}
                    and _name(control) in {"聊天", "微信", "Chats", "Chat"} and _visible(control)
                    and rect is not None and bounds.left <= rect.left < rect.right <= bounds.left + (bounds.right-bounds.left)*.20
                    and bounds.top <= rect.top < rect.bottom <= bounds.bottom
                    and not self._in_message_or_result_list(control) and self._control_in_window(control)):
                entries[_runtime_id(control) or ("object", id(control))] = control
        if len(entries) != 1:
            return False
        entry = next(iter(entries.values()))
        check_cancel(self._cancelled)
        if not self._same_send_window() or not self._foreground() or not self._control_in_window(entry):
            return False
        try:
            entry.Click(simulateMove=False, waitTime=0)
        except Exception:
            return False
        self._tree_nodes = None
        check_cancel(self._cancelled)
        return self._same_send_window() and self._foreground()

    def _search_results(self, contact, identity=None):
        results, native_results = [], []
        identity = identity or {}
        username = str(identity.get("username") or identity.get("session_id") or "").strip()
        for row, _ in _walk(self._window, cancelled=self._cancelled, skip_children=self._chat_tree_skip):
            kind, aid = _prop(row, "ControlTypeName", ""), str(_prop(row, "AutomationId", ""))
            components = aid.casefold().split(".")
            terminal = components[-1]
            native = terminal.startswith("search_item_") and len(terminal) > len("search_item_")
            # Descendant labels retain their row's ID path; they are not new results.
            if (kind not in {"ListItemControl", "TreeItemControl"} and not native) or not _visible(row):
                continue
            if not native and any(part.startswith("search_item_") for part in components[:-1]):
                continue
            if _name(row).split("\n", 1)[0].strip() != contact:
                continue
            parent = _parent(row)
            context = " ".join(_name(c) + " " + str(_prop(c, "AutomationId", "")) for c in (parent, _parent(parent)))
            if not any(x in context.casefold() for x in ("搜索", "联系人", "群聊", "search", "result")):
                continue
            if any(x in context.casefold() for x in ("收藏", "聊天记录", "网络", "网页", "搜一搜", "favorite", "web_search")):
                continue
            category = " ".join(_name(c) for c in (parent, _parent(parent))).casefold()
            group_category = any(x in category for x in ("群聊", "群组", "groups"))
            contact_category = any(x in category for x in ("联系人", "好友", "contacts", "friends"))
            if type(identity.get("is_group")) is bool and group_category != contact_category:
                if group_category != identity["is_group"]:
                    continue
            # A group containing this member or a history preview is not that chat.
            results.append(row)
            if native:
                native_results.append(row)
        # 4.x exposes chat rows as search_item_*; anonymous same-name web/favorite
        # rows in the same table must not compete with these reliable chat results.
        results = list({_runtime_id(row): row for row in (native_results or results)}.values())
        selected = []
        ids = {_runtime_id(row) for row in results}
        for row in results:
            parent = _parent(row)
            nested = False
            for _ in range(32):
                if parent is None:
                    break
                if _runtime_id(parent) in ids:
                    nested = True
                    break
                parent = _parent(parent)
            if not nested:
                selected.append(row)
        positions = [_prop(row, "BoundingRectangle") for row in selected]
        if selected and all(rect is not None for rect in positions):
            selected = [row for _, row in sorted(zip(positions, selected), key=lambda item: (item[0].top, item[0].left))]
        # The user chooses the first visible chat. Keep existing known-ID checks
        # after opening when several real chats share this display name.
        self._require_stable_header = bool(username and len(selected) > 1)
        return selected

    def _switch_contact(self, contact, identity=None):
        if not contact or not self._same_send_window() or not self._activate() or not self._same_send_window():
            return False
        self._keys("{Ctrl}f")
        search = wait_until(self._search_editor, self._timeout(), self._cancelled)
        if search is None and self._return_to_chats():
            self._keys("{Ctrl}f")
            search = wait_until(self._search_editor, self._timeout(), self._cancelled)
        if search is None or not self._focus(search):
            self._set_error("未能确认微信搜索输入框")
            return False
        self._keys("{Ctrl}a")
        self._lease.set_text(contact)
        if not self._focused(search):
            self._set_error("搜索框焦点已改变")
            return False
        self._keys("{Ctrl}v")
        last_signature, since = None, time.monotonic()
        def settled():
            nonlocal last_signature, since
            if not self._focused(search) or self._editor_text(search) != contact:
                return None
            rows = self._search_results(contact, identity)
            signature = tuple(_runtime_id(row) for row in rows)
            if signature != last_signature:
                last_signature, since = signature, time.monotonic()
            return rows if rows and time.monotonic() - since >= 0.18 else None
        rows = wait_until(settled, self._timeout(), self._cancelled)
        if not rows:
            self._set_error("未找到可确认的精确联系人搜索结果")
            return False
        check_cancel(self._cancelled)
        if not self._foreground():
            self._set_error("搜索期间前台窗口已改变")
            return False
        try:
            rows[0].Click(simulateMove=False, waitTime=0)
        except Exception as exc:
            self._set_error("联系人搜索结果无法打开: " + str(exc))
            return False
        def opened_editor():
            self._tree_nodes = None
            editor = self._find_editor()
            return editor if editor is not None and self._chat_verified(contact, editor) else None
        editor = wait_until(opened_editor, self._timeout(), self._cancelled)
        if editor is None:
            self._set_error("聊天标题或消息编辑器未能确认")
            return False
        self._last_contact = contact
        return self._focus(editor)

    def _controls_available(self):
        # Native title-bar buttons do not establish an accessible chat tree.
        self._observed_controls_available = any(_prop(c, "ControlTypeName", "") in {"EditControl", "ListControl"}
                                                for c, _ in self._control_nodes(refresh=True))
        return self._observed_controls_available

    @staticmethod
    def _target_key(identity):
        username = str(identity.get("username") or identity.get("session_id") or "").strip()
        return (username, bool(identity.get("is_group"))) if username else None

    def _input_stamp(self):
        """Human/other desktop input invalidates a cached chat selection."""
        user = self._native_user32()
        if user is None:
            return None  # An injected offline provider has no native input state.
        try:
            class LastInput(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]
            value = LastInput(ctypes.sizeof(LastInput), 0)
            return int(value.dwTime) if user.GetLastInputInfo(ctypes.byref(value)) else False
        except Exception:
            return False

    def _control_path(self, control):
        path = []
        root = _runtime_id(self._window)
        for _ in range(33):
            if control is None:
                return None
            if _runtime_id(control) == root:
                return tuple(reversed(path))
            parent = _parent(control)
            if parent is None:
                return None
            try:
                siblings = parent.GetChildren()
                wanted = _runtime_id(control)
                matches = [index for index, child in enumerate(siblings) if _runtime_id(child) == wanted]
            except Exception:
                return None
            if len(matches) != 1:
                return None
            path.append(matches[0]); control = parent
        return None

    def _path_control(self, path, expected):
        control = self._window
        try:
            for index in path[:-1]:
                control = control.GetChildren()[index]
            if not path:
                return control if tuple(control.GetRuntimeId()) == expected else None
            siblings = control.GetChildren()
            if path[-1] < len(siblings) and tuple(siblings[path[-1]].GetRuntimeId()) == expected:
                return siblings[path[-1]]
            # Qt may insert a sibling without changing the selected chat. The
            # provider's runtime ID still identifies the exact original control.
            return next((child for child in siblings if tuple(child.GetRuntimeId()) == expected), None)
        except Exception:
            return None

    def _remember_target(self, contact, identity):
        key, editor = self._target_key(identity), self._editor_node
        stamp = self._input_stamp()
        if not key or editor is None or stamp is False:
            self._target_proof = None
            return
        controls = [control for control in (self._header_nodes or []) if _name(control) in self._observed_headers
                    and _visible(control) and self._control_in_window(control)]
        try:
            paths = [(self._control_path(control), tuple(control.GetRuntimeId())) for control in [editor, *controls]]
            if not controls or any(path is None or not rid for path, rid in paths):
                self._target_proof = None
                return
            self._target_proof = {"target":key,"contact":contact,"window":self._window_identity(self._window),
                "root_id":tuple(self._window.GetRuntimeId()),"editor":paths[0],"headers":paths[1:],
                "require_native":self._require_stable_header,"input_stamp":stamp}
        except Exception:
            self._target_proof = None

    def _refresh_target_stamp(self):
        if self._target_proof is not None:
            self._target_proof["input_stamp"] = self._input_stamp()

    def _refresh_media_target(self, contact):
        # Committing an attachment may rebuild Qt's header/composer nodes.
        # Keep the just-selected native target, with fresh live controls.
        with self._send_phase("target_refresh_seconds"):
            editor = self._find_editor(refresh=True)
            if editor is not None and self._chat_verified(contact, editor):
                self._remember_target(contact, self._identity)
            else:
                self._target_proof = None

    def _restore_target_proof(self, contact, identity):
        proof = self._target_proof
        if (not proof or proof["target"] != self._target_key(identity) or proof["contact"] != contact
                or proof["window"] != self._window_identity(self._window)
                or proof["root_id"] != _runtime_id(self._window)
                or proof["input_stamp"] is False or proof["input_stamp"] != self._input_stamp()):
            self._target_proof = None
            return False
        editor = self._path_control(*proof["editor"])
        headers = [self._path_control(*item) for item in proof["headers"]]
        if editor is None or any(control is None for control in headers):
            self._target_proof = None
            return False
        self._tree_nodes = [(self._window,0),(editor,len(proof["editor"][0])),*[(control,1) for control in headers]]
        self._tree_window = (_runtime_id(self._window), *self._window_identity(self._window))
        self._editor_node, self._header_nodes, self._header_bounds = editor, headers, None
        self._require_stable_header = proof["require_native"]
        if (not _visible(editor) or not self._control_in_window(editor)
                or any(not _visible(control) or not self._control_in_window(control) for control in headers)):
            self._tree_nodes = self._editor_node = self._header_nodes = None
            self._target_proof = None
            return False
        self._observed_controls_available = True
        self._proof_restored = True
        return True

    def _current_target(self, contact, identity):
        """Reuse a live selected-chat proof, or independently exposed native ID."""
        username = str(identity.get("username") or identity.get("session_id") or "").strip()
        if not username:
            return False
        editor = self._find_editor()
        self._require_stable_header = self._target_proof["require_native"] if self._proof_restored else True
        if (editor is not None and self._activate() and self._focus(editor) and self._same_send_window()
                and self._control_in_window(editor) and self._chat_verified(contact, editor)):
            self._last_contact = contact
            return True
        self._require_stable_header = False
        return False

    def _prepare_target(self, contact, identity):
        if not self._ensure_accessibility(current_target=(contact,identity)):
            if self._window is not None:
                self._set_error(self.accessibility_error())
            return False
        if self.search_enabled:
            if self._current_target(contact, identity):
                if not self._proof_restored:
                    self._remember_target(contact,identity)
                return True
            self._target_proof = None
            with self._send_phase("contact_search_seconds"):
                selected = self._switch_contact(contact, identity)
            if selected:
                self._remember_target(contact,identity)
            return selected
        self._set_error("联系人搜索已关闭，无法验证唯一目标；请开启搜索后发送")
        return False

    def _cancel_test(self, cancel_event, generation):
        def cancelled():
            if self._closed:
                return True
            if cancel_event is not None and cancel_event.is_set():
                return True
            if generation is not None:
                import state
                return state.bridge_generation != generation
            return False
        return cancelled

    @contextmanager
    def _operation(self, contact, identity, cancel_event, generation):
        if self._closed:
            raise RuntimeError("发送器已经关闭")
        cancelled = self._cancel_test(cancel_event, generation)
        started = time.monotonic()
        self._thread_results.timings = {}
        with desktop_operation(cancelled), self._uia_context(), ClipboardLease() as lease:
            self._thread_results.timings["desktop_wait_seconds"] = round(time.monotonic() - started, 3)
            self._cancelled, self._identity, self._lease = cancelled, identity or {}, lease
            self._thread_results.operation_error = ""
            self._thread_results.cleanup_warnings = []
            self._thread_results.draft_cleanup = "not_needed"
            try:
                with self._send_phase("target_seconds"):
                    if not self._prepare_target(contact, self._identity):
                        raise RuntimeError(self._thread_results.operation_error or "无法确认发送目标")
                yield
            finally:
                with self._send_phase("snapshot_seconds"):
                    self._update_operation_snapshot(contact)
                self._thread_results.timings["uia_total_seconds"] = round(time.monotonic() - started, 3)
                self._cancelled, self._identity, self._lease = None, {}, None
                self._require_stable_header = False

    def _update_operation_snapshot(self, contact=""):
        """Save primitive observations without another COM scan after commit.

        This is a dated diagnostic view of the operation, not authorization for
        any later send. diagnostics()/prepare() still obtain a complete fresh
        view in their own COM apartment.
        """
        headers = list(self._observed_headers)
        available = self._observed_controls_available
        window = ({"handle": self._send_window[0], "process_id": self._send_window[1]}
                  if self._send_window is not None else None)
        with self._metadata_lock:
            self._snapshot = {"ready": self._ready and available, "window": window,
                              "editor": dict(self._observed_editor_info) if self._observed_editor_info else None,
                              "current_chat": contact if contact in headers else (headers[0] if headers else ""),
                              "header_candidates": headers[:8], "controls_available": available,
                              "compatibility": "controls_available" if available else ("controls_unavailable" if window else "window_missing"),
                              "control_count": len(self._tree_nodes or []), "tree_classes": [],
                              "snapshot_source": "operation_observation", "snapshot_timestamp": time.time()}

    def _update_snapshot(self, contact=""):
        try:
            editor = self._find_editor() if self._window else None
            headers = self._header_names(editor) if editor else []
            # Explicit diagnostics include history classes/counts; send target
            # discovery intentionally excludes those unrelated descendants.
            nodes = list(_walk(self._window, cancelled=self._cancelled)) if self._window else []
            available = bool(self._window and any(_prop(c, "ControlTypeName", "") in {"EditControl", "ListControl"} for c, _ in nodes))
            snapshot = {"ready": self._ready and available, "window": self._control_info(self._window),
                        "editor": self._control_info(editor), "current_chat": contact if contact in headers else (headers[0] if headers else ""),
                        "header_candidates": headers[:8], "controls_available": available,
                        "compatibility": "controls_available" if available else ("controls_unavailable" if self._window else "window_missing"),
                        "control_count": len(nodes), "tree_classes": list(dict.fromkeys(_prop(c, "ClassName", "") for c, _ in nodes))[:12],
                        "snapshot_source": "fresh_diagnostic", "snapshot_timestamp": time.time(),
                        "screen_reader_flag": self._screen_reader_active(),
                        "screen_reader_flag_note": "Windows SPI_GETSCREENREADER 不是讲述人运行状态；该值仅供诊断。",
                        "accessibility_help": "当前微信版本或账号未暴露可访问控件，无法安全自动发送。微信 4.1+ 的讲述人办法可能无效；需在本机诊断中实际看到搜索、聊天标题和可读编辑器后再验证。" if self._window and not available else "",
                        "supported_profiles": ["WeChat 3.x / WeChatMainWndForPC", "Weixin 4.x / exposed mmui controls (short and dotted IDs)"],
                        "accessibility_reference": "https://github.com/Hello-Mr-Crab/pywechat/blob/main/Weixin4.0.md"}
            with self._metadata_lock:
                self._snapshot = snapshot
        except Exception:
            pass

    @staticmethod
    def _screen_reader_active():
        if os.name != "nt":
            return None
        value = ctypes.c_int()
        try:
            if ctypes.windll.user32.SystemParametersInfoW(0x0046, 0, ctypes.byref(value), 0):
                return bool(value.value)
        except Exception:
            pass
        return None

    @staticmethod
    def _control_info(control):
        if control is None:
            return None
        return {"name": _name(control), "class": _prop(control, "ClassName", ""),
                "type": _prop(control, "ControlTypeName", ""), "automation_id": _prop(control, "AutomationId", ""),
                "process_id": _prop(control, "ProcessId", 0), "handle": _prop(control, "NativeWindowHandle", 0)}

    def _result(self, ok, kind, contact, error="", sent=0, attempted=0, *, result_fields=None):
        message = "" if ok else str(error or getattr(self._thread_results, "operation_error", "") or "发送未确认")
        warnings = list(getattr(self._thread_results, "cleanup_warnings", []))
        with self._metadata_lock:
            self._last_error = message or "; ".join(warnings)
            self._last_send = {"status": "accepted_by_ui" if ok else ("partial" if sent else "failed"),
                               "kind": kind, "contact": contact, "sent_parts": sent, "attempted_parts": attempted,
                               "error": message, "timestamp": time.time(),
                               "result_sequence": getattr(self._thread_results, "sequence", 0) + 1,
                               "verification": "local_ui_accepted" if ok else "not_confirmed", "delivery_verified": False,
                               "cleanup_warnings": warnings}
            self._last_send["draft_cleanup"] = getattr(self._thread_results,"draft_cleanup","not_needed")
            self._last_send["timings"] = dict(getattr(self._thread_results, "timings", {}))
            if result_fields:
                self._last_send.update(result_fields)
            self._thread_results.sequence = self._last_send["result_sequence"]
            self._thread_results.last_send = dict(self._last_send)
            self._thread_results.operation_error = message
            self._thread_results.cleanup_warnings = []
        if not ok:
            log.warning("[UIA] %s → %s: %s", kind, contact, message)
        log.info("[UIA耗时] kind=%s status=%s stages=%s", kind, self._last_send["status"], self._last_send["timings"])
        return bool(ok)

    def last_send_result(self):
        """Current caller's result only; no UI scan or cross-request metadata."""
        result = dict(getattr(self._thread_results, "last_send", {}))
        if "cleanup_warnings" in result:
            result["cleanup_warnings"] = list(result["cleanup_warnings"])
        if "timings" in result:
            result["timings"] = dict(result["timings"])
        return result

    def accessibility_error(self, *, include_last_error=False):
        recovery = self._gate_manager.snapshot()
        check = recovery.get("build_check") or {}
        version = (recovery.get("observed_build") or {}).get("file_version")
        if check.get("status") in {"version_mismatch", "sha256_mismatch"}:
            return f"当前微信 {version or '未知版本'} 尚未适配无障碍恢复，文字、语音、图片和朋友圈发送不可用；请更新适配。"
        return (self._last_error if include_last_error else "") or "微信无障碍控件未暴露；当前 UIA 无法安全发送，请查看 UIA 诊断"

    def diagnostics(self):
        """Read-only: no focus, search, clipboard, typing or click operations."""
        from desktop_coordinator import DESKTOP_LOCK
        acquired = DESKTOP_LOCK.acquire(blocking=False)
        if acquired:
            try:
                with self._uia_context():
                    self._ensure_window()
                    self._update_snapshot()
            except Exception as exc:
                self._set_error(exc)
            finally:
                DESKTOP_LOCK.release()
        with self._metadata_lock:
            return {**self._snapshot, "busy": not acquired, "last_error": self._last_error,
                    "unavailable_reason": "" if self._snapshot.get("ready") else self.accessibility_error(include_last_error=True),
                    "last_send": dict(self._last_send), "safety": "unique_search_and_header_and_focus",
                    "delivery_verification": "local_ui_only", "gate_recovery": self._gate_manager.snapshot()}

    def _composition_empty(self, editor):
        text = self._editor_text(editor)
        return text is not None and not text.strip() and not self._attachment_marker(editor)

    def _attachment_marker(self, editor, filename=""):
        draft = self._editor_text(editor)
        expected_name = os.path.basename(filename) if filename else ""
        if draft and ("\ufffc" in draft or draft.strip() in {"[图片]", "[文件]"}
                      or (filename and draft.strip() in {expected_name, os.path.abspath(filename)})):
            return True
        return next(self._attachment_previews(editor, filename), None) is not None

    def _attachment_previews(self, editor, filename=""):
        expected_name = os.path.basename(filename) if filename else ""
        rect = _prop(editor, "BoundingRectangle")
        for control, _ in _walk(_parent(editor) or editor, max_depth=3, max_nodes=65):
            cr = _prop(control, "BoundingRectangle")
            if rect is not None and cr is not None and (cr.bottom < rect.top or cr.left < rect.left - 20 or cr.right > rect.right + 20):
                continue
            parent = _parent(control)
            if _prop(parent, "ControlTypeName", "") in {"ButtonControl", "ToolBarControl", "MenuItemControl"}:
                continue  # Emoji, voice, screenshot and formatting toolbar icons.
            name, aid, klass = _name(control), str(_prop(control, "AutomationId", "")).casefold(), str(_prop(control, "ClassName", "")).casefold()
            semantic = (name in {"图片预览", "文件预览", "[图片]", "[文件]"}
                        or (expected_name and name == expected_name)
                        or any(s in aid for s in ("attachment_preview", "image_preview", "file_preview", "attached_file"))
                        or any(s in klass for s in ("attachmentpreview", "filepreview", "imagepreview")))
            if _visible(control) and semantic:
                yield control

    def _image_draft_state(self, editor, image_path):
        """Fingerprint one exact preview inside this composer, not its label alone."""
        text = self._editor_text(editor)
        if text not in {"", "\ufffc", "[图片]", "[文件]", os.path.basename(image_path), os.path.abspath(image_path)}:
            return None
        previews = list(self._attachment_previews(editor, image_path))
        ids = {tuple(control.GetRuntimeId()) for control in previews}
        editor_id = tuple(editor.GetRuntimeId())
        if len(ids) != 1 or not next(iter(ids)) or not editor_id:
            return None
        control = previews[0]
        path = self._control_path(control)
        if path is None:
            return None
        rect = _prop(control, "BoundingRectangle")
        bounds = tuple(getattr(rect, key) for key in ("left", "top", "right", "bottom")) if rect else None
        return (editor_id, text, next(iter(ids)), path, _name(control),
                _prop(control, "AutomationId", ""), _prop(control, "ClassName", ""), bounds)

    def _owned_image_draft(self, contact, editor, image_path, input_before=None, first_observation=False):
        # Called only after an empty composer and our own paste attempt. A
        # cancellation must not block this bounded, read-only provenance check.
        original_cancelled = self._cancelled
        self._cancelled = None
        try:
            stamp = self._input_stamp()
            lease = self._lease
            if (stamp is False or lease.owned_sequence != lease.adapter.sequence()
                    or not self._same_send_window() or not self._focused(editor)
                    or not self._control_in_window(editor) or not self._chat_verified(contact, editor)):
                return None
            snapshot = self._image_draft_state(editor, image_path)
            if snapshot is None or self._input_stamp() != stamp or lease.owned_sequence != lease.adapter.sequence():
                return None
            if first_observation and stamp != input_before:
                # LastInputInfo cannot distinguish our injected Ctrl+V from a
                # user's drag/drop during that call. Require an actual file
                # marker when that otherwise-ambiguous input window changed.
                names = {os.path.basename(image_path), os.path.abspath(image_path)}
                if snapshot[1] not in names and snapshot[4] not in names:
                    return None
            return (stamp, snapshot)
        except Exception:
            return None
        finally:
            self._cancelled = original_cancelled

    def _clear_owned_staged_image(self, contact, editor, image_path, proof):
        original_cancelled = self._cancelled
        self._cancelled = None
        try:
            if proof is None or self._owned_image_draft(contact, editor, image_path) != proof:
                self._thread_results.draft_cleanup = "preserved_image_draft_manual_check_required"
                self._thread_results.cleanup_warnings = list(getattr(self._thread_results, "cleanup_warnings", [])) + [
                    "图片草稿来源或当前状态无法确认，已保留；请手动核对并清理预览后继续发送"]
                return False
            self._auto.SendKeys("{Ctrl}a{Backspace}", interval=0.003, waitTime=0)
            cleared = self._composition_empty(editor)
            self._thread_results.draft_cleanup = "cleared_owned_unsubmitted_image" if cleared else "cleanup_unconfirmed"
            if cleared:
                self._refresh_target_stamp()
            return cleared
        except Exception:
            self._thread_results.draft_cleanup = "cleanup_unconfirmed"
            return False
        finally:
            self._cancelled = original_cancelled

    def _commit_guard(self, contact, editor):
        check_cancel(self._cancelled)
        if not self._same_send_window() or not self._chat_verified(contact, editor) or not self._focused(editor):
            raise RuntimeError("发送前聊天标题或编辑器焦点已改变，已取消发送")
        check_cancel(self._cancelled)

    def _clear_owned_staged_text(self, contact, editor, text):
        """Rollback only our exact unsubmitted paste in the still-focused chat."""
        original_cancelled = self._cancelled
        self._cancelled = None  # Cancellation stops delivery, not this tiny rollback.
        try:
            if (not self._same_send_window() or not self._foreground() or not self._control_in_window(editor)
                    or not self._focused(editor) or not self._chat_verified(contact,editor)
                    or self._editor_text(editor) != text):
                self._thread_results.draft_cleanup = "preserved_changed_draft_or_target"
                return False
            self._auto.SendKeys("{Ctrl}a{Backspace}", interval=0.003, waitTime=0)
            cleared = self._editor_text(editor) == ""
            self._thread_results.draft_cleanup = "cleared_owned_unsubmitted" if cleared else "cleanup_unconfirmed"
            if cleared:
                self._refresh_target_stamp()
            return cleared
        except Exception:
            self._thread_results.draft_cleanup = "cleanup_unconfirmed"
            return False
        finally:
            self._cancelled = original_cancelled

    def _paste_and_enter(self, text, contact=None):
        contact = contact or self._last_contact
        with self._send_phase("editor_focus_seconds"):
            editor = self._find_editor()
            if editor is None or not self._focus(editor):
                self._set_error("无法确认当前聊天编辑器")
                return False
        with self._send_phase("draft_precheck_seconds"):
            if not self._composition_empty(editor):
                self._set_error("编辑器已有草稿或无法读取内容，已保留草稿并取消发送")
                return False
        staged = entered = False
        try:
            with self._send_phase("paste_seconds"):
                self._lease.set_text(text)
                self._commit_guard(contact, editor)
                staged = True  # A provider may raise after the paste took effect.
                self._keys("{Ctrl}v")
            with self._send_phase("paste_readback_seconds"):
                if not wait_until(lambda: self._editor_text(editor) == text, self._timeout(), self._cancelled):
                    self._set_error("粘贴文字未能在编辑器中确认")
                    return False
            with self._send_phase("commit_guard_seconds"):
                self._commit_guard(contact, editor)
            with self._send_phase("enter_accept_seconds"):
                check_cancel(self._cancelled)
                entered = True  # Never erase or resend after Enter was attempted.
                self._keys("{Enter}")
                if not wait_until(lambda: self._composition_empty(editor), self._timeout(), self._cancelled):
                    self._set_error("回车后草稿未清空，发送结果不确定；请手动核对")
                    return False
            self._refresh_target_stamp()
            return True
        finally:
            if staged and not entered:
                with self._send_phase("draft_cleanup_seconds"):
                    self._clear_owned_staged_text(contact,editor,text)

    def send_text(self, contact, text, *, identity=None, cancel_event=None, generation=None, stream_sentence=False):
        self._thread_results.timings = {}
        import humanize
        if "PIL." in (text or ""):
            return self._result(False, "text", contact, "拒绝发送图像对象引用")
        # The model stream already supplied a safe natural sentence. Splitting
        # it again into tiny bubbles increases latency and repeats desktop work.
        bubbles = [str(text)] if stream_sentence and str(text).strip() else humanize.prepare_bubbles(text)
        if not bubbles:
            return self._result(False, "text", contact, "待发送文字为空")
        sent = 0
        last_bubble_at = None
        confirmed_texts = []
        def finish(ok, error=""):
            return self._result(ok, "text", contact, error, sent=sent, attempted=len(bubbles), result_fields={"confirmed_texts": list(confirmed_texts)})
        try:
            with self._operation(contact, identity, cancel_event, generation):
                for bubble in bubbles:
                    if last_bubble_at is not None and not humanize.wait_remaining(last_bubble_at, getattr(config, "HUMAN_BUBBLE_INTERVAL_SECONDS", 0.25), self._cancelled):
                        return finish(False, "发送间隔中已取消")
                    with self._send_phase("text_commit_seconds"):
                        confirmed = self._paste_and_enter(bubble, contact)
                    if not confirmed:
                        return finish(False)
                    sent += 1
                    confirmed_texts.append(bubble)
                    last_bubble_at = time.monotonic()
                    try:
                        import state
                        state.note_sent_text(bubble, session_id=(identity or {}).get("session_id", ""))
                    except (ImportError, TypeError):
                        pass
            return finish(True)
        except Exception as exc:
            return finish(False, exc)

    def _copy_file_to_clipboard(self, path):
        self._lease.set_files([path])

    def _copy_image_to_clipboard(self, path):
        self._lease.set_image(path)

    def send_image(self, contact, image_path, *, identity=None, cancel_event=None, generation=None):
        self._thread_results.timings = {}
        if not os.path.isfile(image_path):
            return self._result(False, "image", contact, "图片文件不存在")
        try:
            with self._operation(contact, identity, cancel_event, generation):
                editor = self._find_editor()
                if editor is None or not self._focus(editor) or not self._composition_empty(editor):
                    raise RuntimeError("图片编辑器无法确认或已有草稿")
                if getattr(config, "IMAGE_SEND_METHOD", "cf_hdrop") == "cf_hdrop":
                    self._copy_file_to_clipboard(image_path)
                else:
                    self._copy_image_to_clipboard(image_path)
                staged = entered = False
                proof = None
                try:
                    self._commit_guard(contact, editor)
                    staged = True
                    input_before = self._input_stamp()
                    try:
                        self._keys("{Ctrl}v")
                    finally:
                        # SendKeys can raise after insertion already took effect.
                        proof = self._owned_image_draft(contact, editor, image_path, input_before, True)
                    def preview_ready():
                        nonlocal proof
                        proof = proof or self._owned_image_draft(contact, editor, image_path, input_before, True)
                        return proof is not None or self._attachment_marker(editor, image_path)
                    if not wait_until(preview_ready, self._timeout(), self._cancelled):
                        raise RuntimeError("未观察到当前编辑器的图片预览，已取消发送")
                    self._commit_guard(contact, editor)
                    if proof is not None and self._owned_image_draft(contact, editor, image_path) != proof:
                        raise RuntimeError("图片发送前预览或编辑器内容已改变，已保留草稿")
                    check_cancel(self._cancelled)
                    entered = True
                    self._keys("{Enter}")
                    if not wait_until(lambda: self._composition_empty(editor), self._timeout(), self._cancelled):
                        raise RuntimeError("图片发送后预览未清空，结果不确定；请手动核对")
                    self._refresh_media_target(contact)
                finally:
                    if staged and not entered:
                        with self._send_phase("draft_cleanup_seconds"):
                            self._clear_owned_staged_image(contact, editor, image_path, proof)
            return self._result(True, "image", contact, sent=1, attempted=1)
        except Exception as exc:
            return self._result(False, "image", contact, exc, attempted=1)

    def send_sticker(self, contact, sticker_path="", text="", *, identity=None, cancel_event=None, generation=None):
        kwargs = {"identity": identity, "cancel_event": cancel_event, "generation": generation}
        if sticker_path:
            return self.send_image(contact, sticker_path, **kwargs)
        if text:
            return self.send_text(contact, text, **kwargs)
        return self._result(False, "sticker", contact, "表情内容为空")

    def _find_control_by_name(self, root, names, depth=0, max_depth=32):
        controls = [c for c, _ in _walk(root, max_depth=max_depth) if _name(c) in names and _visible(c)]
        return controls[0] if len(controls) == 1 else None

    @staticmethod
    def _voice_label(control):
        return re.sub(r"\s+", "", _name(control)).replace("（", "(").replace("）", ")")

    @staticmethod
    def _voice_dictation(control):
        # VoiceInput is speech-to-text, not the voice-message recorder.
        for _ in range(33):
            if control is None:
                break
            if str(_prop(control, "ClassName", "")).startswith("mmui::VoiceInput"):
                return True
            control = _parent(control)
        return False

    def _voice_button(self):
        labels = {"按住说话", "按住说话(空格)", "按住说话(右侧Alt)",
                  "Holdtotalk", "Holdtotalk(Space)", "Holdtotalk(RightAlt)"}
        for refresh in (False, True):
            buttons = [c for c, _ in self._control_nodes(refresh=refresh) if _visible(c)
                       and self._voice_label(c) in labels and not self._voice_dictation(c)]
            if buttons or not self._proof_restored:
                return buttons[0] if len(buttons) == 1 else None
        return None

    def _inline_voice_entry(self):
        nodes = self._control_nodes()
        entries = [c for c, _ in nodes
                   if self._voice_label(c) == "发语音(按住右Alt)" and _visible(c)
                   and _prop(c, "ControlTypeName") == "ButtonControl" and _prop(c, "ClassName") == "mmui::XButton"]
        if len(entries) == 1:
            current = _parent(entries[0])
            for _ in range(8):
                if current is None:
                    break
                if _prop(current, "AutomationId") == "tool_bar_accessible":
                    # The recording view mounts directly under this right toolbar.
                    right = [c for c in current.GetChildren() if _prop(c, "ClassName") == "mmui::ChatInputToolbarRightView"]
                    current = right[0] if len(right) == 1 else None
                if _prop(current, "ClassName") == "mmui::ChatInputToolbarRightView":
                    if self._control_in_window(current):
                        return entries[0], current
                    break
                current = _parent(current)
        # Right Alt doesn't need the microphone button: some builds label or
        # place it differently. Keep the live recorder's toolbar in that case.
        toolbars = [c for c, _ in nodes
                    if _prop(c, "ClassName") == "mmui::ChatInputToolbarRightView" and _visible(c)]
        if len(toolbars) == 1 and self._control_in_window(toolbars[0]):
            return None, toolbars[0]
        return None, None

    def _inline_voice_panels(self, toolbar):
        return [c for c in toolbar.GetChildren()
                if _prop(c, "ClassName") == "mmui::ChatVoiceRecordView" and _visible(c)
                and _prop(c, "ProcessId") == self._send_window[1]]

    @staticmethod
    def _inline_voice_actions(panel):
        buttons = [(c, _name(c)) for c, _ in _walk(panel, max_depth=4, max_nodes=24)
                   if _prop(c, "ControlTypeName") == "ButtonControl" and _visible(c)]
        cancel = [c for c, name in buttons if name == "取消"]
        send = [c for c, name in buttons if name == "发送语音"]
        return (cancel[0] if len(cancel) == 1 else None, send[0] if len(send) == 1 else None)

    def _cancel_inline_voice(self, toolbar, recording, contact, editor):
        # Cancellation stops delivery, but must not interrupt recorder cleanup.
        cancelled = self._cancelled
        self._cancelled = None
        try:
            def same_chat():
                return self._same_send_window() and self._control_in_window(toolbar) and self._chat_verified(contact, editor)
            panels = self._inline_voice_panels(toolbar)
            if not panels:
                # No inline button to click. Don't spend seconds traversing
                # chat history with Alt held; use the already bound gesture.
                if self._voice_live_guard(self._voice_watch):
                    return self._cancel_recording(recording)
                if same_chat():
                    self._cancel_recording(recording)
                return
            if len(panels) != 1 or not same_chat():
                return
            panel = panels[0]
            if recording is None:
                # The pane appeared after the start probe. Claim it only if
                # our gesture is still the last input in the verified window.
                if not self._voice_live_guard(self._voice_watch):
                    return
            else:
                recording_id = _runtime_id(recording)
                if not any(_runtime_id(c) == recording_id for c, _ in _walk(panel, max_depth=4, max_nodes=24)):
                    return
            panel_id = _runtime_id(panel)
            def still_same_panel():
                current = self._inline_voice_panels(toolbar)
                return len(current) == 1 and _runtime_id(current[0]) == panel_id
            cancel, _ = self._inline_voice_actions(panel)
            if cancel is not None and self._foreground():
                try:
                    cancel.Click(simulateMove=False, waitTime=0)
                except Exception:
                    log.debug("[UIA] 录音取消点击结果待核验", exc_info=True)
                if wait_until(lambda: not still_same_panel(), 0.35, interval=0.025):
                    return
            if same_chat() and still_same_panel():
                self._cancel_recording(panel)
        finally:
            self._cancelled = cancelled

    @staticmethod
    def _voice_scan_skip(control):
        """The recorder belongs to the composer, never chat/search history."""
        klass = str(_prop(control, "ClassName", ""))
        if klass == "mmui::ChatVoiceRecordView":
            return False
        return (klass.startswith("mmui::VoiceInput")
                or _aid_segment(control, "chat_message_list", "chat_history_list", "message_list", "history_list", "search_list", "search_result_list")
                or any(word in klass.casefold() for word in ("messagelist", "historylist", "searchresult")))

    def _voice_recording_context(self, control):
        in_list = False
        for _ in range(33):
            if control is None:
                return not in_list
            klass = str(_prop(control, "ClassName", ""))
            if klass.startswith("mmui::VoiceInput"):
                return False
            if klass == "mmui::ChatVoiceRecordView":
                return True  # Its UIA role may itself be DocumentControl.
            if self._voice_scan_skip(control):
                return False
            in_list = in_list or _prop(control, "ControlTypeName", "") in {"ListControl", "ListItemControl", "TreeControl", "TreeItemControl", "DocumentControl"}
            control = _parent(control)
        return False

    def _voice_recording_state(self):
        """Observe the live recorder, including its same-process 4.x popup."""
        watch = getattr(self, "_voice_watch", None)
        if watch is not None:
            return self._voice_watched_state(watch)
        roots = self._recorder_roots()
        # 4.x normally exposes the recorder as a small popup. Read it first
        # instead of traversing hundreds of chat-history nodes while holding Alt.
        roots.append(self._window)
        for root in roots:
            # Recording creates/replaces nodes after the hold gesture; never
            # consult the text-send tree snapshot for this changing UI.
            for control, _ in _walk(root, cancelled=self._cancelled,
                                    skip_children=self._voice_scan_skip if root is self._window else None):
                if (_prop(control, "ClassName") == "mmui::ChatVoiceRecordView" and _visible(control)
                        and _prop(_parent(control), "ClassName") == "mmui::ChatInputToolbarRightView"):
                    return control
                label = self._voice_label(control)
                if (_visible(control)
                        and self._recording_label(label)
                        and self._voice_recording_context(control)):
                    return control
        return None

    def _recorder_roots(self):
        """Open only this WeChat process's recorder windows, never the desktop."""
        pid = _prop(self._window, "ProcessId", 0)
        native = native_top_windows(self._auto, pid=pid)
        if native is None:
            candidates = self._auto.GetRootControl().GetChildren()
        else:
            # Qt's native window class can be QWindowToolSaveBits while the
            # accessible element exposes mmui::ChatVoiceRecordView. Only PID
            # and the already-known main HWND are native filter facts here.
            candidates = [self._auto.ControlFromHandle(item["hwnd"]) for item in native
                if item["hwnd"] != _prop(self._window, "NativeWindowHandle", 0)]
        return [root for root in candidates if _prop(root, "ProcessId", 0) == pid and _visible(root)
            and _prop(root, "ClassName", "") == "mmui::ChatVoiceRecordView"
            and _runtime_id(root) != _runtime_id(self._window)]

    @staticmethod
    def _recording_label(label):
        label = label.replace(",", "，").casefold()
        return label in {"松开发送", "正在录音", "录音中", "松开结束", "releasetosend", "正在发送语音，松开发送"} or label.startswith("正在发送语音，松开发送，")

    def _voice_watched_state(self, watch):
        """One live button read normally; only a small recorder subtree otherwise."""
        button = watch["button"]
        if (button is not None and _visible(button) and _prop(button, "ProcessId", 0) == watch["main"][1]
                and self._recording_label(self._voice_label(button))):
            watch["evidence"] = "hold_button_label"
            return button
        def recorder(root, source):
            pid = _prop(root, "ProcessId", 0)
            if pid != watch["main"][1]:
                return None
            handle = int(_prop(root, "NativeWindowHandle", 0) or 0)
            watch["recording_window"] = (handle, pid) if handle else watch["main"]
            if (_prop(_parent(root), "ClassName") == "mmui::ChatInputToolbarRightView"
                    and _visible(root) and all(self._inline_voice_actions(root))):
                watch["evidence"] = "inline_actions"
                return root
            for control, _ in _walk(root, max_depth=4, max_nodes=24, cancelled=self._cancelled):
                if self._recording_label(self._voice_label(control)) and _visible(control):
                    watch["evidence"] = source
                    return control
            return None
        # On 4.x the embedded recorder mounts directly under the right
        # toolbar. Inspect it before opening native popup providers.
        toolbar = watch.get("toolbar")
        if toolbar is not None:
            panels = self._inline_voice_panels(toolbar)
            if len(panels) == 1:
                state = recorder(panels[0], "embedded_recorder_label")
                if state is not None:
                    return state
        current = self._auto.GetForegroundControl()
        for _ in range(8):
            if current is None:
                break
            klass = str(_prop(current, "ClassName", ""))
            if klass == "mmui::ChatVoiceRecordView":
                return recorder(current, "foreground_recorder_label")
            if klass.startswith("mmui::VoiceInput"):
                return None
            current = _parent(current)
        # A small Qt popup may keep the main window foreground. Try native
        # same-process popups before walking any part of the main window.
        for root in self._recorder_roots():
            state = recorder(root, "same_process_popup_label")
            if state is not None:
                return state
        roots = [toolbar] if toolbar is not None else [watch["composer"]]
        if toolbar is None and watch["composer"] is not self._window:
            roots.append(self._window)
        for root in roots:
            for child, _ in _walk(root, max_depth=6, max_nodes=100, cancelled=self._cancelled,
                    skip_children=self._voice_scan_skip):
                if _prop(child, "ClassName", "") == "mmui::ChatVoiceRecordView":
                    state = recorder(child, "embedded_recorder_label")
                    if state is not None:
                        return state
        return None

    def _voice_live_guard(self, watch):
        """During the hold, use native window/input facts, without UIA scans."""
        check_cancel(self._cancelled)
        main, popup, user = watch["main"], watch["recording_window"], watch["native"]
        if user is not None:
            front = int(user.GetForegroundWindow() or 0)
            matches = front in {main[0], popup[0]} or bool(user.IsChild(main[0], front)
                and int(user.GetAncestor(front, 2) or 0) == main[0])
            if not front or not matches or self._native_owner(user, front)[1] != main[1]:
                return False
        else:
            front = self._window_identity(self._auto.GetForegroundControl())
            if front not in {main, popup}:
                return False
        stamp = watch["input_stamp"]
        return stamp is None or stamp is False or self._input_stamp() == stamp

    def _voice_foreground(self, recording=None):
        if self._foreground():
            return True
        current = recording
        for _ in range(33):
            if current is None:
                break
            if (_prop(current, "ClassName", "") == "mmui::ChatVoiceRecordView"
                    and _prop(current, "ProcessId", 0) == _prop(self._window, "ProcessId", -1)
                    and self._foreground(current)):
                return True
            current = _parent(current)
        return False

    def _key_down(self, vk):
        ctypes.windll.user32.keybd_event(vk, 0, 0x0001 if vk == 0xA5 else 0, 0)

    def _key_up(self, vk):
        ctypes.windll.user32.keybd_event(vk, 0, (0x0001 if vk == 0xA5 else 0) | 0x0002, 0)

    def _mouse_down(self, x, y):
        ctypes.windll.user32.SetCursorPos(x, y)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)

    def _mouse_up(self, x, y):
        ctypes.windll.user32.SetCursorPos(x, y)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)

    def _cancel_recording(self, recording=None):
        """Cancel in WeChat only; never type Escape into another foreground app."""
        if self._voice_foreground(recording):
            self._auto.SendKeys("{Esc}", waitTime=0)
            return True
        elif os.name == "nt":
            hwnd = int(_prop(self._window, "NativeWindowHandle", 0) or 0)
            if hwnd:
                user = ctypes.windll.user32
                user.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
                user.PostMessageW(hwnd, 0x0100, 0x1B, 0)
                user.PostMessageW(hwnd, 0x0101, 0x1B, 0)

    @contextmanager
    def _voice_devices(self):
        recovery_token = None
        playback = recording = None
        changed_playback = changed_recording = False
        try:
            with self._send_phase("audio_prepare_seconds"):
                check_cancel(self._cancelled)
                outputs = audio_helper.list_playback_devices(include_default=False)
                check_cancel(self._cancelled)
                inputs = audio_helper.list_recording_devices(include_default=False)
                check_cancel(self._cancelled)
                if not any("CABLE" in d["name"].upper() for d in outputs) or not any("CABLE" in d["name"].upper() for d in inputs):
                    raise RuntimeError("缺少 VB-Cable 播放或录音端点，已取消语音")
                devices = [d for d in inputs if "CABLE" in d["name"].upper()]
                if len(devices) != 1:
                    raise RuntimeError("VB-Cable 录音设备不唯一")
                playback = audio_helper.get_default_playback_device_info()
                check_cancel(self._cancelled)
                recording = audio_helper.get_default_recording_device_info()
                check_cancel(self._cancelled)
                if not playback or not recording:
                    raise RuntimeError("无法备份音频设备，不进行临时设备切换")
                recovery_token = external_state("audio_devices")
                changed_playback = True  # Restore even after a partially failing role update.
                audio_helper.set_default_playback_device(device_name=str(getattr(config, "VB_CABLE_DEVICE", "CABLE Input") or "CABLE Input"), devices=outputs)
                check_cancel(self._cancelled)
                changed_recording = True
                audio_helper.set_default_recording_device(device_id=devices[0]["id"], devices=inputs)
                check_cancel(self._cancelled)
            yield
        finally:
            cleanup_started = time.monotonic()
            restored = True
            for changed, original, restore, label in (
                (changed_playback, playback, audio_helper.restore_default_playback_device, "播放"),
                (changed_recording, recording, audio_helper.restore_default_recording_device, "录音")):
                if not changed:
                    continue
                try:
                    if not restore(original["id"], role_ids=original.get("role_ids")):
                        raise RuntimeError("设备还原接口未确认成功")
                except Exception as error:
                    restored = False
                    # A sent voice must not be marked unsent because cleanup
                    # failed. Expose the failure while trying the other flow.
                    warning = f"{label}设备恢复失败: {error}"
                    self._thread_results.cleanup_warnings = list(getattr(self._thread_results, "cleanup_warnings", [])) + [warning]
                    log.warning("[UIA] %s", warning)
            timings = getattr(self._thread_results, "timings", {})
            timings["audio_restore_seconds"] = round(time.monotonic() - cleanup_started, 3)
            self._thread_results.timings = timings
            if restored:
                release_external_state(recovery_token)

    def _voice_ready_editor(self, contact):
        # Device role changes can rebuild Qt's composer or move focus to its
        # toolbar. Reacquire inside the same verified physical window once.
        check_cancel(self._cancelled)
        proof = self._target_proof
        if proof is not None and proof["input_stamp"] != self._input_stamp():
            raise RuntimeError("语音准备期间检测到其他桌面输入，已取消发送")
        if not self._same_send_window() or not self._activate():
            raise RuntimeError("语音发送前无法恢复已核验的微信窗口焦点")
        editor = self._find_editor()
        if editor is None:
            raise RuntimeError("语音发送前未找到当前聊天编辑器")
        if not self._focus(editor):
            # A device change can leave an old Qt wrapper readable but unable
            # to receive focus. Refresh once on failure, not on every voice.
            if not self._foreground():
                raise RuntimeError("语音发送前无法聚焦当前聊天编辑器")
            editor = self._find_editor(refresh=True)
            if editor is None or not self._focus(editor):
                raise RuntimeError("语音发送前无法聚焦当前聊天编辑器")
        text = self._editor_text(editor)
        if text is None:
            editor = self._find_editor(refresh=True)
            if editor is None or not self._focus(editor):
                raise RuntimeError("语音发送前未找到可用的当前聊天编辑器")
            text = self._editor_text(editor)
            if text is None:
                raise RuntimeError("语音发送前无法读取编辑器内容，已保留当前草稿")
        if text.strip() or self._attachment_marker(editor):
            raise RuntimeError("语音发送前编辑器已有草稿，已保留草稿")
        return editor

    def send_voice(self, contact, audio_path, *, identity=None, cancel_event=None, generation=None):
        self._thread_results.timings = {}
        if not getattr(config, "VOICE_ENABLED", True):
            return self._result(False, "voice", contact, "语音功能已关闭")
        if not os.path.isfile(audio_path):
            return self._result(False, "voice", contact, "音频文件不存在")
        try:
            with self._operation(contact, identity, cancel_event, generation), self._voice_devices():
                player_started = time.monotonic()
                import pygame
                if pygame.mixer.get_init():
                    raise RuntimeError("音频播放设备正被其他任务使用")
                pygame.mixer.init()
                try:
                    check_cancel(self._cancelled)
                    sound = pygame.mixer.Sound(audio_path)
                    duration = float(sound.get_length())
                    if duration <= 0 or duration > 60:
                        raise RuntimeError("语音时长必须在 0 到 60 秒之间")
                    player = pygame.mixer.find_channel()
                    if player is None:
                        raise RuntimeError("没有可用的语音播放通道")
                    self._thread_results.timings["voice_player_prepare_seconds"] = round(time.monotonic() - player_started, 3)
                    check_cancel(self._cancelled)
                    with self._send_phase("voice_editor_refresh_seconds"):
                        editor = self._voice_ready_editor(contact)
                    keyboard = bool(getattr(config, "VOICE_RIGHT_ALT_ENABLED", True))
                    with self._send_phase("voice_preflight_seconds"):
                        available_button = self._voice_button()
                        button = None if keyboard else available_button
                        entry, toolbar = self._inline_voice_entry() if button is None else (None, None)
                        if button is None and entry is None and not keyboard:
                            raise RuntimeError("未发现按住说话控件；可在面板启用右 Alt 录音")
                        already_recording = self._inline_voice_panels(toolbar) if not keyboard and toolbar is not None else self._voice_recording_state()
                        if already_recording:
                            raise RuntimeError("语音发送前已有录音状态，已取消本次发送")
                    gesture = "right_alt" if keyboard else "button" if button is not None else "inline_click"
                    log.info("[UIA] 语音录音入口 gesture=%s recording_button=%s inline_toolbar=%s",
                             gesture, available_button is not None, toolbar is not None)
                    padding = max(0, min(float(getattr(config, "VOICE_EXTRA_PADDING_MS", 300)), 1000)) / 1000
                    hold = max(duration, float(getattr(config, "VOICE_HOLD_MS", 0)) / 1000) + padding
                    position = None
                    recording = None
                    inline_recording = False
                    renew_target = False
                    if button is not None:
                        rect = button.BoundingRectangle
                        position = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
                    watch = {"button": available_button, "composer": _parent(editor) or editor, "toolbar": toolbar,
                             "main": self._send_window, "recording_window": self._send_window,
                             "native": self._native_user32(), "input_stamp": None}
                    self._voice_watch = watch
                    with self._send_phase("voice_commit_guard_seconds"):
                        self._commit_guard(contact, editor)
                    recording_token = external_state("recording")
                    def inline_ready():
                        panels = self._inline_voice_panels(toolbar)
                        ready = len(panels) == 1 and all(self._inline_voice_actions(panels[0]))
                        return panels[0] if ready else None
                    try:
                        gesture_started = time.monotonic()
                        if gesture == "inline_click":
                            entry.Click(simulateMove=False, waitTime=0)
                        elif position:
                            self._mouse_down(*position)
                        else:
                            self._key_down(0xA5)
                        watch["input_stamp"] = self._input_stamp()
                        with self._send_phase("recording_start_seconds"):
                            if gesture == "inline_click":
                                recording = wait_until(inline_ready, self._timeout(), self._cancelled, interval=0.016)
                            else:
                                recording = wait_until(self._voice_recording_state, min(1.2, self._timeout()), self._cancelled)
                        if not recording:
                            error = "未观察到微信录音状态"
                            if keyboard and (entry is not None or available_button is not None):
                                error += "；可在语音设置中关闭“优先使用右 Alt 录音”改用已识别的按钮"
                            raise RuntimeError(error)
                        recording_class = _prop(recording, "ClassName", "")
                        inline_recording = (recording_class == "mmui::ChatVoiceRecordView"
                                            and _prop(_parent(recording), "ClassName") == "mmui::ChatInputToolbarRightView")
                        log.info("[UIA] 录音确认 gesture=%s evidence=%s class=%s type=%s", gesture,
                                 watch.get("evidence", "inline_actions"), recording_class, _prop(recording, "ControlTypeName", ""))
                        observed_at = time.monotonic()
                        check_cancel(self._cancelled)
                        if not self._voice_live_guard(watch):
                            raise RuntimeError("录音期间焦点或聊天对象已改变")
                        # The gesture already bound this chat and toolbar. Walking the
                        # title again with the mic open records the UIA wait as silence.
                        check_cancel(self._cancelled)
                        self._thread_results.timings["post_observation_guard_seconds"] = round(time.monotonic() - observed_at, 3)
                        # Decoding and channel allocation finished before the
                        # hold gesture. Start the prepared sound only after the
                        # live recorder and current target have been confirmed.
                        player.play(sound)
                        self._thread_results.timings["gesture_to_play_seconds"] = round(time.monotonic() - gesture_started, 3)
                        with self._send_phase("voice_playback_seconds"):
                            deadline = time.monotonic() + hold
                            while time.monotonic() < deadline:
                                check_cancel(self._cancelled)
                                if not self._voice_live_guard(watch):
                                    raise RuntimeError("录音期间焦点或聊天对象已改变")
                                check_cancel(self._cancelled)
                                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                        check_cancel(self._cancelled)
                        if inline_recording:
                            panels = self._inline_voice_panels(toolbar or _parent(recording))
                            if len(panels) != 1 or _runtime_id(panels[0]) != _runtime_id(recording):
                                raise RuntimeError("发送前录音栏或聊天对象已改变")
                            if not self._voice_live_guard(watch):
                                raise RuntimeError("录音期间焦点或聊天对象已改变")
                            if gesture == "inline_click":
                                cancel, send = self._inline_voice_actions(panels[0])
                                if cancel is None or send is None:
                                    raise RuntimeError("无法确认当前录音栏的发送控件")
                                send.Click(simulateMove=False, waitTime=0)
                                watch["input_stamp"] = self._input_stamp()
                    except Exception:
                        sound.stop()
                        with self._send_phase("voice_cancel_seconds"):
                            own_idle_hold = (keyboard and recording is None and self._target_proof is not None
                                and type(watch["input_stamp"]) is int and not self._cancelled()
                                and self._voice_live_guard(watch))
                            if toolbar is not None or inline_recording:
                                cancelled_in_chat = self._cancel_inline_voice(toolbar or _parent(recording), recording, contact, editor)
                            else:
                                cancelled_in_chat = self._cancel_recording(recording)
                            renew_target = own_idle_hold and cancelled_in_chat is True
                        raise
                    finally:
                        if position:
                            self._mouse_up(*position)
                        elif keyboard:
                            self._key_up(0xA5)
                        sound.stop()
                        release_external_state(recording_token)
                        if renew_target:
                            # Our own Escape/key-up invalidated the original input
                            # stamp. Keep its exact native paths only if the chat
                            # and empty editor survive cleanup without further input.
                            stamp = self._input_stamp()
                            try:
                                if (type(stamp) is int and self._same_send_window() and self._focused(editor)
                                        and _runtime_id(editor) == self._target_proof["editor"][1]
                                        and self._editor_text(editor) == "" and not self._voice_recording_state()
                                        and self._chat_verified(contact, editor) and stamp == self._input_stamp()):
                                    self._target_proof["input_stamp"] = stamp
                            except Exception:
                                self._target_proof = None
                    with self._send_phase("recording_end_seconds"):
                        def ended():
                            if inline_recording and toolbar is not None:
                                return self._control_in_window(toolbar) and not self._inline_voice_panels(toolbar)
                            return not self._voice_recording_state()
                        if not wait_until(ended, self._timeout(), self._cancelled):
                            if (keyboard and toolbar is not None
                                    and _prop(recording, "ClassName") == "mmui::ChatVoiceRecordView"):
                                self._cancel_inline_voice(toolbar, recording, contact, editor)
                            raise RuntimeError("录音状态未结束，结果不确定")
                        check_cancel(self._cancelled)
                        self._remember_target(contact, self._identity)
                finally:
                    self._voice_watch = None
                    pygame.mixer.quit()
            return self._result(True, "voice", contact, sent=1, attempted=1)
        except Exception as exc:
            return self._result(False, "voice", contact, exc, attempted=1)
