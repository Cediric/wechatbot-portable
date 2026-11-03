"""Verified Moments composer using the same desktop transaction as chat sends."""
from __future__ import annotations

import os
import logging
import re

from desktop_coordinator import ClipboardLease, check_cancel, desktop_operation, native_top_windows, wait_until
from uia_sender import UiaSender, _name, _prop, _parent, _visible, _walk

log = logging.getLogger("weflow-bridge")


def _runtime_id(control):
    # Python wrapper identity cannot replace native identity in a publication
    # guard, especially after a UIA provider or composer has been recreated.
    try:
        return tuple(control.GetRuntimeId())
    except Exception:
        return ()


class MomentsSender(UiaSender):
    MOMENT_EDITORS = {"这一刻的想法", "分享新鲜事", "这一刻的想法...", "这一刻的想法…"}

    def __init__(self):
        super().__init__(search_enabled=False)
        self._main_pid = 0
        self._main_handle = 0

    def _is_wechat(self, window, main=True):
        if super()._is_wechat(window, main=main):
            return True
        # The bound physical HWND can briefly expose only Qt's native stub.
        # Its missing accessible title/tree is a recovery state, not a popout.
        return bool(main and self._main_handle and self._main_pid
            and self._window_identity(window) == (self._main_handle, self._main_pid)
            and re.fullmatch(r"Qt\d+QWindowIcon", str(_prop(window, "ClassName", "")))
            and super()._is_wechat(window, main=False))

    @staticmethod
    def _aid(control, component):
        return component.casefold() in str(_prop(control, "AutomationId", "")).casefold().split(".")

    @staticmethod
    def _same_control(left, right):
        # UIA creates fresh Python wrappers during parent/tree traversal. Their
        # object identity is unrelated to the underlying accessible element.
        if left is None or right is None:
            return False
        token = _runtime_id(left)
        return bool(token) and token == _runtime_id(right)

    def _main_window(self):
        """Fresh tree for the original main HWND; a popout cannot replace it."""
        if not self._main_handle or not self._main_pid:
            return None
        self._main_lookup_observed = None
        missing = object()
        try:
            getter = getattr(self._auto, "ControlFromHandle", missing)
            def valid(window):
                return (window is not None and _prop(window, "ProcessId", 0) == self._main_pid
                    and _prop(window, "NativeWindowHandle", 0) == self._main_handle
                    and self._is_wechat(window) and _prop(window, "IsOffscreen", None) is False)
            if getter is not missing:
                try:
                    window = getter(self._main_handle) if callable(getter) else None
                except Exception:
                    window = None
                observed = window
                self._main_lookup_observed = {"hwnd": _prop(observed, "NativeWindowHandle", 0),
                    "pid": _prop(observed, "ProcessId", 0), "class": _prop(observed, "ClassName", "")}
                # Some Qt releases return a virtual child for ControlFromHandle.
                # Walk its real ancestry only as far as the first physical HWND.
                for _ in range(33):
                    if window is None:
                        break
                    if _prop(window, "ProcessId", 0) != self._main_pid:
                        return None
                    if _prop(window, "NativeWindowHandle", 0):
                        return window if valid(window) else None
                    window = _parent(window)
                # A missing/cold handle provider may still have an exact native
                # root. Enumeration remains bound to the original HWND and PID.
            native = native_top_windows(self._auto, pid=self._main_pid)
            if native is not None:
                # The exact handle was already queried above. A cold provider
                # is recovered by the caller; unrelated desktop providers add
                # no evidence about this bound main window.
                return None
            matches = [w for w in self._auto.GetRootControl().GetChildren() if valid(w)]
            return matches[0] if matches else None  # Every match is the same physical HWND.
        except Exception:
            return None

    @staticmethod
    def _native_attributes(control):
        rect = _prop(control, "BoundingRectangle")
        return {"hwnd": _prop(control, "NativeWindowHandle", 0),
            "pid": _prop(control, "ProcessId", 0), "runtime_id": list(_runtime_id(control)),
            "class": str(_prop(control, "ClassName", ""))[:80],
            "role": str(_prop(control, "ControlTypeName", ""))[:32],
            "aid": str(_prop(control, "AutomationId", ""))[:96],
            "offscreen": _prop(control, "IsOffscreen"),
            "bounds": [rect.left, rect.top, rect.right, rect.bottom] if rect else None}

    def _foreground(self, root=None):
        if super()._foreground(root):
            return True
        try:
            # Native foreground evidence, when available, remains authoritative.
            if self._native_user32() is not None:
                return False
            expected = root or self._window
            front = self._auto.GetForegroundControl()
            physical = self._root_for(front)
            if (physical is not None and expected is not None
                    and self._window_identity(physical) == self._window_identity(expected)
                    and self._is_wechat(physical, main=self._window_identity(expected)
                        == (self._main_handle, self._main_pid)) and _visible(physical)):
                self._activation_failure = ""
                return True
        except Exception:
            pass
        return False

    def _unique(self, root, predicate):
        matches = {}
        for c, _ in _walk(root, cancelled=self._cancelled,
                          skip_children=(lambda item: self._voice_scan_skip(item)
                              or item is not root and (self._aid(item, "sns_list")
                                  or _prop(item, "ClassName", "") in {"mmui::TimeLineListView",
                                  "mmui::TimelineCell", "mmui::TimelineContentCell", "mmui::TimelineCommentCell"}))
                              if root is not None else None):
            if _visible(c) and _prop(c, "ProcessId", 0) == self._main_pid and predicate(c):
                token = _runtime_id(c)
                if not token:
                    return None
                matches[token] = c
        return next(iter(matches.values())) if len(matches) == 1 else None

    def _feed(self, main=None):
        main = main or self._main_window()
        return self._unique(main, lambda c: (self._aid(c, "sns_list")
                            or _prop(c, "ClassName", "") == "mmui::TimeLineListView")
                            and _prop(c, "ControlTypeName", "") in {"ListControl", "PaneControl"}) if main else None

    def _sns_toolbar(self):
        main = self._main_window()
        return self._unique(main, lambda c: self._aid(c, "sns_window_tool_bar")
                            and _prop(c, "ControlTypeName", "") == "ToolBarControl") if main else None

    def _main_named(self, names):
        main = self._main_window()
        return self._unique(main, lambda c: _name(c) in names
                            and _prop(c, "ControlTypeName", "") in {"ButtonControl", "TabItemControl"}) if main else None

    def _moments_page_selected(self, entry):
        if self._sns_toolbar() is not None:
            return True
        try:
            pattern = entry.GetSelectionItemPattern()
            return pattern is not None and pattern.IsSelected is True
        except Exception:
            return False

    def _publish_panels(self):
        main = self._main_window()
        return [c for c, _ in _walk(main, cancelled=self._cancelled,
                skip_children=lambda c: self._aid(c, "sns_list")
                    or _prop(c, "ClassName", "") == "mmui::TimeLineListView")
                if _visible(c) and self._aid(c, "SnsPublishPanel")
                and _prop(c, "ProcessId", 0) == self._main_pid
                and _prop(c, "ControlTypeName", "") == "WindowControl"] if main else []

    def _composer_for(self, editor):
        current = _parent(editor)
        for _ in range(33):
            if current is None or _prop(current, "ProcessId", 0) != self._main_pid:
                break
            if _prop(current, "ControlTypeName", "") == "WindowControl":
                if self._aid(current, "SnsPublishPanel"):
                    return current
                if (_prop(current, "NativeWindowHandle", 0) != self._main_handle
                        and ("moment" in str(_prop(current, "ClassName", "")).casefold()
                             or _name(current) in {"发表朋友圈", "发布朋友圈"})):
                    return current
            current = _parent(current)
        return None

    def _roots(self):
        # Never inspect/click controls in other applications, even if named 发表.
        native = native_top_windows(self._auto, pid=self._main_pid)
        if native is None:
            candidates = self._auto.GetRootControl().GetChildren()
        else:
            candidates = []
            for item in native:
                check_cancel(self._cancelled)
                candidates.append(self._auto.ControlFromHandle(item["hwnd"]))
        return [w for w in candidates
                if _prop(w, "ProcessId", 0) == self._main_pid and self._is_wechat(w, main=False)]

    def _find_control_global(self, names, types=None):
        matches = {}
        for root in self._roots():
            for control, _ in _walk(root):
                if _name(control) in names and _visible(control) and (types is None or _prop(control, "ControlTypeName", "") in types):
                    token = _runtime_id(control)
                    if not token:
                        return None
                    matches[token] = control
        return next(iter(matches.values())) if len(matches) == 1 else None

    def _root_for(self, control):
        token = _runtime_id(control)
        current = control
        for _ in range(33):  # Same supported nesting bound as the UI tree walk.
            if current is None or _prop(current, "ProcessId", 0) != self._main_pid:
                break
            handle = _prop(current, "NativeWindowHandle", 0)
            kind = _prop(current, "ControlTypeName", "")
            if handle == self._main_handle and handle:
                # A virtual pane can report its owning HWND while its class is
                # not the physical main-window class. Resolve that exact HWND.
                return self._main_window()
            if handle and kind == "WindowControl":
                return current  # Nearest physical modal, or the embedded main.
            if handle and kind == "PaneControl" and not (
                    _prop(current, "ClassName", "") == "ComboBoxEx32"
                    and str(_prop(current, "AutomationId", "")) == "1148"):
                # Some Qt providers expose the physical main as a pane. Only
                # the original main HWND may replace the usual window role.
                # The Win32 filename combo's host is a child HWND of the picker.
                return current if (handle == self._main_handle
                    and self._is_wechat(current)
                    and _prop(current, "IsOffscreen", None) is False) else None
            current = _parent(current)
        if current is None and token:
            main = self._main_window()
            # A missing virtual parent link is not a changed physical window.
            # Prove membership by enumeration without crossing another HWND.
            for candidate, _ in _walk(main, cancelled=self._cancelled,
                    skip_children=lambda c: bool(_prop(c, "NativeWindowHandle", 0))
                    and _prop(c, "NativeWindowHandle", 0) != self._main_handle):
                if _runtime_id(candidate) == token and _prop(candidate, "ProcessId", 0) == self._main_pid:
                    return main
        return None

    def _click_control(self, control, *, right=False, activate=True):
        if control is None or not _visible(control) or not _prop(control, "IsEnabled", False):
            return False
        root = self._root_for(control)
        if root is None or (activate and not self._activate(root)):
            return False
        check_cancel(self._cancelled)
        if not self._foreground(root):
            return False
        try:
            method = control.RightClick if right else control.Click
            method(simulateMove=False, waitTime=0)
            return True
        except Exception as exc:
            self._set_error("朋友圈控件无法点击: " + str(exc))
            return False

    def _text_menu_item(self):
        items = {}
        for window in self._roots():
            for item, _ in _walk(window):
                if (not _visible(item) or _prop(item, "ProcessId", 0) != self._main_pid
                        or _prop(item, "ControlTypeName", "") != "MenuItemControl"
                        or _name(item) != "发表文字" or not self._aid(item, "XMenuItem")
                        or str(_prop(item, "ClassName", "")).casefold() != "mmui::xmenuview"):
                    continue
                menu = self._root_for(item)
                if (menu is None or _prop(menu, "ProcessId", 0) != self._main_pid
                        or _prop(menu, "ControlTypeName", "") != "WindowControl"
                        or str(_prop(menu, "ClassName", "")).casefold() != "mmui::xmenu"
                        or not _visible(menu) or not _prop(menu, "NativeWindowHandle", 0)
                        or _prop(menu, "NativeWindowHandle", 0) == self._main_handle):
                    continue
                token, menu_token = _runtime_id(item), _runtime_id(menu)
                if not token or not menu_token:
                    return None
                items[token] = item
        return next(iter(items.values())) if len(items) == 1 else None

    def _click_text_menu(self, item, main_token):
        """Qt's nonactivating XMenu stays owned by the main foreground window."""
        menu = self._root_for(item)
        item_token, menu_token = _runtime_id(item), _runtime_id(menu)
        if not main_token or not item_token or not menu_token:
            return False
        check_cancel(self._cancelled)
        current = self._text_menu_item()
        current_menu = self._root_for(current)
        main = self._main_window()
        if (main is None or _runtime_id(main) != main_token
                or current is None or _runtime_id(current) != item_token
                or current_menu is None or _runtime_id(current_menu) != menu_token
                or _prop(current_menu, "NativeWindowHandle", 0) != _prop(menu, "NativeWindowHandle", -1)):
            return False
        try:
            foreground = self._auto.GetForegroundControl()
            if (_prop(foreground, "ProcessId", 0) != self._main_pid
                    or (_runtime_id(foreground), _prop(foreground, "NativeWindowHandle", 0)) not in {
                        (main_token, self._main_handle),
                        (menu_token, _prop(current_menu, "NativeWindowHandle", 0))}):
                return False
            check_cancel(self._cancelled)
            # Activating this popup closes it in WeChat 4.1. Never call
            # SetActive or type keys here; click only the freshly verified item.
            current.Click(simulateMove=False, waitTime=0)
            return True
        except Exception as error:
            self._set_error("朋友圈发表文字菜单无法点击: " + str(error))
            return False

    def _find_moment_editor(self):
        panels = self._publish_panels()
        if panels:
            if len(panels) != 1:
                return None
            # In 4.x the verified child Window contains one unnamed Edit.
            return self._unique(panels[0], lambda c: _prop(c, "ControlTypeName", "") == "EditControl")
        candidates = []
        for root in self._roots():
            for control, _ in _walk(root):
                if _prop(control, "ControlTypeName", "") != "EditControl" or not _visible(control):
                    continue
                aid = str(_prop(control, "AutomationId", "")).casefold()
                if ((_name(control) in self.MOMENT_EDITORS or any(s in aid for s in ("moment_input", "moments_input", "moment_editor")))
                        and self._composer_for(control) is not None):
                    candidates.append(control)
        return candidates[0] if len(candidates) == 1 else None

    def _has_composer(self):
        # Refuse even an empty composer opened by the user, or a malformed /
        # duplicate composer. Looking only for a unique editor misses those.
        if self._publish_panels():
            return True
        return any(self._composer_for(c) is not None for root in self._roots() for c, _ in _walk(root,
                   cancelled=self._cancelled, skip_children=lambda c: self._aid(c, "sns_list")
                       or _prop(c, "ClassName", "") == "mmui::TimeLineListView")
                   if _visible(c) and _prop(c, "ControlTypeName", "") == "EditControl")

    def _file_dialog(self):
        matches = {}
        seen = set()
        roles = {"1148": {"EditControl"}, "1": {"SplitButtonControl", "ButtonControl"}, "2": {"ButtonControl"}}
        for root in self._roots():
            for dialog, _ in _walk(root, cancelled=self._cancelled,
                    skip_children=lambda c: self._aid(c, "sns_list")
                        or _prop(c, "ClassName", "") in {"#32770", "mmui::TimeLineListView"}):
                handle = _prop(dialog, "NativeWindowHandle", 0)
                if (not handle or handle == self._main_handle or handle in seen
                        or _prop(dialog, "ControlTypeName", "") != "WindowControl" or not _visible(dialog)):
                    continue
                # Windows exposes the same owned dialog both beneath WeChat and
                # as a top-level HWND. Its folder view may contain thousands of files.
                seen.add(handle)
                controls = {}
                for child, _ in _walk(dialog, cancelled=self._cancelled,
                                      skip_children=lambda c: str(_prop(c, "AutomationId", "")) in
                                          {"listview", "ProperTreeHost", "FolderBandModuleInner"}):
                    aid = str(_prop(child, "AutomationId", ""))
                    if (_prop(child, "ControlTypeName", "") not in roles.get(aid, ())
                            or not _visible(child) or _prop(child, "ProcessId", 0) != self._main_pid):
                        continue
                    if aid == "1148":
                        combo = _parent(child)
                        if _prop(combo, "ControlTypeName", "") != "ComboBoxControl" or str(_prop(combo, "AutomationId", "")) != aid:
                            continue
                    token = _runtime_id(child)
                    if token:
                        controls.setdefault(aid, {})[token] = child
                if all(len(controls.get(aid, {})) == 1 for aid in ("1148", "1", "2")):
                    filename = next(iter(controls["1148"].values()))
                    if self._same_control(self._root_for(filename), dialog):
                        matches[handle] = (dialog, filename, next(iter(controls["1"].values())))
        return next(iter(matches.values())) if len(matches) == 1 else None

    def _choose_image(self, publish, image_path):
        if not self._click_control(publish):
            raise RuntimeError("朋友圈图片选择入口无法确认")
        selection = wait_until(self._file_dialog, self._timeout(), self._cancelled)
        if selection is None:
            raise RuntimeError("朋友圈图片文件对话框无法确认，已停止")
        dialog, filename, opening = selection
        self._window = dialog
        path = os.path.abspath(image_path)
        if not self._activate(dialog) or not self._focus(filename) or self._moment_text(filename) is None:
            raise RuntimeError("朋友圈文件名输入框不可读或焦点无法确认")
        self._lease.set_text(path)
        self._keys("{Ctrl}a")
        if not self._focused(filename):
            raise RuntimeError("朋友圈文件名输入焦点已改变")
        self._keys("{Ctrl}v")
        if not wait_until(lambda: self._moment_text(filename) == path, self._timeout(), self._cancelled):
            raise RuntimeError("朋友圈图片路径未能在文件名输入框中确认")
        check_cancel(self._cancelled)
        current = self._file_dialog()
        if (not self._main_window() or not current or not self._same_control(current[0], dialog)
                or not self._same_control(current[1], filename)
                or not self._same_control(current[2], opening)
                or self._moment_text(filename) != path
                or not self._focused(filename) or not self._click_control(current[2], activate=False)):
            raise RuntimeError("朋友圈图片选择对话框或焦点已改变")

    def _moment_attachment_ids(self, editor, image_path):
        composer = self._composer_for(editor)
        filename = os.path.basename(image_path)
        found = set()
        if composer is None:
            return frozenset()
        if (str(_prop(composer, "ClassName", "")).casefold() == "mmui::snspublishpanel"
                and self._aid(composer, "SnsPublishPanel")):
            return self._grid_attachment_ids(composer)
        for c, _ in _walk(composer):
            if not _visible(c) or _prop(c, "ControlTypeName", "") not in {"ImageControl", "ListItemControl", "PaneControl", "TextControl"}:
                continue
            current, toolbar, within_composer = _parent(c), False, False
            for _ in range(33):
                if current is None:
                    break
                if self._same_control(current, composer):
                    within_composer = True
                    break
                if _prop(current, "ControlTypeName", "") in {"ButtonControl", "ToolBarControl", "MenuItemControl"}:
                    toolbar = True
                    break
                current = _parent(current)
            name, aid = _name(c), str(_prop(c, "AutomationId", "")).casefold()
            klass = str(_prop(c, "ClassName", "")).casefold()
            if within_composer and not toolbar and (name in {"图片预览", "[图片]"}
                                or (filename and name == filename and _prop(c, "ControlTypeName", "") == "ImageControl")
                                or any(s in aid for s in ("image_preview", "attachment_preview", "attached_image"))
                                or "imagepreview" in klass):
                token = _runtime_id(c)
                if not token:
                    return frozenset()
                found.add(token)
        return frozenset(found)

    def _grid_attachment_ids(self, composer):
        """The observed 4.1 photo grid; the anonymous add-image icon is not proof."""
        grids = {}
        for grid, _ in _walk(composer):
            if (_prop(grid, "ControlTypeName", "") != "ListControl"
                    or str(_prop(grid, "ClassName", "")).casefold() != "mmui::xdraggridview"):
                continue
            token = _runtime_id(grid)
            if not token or not _visible(grid) or _prop(grid, "ProcessId", 0) != self._main_pid:
                return frozenset()
            current, inside = _parent(grid), False
            for _ in range(33):
                if current is None or _prop(current, "ProcessId", 0) != self._main_pid:
                    break
                if self._same_control(current, composer):
                    inside = True
                    break
                if _prop(current, "ControlTypeName", "") in {"WindowControl", "ButtonControl", "ToolBarControl", "MenuItemControl"}:
                    break
                current = _parent(current)
            if not inside:
                return frozenset()
            grids[token] = grid
        if len(grids) != 1:
            return frozenset()
        grid_token, grid = next(iter(grids.items()))
        try:
            children = grid.GetChildren()
        except Exception:
            return frozenset()
        images = []
        for cell in children:
            if _prop(cell, "ControlTypeName", "") != "ListItemControl":
                continue
            klass = str(_prop(cell, "ClassName", "")).casefold()
            if (klass == "mmui::publishimageaddgridcell" and _name(cell) == "添加图片"
                    and self._same_control(_parent(cell), grid)):
                continue
            token = _runtime_id(cell)
            if (klass != "mmui::publishimagegridcell" or _name(cell) != "图片"
                    or not token or not _visible(cell) or _prop(cell, "ProcessId", 0) != self._main_pid
                    or not self._same_control(_parent(cell), grid)):
                return frozenset()
            images.append(token)
        if len(images) != 1:
            return frozenset()
        # Capture both identities: replacing the grid or its sole image after
        # the verified file selection invalidates the final submit guard.
        return frozenset({(grid_token, images[0])})

    def _moment_attachment(self, editor, image_path):
        return bool(self._moment_attachment_ids(editor, image_path))

    def _wait_main(self, getter, *, recover_missing=False):
        def fresh():
            main = self._main_window()
            if main is None:
                return None
            self._window = main
            return getter()
        result = wait_until(fresh, min(0.3, self._timeout()), self._cancelled)
        if result:
            return result
        # Navigation can replace a provider with a cold tree. Refresh only the
        # same verified HWND, with the already tested single Tab path.
        if self._main_window() is None:
            return None
        if recover_missing:
            # A chat list does not establish that the selected SNS page is warm.
            self._recover_current_window(controls_ready=lambda: getter() is not None)
        elif not self._controls_available() and not self._recover_current_window():
            return None
        return wait_until(fresh, self._timeout(), self._cancelled)

    def _open_composer(self, image_path):
        if self._has_composer() or self._file_dialog():
            raise RuntimeError("朋友圈已有编辑窗口，已保留现有草稿并取消自动发表")
        # Clicking an already selected 4.x 朋友圈 entry toggles the feed off.
        if self._feed() is None:
            entry = self._main_named({"朋友圈"})
            if self._moments_page_selected(entry):
                if self._wait_main(self._feed, recover_missing=True) is None:
                    raise RuntimeError("当前朋友圈页面已打开，但列表控件尚未暴露")
            else:
                if entry is None:
                    discover = self._main_named({"发现"})
                    if discover is None:
                        found = self._wait_main(lambda: self._main_named({"朋友圈"})
                            or self._main_named({"发现"}), recover_missing=True)
                        if _name(found) == "朋友圈":
                            entry = found
                        else:
                            discover = found
                    if entry is None and not self._click_control(discover):
                        raise RuntimeError("未找到唯一的朋友圈入口")
                    if entry is None:
                        entry = self._wait_main(lambda: self._main_named({"朋友圈"}))
                if not self._click_control(entry):
                    raise RuntimeError("朋友圈入口无法确认或点击")
        legacy = self._find_control_global({"相机", "发表朋友圈", "发布朋友圈"}, {"ButtonControl"})
        toolbar = self._sns_toolbar() if legacy else self._wait_main(self._sns_toolbar, recover_missing=True)
        if toolbar is not None:
            if self._feed() is None:
                raise RuntimeError("朋友圈列表未能确认")
            publish = self._unique(toolbar, lambda c: _name(c) == "发表"
                                   and _prop(c, "ControlTypeName", "") == "ButtonControl")
            if image_path:
                self._choose_image(publish, image_path)
                return self._wait_main(self._find_moment_editor), True
            main = self._main_window()
            main_token = _runtime_id(main)
            if not main_token or not self._click_control(publish, right=True):
                raise RuntimeError("朋友圈文字菜单入口无法确认")
            text_item = wait_until(self._text_menu_item, self._timeout(), self._cancelled)
            if text_item is None:
                raise RuntimeError("未找到唯一的朋友圈发表文字菜单")
            if not self._click_text_menu(text_item, main_token):
                raise RuntimeError("朋友圈发表文字菜单身份或前台无法确认，已停止")
            editor = self._wait_main(self._find_moment_editor)
            return editor, True
        publish = legacy or self._find_control_global({"相机", "发表朋友圈", "发布朋友圈"}, {"ButtonControl"})
        if not self._click_control(publish):
            raise RuntimeError("未找到唯一的朋友圈编辑入口")
        return wait_until(self._find_moment_editor, self._timeout(), self._cancelled), False

    def _composer_closed(self, composer, embedded):
        main = self._main_window()
        if main is None:
            return False  # Closing WeChat itself is not publication evidence.
        if embedded:
            self._window = main
            return self._feed() is not None and not self._publish_panels()
        handle = _prop(composer, "NativeWindowHandle", 0)
        if not handle or self._has_composer():
            return False
        if any(_prop(c, "ControlTypeName", "") == "WindowControl"
               and _prop(c, "NativeWindowHandle", 0) == handle
               for root in self._roots() for c, _ in _walk(root)):
            return False
        return bool(_runtime_id(composer)) and all(not self._same_control(c, composer)
                                                  for root in self._roots() for c, _ in _walk(root))

    def _moment_text(self, editor):
        # A placeholder Name is not draft evidence. A real draft equal to that
        # placeholder must also be preserved instead of being treated as empty.
        observed_empty = False
        for method in ("GetValuePattern", "GetTextPattern", "GetLegacyIAccessiblePattern"):
            try:
                pattern = getattr(editor, method)()
                if pattern is not None:
                    value = pattern.DocumentRange.GetText(-1) if method == "GetTextPattern" else pattern.Value
                    if value is not None:
                        value = str(value).replace("\r\n", "\n").replace("\r", "\n")
                        if value:
                            return value
                        observed_empty = True
            except Exception:
                pass
        return "" if observed_empty else None

    def _current_moment_editor(self, editor, root, composer, *, require_focus=False):
        current = self._find_moment_editor()
        if (self._main_window() is None or current is None
                or _prop(current, "ProcessId", 0) != self._main_pid
                or not self._same_control(current, editor)
                or not self._same_control(self._root_for(current), root)
                or not self._same_control(self._composer_for(current), composer)):
            raise RuntimeError("朋友圈编辑器或窗口已改变，已取消发表")
        if not self._foreground(root):
            raise RuntimeError("朋友圈编辑窗口前台焦点已改变，已取消发表")
        if require_focus and not self._focused(current):
            raise RuntimeError("朋友圈编辑器焦点已改变，已取消发表")
        return current

    @staticmethod
    def _moment_value_setter(editor):
        try:
            pattern = editor.GetValuePattern()
            setter = getattr(pattern, "SetValue", None) if pattern is not None else None
            return setter if callable(setter) and not getattr(pattern, "IsReadOnly", False) else None
        except (AttributeError, NotImplementedError):
            return None
        except Exception as error:
            raise RuntimeError("朋友圈文本值模式无法确认，已取消发表") from error

    def _wait_moment_draft(self, editor, root, composer, *, require_focus=True):
        if not _runtime_id(editor):
            raise RuntimeError("朋友圈编辑器身份无法确认，已取消发表")
        def readable():
            current = self._current_moment_editor(editor, root, composer, require_focus=require_focus)
            value = self._moment_text(current)
            # Empty text is a successful observation; None is still unreadable.
            return (value,) if value is not None else None
        result = wait_until(readable, self._timeout(), self._cancelled)
        if result is None:
            raise RuntimeError("朋友圈编辑器内容无法读取，已取消发表")
        return result[0]

    def _moment_result(self, ok, error="", *, sent=0, attempted=0, submit_attempted=False):
        # A click can take effect even when the UI provider raises or disappears.
        # Publish these flags with the same lock and thread-local result as the
        # existing metadata so callers can block a duplicate after uncertainty.
        return self._result(ok, "moment", "朋友圈", error, sent=sent, attempted=attempted,
                            result_fields={"submit_attempted": bool(submit_attempted),
                                           "outcome_uncertain": bool(submit_attempted and not ok)})

    def send_moment(self, content, image_path="", *, cancel_event=None, generation=None):
        if not content and not image_path:
            return self._moment_result(False, "朋友圈内容为空")
        if image_path and not os.path.isfile(image_path):
            return self._moment_result(False, "朋友圈图片不存在")
        cancelled = self._cancel_test(cancel_event, generation)
        submit_attempted = False
        try:
            with desktop_operation(cancelled), self._uia_context(), ClipboardLease() as lease:
                self._cancelled, self._lease = cancelled, lease
                self._thread_results.operation_error = ""
                self._thread_results.cleanup_warnings = []
                try:
                    if not self._ensure_accessibility():
                        raise RuntimeError("微信窗口或无障碍控件不可用，无法安全发表朋友圈")
                    self._main_pid = _prop(self._window, "ProcessId", 0)
                    self._main_handle = _prop(self._window, "NativeWindowHandle", 0)
                    editor, embedded = self._open_composer(image_path)
                    if editor is None:
                        raise RuntimeError("无法确认朋友圈文本编辑器")
                    composer = self._composer_for(editor)
                    root = self._root_for(editor)
                    if composer is None or root is None or not self._activate(root):
                        raise RuntimeError("朋友圈编辑窗口未获得前台焦点")
                    self._window = root
                    direct_value = self._moment_value_setter(editor) is not None
                    keyboard_input = not direct_value or bool(image_path and not embedded)
                    if keyboard_input and not self._focus(editor):
                        raise RuntimeError("朋友圈编辑器输入焦点无法确认，已取消发表")
                    if self._wait_moment_draft(editor, root, composer, require_focus=keyboard_input) != "":
                        raise RuntimeError("朋友圈编辑器已有草稿，已保留并取消发表")
                    image_tokens = frozenset()
                    if image_path and not embedded:
                        lease.set_files([image_path])
                        if not self._focused(editor):
                            raise RuntimeError("朋友圈编辑器焦点已改变")
                        self._keys("{Ctrl}v")
                        if not wait_until(lambda: self._moment_attachment(editor, image_path), self._timeout(), cancelled):
                            raise RuntimeError("朋友圈图片预览未能确认")
                    elif image_path and not wait_until(lambda: self._moment_attachment(editor, image_path), self._timeout(), cancelled):
                        raise RuntimeError("朋友圈图片预览未能确认，已停止发表")
                    if image_path:
                        image_tokens = self._moment_attachment_ids(editor, image_path)
                    if content:
                        if direct_value:
                            current = self._current_moment_editor(editor, root, composer)
                            if self._moment_text(current) != "":
                                raise RuntimeError("朋友圈编辑器已有草稿或无法读取，已保留并取消发表")
                            setter = self._moment_value_setter(current)
                            if setter is None:
                                raise RuntimeError("朋友圈文本值模式已改变，已取消发表")
                            check_cancel(cancelled)
                            if self._moment_text(self._current_moment_editor(editor, root, composer)) != "":
                                raise RuntimeError("朋友圈写入前已有草稿或无法读取，已保留并取消发表")
                            check_cancel(cancelled)
                            if setter(content) is False:
                                raise RuntimeError("朋友圈正文定向写入失败，已停止发表")
                            check_cancel(cancelled)
                        else:
                            if not self._focus(editor):
                                raise RuntimeError("朋友圈文字编辑器焦点已改变")
                            if self._wait_moment_draft(editor, root, composer) != "":
                                raise RuntimeError("朋友圈编辑器已有草稿，已保留并取消发表")
                            lease.set_text(content)
                            self._keys("{Ctrl}v")
                        if not wait_until(lambda: self._moment_text(self._current_moment_editor(
                                editor, root, composer, require_focus=not direct_value)) == content,
                                self._timeout(), cancelled):
                            raise RuntimeError("朋友圈正文未能在编辑器中确认")
                    # Find the final button only inside this verified composer.
                    def enabled_final():
                        current = self._current_moment_editor(editor, root, composer, require_focus=not direct_value)
                        if self._moment_text(current) != content or not self._same_control(self._composer_for(current), composer):
                            raise RuntimeError("朋友圈发表前正文或编辑窗口已改变")
                        return self._unique(composer, lambda c: _name(c) in {"发表", "发布"}
                                            and _prop(c, "ControlTypeName", "") == "ButtonControl")
                    final = wait_until(enabled_final, self._timeout(), cancelled)
                    if final is None or not _prop(final, "IsEnabled", False):
                        raise RuntimeError("朋友圈发表按钮不唯一或无法确认")
                    check_cancel(cancelled)
                    current_editor = self._find_moment_editor()
                    if (self._main_window() is None or current_editor is None
                            or not self._same_control(current_editor, editor)
                            or not self._foreground(root) or self._moment_text(current_editor) != content):
                        raise RuntimeError("发表前朋友圈焦点或正文已改变")
                    if image_path and (not image_tokens or self._moment_attachment_ids(current_editor, image_path) != image_tokens):
                        raise RuntimeError("发表前朋友圈图片预览已消失")
                    current_composer = self._composer_for(current_editor)
                    current_final = self._unique(current_composer, lambda c: _name(c) in {"发表", "发布"}
                                                 and _prop(c, "ControlTypeName", "") == "ButtonControl")
                    if (current_composer is None or not self._same_control(current_composer, composer)
                            or current_final is None or not self._same_control(current_final, final)):
                        raise RuntimeError("朋友圈发表按钮已改变，已停止发表")
                    submit_attempted = True
                    if not self._click_control(current_final, activate=False):
                        raise RuntimeError("朋友圈发表按钮点击失败")
                    # Clicking alone is insufficient: require the composer to close.
                    if not wait_until(lambda: self._composer_closed(composer, embedded), self._timeout(), cancelled):
                        raise RuntimeError("朋友圈编辑器未关闭，发表结果不确定；请手动核对")
                finally:
                    self._update_operation_snapshot("朋友圈")
                    self._cancelled, self._lease = None, None
            return self._moment_result(True, sent=1, attempted=1, submit_attempted=submit_attempted)
        except Exception as exc:
            return self._moment_result(False, exc, attempted=1, submit_attempted=submit_attempted)
