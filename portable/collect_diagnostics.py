"""Read-only package/process metadata; never collects chat, passwords or raw logs."""
from __future__ import annotations
import ctypes
from collections.abc import Mapping
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "wechat-weflow-bridge-ob11"
RUNTIME = ROOT / "portable" / "runtime"


def load_object(path):
    try:
        if path.stat().st_size > 1024 * 1024:
            return {}
        result = json.loads(path.read_text(encoding="utf-8-sig"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def file_metadata(path):
    try:
        info = path.stat()
        return {"exists": path.is_file(), "bytes": info.st_size}
    except OSError:
        return {"exists": False}


def safe_build(identity):
    """Whitelist native build fields, excluding paths, account and process argv."""
    identity = identity if isinstance(identity, Mapping) else {}
    result = {"file_version": "", "sha256": "", "module_size": None, "architecture": "unknown"}
    value = identity.get("file_version")
    if (isinstance(value, str) and re.fullmatch(r"\d{1,5}(?:\.\d{1,5}){3}", value, re.ASCII)
            and all(int(part) <= 65535 for part in value.split("."))):
        result["file_version"] = value
    value = identity.get("sha256")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        result["sha256"] = value.lower()
    value = identity.get("module_size")
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 0xffffffff:
        result["module_size"] = value
    if isinstance(identity.get("architecture"), str) and identity["architecture"] in {"AMD64", "I386", "ARM64"}:
        result["architecture"] = identity["architecture"]
    return result


def recovery_match(identity):
    """Select from built-in reviewed profiles; no candidate discovery or activation."""
    profiles = bridge_module("uia_build_profiles")
    def public(item):
        result = safe_build(dict(item, module_size=item.get("size_of_image")))
        basis = item.get("review_basis")
        result["review_basis"] = basis if basis in ("reviewed_build", "static_equivalence") else "unknown"
        return result
    supported = [public(item) for item in profiles.supported_builds()]
    try:
        selected = public(profiles.select_profile(identity).metadata())
        code = "matched"
    except profiles.ProfileSelectionError as error:
        selected = None
        allowed = {"version_unavailable", "version_mismatch", "sha256_unavailable", "sha256_mismatch",
                   "architecture_unavailable", "architecture_mismatch", "image_size_unavailable", "image_size_mismatch", "ambiguous_profile"}
        code = error.code if error.code in allowed else "unverified_build"
    return {"supported_recovery_builds": supported, "selected_recovery_build": selected,
            "matches_reviewed_recovery_build": selected is not None, "build_check": code}


def bridge_module(name):
    if str(BRIDGE) not in sys.path:
        sys.path.insert(0, str(BRIDGE))
    spec = importlib.util.spec_from_file_location("wechatbot_diagnostic_" + name, BRIDGE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def wechat_build():
    if os.name != "nt":
        return {"status": "windows_required"}
    try:
        import psutil
        from ctypes import wintypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.IsWindowVisible.restype = wintypes.BOOL
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        matches = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, unused):
            if not user.IsWindowVisible(hwnd):
                return True
            name = ctypes.create_unicode_buffer(256)
            user.GetClassNameW(hwnd, name, len(name))
            if name.value not in {"WeChatMainWndForPC", "mmui::MainWindow"} and not re.fullmatch(r"Qt\d+QWindowIcon", name.value):
                return True
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                if psutil.Process(pid.value).name().lower() in {"weixin.exe", "wechat.exe"}:
                    matches.append((int(hwnd), pid.value))
            except psutil.Error:
                pass
            return True
        callback = callback_type(visit)
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.EnumWindows.restype = wintypes.BOOL
        if not user.EnumWindows(callback, 0):
            return {"status": "window_query_failed"}
        if len(matches) != 1:
            return {"status": "window_missing" if not matches else "window_ambiguous", "window_count": len(matches)}
        probe = bridge_module("uia_recovery")
        hwnd, pid = matches[0]
        if probe.pid_from_hwnd(hwnd) != pid:
            return {"status": "window_process_changed"}
        # Unknown builds are profiled without reading any build-specific RVA.
        with probe.NativeProcessProbe(pid, None, permit_write=False) as adapter:
            identity = adapter.identity()
        if probe.pid_from_hwnd(hwnd) != pid:
            return {"status": "window_process_changed"}
        actual = safe_build(identity)
        return {"status": "read_only_profiled", "actual": actual, **recovery_match(identity)}
    except Exception as error:
        return {"status": "profile_failed", "error_type": type(error).__name__}


def service_port(name, port):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return {"status": "invalid_port"}
    result = {"port": port, "status": "not_checked"}
    if os.name != "nt":
        return result
    try:
        control = bridge_module("startup_control")
        pid = control._listener_pid(port)
        result.update(status="listening", listener_pid=pid)
    except ValueError:
        result.update(status="no_unique_listener", reason="此端口未发现唯一可核验的本机监听进程。")
    except Exception as error:
        result.update(status="not_confirmed", error_type=type(error).__name__)
    if name == "bridge" and result["status"] == "listening":
        try:
            owned = control.owned_running()
            result["owned_by_this_package"] = bool(owned and owned.get("pid") == pid)
        except Exception as error:
            result.update(owned_by_this_package=None, ownership_error_type=type(error).__name__)
    return result


def weflow_extension(settings):
    local = "http://127.0.0.1:5031"
    base = settings.get("weflow_base_url", local)
    if not isinstance(base, str) or base.rstrip("/") not in (local, "http://localhost:5031"):
        return {"status": "not_checked", "message": "自定义地址，未执行本机默认端口探测。"}
    token = settings.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return {"status": "not_checked", "message": "本包未配置 WeFlow 连接令牌。"}
    from weflow_setup import api_probe
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    if api_probe(local, token, open_request=opener.open):
        return {"status": "verified", "message": "已验证身份快照、朋友圈媒体密钥能力及原生互动详情路由。"}
    return {"status": "not_verified", "message": "增强接口未能核验，请检查 WeFlow 运行状态、令牌及扩展。"}


def collect():
    bridge = load_object(BRIDGE / "config.json")
    instance = load_object(RUNTIME / "astrbot-instance" / "data" / "cmd_config.json")
    dashboard = instance.get("dashboard", {})
    port = dashboard.get("port", 6185) if isinstance(dashboard, dict) else None
    files = {"portable/runtime/startup-error.txt": RUNTIME / "startup-error.txt",
             "wechat-weflow-bridge-ob11/startup_astrbot.log": BRIDGE / "startup_astrbot.log",
             "wechat-weflow-bridge-ob11/startup_bridge.log": BRIDGE / "startup_bridge.log"}
    return {"release": "微信bot v6.33", "purpose": "read_only_startup_and_wechat_metadata",
            "python_version": sys.version.split()[0], "wechat": wechat_build(), "weflow_extension": weflow_extension(bridge),
            "services": {"bridge": service_port("bridge", bridge.get("web_port", 8766)),
                         "astrbot": service_port("astrbot", port)},
            "diagnostic_files": {name: file_metadata(path) for name, path in files.items()},
            "privacy": "不包含聊天、原始日志、配置、密钥、密码或用户路径；未修改微信或服务。"}


def main():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    output = RUNTIME / "只读诊断报告.json"
    temporary = output.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(collect(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print("只读诊断报告已生成：" + str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
