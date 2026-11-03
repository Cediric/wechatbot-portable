"""Main-window UIA browsing and verified Moments interactions.

Native post/comment IDs come from WeFlow. A display name is never an identity.
UI controls are reacquired in the operation's COM apartment and desktop lease.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import json
import logging
import os
import re
import time

import config
import state
from moments_feed import MomentsFeedClient, real_id, find_new_self_comment
from stable_id import validate_username

from desktop_coordinator import check_cancel, desktop_operation, wait_until
from moments_sender import MomentsSender, _runtime_id
from uia_sender import _name, _prop, _parent, _visible, _walk

log = logging.getLogger("weflow-bridge")


class MomentsSocialSender(MomentsSender):
    def __init__(self, feed_client=None):
        super().__init__()
        self.feed_client = feed_client or MomentsFeedClient(getattr(state, "identity_registry", None))

    @staticmethod
    def _flat(text):
        return " ".join(str(text).split())

    @staticmethod
    def _control_shape(control):
        rect = _prop(control, "BoundingRectangle")
        return (_name(control), _prop(control, "ControlTypeName", ""),
            _prop(control, "ClassName", ""), _prop(control, "AutomationId", ""),
            (rect.left, rect.top, rect.right, rect.bottom) if rect else None)

    def _coalesce_wrappers(self, controls):
        """Collapse only identical ancestor/child proxies, never sibling rows."""
        matches = {}
        for control in controls:
            token = _runtime_id(control)
            if not token:
                return []
            matches[token] = control
        if len(matches) > 1:
            shapes = {}
            for token, control in matches.items():
                shapes.setdefault(self._control_shape(control), []).append(token)
            for tokens in shapes.values():
                if len(tokens) < 2:
                    continue
                for token in tokens:
                    control = matches.get(token)
                    if control is None:
                        continue
                    parent = _parent(control)
                    for _ in range(32):
                        if parent is None or _prop(parent, "ProcessId", 0) != self._main_pid:
                            break
                        parent_token = _runtime_id(parent)
                        if parent_token != token and parent_token in tokens:
                            matches.pop(parent_token, None)
                        if _prop(parent, "NativeWindowHandle", 0):
                            break
                        parent = _parent(parent)
        return list(matches.values())

    def _unique(self, root, predicate):
        controls = [control for control, _ in _walk(root, cancelled=self._cancelled,
            skip_children=(lambda c: self._voice_scan_skip(c) or c is not root and (self._aid(c, "sns_list")
                or _prop(c, "ClassName", "") in {"mmui::TimeLineListView", "mmui::TimelineCell",
                    "mmui::TimelineContentCell", "mmui::TimelineCommentCell"})) if root is not None else None)
            if _visible(control) and _prop(control, "ProcessId", 0) == self._main_pid and predicate(control)]
        controls = self._coalesce_wrappers(controls)
        return controls[0] if len(controls) == 1 else None

    def _timeline_cells(self, feed):
        classes = {"mmui::TimelineCell", "mmui::TimelineContentCell", "mmui::TimelineCommentCell"}
        # Recent Qt providers insert anonymous layout panes between the list
        # and its delegates. Never recurse into a post/comment's private tree.
        controls = [control for control, _ in _walk(feed, cancelled=self._cancelled,
            skip_children=lambda c: c is not feed and (_prop(c, "ClassName", "") in classes
                or bool(_prop(c, "NativeWindowHandle", 0) not in (0, self._main_handle))
                or _prop(c, "ProcessId", 0) != self._main_pid))
            if _prop(control, "ProcessId", 0) == self._main_pid and _prop(control, "ClassName", "") in classes]
        return self._coalesce_wrappers(controls)

    def _row_contains_hit(self, row, hit):
        """A wheel may hit visible post content instead of its Qt delegate."""
        token = _runtime_id(row)
        if not token:
            return False
        current = hit
        for _ in range(33):
            if current is None:
                break
            if not _visible(current) or _prop(current, "ProcessId", 0) != self._main_pid:
                return False
            handle = _prop(current, "NativeWindowHandle", 0)
            if handle and handle != self._main_handle:
                return False
            if _runtime_id(current) == token:
                return True
            current = _parent(current)
        # Virtual Qt children can omit their parent link while remaining in
        # the current row's enumerated subtree. Runtime IDs still prove the hit.
        hit_token = _runtime_id(hit)
        return bool(hit_token and _visible(hit) and _prop(hit, "ProcessId", 0) == self._main_pid
            and any(_runtime_id(child) == hit_token for child, _ in _walk(row, max_nodes=120,
                cancelled=self._cancelled, skip_children=lambda c: _prop(c, "ProcessId", 0) != self._main_pid
                    or _prop(c, "NativeWindowHandle", 0) not in (0, self._main_handle))
                if _prop(child, "ProcessId", 0) == self._main_pid
                and _prop(child, "NativeWindowHandle", 0) in (0, self._main_handle)))

    def _scroll_hit(self, row, feed, hit):
        if self._row_contains_hit(row, hit):
            return True
        # Wheel navigation needs ownership of the list, not a clickable post.
        # Qt sometimes hit-tests its list viewport instead of virtual rows.
        return (self._same_control(feed, hit) and _prop(hit, "ProcessId", 0) == self._main_pid
            and self._window_identity(self._root_for(hit)) == (self._main_handle, self._main_pid))

    def _wheel_viewport_hit(self, feed, hit, x, y):
        """An empty Qt hit-test must not make a visible list unscrollable."""
        if self._row_contains_hit(feed, hit):
            return True
        if _prop(hit, "ProcessId", 0) or _runtime_id(hit):
            return False  # A real conflicting hit remains an obstruction.
        user = self._native_user32()
        if user is None:
            return False
        # Navigation only: no click, focus change, comment or submission. The
        # live UIA viewport supplies the point and Win32 proves its HWND owner.
        from ctypes import wintypes
        locate = user.WindowFromPoint
        locate.argtypes, locate.restype = [wintypes.POINT], ctypes.c_void_p
        handle = int(locate(wintypes.POINT(x, y)) or 0)
        return bool(handle and int(user.GetAncestor(handle, 2) or handle) == self._main_handle
            and self._native_owner(user, handle)[1] == self._main_pid)

    def _ui_error(self, stage, message, *, main=None, feed=None, hit=None, expected_token=None):
        # Cancellation hands the desktop straight back to chat. Diagnostics
        # must not walk a cold provider while the private reply waits for it.
        check_cancel(self._cancelled)
        try:
            foreground = self._auto.GetForegroundControl()
        except Exception:
            foreground = None
        detail = {"substage": stage, "expected_main": {"hwnd": self._main_handle, "pid": self._main_pid},
            "observed_main": self._native_attributes(main), "observed_feed": self._native_attributes(feed),
            "observed_hit": self._native_attributes(hit), "observed_foreground": self._native_attributes(foreground),
            "expected_feed_runtime_id": list(expected_token or ()),
            "foreground_reason": getattr(self, "_activation_failure", ""),
            "main_lookup": getattr(self, "_main_lookup_observed", None)}
        # No names, post bodies, comments, or credentials enter this diagnostic.
        error = RuntimeError("[%s] %s；预期主窗=%s/%s；实际主窗=%s/%s；列表=%s/%s/%s；前台=%s/%s；原因=%s" % (
            stage, message, self._main_handle, self._main_pid,
            detail["observed_main"]["hwnd"], detail["observed_main"]["pid"],
            detail["observed_feed"]["hwnd"], detail["observed_feed"]["pid"], detail["observed_feed"]["role"],
            detail["observed_foreground"]["hwnd"], detail["observed_foreground"]["pid"], detail["foreground_reason"]))
        error.uia_diagnostics = detail
        return error

    def _feed_after_navigation(self, previous_shape):
        """Completed navigation may rebuild Qt's provider inside the same HWND."""
        check_cancel(self._cancelled)
        main = self._main_window()
        feed = self._feed(main) if main is not None else None
        if main is not None and feed is None:
            if not self._foreground(main):
                raise RuntimeError("朋友圈导航后前台已经改变")
            self._window = main
            feed = self._wait_main(self._ready_feed, recover_missing=True)
            main = self._main_window()
        root = self._root_for(feed) if feed is not None else None
        if (main is None or feed is None or not _runtime_id(feed)
                or self._window_identity(main) != (self._main_handle, self._main_pid)
                or root is None or self._window_identity(root) != (self._main_handle, self._main_pid)
                or _prop(feed, "ProcessId", 0) != self._main_pid
                or not self._foreground(main)):
            raise RuntimeError("朋友圈导航后列表或主窗口已经改变")
        return feed

    def _profile_popup(self):
        matches = {}
        # Native ProfileUniquePop is a physical top-level popup. Scanning the
        # entire feed before reading it can consume the popup's wait budget.
        for root in self._roots():
            if (_visible(root) and _prop(root, "ClassName", "") == "mmui::ProfileUniquePop"
                    and _prop(root, "ProcessId", 0) == self._main_pid
                    and _prop(root, "NativeWindowHandle", 0)):
                token = _runtime_id(root)
                if not token:
                    return None
                matches[(_prop(root, "NativeWindowHandle", 0), self._main_pid)] = root
        if matches:
            return next(iter(matches.values())) if len(matches) == 1 else None
        # Qt can expose the profile as a child of its physical main window.
        # A profile popup is outside the timeline; do not scan every post on
        # each preflight just to establish that no profile is open.
        main = self._main_window()
        embedded = [control for control, _ in _walk(main, cancelled=self._cancelled,
            skip_children=lambda c: self._voice_scan_skip(c) or self._aid(c, "sns_list")
                or _prop(c, "ClassName", "") == "mmui::TimeLineListView")
            if _visible(control) and _prop(control, "ProcessId", 0) == self._main_pid
            and _prop(control, "ClassName", "") == "mmui::ProfileUniquePop"
            and _prop(control, "ControlTypeName", "") in {"WindowControl", "PaneControl"}] if main else []
        embedded = self._coalesce_wrappers(embedded)
        popup = embedded[0] if len(embedded) == 1 else None
        root = self._root_for(popup) if popup is not None else None
        return popup if root is not None and self._window_identity(root) == (self._main_handle, self._main_pid) else None

    def _fresh_identities(self):
        import name_refresh
        snapshot, result = name_refresh._get_snapshot(force=True)
        if result == "fresh" and isinstance(snapshot, dict):
            contacts = snapshot["contacts"]
        elif result == "unavailable":
            # Unmodified WeFlow exposes native username/alias pairs too. Always
            # match them against the WeChat profile card before an interaction.
            contacts, ok = name_refresh._get_list("contacts")
            if not ok or not contacts:
                raise RuntimeError("WeFlow 联系人接口没有返回微信 ID 与微信号映射")
        else:
            raise RuntimeError("WeFlow 身份快照读取失败，请检查当前账号和数据库连接")
        records = {}
        for row in contacts:
            username = row.get("username", "")
            if isinstance(username, str) and username.endswith("@chatroom"):
                continue
            try:
                validate_username(username)
            except (TypeError, ValueError):
                continue
            if username in records:
                raise RuntimeError("当前联系人身份快照不唯一")
            records[username] = row
        return records

    def _read_profile_actor(self, records):
        popup = wait_until(self._profile_popup, self._timeout(), self._cancelled)
        if popup is None:
            raise RuntimeError("无法读取作者的微信身份卡")
        # A real card lists alias, region and gender with the same
        # ProfileTextView class, so a "unique value" lookup returns None and
        # every interaction stopped at account verification. Bind the alias to
        # the 微信号 label by position instead of assuming one value control.
        controls = [(control, _prop(control, "BoundingRectangle"))
                    for control, _ in _walk(popup, cancelled=self._cancelled)
                    if _visible(control) and _prop(control, "ProcessId", 0) == self._main_pid]
        labels = self._coalesce_wrappers([control for control, _ in controls
            if self._flat(_name(control)).rstrip("：:") == "微信号"
            and _prop(control, "ControlTypeName", "") == "TextControl"])
        if len(labels) != 1:
            self._log_profile_shape(controls)
            raise RuntimeError("微信身份卡没有可核验的微信号")
        label = _prop(labels[0], "BoundingRectangle")
        values = [(control, rect) for control, rect in controls
                  if rect and _prop(control, "ClassName", "") == "mmui::ProfileTextView"
                  and "basic_line_view" in str(_prop(control, "AutomationId", ""))]
        value = self._value_on_label_line(label, values)
        if value is None:
            # Geometry is the primary binding. When the provider reports no
            # rectangles, a single value control is still unambiguous.
            unplaced = [control for control, rect in controls
                        if _prop(control, "ClassName", "") == "mmui::ProfileTextView"
                        and "basic_line_view" in str(_prop(control, "AutomationId", ""))]
            if len(unplaced) == 1:
                value = unplaced[0]
        if value is None:
            self._log_profile_shape(controls)
            raise RuntimeError("微信身份卡没有可核验的微信号")
        alias = _name(value)
        actors = {username for username, row in records.items()
            if alias and alias in {username, row.get("alias")}}
        if len(actors) != 1:
            raise RuntimeError("微信号与真实微信 ID 无法唯一对应")
        return next(iter(actors))

    @staticmethod
    def _value_on_label_line(label, values):
        """The only profile value sharing the label's line and sitting to its right."""
        if label is None:
            return None
        height = max(1, label.bottom - label.top)
        center = label.top + label.bottom
        same_line = [(rect.left, control) for control, rect in values
                     if abs((rect.top + rect.bottom) - center) <= height
                     and rect.left >= label.right - 1]
        if not same_line:
            return None
        same_line.sort(key=lambda item: item[0])
        if len(same_line) > 1 and abs(same_line[0][0] - same_line[1][0]) < 4:
            # Two values on the same line make the alias ambiguous.
            return None
        return same_line[0][1]

    @staticmethod
    def _log_profile_shape(controls):
        """Card structure only: class, automation id and name length, no values."""
        try:
            rows = [{"type": _prop(c, "ControlTypeName", ""), "class": _prop(c, "ClassName", ""),
                     "aid": str(_prop(c, "AutomationId", ""))[-48:],
                     "name_len": len(" ".join(str(_prop(c, "Name", "")).split()))}
                    for c, _ in controls[:48]]
            log.info("[moments-social] 身份卡结构 %s", json.dumps(rows, ensure_ascii=False))
        except Exception:
            pass

    def _dismiss_profile(self):
        popup = self._profile_popup()
        if popup is not None:
            main = self._main_window()
            focused = main is not None and (self._foreground(main) or self._foreground(popup))
            if not focused and main is not None and os.name == "nt":
                # Qt can report the avatar as the foreground UIA element,
                # without its physical popup HWND. Compare only the two
                # already verified physical windows, never a PID alone.
                getter = ctypes.windll.user32.GetForegroundWindow
                getter.restype = ctypes.c_void_p
                front = getter()
                focused = bool(front and front in {self._main_handle,
                    _prop(popup, "NativeWindowHandle", 0)})
            if not focused:
                raise RuntimeError("微信前台已改变，身份卡未自动关闭")
            check_cancel(self._cancelled)
            self._keys("{Esc}")
            if not wait_until(lambda: self._profile_popup() is None, self._timeout(), self._cancelled):
                raise RuntimeError("微信身份卡没有关闭")

    def _row_point(self, row, x, y, *, allow_avatar=False, before_input=None):
        feed = self._feed()
        r, f = _prop(row, "BoundingRectangle"), _prop(feed, "BoundingRectangle")
        token = _runtime_id(row)
        if not token or not r or not f or not (f.left <= x < f.right and f.top <= y < f.bottom
                and r.left <= x < r.right and r.top <= y < r.bottom):
            raise RuntimeError("朋友圈目标位置不在当前 UIA 列表内")
        hit = self._auto.ControlFromPoint(int(x), int(y))
        accepted_hit = _runtime_id(hit) == token
        if not accepted_hit and allow_avatar and _prop(hit, "ProcessId", 0) == self._main_pid:
            h = _prop(hit, "BoundingRectangle")
            scale = self._dpi_scale()
            # Qt's cover delegate reports the cover image itself under the
            # overlaid avatar. The live header/cover geometry anchors this
            # navigation; the resulting profile's real ID is checked before
            # any interaction is submitted. Avoid virtual-parent traversal.
            accepted_hit = bool(h and _runtime_id(hit)
                and _prop(hit, "ControlTypeName", "") == "ButtonControl"
                and h.left <= x < h.right and h.top <= y < h.bottom
                and ((30*scale <= h.right-h.left <= 80*scale
                      and 30*scale <= h.bottom-h.top <= 80*scale
                      and r.left <= h.left and h.right <= r.right)
                    or (_prop(hit, "ClassName", "") == "mmui::XImageView"
                        and abs(h.left-r.left) <= scale and abs(h.right-r.right) <= scale
                        and h.top <= r.top and r.bottom-20*scale <= h.bottom <= r.bottom)))
        if not accepted_hit:
            raise RuntimeError("朋友圈目标被遮挡或控件已变化")
        if not self._foreground(self._main_window()):
            raise RuntimeError("微信前台窗口已改变")
        if before_input is not None:
            before_input()
        check_cancel(self._cancelled)
        row.Click(x=int(x-r.left), y=int(y-r.top), simulateMove=False, waitTime=0)

    def _account_time_left(self, deadline):
        check_cancel(self._cancelled)
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise RuntimeError("朋友圈账号封面回顶耗时超过界限，已停止")
        return remaining

    def _account_feed_context(self, feed_token=None, *, deadline=None, resume_main=False):
        """Reacquire the same main feed without disturbing a user draft."""
        check_cancel(self._cancelled)
        if deadline is not None:
            self._account_time_left(deadline)
        main = self._main_window()
        if (resume_main and main is not None and not self._foreground(main)
                and self._activation_failure == "foreground_other_process"):
            self._activate(main)
        feed = self._feed(main) if main is not None else None
        if (main is None or feed is None
                or _prop(main, "NativeWindowHandle", 0) != self._main_handle
                or _prop(main, "ProcessId", 0) != self._main_pid
                or _prop(feed, "ProcessId", 0) != self._main_pid
                or not _runtime_id(feed)
                or self._window_identity(self._root_for(feed)) != (self._main_handle, self._main_pid)
                or not self._foreground(main)):
            raise self._ui_error("account_feed_context", "朋友圈账号核验时主窗口、列表或前台已经改变",
                main=main, feed=feed, expected_token=feed_token)
        viewport = _prop(feed, "BoundingRectangle")
        if not viewport or viewport.right <= viewport.left or viewport.bottom <= viewport.top:
            raise RuntimeError("朋友圈账号核验时列表位置无法确认")
        focused = self._auto.GetFocusedControl()
        if (self._has_composer() or self._file_dialog() or self._profile_popup() is not None
                or self._social_popup() is not None
                or (_prop(focused, "ClassName", "") == "mmui::CommentReplyTextEdit"
                    and _visible(focused))):
            raise RuntimeError("朋友圈已有草稿或弹窗，已保留当前操作")
        if deadline is not None:
            self._account_time_left(deadline)
        return feed

    def _account_feed_position(self, feed):
        # Include the cover: reaching it can change only header geometry while
        # a virtualized content row keeps its native ID and label.
        rows = []
        for control in self._timeline_cells(feed):
            if (_visible(control) and _prop(control, "ClassName", "") in {
                    "mmui::TimelineCell", "mmui::TimelineContentCell", "mmui::TimelineCommentCell"}):
                rect = _prop(control, "BoundingRectangle")
                rows.append((_name(control),
                    (rect.left, rect.top, rect.right, rect.bottom) if rect else None))
        return tuple(rows)

    def _account_scroll_hit(self, feed):
        viewport = _prop(feed, "BoundingRectangle")
        hit = None
        for row in self._timeline_cells(feed):
            rect, token = _prop(row, "BoundingRectangle"), _runtime_id(row)
            if (not _visible(row) or _prop(row, "ProcessId", 0) != self._main_pid
                    or _prop(row, "ClassName", "") != "mmui::TimelineContentCell"
                    or not rect or not token or min(rect.right, viewport.right)-max(rect.left, viewport.left) <= 8
                    or min(rect.bottom, viewport.bottom)-max(rect.top, viewport.top) <= 8):
                continue
            x = int((max(rect.left, viewport.left)+min(rect.right, viewport.right))/2)
            y = int((max(rect.top, viewport.top)+min(rect.bottom, viewport.bottom))/2)
            hit = self._auto.ControlFromPoint(x, y)
            if self._scroll_hit(row, feed, hit):
                return
        raise self._ui_error("account_scroll_hit", "朋友圈回顶位置被遮挡或无法对应当前 UIA 动态",
            main=self._main_window(), feed=feed, hit=hit)

    def _account_home_focus(self, feed_token):
        focused = self._auto.GetFocusedControl()
        token = _runtime_id(focused)
        if not token or _prop(focused, "ProcessId", 0) != self._main_pid:
            raise RuntimeError("朋友圈快速回顶焦点身份无法确认")
        kind, cls = _prop(focused, "ControlTypeName", ""), _prop(focused, "ClassName", "")
        if token == feed_token:
            if kind != "ListControl" or cls != "mmui::TimeLineListView":
                raise RuntimeError("朋友圈快速回顶焦点不是原列表")
        else:
            parent = _parent(focused)
            # Qt SetFocus(list) can retain one empty virtual direct child as
            # the actual focused element. Editors and nested children cannot
            # establish keyboard ownership of this feed.
            if (kind != "ListItemControl" or cls not in {"mmui::TimelineCell",
                    "mmui::TimelineContentCell", "mmui::TimelineCommentCell"}
                    or _prop(focused, "NativeWindowHandle", 0) != 0
                    or _runtime_id(parent) != feed_token or _prop(parent, "ProcessId", 0) != self._main_pid
                    or _prop(parent, "ClassName", "") != "mmui::TimeLineListView"
                    or focused.GetChildren()):
                raise RuntimeError("朋友圈快速回顶焦点不属于原列表的空虚拟行")
        rect = _prop(focused, "BoundingRectangle")
        return token, kind, cls, (rect.left, rect.top, rect.right, rect.bottom) if rect else None

    def _jump_account_cover(self, feed_token, headers, *, deadline):
        feed = self._account_feed_context(feed_token, deadline=deadline)
        if len(headers) == 1:
            token = _runtime_id(headers[0])
            getter = getattr(headers[0], "GetScrollItemPattern", None)
            pattern = getter() if callable(getter) else None
            if pattern is not None:
                feed = self._account_feed_context(feed_token, deadline=deadline)
                current = [c for c in feed.GetChildren() if _runtime_id(c) == token
                    and _prop(c, "ProcessId", 0) == self._main_pid
                    and _prop(c, "ClassName", "") == "mmui::TimelineCell" and _name(c) == ""]
                if not token or len(current) != 1:
                    raise RuntimeError("朋友圈快速回顶的原封面元素已改变")
                self._account_scroll_hit(feed)
                self._account_feed_context(feed_token, deadline=deadline)
                self._account_time_left(deadline)
                pattern.ScrollIntoView(waitTime=0)
                return True
        feed = self._account_feed_context(feed_token, deadline=deadline)
        if (_prop(feed, "ClassName", "") != "mmui::TimeLineListView"
                or _prop(feed, "IsKeyboardFocusable", None) is not True):
            return False
        self._account_time_left(deadline)
        if feed.SetFocus() is False:
            raise RuntimeError("朋友圈列表无法取得快速回顶焦点")
        self._account_feed_context(feed_token, deadline=deadline)
        before = self._account_home_focus(feed_token)
        self._account_feed_context(feed_token, deadline=deadline)
        if self._account_home_focus(feed_token) != before:
            raise RuntimeError("朋友圈快速回顶的焦点已改变")
        self._account_time_left(deadline)
        self._keys("{Ctrl}{Home}")
        return True

    def _verify_account(self, bot_id, records):
        deadline, jumped = time.monotonic()+30.0, False
        # A manual panel run or the preceding native-data read may leave Chrome
        # in front. Resume this bound main once before account navigation.
        main = self._main_window()
        if main is None or (not self._foreground(main) and not self._activate(main)):
            raise RuntimeError("朋友圈账号核验前无法恢复微信主窗口")
        self._window = main
        sidebar = self._unique(main, lambda c: _prop(c, "ClassName", "") == "mmui::MainTabBar"
            and self._aid(c, "main_tabbar") and _prop(c, "ControlTypeName", "") == "ToolBarControl")
        if sidebar is not None:
            rect = _prop(sidebar, "BoundingRectangle")
            width = rect.right-rect.left if rect else 0
            scale = self._dpi_scale()
            # The avatar is painted into this toolbar on WeChat 4.x. Opening
            # it is read-only; the card's real WeChat ID proves the account.
            if 48*scale <= width <= 72*scale and rect.bottom-rect.top > 3*width:
                x, y = width//2, width
                hit = self._auto.ControlFromPoint(rect.left+x, rect.top+y)
                if self._same_control(hit, sidebar) and self._foreground(main):
                    check_cancel(self._cancelled)
                    sidebar.Click(x=x, y=y, simulateMove=False, waitTime=0)
                    try:
                        if self._read_profile_actor(records) != bot_id:
                            raise RuntimeError("当前微信登录账号与配置的 bot 微信 ID 不一致")
                    finally:
                        self._dismiss_profile()
                    return
        if self._wait_main(self._ready_feed, recover_missing=True) is None:
            raise self._ui_error("account_feed_context", "朋友圈账号核验前列表未能恢复",
                main=self._main_window())
        feed = self._account_feed_context(deadline=deadline, resume_main=True)
        feed_token, scale = _runtime_id(feed), self._dpi_scale()
        # Every interaction verifies the real account again. A preceding post
        # lookup can leave the cover offscreen; return through this same feed
        # with observable progress instead of treating that as identity proof.
        for step in range(25):
            if step:
                feed = self._account_feed_context(feed_token, deadline=deadline)
            viewport = _prop(feed, "BoundingRectangle")
            all_headers = [c for c in self._timeline_cells(feed) if _name(c) == ""
                and _prop(c, "ClassName", "") == "mmui::TimelineCell"
                and _prop(c, "ProcessId", 0) == self._main_pid and _runtime_id(c)]
            all_headers = self._coalesce_wrappers(all_headers)
            if len(all_headers) > 1:
                raise RuntimeError("当前朋友圈账号入口无法唯一确认")
            headers = [c for c in all_headers if _visible(c)]
            header = headers[0] if headers else None
            rect = _prop(header, "BoundingRectangle")
            if (rect and rect.bottom-rect.top >= 150*scale
                    and viewport.left <= rect.left < rect.right <= viewport.right
                    and viewport.top <= rect.top < rect.bottom <= viewport.bottom):
                break
            if step == 24:
                raise RuntimeError("朋友圈账号封面未能在回顶界限内完整显示")
            before = self._account_feed_position(feed)
            if not jumped:
                jumped = True
                feed_shape = self._control_shape(feed)
                if self._jump_account_cover(feed_token, all_headers, deadline=deadline):
                    feed = self._feed_after_navigation(feed_shape)
                    feed_token = _runtime_id(feed)
                    wait_until(lambda: self._account_feed_position(
                        self._account_feed_context(feed_token, deadline=deadline)) != before,
                        min(.5, self._timeout(), self._account_time_left(deadline)), self._cancelled)
                    continue
            pattern = feed.GetScrollPattern()
            if pattern is not None and not pattern.VerticallyScrollable:
                raise RuntimeError("朋友圈账号封面没有完整显示且列表不可回顶")
            feed = self._account_feed_context(feed_token, deadline=deadline)
            feed_shape = self._control_shape(feed)
            if pattern is None:
                # One tick at a time allows the cover to be checked immediately
                # and retains the existing before/after cursor hit guards.
                self._wheel_feed(feed, up=True, steps=1,
                    before_input=lambda: self._account_feed_context(feed_token, deadline=deadline))
            else:
                self._account_scroll_hit(feed)
                self._account_feed_context(feed_token, deadline=deadline)
                self._account_time_left(deadline)
                pattern.Scroll(self._auto.ScrollAmount.NoAmount,
                    self._auto.ScrollAmount.LargeDecrement, waitTime=0)
            feed = self._feed_after_navigation(feed_shape)
            feed_token = _runtime_id(feed)
            if not wait_until(lambda: self._account_feed_position(
                    self._account_feed_context(feed_token, deadline=deadline)) != before,
                    min(.5, self._timeout(), self._account_time_left(deadline)), self._cancelled):
                raise RuntimeError("朋友圈回顶没有可核验的进度，已停止账号核验")
        self._account_feed_context(feed_token, deadline=deadline, resume_main=True)
        self._row_point(header, rect.right-50*scale, rect.bottom-30*scale, allow_avatar=True,
            before_input=lambda: self._account_time_left(deadline))
        try:
            if self._read_profile_actor(records) != bot_id:
                raise RuntimeError("当前微信登录账号与配置的 bot 微信 ID 不一致")
        finally:
            self._dismiss_profile()

    def _verify_author(self, row, actor_id, records):
        r, s = _prop(row, "BoundingRectangle"), self._dpi_scale()
        self._row_point(row, (r.left+r.right)/2-235*s, r.top+40*s)
        try:
            if self._read_profile_actor(records) != actor_id:
                raise RuntimeError("动态作者微信 ID 与当前身份卡不一致")
        finally:
            self._dismiss_profile()

    @staticmethod
    def _time_matches(text, created_at, now=None):
        age = (time.time() if now is None else now)-created_at
        if age < -120:
            return False
        text = text.strip()
        if text == "刚刚":
            return age <= 120
        m = re.fullmatch(r"(\d+)分钟前", text)
        if m:
            # Qt can retain the displayed minute label until the feed is
            # reopened. It is a lower bound for a post in the current hour.
            minutes = int(m[1])
            return 0 <= minutes < 60 and minutes*60-120 <= age < 3720
        m = re.fullmatch(r"(\d+)小时前", text)
        if m:
            return int(m[1])*3600-120 <= age < (int(m[1])+1)*3600+120
        from datetime import datetime, timezone, timedelta
        stamp = datetime.fromtimestamp(created_at, timezone(timedelta(hours=8)))
        current = datetime.fromtimestamp(time.time() if now is None else now, timezone(timedelta(hours=8)))
        if text == "昨天":
            return stamp.date() == (current-timedelta(days=1)).date()
        return text in {stamp.strftime("%m月%d日"), f"{stamp.month}月{stamp.day}日",
            stamp.strftime("%Y年%m月%d日"), f"{stamp.year}年{stamp.month}月{stamp.day}日"}

    def _matches_post(self, row, source, records):
        record = records.get(source["author_id"], {})
        names = {n for n in [*source.get("author_names", []), source.get("author_name"),
            record.get("remark"), record.get("nickname"), record.get("displayName")] if isinstance(n, str) and n}
        label, content = self._flat(_name(row)), self._flat(source.get("content", ""))
        for name in names:
            prefix = self._flat(name)+(" "+content if content else "")+" "
            if not label.startswith(prefix):
                continue
            suffix = label[len(prefix):]
            suffix = re.sub(r"^包含\d+张图片\s+", "", suffix)
            if self._time_matches(suffix, source["created_at"]):
                return True
        return False

    def _locate_post(self, source, records, *, for_menu=False):
        large_steps = small_steps = 0
        scroll_into_view_used = False
        centered_token = centered_label = None
        def position(feed=None):
            rows = []
            for control in self._timeline_cells(feed or self._feed()):
                rect = _prop(control, "BoundingRectangle")
                rows.append((_name(control),
                    (rect.left, rect.top, rect.right, rect.bottom) if rect else None))
            return tuple(rows)

        for _ in range(65):
            check_cancel(self._cancelled)
            main = self._main_window()
            feed = self._feed(main)
            if main is not None and feed is None:
                feed = self._feed_after_navigation(())
                main = self._main_window()
            if (feed is None or main is None
                    or _prop(main, "NativeWindowHandle", 0) != self._main_handle
                    or _prop(main, "ProcessId", 0) != self._main_pid
                    or _prop(feed, "ProcessId", 0) != self._main_pid
                    or not _runtime_id(feed) or not self._foreground(main)):
                raise RuntimeError("朋友圈定位时主窗口或前台已经改变")
            rows = self._post_rows(feed)
            matches = [c for c in rows if self._matches_post(c, source, records)]
            if len(matches) > 1:
                raise RuntimeError("朋友圈出现重复正文与时间的动态，已跳过")
            if centered_token and (len(matches) != 1 or self._flat(_name(matches[0])) != centered_label):
                raise RuntimeError("朋友圈移至视口中部时目标动态已改变")
            amount = None
            centering = False
            tall = False
            if len(matches) == 1:
                rect, viewport = _prop(matches[0], "BoundingRectangle"), _prop(feed, "BoundingRectangle")
                if (not rect or not viewport or rect.bottom <= rect.top
                        or viewport.bottom <= viewport.top or rect.left < viewport.left
                        or rect.right > viewport.right):
                    raise RuntimeError("目标动态尺寸无法在当前朋友圈窗口核验")
                target_top, target_bottom = rect.top, rect.bottom
                tall = rect.bottom-rect.top > viewport.bottom-viewport.top
                if tall:
                    # The avatar and ellipsis are on opposite ends of a tall
                    # post. Re-match the post after scrolling to each endpoint.
                    inset = (11 if for_menu else 40) * self._dpi_scale()
                    target_top = target_bottom = rect.bottom-inset if for_menu else rect.top+inset
                if viewport.top <= target_top and target_bottom <= viewport.bottom and target_top < viewport.bottom:
                    # Keep small posts and their menus above the bottom toast
                    # area. A completed scroll may rebuild its Qt row provider;
                    # the unique source label is checked again on the fresh tree.
                    if (rect.bottom-rect.top <= .4*(viewport.bottom-viewport.top)
                            and (rect.top+rect.bottom)/2 > viewport.top+.6*(viewport.bottom-viewport.top)
                            and small_steps < 8):
                        centering = True
                        centered_token, centered_label = _runtime_id(matches[0]), self._flat(_name(matches[0]))
                        amount = self._auto.ScrollAmount.SmallIncrement
                        small_steps += 1
                    else:
                        return matches[0]
                else:
                    if small_steps >= 24:
                        break
                    amount = (self._auto.ScrollAmount.SmallDecrement if target_top < viewport.top
                        else self._auto.ScrollAmount.SmallIncrement)
                    small_steps += 1
            else:
                if large_steps >= 40:
                    break
                large_steps += 1
                amount = self._auto.ScrollAmount.LargeIncrement
            before = position(feed)
            feed_shape = self._control_shape(feed)
            pattern = feed.GetScrollPattern()
            item = None
            if len(matches) == 1 and not tall and not centering and not scroll_into_view_used:
                getter = getattr(matches[0], "GetScrollItemPattern", None)
                item = getter() if callable(getter) else None
            check_cancel(self._cancelled)
            current_main = self._main_window()
            current_feed = self._feed(current_main)
            if current_main is not None and current_feed is None:
                current_feed = self._feed_after_navigation(feed_shape)
                current_main = self._main_window()
            if (current_main is None or current_feed is None
                    or _prop(current_main, "NativeWindowHandle", 0) != self._main_handle
                    or _prop(current_main, "ProcessId", 0) != self._main_pid
                    or _prop(current_feed, "ProcessId", 0) != self._main_pid
                    or not self._foreground(current_main)):
                raise RuntimeError("朋友圈滚动前窗口或控件已经改变")
            def centered_visible():
                check_cancel(self._cancelled)
                current_main = self._main_window()
                current_feed = self._feed(current_main)
                candidates = [c for c in self._post_rows(current_feed) if self._matches_post(c, source, records)]
                viewport = _prop(current_feed, "BoundingRectangle")
                candidate = candidates[0] if len(candidates) == 1 else None
                rect = _prop(candidate, "BoundingRectangle")
                if (current_main is None or not self._foreground(current_main)
                        or _prop(current_main, "NativeWindowHandle", 0) != self._main_handle
                        or _prop(current_main, "ProcessId", 0) != self._main_pid
                        or _prop(current_feed, "ProcessId", 0) != self._main_pid
                        or not _runtime_id(candidate) or self._flat(_name(candidate)) != centered_label
                        or not rect or not viewport or not (viewport.left <= rect.left < rect.right <= viewport.right
                            and viewport.top <= rect.top < rect.bottom <= viewport.bottom)):
                    raise RuntimeError("朋友圈中部定位结束时目标或窗口已改变")
                return candidate
            if pattern is not None and not pattern.VerticallyScrollable:
                if centering:
                    return centered_visible()
                break
            navigated_feed = None
            if item is not None:
                check_cancel(self._cancelled)
                item.ScrollIntoView(waitTime=0)
                scroll_into_view_used = True
            elif pattern is None:
                navigated_feed = self._wheel_feed(current_feed,
                    up=amount == self._auto.ScrollAmount.SmallDecrement,
                    steps=3 if amount == self._auto.ScrollAmount.LargeIncrement else 1)
            else:
                pattern.Scroll(self._auto.ScrollAmount.NoAmount, amount, waitTime=0)
            feed = navigated_feed if navigated_feed is not None else self._feed_after_navigation(feed_shape)
            if not wait_until(lambda: position(feed) != before, min(.5, self._timeout()), self._cancelled):
                if centering:
                    return centered_visible()
                break
        raise RuntimeError("目标朋友圈未能在当前列表中唯一定位")

    def _wheel_feed(self, feed, *, up=False, steps=1, before_input=None):
        # This Qt SNS provider has no ScrollPattern. Wheel input is anchored to
        # its live list, with hit/window checks before and after cursor motion.
        # A large step is one short burst, not three full UIA tree scans.
        # Control.WheelDown would SetFocus and reactivate a changed window.
        feed_token = _runtime_id(feed)
        feed_shape = self._control_shape(feed)
        wheel = getattr(self._auto, "WheelUp" if up else "WheelDown", None)
        move = getattr(self._auto, "MoveTo", None)
        cursor = getattr(self._auto, "GetCursorPos", None)
        if not feed_token or not all(callable(method) for method in (wheel, move, cursor)):
            raise RuntimeError("朋友圈没有可核验的滚动接口")
        check_cancel(self._cancelled)
        main = self._main_window()
        current_feed = self._feed(main)
        if main is not None and current_feed is None:
            current_feed = self._feed_after_navigation(feed_shape)
            main = self._main_window()
        if (main is None or current_feed is None
                or _prop(main, "NativeWindowHandle", 0) != self._main_handle
                or _prop(main, "ProcessId", 0) != self._main_pid
                or _prop(current_feed, "ProcessId", 0) != self._main_pid
                or not self._foreground(main)):
            raise self._ui_error("wheel_context", "朋友圈滚动时窗口已经改变",
                main=main, feed=current_feed, expected_token=feed_token)
        viewport = _prop(current_feed, "BoundingRectangle")
        if not viewport or viewport.right <= viewport.left or viewport.bottom <= viewport.top:
            raise RuntimeError("朋友圈滚动列表位置无法确认")
        target = None
        hit = None
        for row in self._post_rows(current_feed):
            rect = _prop(row, "BoundingRectangle")
            if (viewport and rect and _runtime_id(row)
                    and min(rect.right, viewport.right)-max(rect.left, viewport.left) > 8
                    and min(rect.bottom, viewport.bottom)-max(rect.top, viewport.top) > 8):
                x = int((max(rect.left, viewport.left)+min(rect.right, viewport.right))/2)
                y = int((max(rect.top, viewport.top)+min(rect.bottom, viewport.bottom))/2)
                hit = self._auto.ControlFromPoint(x, y)
                if self._scroll_hit(row, current_feed, hit):
                    target = row
                    break
        if target is None:
            # A long comment block can fill the viewport with no visible
            # content delegate. Scrolling belongs to the list, not a post.
            x = int((viewport.left+viewport.right)/2)
            y = int((viewport.top+viewport.bottom)/2)
            hit = self._auto.ControlFromPoint(x, y)
            if not self._wheel_viewport_hit(current_feed, hit, x, y):
                raise self._ui_error("wheel_hit", "朋友圈滚动位置无法对应当前 UIA 列表",
                    main=main, feed=current_feed, hit=hit, expected_token=feed_token)
        feed_shape = self._control_shape(current_feed)
        if before_input is not None:
            before_input()
        check_cancel(self._cancelled)
        move(x, y, moveSpeed=0, waitTime=0)
        check_cancel(self._cancelled)
        main = self._main_window()
        current_feed = self._feed(main)
        if main is not None and current_feed is None:
            current_feed = self._feed_after_navigation(feed_shape)
            main = self._main_window()
        hit = self._auto.ControlFromPoint(x, y)
        current_viewport = _prop(current_feed, "BoundingRectangle") if current_feed else None
        target_valid = self._wheel_viewport_hit(current_feed, hit, x, y)
        if (main is None or current_feed is None
                or _prop(main, "NativeWindowHandle", 0) != self._main_handle
                or _prop(main, "ProcessId", 0) != self._main_pid
                or _prop(current_feed, "ProcessId", 0) != self._main_pid
                or not target_valid
                or not current_viewport or not (current_viewport.left <= x < current_viewport.right
                    and current_viewport.top <= y < current_viewport.bottom)
                or tuple(cursor()) != (x, y)
                or not self._foreground(main)):
            raise self._ui_error("wheel_before_input", "朋友圈滚轮输入前位置或前台已经改变",
                main=main, feed=current_feed, hit=hit, expected_token=feed_token)
        if before_input is not None:
            before_input()
        check_cancel(self._cancelled)
        wheel(wheelTimes=min(3, max(1, steps)), interval=0, waitTime=0)
        return self._feed_after_navigation(feed_shape)

    def _comment_cell(self, row):
        feed = self._feed()
        rows, seen = [], set()
        for control in self._timeline_cells(feed):
            token = _runtime_id(control)
            if token and token in seen:
                continue
            rows.append(control)
            if token:
                seen.add(token)
        for i, c in enumerate(rows[:-1]):
            if self._same_control(c, row):
                following = rows[i+1]
                if _prop(following, "ClassName", "") == "mmui::TimelineCommentCell":
                    return following
        return None

    def _comment_context(self, row, editor_token=None):
        try:
            editor = self._auto.GetFocusedControl()
        except Exception:
            return None
        if (editor is None or _prop(editor, "ClassName", "") != "mmui::CommentReplyTextEdit"
                or _prop(editor, "ProcessId", 0) != self._main_pid or not _visible(editor)
                or not _runtime_id(editor) or (editor_token and _runtime_id(editor) != editor_token)):
            return None
        cell, view, holder = None, None, None
        current = _parent(editor)
        for _ in range(12):
            if current is None or _prop(current, "ProcessId", 0) != self._main_pid:
                break
            if _prop(current, "ClassName", "") == "mmui::CommentReplyTextView":
                view, holder = current, _parent(current)
            if _prop(current, "ClassName", "") == "mmui::TimelineCommentCell":
                cell = current
                break
            current = _parent(current)
        actual = self._comment_cell(row)
        r, e = _prop(actual, "BoundingRectangle"), _prop(editor, "BoundingRectangle")
        if (view is None or not self._same_control(cell, actual) or not r or not e
                or e.bottom <= e.top or not (r.left <= e.left < e.right <= r.right
                    and r.top <= e.top < e.bottom <= r.bottom)
                or not self._foreground(self._main_window())):
            return None
        return editor, view, holder

    def _open_comment(self, row):
        popup = self._open_post_menu(row)
        self._popup_button(popup, "评论").Click(simulateMove=False, waitTime=0)
        context = wait_until(lambda: self._comment_context(row), self._timeout(), self._cancelled)
        if context is None:
            raise RuntimeError("无法核验目标动态的评论编辑器")
        if self._moment_text(context[0]) != "":
            raise RuntimeError("朋友圈评论已有草稿，已保留并停止")
        return context

    def _close_owned_empty_comment(self):
        """Close only this operation's still-empty comment editor."""
        owned = getattr(self, "_owned_empty_comment", None)
        self._owned_empty_comment = None
        if owned is None:
            return
        row, context = owned
        cancelled = self._cancelled
        try:
            # A cancelled task can still roll back its own empty editor before
            # releasing the desktop. Identity, emptiness and foreground remain
            # mandatory; no content is entered or submitted during cleanup.
            self._cancelled = None
            current = self._comment_context(row, _runtime_id(context[0]))
            if (current is not None and self._moment_text(current[0]) == ""
                    and tuple(_runtime_id(c) for c in current) == tuple(_runtime_id(c) for c in context)
                    and self._foreground(self._main_window())):
                self._auto.SendKeys("{Esc}", waitTime=0)
        except Exception:
            pass  # Preserve a changed draft/provider and the original failure.
        finally:
            self._cancelled = cancelled

    def _same_author_targets(self, targets, comment, source):
        """Reduce duplicate UI matches only when they are one person's own repeats.

        The reply recipient is a person, so picking any of their identical
        comments under the same post is equivalent. Two different authors with
        the same text stay ambiguous and keep the reply stopped.
        """
        if len(targets) < 2 or not isinstance(source, dict):
            return targets
        author, content = comment.get("author_id"), self._flat(comment.get("content", ""))
        if not author or not content:
            return targets
        same = [c for c in source.get("comments", []) if isinstance(c, dict)
                and self._flat(c.get("content", "")) == content]
        if not same or any(c.get("author_id") != author for c in same):
            return targets
        return targets[:1]

    def _choose_reply(self, row, context, comment, records, *, source=None):
        # Focus gives access to the virtual comment cell's otherwise hidden
        # subtree. A comment's native ID and actor were checked in the cache;
        # text/actor must also uniquely match this post's live UI subtree.
        holder = context[2]
        names = self._comment_names(comment, records)
        content = self._flat(comment.get("content", ""))
        labels = self._comment_labels(comment, records)
        matches = []
        for c, _ in _walk(holder):
            label = self._flat(_name(c))
            if (_visible(c) and _prop(c, "ProcessId", 0) == self._main_pid
                    and _prop(c, "ControlTypeName", "") == "TextControl"
                    and _prop(c, "ClassName", "") == "mmui::CommentTextView"
                    and content and label in labels):
                matches.append(c)
        if any(not _runtime_id(c) for c in matches):
            raise RuntimeError("目标评论控件身份不完整，已跳过回复")
        targets = self._coalesce_wrappers(matches)
        if len(targets) != 1:
            targets = self._same_author_targets(targets, comment, source)
        if len(targets) != 1:
            raise RuntimeError("目标评论在 UIA 中无法唯一对应，已跳过回复")
        target = targets[0]
        original_cell = self._comment_cell(row)
        original_cell_token = _runtime_id(original_cell)
        original_editor_token = _runtime_id(context[0])
        original_row_token = _runtime_id(row)
        original_row_label = self._flat(_name(row))
        original_target_token = _runtime_id(target)
        original_label = self._flat(_name(target))
        def bounds(control):
            rect = _prop(control, "BoundingRectangle")
            return (rect.left, rect.top, rect.right, rect.bottom) if rect else None
        original_feed = self._feed()
        geometry = (bounds(row), bounds(original_cell), bounds(context[1]), bounds(target),
            (_runtime_id(original_feed), _prop(original_feed, "ProcessId", 0), bounds(original_feed)))
        body_point = self._reply_body_point(target, comment, records)
        evidence = (original_row_token, original_row_label, original_cell_token,
            original_target_token, original_label, geometry)
        if (not original_editor_token or self._comment_context(row, original_editor_token) is None
                or self._moment_text(context[0]) != ""):
            raise RuntimeError("回复导航前草稿或焦点已经改变，已保留当前操作")
        self._click_reply_body(row, context, target, body_point, comment, records, source, evidence)
        new_context = wait_until(lambda: self._comment_context(row), self._timeout(), self._cancelled)
        if new_context is None:
            # Qt first closes the empty generic editor. A second navigation is
            # permitted only while the same comment remains in the same cell.
            check_cancel(self._cancelled)
            main = self._main_window()
            cell = self._comment_cell(row)
            try:
                focused = self._auto.GetFocusedControl()
            except Exception:
                focused = None
            closed = (_prop(context[0], "IsOffscreen", None) is True
                and original_editor_token and _runtime_id(context[0]) == original_editor_token
                and _prop(context[0], "ProcessId", 0) == self._main_pid
                and self._moment_text(context[0]) == "")
            stable = not (main is None or self._profile_popup() is not None
                    or _prop(main, "NativeWindowHandle", 0) != self._main_handle
                    or _prop(main, "ProcessId", 0) != self._main_pid
                    or not original_row_token or _runtime_id(row) != original_row_token
                    or _prop(row, "ProcessId", 0) != self._main_pid
                    or self._flat(_name(row)) != original_row_label
                    or not self._foreground(main) or not original_cell_token
                    or _runtime_id(cell) != original_cell_token
                    or _prop(cell, "ProcessId", 0) != self._main_pid
                    or _prop(cell, "ClassName", "") != "mmui::TimelineCommentCell"
                    or _prop(focused, "ClassName", "") == "mmui::CommentReplyTextEdit")
            if not stable:
                raise RuntimeError("回复导航后控件或草稿改变，已停止")
            if not closed:
                if (_runtime_id(context[0]) or _prop(context[0], "ProcessId", 0)
                        or _prop(context[0], "ClassName", "")
                        or _prop(context[0], "IsOffscreen", None) is not None
                        or self._moment_text(context[0]) is not None
                        or _runtime_id(target) or _prop(target, "ProcessId", 0)
                        or _prop(target, "ClassName", "") or _name(target)):
                    raise RuntimeError("原回复控件未完整退出，已保留当前操作")
                self._navigate_collapsed_reply(row, cell, focused, geometry, source, comment, records,
                    point=body_point)
            else:
                if (_runtime_id(target) != original_target_token or not _visible(target)
                    or _prop(target, "ProcessId", 0) != self._main_pid
                    or _prop(target, "ClassName", "") != "mmui::CommentTextView"
                    or _prop(target, "ControlTypeName", "") != "TextControl"
                    or self._flat(_name(target)) != original_label):
                    raise RuntimeError("回复导航后控件或草稿改变，已停止")
                current, anchored = _parent(target), False
                for _ in range(12):
                    if current is None or _prop(current, "ProcessId", 0) != self._main_pid:
                        break
                    if self._same_control(current, cell):
                        anchored = True
                        break
                    current = _parent(current)
                if not anchored:
                    raise RuntimeError("回复评论已离开原动态，已停止")
                check_cancel(self._cancelled)
                self._click_reply_body(row, context, target, body_point, comment, records, source,
                    evidence, closed=True)
            new_context = wait_until(lambda: self._comment_context(row), self._timeout(), self._cancelled)
        if new_context is None or self._moment_text(new_context[0]) != "":
            raise RuntimeError("回复编辑器身份或草稿无法核验")
        self._owned_empty_comment = (row, new_context)
        self._reply_caption_source = source
        if not self._reply_target_matches(new_context, comment, records, row=row, wait_for_caption=True):
            raise RuntimeError("回复对象没有在编辑器中确认")
        return new_context

    def _reply_body_point(self, target, comment, records):
        """Use the comment body, never the author hyperlink at a leaf's center."""
        label, content = self._flat(_name(target)), self._flat(comment.get("content", ""))
        rect, scale = _prop(target, "BoundingRectangle"), self._dpi_scale()
        if (not content or label not in self._comment_labels(comment, records)
                or not label.endswith(content) or not rect or rect.right <= rect.left
                or rect.bottom <= rect.top):
            raise RuntimeError("目标评论正文位置无法核验")
        getter = getattr(target, "GetTextPattern", None)
        pattern = getter() if callable(getter) else None
        if pattern is not None:
            document = pattern.DocumentRange
            body = document.FindText(str(comment["content"]).strip(), True, False)
            if (self._flat(document.GetText(-1)) != label or body is None
                    or self._flat(body.GetText(-1)) != content):
                raise RuntimeError("目标评论文本范围与原评论不一致")
            rectangles = body.GetBoundingRectangles()
            if not rectangles:
                raise RuntimeError("目标评论正文没有可核验的字符边界")
            glyphs = rectangles[-1]
            if not (rect.left <= glyphs.left < glyphs.right <= rect.right
                    and rect.top <= glyphs.top < glyphs.bottom <= rect.bottom):
                raise RuntimeError("目标评论字符边界已离开原控件")
            return int((glyphs.left+glyphs.right)/2), int((glyphs.top+glyphs.bottom)/2)
        # This Qt provider exposes a tight single-line rich-text rectangle but
        # no TextPattern. Its trailing body is beyond the blue author link.
        if ("\n" in _name(target) or "\r" in _name(target)
                or "\n" in str(comment.get("content", "")) or "\r" in str(comment.get("content", ""))
                or rect.bottom-rect.top > 32*scale or rect.right-rect.left <= 8*scale):
            raise RuntimeError("目标评论没有可核验的单行正文落点")
        return int(rect.right-4*scale), int((rect.top+rect.bottom)/2)

    def _click_reply_body(self, row, context, target, point, comment, records, source, evidence, *, closed=False):
        row_token, row_label, cell_token, target_token, label, geometry = evidence
        before_row, _, _, leaf, feed_evidence = geometry
        feed_token, feed_pid, feed_bounds = feed_evidence
        def bounds(control):
            rect = _prop(control, "BoundingRectangle")
            return (rect.left, rect.top, rect.right, rect.bottom) if rect else None
        def snapshot():
            check_cancel(self._cancelled)
            main, feed, cell = self._main_window(), self._feed(), self._comment_cell(row)
            x, y = point
            if (main is None or not self._foreground(main) or self._profile_popup() is not None
                    or _prop(main, "NativeWindowHandle", 0) != self._main_handle
                    or _prop(main, "ProcessId", 0) != self._main_pid
                    or not isinstance(source, dict) or not self._matches_post(row, source, records)
                    or not row_token or _runtime_id(row) != row_token or self._flat(_name(row)) != row_label
                    or _prop(row, "ProcessId", 0) != self._main_pid or bounds(row) != before_row
                    or not cell_token or _runtime_id(cell) != cell_token or not _visible(cell)
                    or _prop(cell, "ClassName", "") != "mmui::TimelineCommentCell"
                    or _prop(cell, "ProcessId", 0) != self._main_pid
                    or not target_token or _runtime_id(target) != target_token or not _visible(target)
                    or _prop(target, "ProcessId", 0) != self._main_pid
                    or _prop(target, "ClassName", "") != "mmui::CommentTextView"
                    or _prop(target, "ControlTypeName", "") != "TextControl"
                    or self._flat(_name(target)) != label or bounds(target) != leaf
                    or not feed_token or _runtime_id(feed) != feed_token or feed_pid != self._main_pid
                    or _prop(feed, "ProcessId", 0) != self._main_pid or bounds(feed) != feed_bounds
                    or not leaf or not feed_bounds or not bounds(cell)
                    or not (leaf[0] <= x < leaf[2] and leaf[1] <= y < leaf[3]
                        and feed_bounds[0] <= x < feed_bounds[2] and feed_bounds[1] <= y < feed_bounds[3]
                        and bounds(cell)[0] <= x < bounds(cell)[2] and bounds(cell)[1] <= y < bounds(cell)[3])):
                raise RuntimeError("回复正文导航前控件、落点或原动态已改变")
            current, anchored = _parent(target), False
            for _ in range(12):
                if current is None or _prop(current, "ProcessId", 0) != self._main_pid:
                    break
                if self._same_control(current, cell):
                    anchored = True
                    break
                current = _parent(current)
            if not anchored or self._moment_text(context[0]) != "":
                raise RuntimeError("回复正文已离开原评论区或出现草稿")
            if closed:
                if (_prop(context[0], "IsOffscreen", None) is not True
                        or _prop(self._auto.GetFocusedControl(), "ClassName", "") == "mmui::CommentReplyTextEdit"):
                    raise RuntimeError("回复正文二次导航前出现新的编辑器")
            elif self._comment_context(row, _runtime_id(context[0])) is None:
                raise RuntimeError("回复正文导航前空编辑器焦点已改变")
            hit = self._auto.ControlFromPoint(int(x), int(y))
            if (_prop(hit, "ProcessId", 0) != self._main_pid or not _visible(hit)
                    or not ((_runtime_id(hit) == target_token and bounds(hit) == leaf)
                        or (_runtime_id(hit) == cell_token and bounds(hit) == bounds(cell)
                            and _prop(hit, "ClassName", "") == "mmui::TimelineCommentCell"))):
                raise RuntimeError("回复正文落点被遮挡或不属于原评论区")
            return bounds(cell), _runtime_id(hit), bounds(hit)
        before = snapshot()
        if before[1] == cell_token:
            self._verify_collapsed_reply_comments(source, comment, records)
        if snapshot() != before:
            raise RuntimeError("回复正文导航的原评论区在核验中改变")
        check_cancel(self._cancelled)
        if before[1] == cell_token:
            self._reply_geometry_used = True
        target.Click(x=int(point[0]-leaf[0]), y=int(point[1]-leaf[1]), simulateMove=False, waitTime=0)

    def _navigate_collapsed_reply(self, row, cell, focused, geometry, source, comment, records, *, point):
        """Navigate the same UIA comment after Qt destroys its child provider."""
        before_row, before_cell, reply_view, leaf, feed_evidence = geometry
        feed_token, feed_pid, feed_bounds = feed_evidence
        feed = self._feed()
        current_row = _prop(row, "BoundingRectangle")
        current_cell = _prop(cell, "BoundingRectangle")
        row_token, cell_token = _runtime_id(row), _runtime_id(cell)
        def bounds(rect):
            return (rect.left, rect.top, rect.right, rect.bottom) if rect else None
        if (not isinstance(source, dict) or source.get("interaction_identity_verified") is not True
                or not all(geometry) or not self._same_control(cell, focused) or not _visible(focused)
                or not feed_token or feed_pid != self._main_pid or _runtime_id(feed) != feed_token
                or _prop(feed, "ProcessId", 0) != self._main_pid
                or bounds(_prop(feed, "BoundingRectangle")) != feed_bounds
                or _prop(focused, "ClassName", "") != "mmui::TimelineCommentCell"
                or _prop(focused, "ProcessId", 0) != self._main_pid
                or bounds(current_row) != before_row or not current_cell
                or (current_cell.left, current_cell.top, current_cell.right) != before_cell[:3]):
            raise RuntimeError("评论子控件重建后无法核验原动态与评论区")
        margin = before_cell[3]-reply_view[3]
        if (not 0 <= margin <= 48 or current_cell.bottom != reply_view[1]-margin
                or not (current_cell.left <= leaf[0] < leaf[2] <= current_cell.right
                    and current_cell.top <= leaf[1] < leaf[3] <= current_cell.bottom)):
            raise RuntimeError("评论区未仅移除原空编辑器，已停止导航")
        x, y = point
        if not (leaf[0] <= x < leaf[2] and leaf[1] <= y < leaf[3]):
            raise RuntimeError("目标评论正文落点不在已核验的原控件内")
        if not feed_bounds or not (feed_bounds[0] <= x < feed_bounds[2]
                and feed_bounds[1] <= y < feed_bounds[3]):
            raise RuntimeError("目标评论位置已经离开当前朋友圈列表")
        # A new/reused editor or a partially exposed subtree must not be
        # mistaken for the verified collapsed native comment cell.
        if cell.GetChildren():
            raise RuntimeError("评论区出现新的编辑控件，已保留当前操作")
        self._verify_collapsed_reply_comments(source, comment, records)
        check_cancel(self._cancelled)
        main, actual, feed = self._main_window(), self._comment_cell(row), self._feed()
        hit = self._auto.ControlFromPoint(x, y)
        if (main is None or _prop(main, "NativeWindowHandle", 0) != self._main_handle
                or _prop(main, "ProcessId", 0) != self._main_pid or not self._foreground(main)
                or self._profile_popup() is not None or not cell_token or not row_token
                or _runtime_id(feed) != feed_token or _prop(feed, "ProcessId", 0) != self._main_pid
                or bounds(_prop(feed, "BoundingRectangle")) != feed_bounds
                or _runtime_id(actual) != cell_token or _runtime_id(row) != row_token
                or _runtime_id(self._auto.GetFocusedControl()) != cell_token
                or _runtime_id(hit) != cell_token or _prop(hit, "ProcessId", 0) != self._main_pid
                or _prop(actual, "ProcessId", 0) != self._main_pid
                or _prop(actual, "ClassName", "") != "mmui::TimelineCommentCell"
                or bounds(_prop(actual, "BoundingRectangle")) != bounds(current_cell)
                or bounds(_prop(row, "BoundingRectangle")) != before_row
                or not self._matches_post(row, source, records) or actual.GetChildren()):
            raise RuntimeError("回复导航落点、身份或焦点已经改变")
        check_cancel(self._cancelled)
        self._reply_geometry_used = True
        actual.Click(x=x-current_cell.left, y=y-current_cell.top, simulateMove=False, waitTime=0)

    def _verify_collapsed_reply_comments(self, source, comment, records):
        def comments_key(post):
            comments = post.get("comments", [])
            if (post.get("identity_verified") is not True
                    or post.get("interaction_identity_verified") is not True
                    or any(c.get("identity_verified") is not True for c in comments)):
                return None
            target = [c for c in comments if c.get("author_id") == comment.get("author_id")
                and c.get("comment_id") == comment.get("comment_id")]
            if len(target) != 1:
                return None
            # The clicked live label already identifies this body. Multiple
            # distinct comments by one actor are valid; identical labels with
            # different native IDs remain ambiguous.
            labels = self._comment_labels(target[0], records)
            if not labels or any(labels & self._comment_labels(c, records)
                    for c in comments if c.get("comment_id") != comment.get("comment_id")):
                return None
            captions = self._reply_captions(comment, records, canonical=True)
            if not captions or any(captions & self._reply_captions(c, records, canonical=True)
                    for c in comments if c.get("author_id") != comment.get("author_id")):
                return None
            return [(c.get("comment_id"), c.get("author_id"), c.get("content"),
                c.get("ref_comment_id"), c.get("ref_author_id"), c.get("author_names")) for c in comments]
        expected = comments_key(source)
        if expected is None:
            raise RuntimeError("评论子控件重建后无法唯一核验真实回复 ID")
        bot_id = validate_username(getattr(config, "BOT_WXID", ""))
        cancelled = self._cancelled
        check_cancel(cancelled)
        cancellation = type("Cancellation", (), {"is_set": lambda _: bool(cancelled and cancelled())})()
        fresh = self.feed_client.read_post_details(source, bot_id=bot_id, cancel_event=cancellation)
        check_cancel(cancelled)
        if (any(fresh.get(key) != source.get(key) for key in ("post_id", "author_id", "content", "created_at"))
                or expected != comments_key(fresh)):
            raise RuntimeError("原生评论列表在回复导航中改变，已停止")

    def _reply_captions(self, comment, records, *, canonical=False):
        names = self._comment_names(comment, records)
        captions = {self._flat("回复"+gap+name+suffix) for name in names
            for gap in ("", " ") for suffix in ("", "：", ":")}
        return {re.sub(r"\s+", "", c) for c in captions} if canonical else captions

    def _uia_reply_target(self, context, comment, records, row):
        if row is not None:
            actual = self._comment_context(row, _runtime_id(context[0]))
            if (actual is None or any(not _runtime_id(c) for c in context)
                    or tuple(_runtime_id(c) for c in actual) != tuple(_runtime_id(c) for c in context)):
                return False
            context = actual
        captions = self._reply_captions(comment, records)
        help_text = self._flat(_prop(context[0], "HelpText", ""))
        # Qt can expose the typed draft as the editor's name and as a text child.
        editor_id = _runtime_id(context[0])
        reply_captions = set()
        for c, _ in _walk(context[1], skip_children=lambda node: _runtime_id(node) == editor_id):
            if _visible(c) and _prop(c, "ProcessId", 0) == self._main_pid:
                name = self._flat(_name(c))
                if name.startswith("回复") and _runtime_id(c) != editor_id:
                    reply_captions.add(name)
        # Some Qt builds expose generic input instructions in HelpText even
        # while the recipient placeholder is painted inside the editor.
        if help_text and help_text not in {"评论", "输入评论", "请输入评论"}:
            reply_captions.add(help_text)
        if reply_captions:
            return reply_captions <= captions
        return None

    def _reply_target_matches(self, context, comment, records, *, row=None, wait_for_caption=False):
        binding = (comment.get("comment_id"), comment.get("author_id"))
        proof = getattr(self, "_reply_target_proof", None)
        if proof is not None and row is not None and proof[1] == binding:
            if (self._caption_snapshot(context, row) == proof[0]
                    and 0 <= time.monotonic()-proof[2] <= 30):
                return True

        def probe():
            result = self._uia_reply_target(context, comment, records, row)
            # wait_until must stop on both a match and an explicit mismatch.
            return None if result is None else (result,)

        decision = (wait_until(probe, min(self._timeout(), 0.4), self._cancelled)
                    if wait_for_caption else probe())
        if decision is not None:
            return decision[0]
        # Three states: True (captions name this person), False (a caption names
        # someone else, stop), None (nothing readable). Only None may fall back.
        readable = self._verify_reply_caption_ocr(context, row, comment, records)
        if readable is not None:
            return readable
        return self._reply_target_by_native_identity(context, row, comment, records)

    def _reply_target_by_native_identity(self, context, row, comment, records):
        """Accept the editor when no recipient caption can be read at all.

        Recent WeChat builds paint "回复 X" as an in-editor placeholder that
        neither UIA nor OCR exposes. A `None` result means "nothing readable",
        never "names the wrong person" — an explicit mismatch stops earlier.
        The native comment list is re-read and compared instead; a changed,
        duplicated-across-authors or already-answered target still stops.
        """
        source = getattr(self, "_reply_caption_source", None)
        if not isinstance(source, dict) or row is None:
            return False
        if self._moment_text(context[0]) != "":
            return False
        signature = self._caption_snapshot(context, row)
        if signature is None:
            return False
        try:
            self._verify_collapsed_reply_comments(source, comment, records)
        except Exception:
            return False
        self._reply_target_proof = (signature, (comment.get("comment_id"), comment.get("author_id")),
                                    time.monotonic())
        log.info("[moments-social] 编辑器没有暴露收件人提示，按原生评论 ID 与控件唯一匹配继续")
        return True

    def _caption_snapshot(self, context, row):
        if row is None:
            return None
        actual = self._comment_context(row, _runtime_id(context[0]))
        if (actual is None or any(not _runtime_id(c) for c in context)
                or tuple(_runtime_id(c) for c in actual) != tuple(_runtime_id(c) for c in context)):
            return None
        context = actual
        main, feed, cell = self._main_window(), self._feed(), self._comment_cell(row)
        if (main is None or _prop(main, "NativeWindowHandle", 0) != self._main_handle
                or _prop(main, "ProcessId", 0) != self._main_pid or not self._foreground(main)
                or self._profile_popup() is not None):
            return None
        result = [self._main_handle, self._main_pid]
        for control in (feed, row, cell, *context[:2]):
            rect = _prop(control, "BoundingRectangle")
            token = _runtime_id(control)
            if not token or not rect or _prop(control, "ProcessId", 0) != self._main_pid:
                return None
            result.extend((token, (rect.left, rect.top, rect.right, rect.bottom)))
        holder_token = _runtime_id(context[2])
        if not holder_token or _prop(context[2], "ProcessId", 0) != self._main_pid:
            return None
        result.append(holder_token)
        result.append(self._flat(_name(row)))
        return tuple(result)

    def _caption_point_matches(self, editor, box):
        token = _runtime_id(editor)
        if not token:
            return False
        # Qt's virtual feed can report the enclosing comment cell under the
        # focused editor. Accept only that editor's live, verified cell;
        # generic ancestors and controls from other rows are never sufficient.
        cell, saw_view, current = None, False, _parent(editor)
        for _ in range(12):
            if (current is None or _prop(current, "ProcessId", 0) != self._main_pid
                    or not _runtime_id(current)):
                break
            kind = _prop(current, "ClassName", "")
            saw_view = saw_view or kind == "mmui::CommentReplyTextView"
            if kind == "mmui::TimelineCommentCell":
                cell = current if saw_view else None
                break
            current = _parent(current)
        for x in (box[0]+8, box[2]-8):
            try:
                current = self._auto.ControlFromPoint(x, box[1]+min(24, (box[3]-box[1])//2))
                rect = _prop(current, "BoundingRectangle")
                if (cell is not None and _runtime_id(cell) and _runtime_id(current) == _runtime_id(cell)
                        and _prop(current, "ClassName", "") == "mmui::TimelineCommentCell"
                        and _prop(current, "ProcessId", 0) == self._main_pid and _visible(current)
                        and rect and rect.left <= box[0] < box[2] <= rect.right
                        and rect.top <= box[1] < box[3] <= rect.bottom):
                    focused = self._auto.GetFocusedControl()
                    if (_runtime_id(focused) != token or _prop(focused, "ProcessId", 0) != self._main_pid):
                        return False
                    continue
                for _ in range(12):
                    if current is None or _prop(current, "ProcessId", 0) != self._main_pid:
                        return False
                    if _runtime_id(current) == token:
                        break
                    current = _parent(current)
                else:
                    return False
            except Exception:
                return False
        return True

    def _read_reply_caption(self, box):
        import io
        from PIL import ImageGrab
        from moments_caption_ocr import recognize_caption
        with ImageGrab.grab(bbox=box, all_screens=True) as screenshot:
            with io.BytesIO() as stream:
                screenshot.save(stream, format="PNG")
                return recognize_caption(stream.getvalue(), timeout=4.0)

    def _verify_reply_caption_ocr(self, context, row, comment, records):
        """True: names this person. False: names someone else. None: unreadable."""
        source = getattr(self, "_reply_caption_source", None)
        if not isinstance(source, dict) or row is None or not self._matches_post(row, source, records):
            return False
        signature = self._caption_snapshot(context, row)
        binding = (source.get("post_id"), comment.get("comment_id"), comment.get("author_id"),
            tuple(sorted(self._reply_captions(comment, records, canonical=True))))
        proof = getattr(self, "_reply_caption_proof", None)
        if proof is not None and signature and proof[0] == signature and proof[1] == binding \
                and 0 <= time.monotonic()-proof[2] <= 30:
            return True
        if signature is None or self._moment_text(context[0]) != "":
            return False
        rect, viewport = _prop(context[0], "BoundingRectangle"), _prop(self._feed(), "BoundingRectangle")
        if (not rect or not viewport or not (viewport.left <= rect.left < rect.right <= viewport.right
                and viewport.top <= rect.top < rect.bottom <= viewport.bottom)
                or not 16 <= rect.right-rect.left <= 2048 or not 16 <= rect.bottom-rect.top <= 512):
            return False
        box = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        try:
            self._verify_collapsed_reply_comments(source, comment, records)
            check_cancel(self._cancelled)
            if (self._caption_snapshot(context, row) != signature or self._moment_text(context[0]) != ""
                    or not self._caption_point_matches(context[0], box)):
                return False
            try:
                text = self._read_reply_caption(box)
            except Exception:
                # No OCR backend or an unreadable crop is not proof of a
                # wrong recipient; let the caller fall back to native IDs.
                return None
            check_cancel(self._cancelled)
            if not isinstance(text, str) or not text.strip():
                return None
            if (re.sub(r"\s+", "", text) not in binding[3]
                    or self._caption_snapshot(context, row) != signature
                    or self._moment_text(context[0]) != ""
                    or not self._caption_point_matches(context[0], box)):
                return False
            self._reply_caption_ocr_used = True
            self._reply_caption_proof = (signature, binding, time.monotonic())
            return True
        except Exception:
            check_cancel(self._cancelled)
            return False

    def _comment_names(self, comment, records):
        record = records.get(comment.get("author_id"), {})
        return {self._flat(n) for n in [comment.get("author_name"), *comment.get("author_names", []),
            record.get("remark"), record.get("nickname"), record.get("displayName")]
            if isinstance(n, str) and n}

    def _comment_labels(self, comment, records):
        content = self._flat(comment.get("content", ""))
        names = self._comment_names(comment, records)
        references = self._comment_names({"author_id": comment.get("ref_author_id"),
            "author_name": comment.get("ref_author_name")}, records)
        prefixes = set(names)
        if references:
            prefixes.update(name+separator+recipient for name in names for recipient in references
                for separator in (" 回复 ", "回复"))
        return {self._flat(prefix+colon+gap+content) for prefix in prefixes
            for colon in ("：", ":") for gap in ("", " ")} if content else set()

    def _fresh_source(self, source, action, bot_id, records=None):
        post_id = real_id(source.get("post_id"))
        actor = validate_username(source.get("author_id"))
        if not post_id or source.get("identity_verified") is not True:
            raise RuntimeError("朋友圈缺少核验过的真实动态 ID 与作者微信 ID")
        cancelled = self._cancelled
        cancellation = type("Cancellation", (), {"is_set": lambda _: bool(cancelled and cancelled())})()
        rows = self.feed_client.fetch(limit=100, usernames=[actor], bot_id=bot_id, cancel_event=cancellation)
        matches = [p for p in rows if p.get("post_id") == post_id]
        if len(matches) != 1:
            raise RuntimeError("目标朋友圈已删除或身份记录不唯一")
        current = matches[0]
        if any(current.get(k) != source.get(k) for k in ("author_id", "content", "created_at")):
            raise RuntimeError("目标朋友圈内容或作者已经改变")
        if ("media" in source or "media" in current) and current.get("media", []) != source.get("media", []):
            raise RuntimeError("目标朋友圈媒体已经改变，请重新查看后再决策")
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        same_day = lambda stamp: datetime.fromtimestamp(stamp, tz).date()
        if sum(self._flat(p.get("content", "")) == self._flat(current.get("content", ""))
                and (same_day(p["created_at"]) == same_day(current["created_at"])
                     or abs(p["created_at"]-current["created_at"]) < 3840) for p in rows) != 1:
            raise RuntimeError("朋友圈内容与时间无法唯一对应真实动态 ID")
        if action.get("type") == "reply":
            comments = [c for c in current.get("comments", []) if c.get("comment_id") == action.get("comment_id")
                and c.get("identity_verified") is True and c.get("author_id") == action.get("comment_author_id")]
            if len(comments) != 1 or comments[0].get("author_id") == bot_id:
                raise RuntimeError("原评论 ID 与评论者微信 ID 无法核验")
            previous = [c for c in source.get("comments", []) if c.get("comment_id") == action.get("comment_id")
                and c.get("author_id") == action.get("comment_author_id") and c.get("identity_verified") is True]
            if len(previous) != 1 or previous[0].get("content") != comments[0].get("content"):
                raise RuntimeError("原评论正文已经改变")
            target = comments[0]
            records = self._fresh_identities() if records is None else records
            target_names = self._comment_names(target, records)
            target_labels = self._comment_labels(target, records)
            target_content = self._flat(target.get("content", ""))
            for c in current.get("comments", []):
                if c is target:
                    continue
                names = self._comment_names(c, records)
                same_label = bool(target_labels & self._comment_labels(c, records))
                same_text = bool(target_names & names) and self._flat(c.get("content", "")) == target_content
                if not (same_label or same_text):
                    continue
                # A reply addresses a person: one person's repeated identical
                # comments are interchangeable, so replying to any of them reads
                # the same. Only a collision across two different authors leaves
                # the UI target genuinely ambiguous.
                if c.get("author_id") == target.get("author_id"):
                    continue
                error = RuntimeError("同一动态存在同名同正文评论，无法唯一对应真实评论 ID")
                error.reason_code = "comment_identity_ambiguous"
                raise error
        return current

    def _comment_accepted(self, row, editor, view, original_rect):
        """Focus loss alone is never an acknowledgement of comment submission."""
        main = self._main_window()
        if main is None or not self._foreground(main):
            return False
        cell = self._comment_cell(row)
        if cell is None or not _runtime_id(cell):
            return False
        r = _prop(cell, "BoundingRectangle")
        if r and original_rect and r.bottom <= original_rect.top:
            return True  # The verified row no longer contains the editor region.
        if _prop(editor, "IsOffscreen", None) is True:
            return True
        if self._moment_text(editor) != "":
            return False
        # _unique excludes disabled controls, so inspect the captured view's
        # children explicitly to prove its formerly enabled submit was reset.
        buttons = [c for c, _ in _walk(view) if _name(c) == "发送"
            and _prop(c, "ControlTypeName", "") == "ButtonControl"
            and _prop(c, "ProcessId", 0) == self._main_pid and _runtime_id(c)]
        return len(buttons) == 1 and _prop(buttons[0], "IsEnabled", None) is False

    def _comment_receipt(self, source, bot_id, text, reply_comment_id="", *, timeout=3.0):
        check_cancel(self._cancelled)
        if timeout <= 0:
            return None
        cancelled = self._cancelled
        cancellation = type("Cancellation", (), {"is_set": lambda _: bool(cancelled and cancelled())})()
        try:
            fresh = self.feed_client.read_post_details(source, bot_id=bot_id,
                cancel_event=cancellation, timeout=min(3.0, timeout))
            check_cancel(self._cancelled)
            return find_new_self_comment(source, fresh, bot_id=bot_id, text=text,
                reply_comment_id=reply_comment_id)
        except Exception:
            check_cancel(self._cancelled)
            return None  # Missing evidence cannot turn an uncertain send into success.

    def identity_diagnostics(self, source, *, cancel_event=None, generation=None):
        """Verify current account, post and author without submitting anything."""
        stage = "window"
        try:
            bot_id = validate_username(getattr(config, "BOT_WXID", ""))
            with self._social_operation(cancel_event, generation):
                self._open_feed()
                stage = "fresh_identity"
                records = self._fresh_identities()
                stage = "fresh_source"
                current = self._fresh_source(source, {"type":"comment"}, bot_id, records)
                stage = "account"
                self._verify_account(bot_id, records)
                stage = "locate"
                row = self._locate_post(current, records)
                stage = "author"
                self._verify_author(row, current["author_id"], records)
                return {"ok": True, "account_verified":True, "author_verified":True,
                    "post_id_verified":True}
        except Exception as error:
            return {"ok": False, "stage":stage, "error": str(error)}

    @staticmethod
    def _social_result(ok, error="", *, submitted=False, status=None):
        result = {"ok": bool(ok), "status": status or ("accepted_by_ui" if ok else "failed"),
            "submit_attempted": bool(submitted), "outcome_uncertain": bool(submitted and not ok),
            "delivery_verified": False, "error": str(error)}
        if getattr(error, "uia_diagnostics", None):
            result["uia_diagnostics"] = error.uia_diagnostics
        if getattr(error, "reason_code", None):
            result["error_code"] = error.reason_code
        return result

    def execute_interaction(self, action, *, cancel_event=None, generation=None):
        submitted = False
        stage = "prepare"
        try:
            if not isinstance(action, dict) or action.get("type") not in {"like", "comment", "reply"}:
                raise RuntimeError("朋友圈互动类型无效")
            kind, source, text = action["type"], action.get("source"), action.get("text", "")
            if not isinstance(source, dict) or not isinstance(text, str) or "\0" in text:
                raise RuntimeError("朋友圈互动来源或文字无效")
            if kind != "like" and not text.strip():
                raise RuntimeError("朋友圈评论文字为空")
            bot_id = validate_username(getattr(config, "BOT_WXID", ""))
            with self._social_operation(cancel_event, generation):
                self._open_feed()
                if self._profile_popup() is not None:
                    raise RuntimeError("微信已有身份卡打开，已保留当前操作")
                try:
                    focused = self._auto.GetFocusedControl()
                except Exception:
                    focused = None
                if _prop(focused, "ClassName", "") == "mmui::CommentReplyTextEdit" and _visible(focused):
                    raise RuntimeError("朋友圈已有评论草稿，已保留当前操作")
                stage = "source"
                records = self._fresh_identities()
                current = self._fresh_source(source, action, bot_id, records)
                check_cancel(self._cancelled)
                stage = "account"
                self._verify_account(bot_id, records)
                stage = "locate_post"
                row = self._locate_post(current, records)
                stage = "author"
                self._verify_author(row, current["author_id"], records)
                stage = "relocate_post"
                # The profile popup or viewport scroll can recreate the row.
                row = self._locate_post(current, records, for_menu=True)
                row_token = _runtime_id(row)
                if kind == "like":
                    stage = "like_menu"
                    popup = self._open_post_menu(row)
                    if "取消赞" in self._social_menu_buttons:
                        if self._foreground(self._main_window()) and self._same_control(popup, self._social_popup()):
                            self._keys("{Esc}")
                        return self._social_result(False, "这条动态已点赞，已跳过", status="already_liked")
                    stage = "submit_like"
                    button = self._popup_button(popup, "赞")
                    submitted = True
                    button.Click(simulateMove=False, waitTime=0)
                    stage = "verify_like"
                    if not wait_until(lambda: self._social_popup() is None, self._timeout(), self._cancelled):
                        raise RuntimeError("点赞菜单未关闭，结果待核对")
                    popup = self._open_post_menu(self._locate_post(current, records, for_menu=True))
                    if "取消赞" not in self._social_menu_buttons:
                        raise RuntimeError("点赞后的状态没有确认")
                    self._keys("{Esc}")
                    return self._social_result(True, submitted=True)
                stage = "comment_editor"
                context = self._open_comment(row)
                self._owned_empty_comment = (row, context)
                if kind == "reply":
                    comment = next(c for c in current["comments"] if c.get("comment_id") == action["comment_id"])
                    stage = "reply_target"
                    context = self._choose_reply(row, context, comment, records, source=current)
                editor, view, _ = context
                token = _runtime_id(editor)
                setter = self._moment_value_setter(editor)
                if setter is None:
                    raise RuntimeError("评论编辑器不支持可核验的定向写入")
                if kind == "reply" and (getattr(self, "_reply_geometry_used", False)
                        or getattr(self, "_reply_caption_ocr_used", False)):
                    self._verify_collapsed_reply_comments(current, comment, records)
                if (not self._matches_post(row, current, records)
                        or not row_token or _runtime_id(row) != row_token
                        or _prop(row, "ProcessId", 0) != self._main_pid
                        or self._comment_context(row, token) is None or self._moment_text(editor) != ""):
                    raise RuntimeError("评论输入前草稿或焦点已经改变")
                if kind == "reply" and not self._reply_target_matches(context, comment, records, row=row):
                    raise RuntimeError("回复输入前对象已经改变")
                check_cancel(self._cancelled)
                stage = "write_comment"
                if setter(text) is False:
                    raise RuntimeError("朋友圈评论写入失败")
                if not wait_until(lambda: self._comment_context(row, token)
                        and self._moment_text(editor) == text, self._timeout(), self._cancelled):
                    raise RuntimeError("评论文字没有在目标编辑器中确认")
                button = self._unique(view, lambda c: _name(c) == "发送" and _prop(c, "ControlTypeName", "") == "ButtonControl")
                if button is None or not _prop(button, "IsEnabled", False):
                    raise RuntimeError("朋友圈评论发送按钮不可核验")
                check_cancel(self._cancelled)
                if kind == "reply" and (getattr(self, "_reply_geometry_used", False)
                        or getattr(self, "_reply_caption_ocr_used", False)):
                    self._verify_collapsed_reply_comments(current, comment, records)
                if (not self._matches_post(row, current, records)
                        or not row_token or _runtime_id(row) != row_token
                        or _prop(row, "ProcessId", 0) != self._main_pid
                        or self._comment_context(row, token) is None or self._moment_text(editor) != text):
                    raise RuntimeError("评论提交前编辑器或文字已改变")
                if kind == "reply" and not self._reply_target_matches(context, comment, records, row=row):
                    raise RuntimeError("回复提交前对象已经改变")
                stage = "submit_comment"
                current_button = self._unique(view, lambda c: _name(c) == "发送" and _prop(c, "ControlTypeName", "") == "ButtonControl")
                if not self._same_control(button, current_button):
                    raise RuntimeError("朋友圈评论发送按钮已改变")
                submitted = True
                self._owned_empty_comment = None  # Never close a possibly submitted editor.
                original_rect = _prop(editor, "BoundingRectangle")
                current_button.Click(simulateMove=False, waitTime=0)
                stage = "verify_comment"
                ui_ack = wait_until(lambda: self._comment_accepted(row, editor, view, original_rect),
                    self._timeout(), self._cancelled)
                receipt = None
                if not ui_ack and current.get("interaction_identity_verified") is True:
                    receipt_deadline = time.monotonic()+6.0
                    receipt = wait_until(lambda: self._comment_receipt(current, bot_id, text,
                        action["comment_id"] if kind == "reply" else "",
                        timeout=receipt_deadline-time.monotonic()), 6.0, self._cancelled, interval=.25)
                if not ui_ack and receipt is None:
                    raise RuntimeError("评论提交结果无法核验，待核对")
                result = self._social_result(True, submitted=True)
                result["acknowledgement"] = "native_comment_record" if receipt else "editor_reset"
                return result
        except Exception as error:
            return dict(self._social_result(False, error, submitted=submitted), stage=stage)

    def _post_rows(self, feed=None):
        feed = feed or self._feed()
        rows = [c for c in self._timeline_cells(feed) if _visible(c)
            and _prop(c, "ProcessId", 0) == self._main_pid
            and _prop(c, "ClassName", "") == "mmui::TimelineContentCell"] if feed else []
        return rows

    def _dpi_scale(self):
        if os.name != "nt":
            raise RuntimeError("朋友圈列表布局仅支持 Windows")
        get_dpi = ctypes.windll.user32.GetDpiForWindow
        get_dpi.argtypes, get_dpi.restype = [ctypes.c_void_p], ctypes.c_uint
        dpi = get_dpi(self._main_handle)
        if not 96 <= dpi <= 384:
            raise RuntimeError("微信窗口 DPI 无法核验")
        return dpi / 96

    def _open_post_menu(self, row):
        # The Qt delegate exposes the entire post as one UIA ListItem. Its
        # ellipsis has no child element. Use only this live, verified UIA row's
        # layout, then require the semantic two-button popup before any write.
        token = _runtime_id(row)
        feed = self._feed()
        current = [c for c in self._post_rows(feed) if _runtime_id(c) == token] if feed is not None else []
        if not token or len(current) != 1 or not self._foreground(self._main_window()):
            raise RuntimeError("朋友圈动态控件已改变")
        row = current[0]
        r, f = _prop(row, "BoundingRectangle"), _prop(feed, "BoundingRectangle")
        scale = self._dpi_scale()
        if not r or not f:
            raise RuntimeError("朋友圈操作入口尺寸不可用")
        more = self._coalesce_wrappers([c for c, _ in _walk(row, max_depth=3, max_nodes=32, cancelled=self._cancelled)
            if _prop(c, "ControlTypeName", "") == "ButtonControl" and _visible(c)
            and _prop(c, "ProcessId", 0) == self._main_pid and _name(c).strip().casefold() in {"更多", "更多操作", "more", "…", "···", "..."}])
        if len(more) > 1:
            raise RuntimeError("这条朋友圈有多个操作入口，无法定位")
        if more:
            rect = _prop(more[0], "BoundingRectangle")
            if not rect or not _prop(more[0], "IsEnabled", False):
                raise RuntimeError("朋友圈更多操作按钮不可用")
            x, y = int((rect.left+rect.right)/2), int((rect.top+rect.bottom)/2)
        else:
            x = int((r.left+r.right)/2 + min(240*scale, (r.right-r.left)/2-24*scale))
            y = int(r.bottom-11*scale)
        if not (max(r.left, f.left) <= x < min(r.right, f.right) and max(r.top, f.top) <= y < min(r.bottom, f.bottom)):
            raise RuntimeError("朋友圈操作入口尚未显示，已跳过")
        hit = self._auto.ControlFromPoint(x, y)
        if _runtime_id(hit) != (_runtime_id(more[0]) if more else token):
            raise RuntimeError("朋友圈操作入口被遮挡或布局不匹配")
        check_cancel(self._cancelled)
        self._social_menu_row = row
        self._social_menu_issue = "未发现目标动态的微信操作菜单"
        if more:
            more[0].Click(simulateMove=False, waitTime=0)
        else:
            row.Click(x=x-r.left, y=y-r.top, simulateMove=False, waitTime=0)
        popup = wait_until(self._social_popup, self._timeout(), self._cancelled)
        if popup is None:
            raise RuntimeError("朋友圈点赞评论菜单不可核验：" + self._social_menu_issue)
        pr = _prop(popup, "BoundingRectangle")
        if not pr or not (r.top <= pr.top <= r.bottom and pr.right <= r.right and pr.left >= r.left):
            raise RuntimeError("朋友圈操作菜单与目标动态位置不一致")
        return popup

    def _social_popup(self):
        # Qt may wrap actions in a pane or publish the popup as an owned HWND.
        # Only examine descendants of the timeline menu class for action labels.
        def is_shell(control):
            return _prop(control, "ClassName", "") == "mmui::TimelineFloatMenu"
        popup = self._unique(self._main_window(), is_shell)
        row = getattr(self, "_social_menu_row", None)
        if popup is None and row is not None:
            popup = self._unique(row, is_shell)
        if popup is None and row is not None:
            user = self._native_user32()
            if user is not None:
                candidates = [w for w in self._roots() if is_shell(w) and _visible(w)
                    and _runtime_id(w) and _prop(w, "NativeWindowHandle", 0) != self._main_handle
                    and user.GetAncestor(_prop(w, "NativeWindowHandle", 0), 3) == self._main_handle]
                if len(candidates) == 1:
                    popup = candidates[0]
        if popup is None:
            return None
        labels = {"赞": "赞", "点赞": "赞", "like": "赞", "取消": "取消赞", "取消赞": "取消赞",
                  "取消点赞": "取消赞", "unlike": "取消赞", "评论": "评论", "comment": "评论"}
        buttons = {}
        for item, _ in _walk(popup, max_depth=5, max_nodes=48, cancelled=self._cancelled):
            if (_prop(item, "ControlTypeName", "") not in {"ButtonControl", "MenuItemControl"}
                    or _prop(item, "ProcessId", 0) != self._main_pid or not _visible(item)):
                continue
            key = labels.get(_name(item).strip().casefold())
            if key:
                buttons.setdefault(key, []).append(item)
        actions = {key: self._coalesce_wrappers(items) for key, items in buttons.items()}
        if (len(actions) != 2 or "评论" not in actions or any(len(items) != 1 for items in actions.values())):
            self._social_menu_issue = "已找到菜单，点赞状态或评论操作项缺失或不唯一"
            return None
        self._social_menu_buttons = {key: items[0] for key, items in actions.items()}
        return popup

    def _popup_button(self, popup, name):
        current = self._social_popup()
        if not self._same_control(popup, current) or not (self._foreground(self._main_window()) or self._foreground(popup)):
            raise RuntimeError("朋友圈操作菜单或前台已改变")
        button = self._social_menu_buttons.get(name)
        if button is None or not _prop(button, "IsEnabled", False):
            raise RuntimeError("朋友圈操作按钮无法确认")
        check_cancel(self._cancelled)
        return button

    @contextmanager
    def _social_operation(self, cancel_event, generation):
        cancelled = self._cancel_test(cancel_event, generation)
        with desktop_operation(cancelled), self._uia_context():
            self._cancelled = cancelled
            self._reply_geometry_used = False
            self._reply_caption_proof = self._reply_caption_source = None
            self._reply_caption_ocr_used = False
            self._owned_empty_comment = None
            try:
                if not self._ensure_accessibility():
                    raise RuntimeError(self.accessibility_error())
                self._main_pid = _prop(self._window, "ProcessId", 0)
                self._main_handle = _prop(self._window, "NativeWindowHandle", 0)
                if not self._activate(self._window):
                    raise RuntimeError("微信主窗口无法取得前台焦点")
                yield
            finally:
                self._social_menu_row = None
                self._social_menu_buttons = {}
                self._close_owned_empty_comment()
                self._update_operation_snapshot("朋友圈")
                self._cancelled = None
                self._reply_geometry_used = False
                self._reply_caption_proof = self._reply_caption_source = None
                self._reply_caption_ocr_used = False

    def _open_feed(self):
        if self._has_composer() or self._file_dialog():
            raise RuntimeError("朋友圈已有草稿或文件窗口，已保留现有操作")
        feed = wait_until(self._ready_feed, min(.3, self._timeout()), self._cancelled)
        if feed is not None:
            return feed
        entry = self._main_named({"朋友圈"})
        # Clicking an already selected entry can close the SNS page. Its live
        # toolbar or native selection pattern is sufficient to avoid toggling.
        if self._moments_page_selected(entry):
            feed = self._wait_main(self._ready_feed, recover_missing=True)
            if feed is None:
                raise self._ui_error("open_selected_feed", "当前朋友圈页面已打开，但列表控件尚未暴露",
                    main=self._main_window(), feed=self._feed())
            return feed
        if entry is None:
            discover = self._main_named({"发现"})
            if discover is None:
                # The same HWND can go cold after page navigation although its
                # native shell remains visible. Warm it once before concluding
                # that the sidebar entry is missing.
                found = self._wait_main(lambda: self._main_named({"朋友圈"})
                    or self._main_named({"发现"}), recover_missing=True)
                if _name(found) == "朋友圈":
                    entry = found
                else:
                    discover = found
            if entry is None and not self._click_control(discover):
                raise RuntimeError("未找到唯一的发现入口")
            if entry is None:
                entry = self._wait_main(lambda: self._main_named({"朋友圈"}))
        if not self._click_control(entry):
            raise RuntimeError("未找到唯一的朋友圈入口")
        feed = self._wait_main(self._ready_feed, recover_missing=True)
        if feed is None:
            raise self._ui_error("open_feed", "朋友圈列表无法确认", main=self._main_window())
        return feed

    def _ready_feed(self):
        feed = self._feed()
        return feed if feed is not None and self._timeline_cells(feed) else None

    def refresh_feed(self, *, cancel_event=None, generation=None):
        try:
            with self._social_operation(cancel_event, generation):
                self._open_feed()
                main_identity = (self._main_handle, self._main_pid)
                toolbar = self._wait_main(self._sns_toolbar, recover_missing=True)
                refresh = self._unique(toolbar, lambda c: _name(c) == "刷新"
                    and _prop(c, "ControlTypeName", "") == "ButtonControl")
                main = self._main_window()
                if (refresh is None or main is None
                        or self._window_identity(main) != main_identity
                        or (not self._foreground(main) and not self._activate(main))):
                    raise RuntimeError("朋友圈刷新前窗口或按钮已经改变")
                # Activation/navigation can rebuild the toolbar. Use its live
                # unique refresh button; old bounds and wrapper IDs are not
                # identity for this read-only navigation command.
                current = self._unique(self._sns_toolbar(), lambda c: _name(c) == "刷新"
                    and _prop(c, "ControlTypeName", "") == "ButtonControl")
                refresh_token = _runtime_id(current)
                root, main = self._root_for(current), self._main_window()
                if (current is None or not refresh_token or not _visible(current)
                        or not _prop(current, "IsEnabled", False)
                        or _prop(current, "ProcessId", 0) != main_identity[1]
                        or root is None or main is None
                        or self._window_identity(root) != main_identity
                        or self._window_identity(main) != main_identity
                        or not self._foreground(main)):
                    raise RuntimeError("朋友圈刷新按钮无法确认")
                check_cancel(self._cancelled)
                if _runtime_id(current) != refresh_token:
                    raise RuntimeError("朋友圈刷新按钮无法确认")
                try:
                    current.Click(simulateMove=False, waitTime=0)
                except Exception:
                    raise RuntimeError("朋友圈刷新按钮无法确认") from None
                if self._wait_main(self._ready_feed, recover_missing=True) is None:
                    raise self._ui_error("refresh_feed_wait", "刷新后朋友圈列表不可用", main=self._main_window())
                return {"ok": True, "status": "refresh_requested", "refreshed_at": time.time(), "error": ""}
        except Exception as error:
            result = {"ok": False, "status": "failed", "error": str(error)}
            if getattr(error, "uia_diagnostics", None):
                result["uia_diagnostics"] = error.uia_diagnostics
            return result

    @staticmethod
    def _control_info(control):
        if control is None:
            return None
        rect = _prop(control, "BoundingRectangle")
        return {"name": _name(control), "type": _prop(control, "ControlTypeName", ""),
            "class": _prop(control, "ClassName", ""), "aid": _prop(control, "AutomationId", ""),
            "pid": _prop(control, "ProcessId", 0), "hwnd": _prop(control, "NativeWindowHandle", 0),
            "runtime_id": list(_runtime_id(control)),
            "rect": [rect.left, rect.top, rect.right, rect.bottom] if rect else None}

    def social_diagnostics(self, *, cancel_event=None, generation=None, open_comment_for="", open_profile_for=""):
        """Read-only diagnostic evidence; point lookup performs no mouse input."""
        try:
            with self._social_operation(cancel_event, generation):
                if open_profile_for:
                    matches = [c for c in self._post_rows() if _name(c) == open_profile_for]
                    if len(matches) != 1:
                        raise RuntimeError("诊断动态不唯一")
                    row = matches[0]
                    r = _prop(row, "BoundingRectangle")
                    s = self._dpi_scale()
                    row.Click(x=int((r.right-r.left)/2-235*s), y=int(40*s), simulateMove=False, waitTime=0)
                if open_comment_for:
                    matches = [c for c in self._post_rows() if _name(c) == open_comment_for]
                    if len(matches) != 1:
                        raise RuntimeError("诊断动态不唯一")
                    existing = self._auto.GetFocusedControl()
                    if _prop(existing, "ClassName", "") != "mmui::CommentReplyTextEdit":
                        popup = self._open_post_menu(matches[0])
                        popup_info = self._control_info(popup)
                        self._popup_button(popup, "评论").Click(simulateMove=False, waitTime=0)
                    else:
                        popup_info = None
                else:
                    popup_info = None
                feed = self._feed()
                rows = []
                for row in feed.GetChildren() if feed is not None else []:
                    data = self._control_info(row)
                    data["children"] = [self._control_info(c) for c in row.GetChildren()]
                    data["value"] = self._moment_text(row)
                    r = _prop(row, "BoundingRectangle")
                    data["points"] = []
                    if r and r.bottom > r.top and r.right > r.left:
                        for rx, ry in ((.95, .82), (.5, .5)):
                            c = self._auto.ControlFromPoint(int(r.left+(r.right-r.left)*rx), int(r.top+(r.bottom-r.top)*ry))
                            data["points"].append(self._control_info(c))
                    rows.append(data)
                editors = [self._control_info(c) for c, _ in _walk(self._main_window())
                    if _prop(c, "ControlTypeName", "") == "EditControl"]
                try:
                    focused = self._auto.GetFocusedControl()
                except Exception:
                    focused = None
                ancestry = []
                current = focused
                for _ in range(8):
                    if current is None or _prop(current, "ProcessId", 0) != self._main_pid:
                        break
                    ancestry.append({"control": self._control_info(current),
                        "children": [self._control_info(c) for c in current.GetChildren()]})
                    current = _parent(current)
                return {"ok": True, "rows": rows, "editors": editors,
                    "popup": popup_info,
                    "focused": self._control_info(focused), "focused_value": self._moment_text(focused),
                    "focused_ancestry": ancestry,
                    "profile_controls": [self._control_info(c) for root in self._roots() for c, _ in _walk(root)
                        if _prop(c, "ControlTypeName", "") in {"TextControl", "WindowControl"} or "profile" in str(_prop(c, "ClassName", "")).casefold()],
                    "comment_controls": [self._control_info(c) for c, _ in _walk(_parent(_parent(_parent(focused))))]
                        if _prop(focused, "ClassName", "") == "mmui::CommentReplyTextEdit" else [],
                    "roots": [self._control_info(c) for c in self._roots()]}
        except Exception as error:
            return {"ok": False, "error": str(error)}
