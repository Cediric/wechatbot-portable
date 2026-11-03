"""Project-owned process control. A listening port never authorizes termination."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
import re
import socket
import struct
import stat
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor, HTTPRedirectHandler
from contextlib import contextmanager

BASE = os.path.dirname(os.path.abspath(__file__))
_THREAD_LOCK = threading.RLock()
_OWNED_RECORDS = {}


def _canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _kernel():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _process_handle(pid, terminate=False):
    kernel = _kernel()
    kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel, kernel.OpenProcess(0x1000 | (0x0001 | 0x00100000 if terminate else 0), False, pid)


def _handle_identity(kernel, handle):
    kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)]
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
    value, size = ctypes.create_unicode_buffer(32768), ctypes.c_uint(32768)
    if not kernel.QueryFullProcessImageNameW(handle, 0, value, ctypes.byref(size)):
        raise OSError("无法读取进程解释器")
    stamps = [ctypes.c_uint64() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(s) for s in stamps)):
        raise OSError("无法读取进程创建身份")
    return {"executable": value.value, "creation": str(stamps[0].value)}


def _native_process(pid):
    """Distinguish a proven missing PID from permissions/provider failures."""
    if os.name != "nt":
        return {"status": "unknown"}
    kernel, handle = _process_handle(pid)
    if not handle:
        return {"status": "missing" if ctypes.get_last_error() == 87 else "unknown"}
    try:
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        code = ctypes.c_uint()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return {"status": "unknown"}
        if code.value != 259:
            return {"status": "missing"}
        return {"status": "alive", **_handle_identity(kernel, handle)}
    except OSError:
        return {"status": "unknown"}
    finally:
        kernel.CloseHandle(handle)


def _process_image(pid):
    info = _native_process(pid)
    return None if info["status"] == "missing" else info.get("executable", "unknown")


def _command_args(pid):
    # The package environment includes psutil's native Windows process reader.
    # Avoid starting a PowerShell/CIM process for every readiness recheck.
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            args = psutil.Process(pid).cmdline()
        except psutil.NoSuchProcess:
            return None
        except psutil.Error:
            raise RuntimeError("无法读取进程的原生命令行，未通过归属核验。") from None
        if not isinstance(args, list) or not args or any(not isinstance(arg, str) for arg in args):
            return None
        return args
    return _cim_command_args(pid)


def _cim_command_args(pid):
    """Fallback for external interpreters without the bundled native reader."""
    script = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
              f"Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}' | "
              "Select-Object CommandLine | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                                capture_output=True, text=True, encoding="utf-8", timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise RuntimeError("进程命令行 CIM 查询超过 5 秒，未通过归属核验。") from None
    except OSError:
        raise RuntimeError("进程命令行 CIM 查询无法启动，未通过归属核验。") from None
    if result.returncode:
        raise RuntimeError("进程命令行 CIM 查询失败，退出码 %s；未通过归属核验。" % result.returncode)
    try:
        command = json.loads(result.stdout or "null").get("CommandLine")
    except (ValueError, AttributeError):
        return None
    if not isinstance(command, str) or not command:
        return None
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        return None
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        kernel = _kernel()
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(argv)


@contextmanager
def _pid_guard(path):
    """Named mutex prevents two launchers from deleting/replacing each other's PID."""
    with _THREAD_LOCK:
        if os.name != "nt":
            yield
            return
        kernel = _kernel()
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        name = "Local\\AkashaBridgePid_" + hashlib.sha256(_canonical(path).encode()).hexdigest()[:32]
        handle = kernel.CreateMutexW(None, False, name)
        if not handle:
            raise OSError("无法获得本项目启动锁")
        acquired = False
        try:
            acquired = kernel.WaitForSingleObject(handle, 3000) in (0, 0x80)
            if not acquired:
                raise OSError("另一个本项目启动操作尚未结束")
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)


def _read_pid(path):
    with open(path, "rb") as stream:
        raw = stream.read(16384)
    info = json.loads(raw.decode("utf-8"))
    if not isinstance(info, dict) or isinstance(info.get("pid"), bool) or not isinstance(info.get("pid"), int) or info["pid"] <= 0:
        raise ValueError("PID 元数据格式无法核验")
    if (_canonical(info.get("project", "")) != _canonical(BASE)
            or _canonical(info.get("command", "")) != _canonical(os.path.join(BASE, "main.py"))
            or not os.path.isabs(info.get("executable", "")) or not str(info.get("creation", "")).isdigit()):
        raise ValueError("PID 元数据不能证明属于本项目")
    if info.get("panel_url"):
        match = re.fullmatch(r"http://127\.0\.0\.1:(\d+)", str(info["panel_url"]))
        if not match or not 1024 <= int(match.group(1)) <= 65535:
            raise ValueError("PID 元数据的面板地址无法核验")
    return info, raw


def _unlink_unchanged(path, raw):
    with open(path, "rb") as stream:
        if stream.read(16384) != raw:
            raise RuntimeError("PID 元数据已被另一个启动操作替换")
    if os.name == "nt" and os.stat(path).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
        os.chmod(path, stat.S_IWRITE)
    os.unlink(path)


def _stale(info, live):
    if live["status"] == "missing":
        return True
    return bool(live["status"] == "alive" and live.get("creation") and live["creation"] != info["creation"])


def acquire_pid(path):
    try:
        with _pid_guard(path):
            if os.path.exists(path):
                info, raw = _read_pid(path)
                live = _native_process(info["pid"])
                if not _stale(info, live):
                    print("本项目已有进程，或无法证明 PID 已失效；保留 PID 文件。")
                    return False
                _unlink_unchanged(path, raw)
            mine = _native_process(os.getpid())
            if mine["status"] != "alive":
                print("无法核验当前进程创建身份，未创建 PID 文件。")
                return False
            # Windows venv python.exe is a redirector; the live process image is
            # its managed interpreter, not necessarily sys.executable.
            record = {"version": 2, "pid": os.getpid(), "executable": mine["executable"],
                      "project": BASE, "command": os.path.join(BASE, "main.py"), "creation": mine["creation"],
                      "panel_url": panel_url()}
            raw = json.dumps(record, ensure_ascii=False).encode("utf-8")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            _OWNED_RECORDS[_canonical(path)] = raw
            return True
    except (OSError, ValueError, TypeError) as exc:
        print("无法安全取得启动权限: " + str(exc))
        return False


def release_pid(path):
    try:
        with _pid_guard(path):
            raw = _OWNED_RECORDS.pop(_canonical(path), None)
            if raw is not None:
                _unlink_unchanged(path, raw)
    except (OSError, RuntimeError):
        pass


def panel_url():
    """config.json is the single source; never read panel_port.txt."""
    path = os.path.join(BASE, "config.json")
    if not os.path.exists(path):
        path = os.path.join(BASE, "config.example.json")
    with open(path, encoding="utf-8") as stream:
        cfg = json.load(stream)
    if not isinstance(cfg, dict):
        raise ValueError("config.json 必须是配置对象")
    port = cfg.get("web_port", 8766)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("config.json 的 web_port 必须是 1024 到 65535 的整数")
    return f"http://127.0.0.1:{port}"


def _terminate_verified(info):
    """Hold a verified native handle across termination to prevent PID-reuse races."""
    kernel, handle = _process_handle(info["pid"], terminate=True)
    if not handle:
        raise OSError("没有终止本项目进程的权限")
    try:
        identity = _handle_identity(kernel, handle)
        if identity["creation"] != info["creation"] or _canonical(identity["executable"]) != _canonical(info["executable"]):
            raise ValueError("进程创建身份或解释器发生变化")
        kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        if not kernel.TerminateProcess(handle, 0):
            raise OSError("本项目进程未能退出")
        if kernel.WaitForSingleObject(handle, 5000) != 0:
            raise OSError("本项目进程尚未确认退出")
    finally:
        kernel.CloseHandle(handle)


def _verify_command(info):
    args = _command_args(info["pid"])
    script = _canonical(os.path.join(BASE, "main.py"))
    if not args or len(args) < 2 or _canonical(args[0]) != _canonical(info["executable"]):
        raise ValueError("进程命令行解释器无法核验")
    script_args = args[1:]
    while script_args:
        if script_args[0] in {"-u", "-B", "-E", "-s", "-S", "-I"}:
            script_args = script_args[1:]
        elif len(script_args) > 1 and script_args[:2] == ["-X", "utf8"]:
            script_args = script_args[2:]
        else:
            break
    if not script_args or not os.path.isabs(script_args[0]) or _canonical(script_args[0]) != script:
        raise ValueError("进程命令行未执行本项目绝对路径 main.py")
    current = _native_process(info["pid"])
    if (current.get("status") != "alive" or current.get("creation") != info["creation"]
            or _canonical(current.get("executable", "")) != _canonical(info["executable"])):
        raise ValueError("读取命令行期间进程创建身份发生变化，未通过归属核验。")


def _listener_pid(port):
    """Read the IPv4 listener owner, never infer ownership from an open port."""
    if os.name != "nt":
        raise OSError("仅 Windows 支持本项目监听进程核验")
    api = ctypes.WinDLL("iphlpapi", use_last_error=True)
    api.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.c_bool,
                                       ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
    api.GetExtendedTcpTable.restype = ctypes.c_uint
    size = ctypes.c_uint()
    code = api.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 3, 0)
    for _ in range(3):
        if code not in (0, 122) or not 4 <= size.value <= 8 * 1024 * 1024:
            raise OSError("无法核验面板监听进程")
        table = ctypes.create_string_buffer(size.value)
        code = api.GetExtendedTcpTable(table, ctypes.byref(size), False, 2, 3, 0)
        if code == 122:
            continue
        if code:
            raise OSError("无法读取面板监听进程")
        data = table.raw[:size.value]
        count = struct.unpack_from("<I", data)[0]
        if count > (len(data) - 4) // 24:
            raise OSError("监听进程表不完整")
        owners = set()
        for offset in range(4, 4 + count * 24, 24):
            state, address, raw_port, _, _, pid = struct.unpack_from("<6I", data, offset)
            if state == 2 and socket.ntohs(raw_port & 0xffff) == port and socket.inet_ntoa(struct.pack("<I", address)) in {"127.0.0.1", "0.0.0.0"}:
                owners.add(pid)
        if len(owners) != 1:
            raise ValueError("面板监听进程不唯一或尚未可用")
        return owners.pop()
    raise OSError("监听进程表持续变化，未执行停止")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _CsrfParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            fields = dict(attrs)
            if fields.get("name") == "panel-csrf":
                self.tokens.append(fields.get("content", ""))


def _panel_session():
    return build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()), _NoRedirect())


def _panel_response(session, request):
    with session.open(request, timeout=10) as response:
        if response.status != 200:
            raise OSError("本项目面板未确认停止请求")
        data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("本项目面板响应过大")
        return data


def _graceful_stop_verified(info):
    """Restore desktop leases before terminating a positively identified bridge."""
    url = info.get("panel_url")
    if not url:
        raise ValueError("PID 元数据缺少启动时面板地址；未强制终止")
    port = urlsplit(url).port
    def verify_listener():
        current = _native_process(info["pid"])
        if (current.get("status") != "alive" or current.get("creation") != info["creation"]
                or _canonical(current.get("executable", "")) != _canonical(info["executable"])
                or _listener_pid(port) != info["pid"]):
            raise ValueError("面板监听服务与已核验进程不符；未发送停止请求")
    verify_listener()
    session = _panel_session()
    parser = _CsrfParser()
    parser.feed(_panel_response(session, Request(url + "/")).decode("utf-8"))
    if len(parser.tokens) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", parser.tokens[0]):
        raise ValueError("本项目面板操作凭证无法核验；未强制终止")
    verify_listener()
    body = _panel_response(session, Request(url + "/stop", data=b"{}", headers={
        "Content-Type": "application/json", "Origin": url, "X-Panel-Token": parser.tokens[0]}, method="POST"))
    result = json.loads(body.decode("utf-8"))
    if not isinstance(result, dict) or result.get("running") is not False or result.get("ok") is False:
        raise ValueError("本项目尚未确认停止；未强制终止")
    restored = result.get("uia_restore_result")
    if not isinstance(restored, dict) or restored.get("ok") is not True:
        raise ValueError("UIA 临时状态未确认还原；保留进程与 PID 文件")


def owned_running():
    """Return verified live metadata; unknown evidence raises, never cleans a file."""
    path = os.path.join(BASE, "bridge.pid")
    with _pid_guard(path):
        if not os.path.exists(path):
            return None
        info, _ = _read_pid(path)
        live = _native_process(info["pid"])
        if _stale(info, live):
            return None
        if live["status"] != "alive" or _canonical(live["executable"]) != _canonical(info["executable"]):
            raise ValueError("PID 所属进程无法核验，已保留元数据")
        _verify_command(info)
        return info


def owned_listener(port):
    """Prove a live main.py listener directly; a saved PID file is optional."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("桥接监听端口无效")
    pid = _listener_pid(port)
    live = _native_process(pid)
    if live.get("status") != "alive":
        raise ValueError("桥接监听进程的创建身份无法核验")
    info = {"version": 2, "pid": pid, "executable": live["executable"],
            "creation": live["creation"], "project": BASE,
            "command": os.path.join(BASE, "main.py"),
            "panel_url": f"http://127.0.0.1:{port}"}
    _verify_command(info)
    if _listener_pid(port) != pid:
        raise ValueError("桥接监听进程在命令行核验期间改变")
    return info


def stop_listener(info):
    """Stop a proven listener without repairing or trusting obsolete records."""
    path = os.path.join(BASE, "bridge.pid")
    try:
        url = info.get("panel_url", "") if isinstance(info, dict) else ""
        match = re.fullmatch(r"http://127\.0\.0\.1:(\d+)", str(url))
        if not match:
            raise ValueError("桥接监听身份没有有效的本机面板地址")
        port = int(match.group(1))
        with _pid_guard(path):
            record, raw = None, None
            try:
                record, raw = _read_pid(path)
            except (OSError, ValueError, TypeError):
                pass  # Missing/legacy/corrupt metadata does not identify this listener.
            if owned_listener(port) != info:
                raise ValueError("桥接监听 PID、创建身份或入口已改变；未发送退出请求")
            _graceful_stop_verified(info)
            if owned_listener(port) != info:
                raise ValueError("桥接监听身份在正常停止期间改变；未终止其他进程")
            _terminate_verified(info)
            if (record and record["pid"] == info["pid"] and record["creation"] == info["creation"]
                    and _canonical(record["executable"]) == _canonical(info["executable"])):
                try:
                    _unlink_unchanged(path, raw)
                except (OSError, RuntimeError):
                    # The old process is already gone. Another launcher's new
                    # record must survive and cannot turn that exit into failure.
                    pass
            return True
    except Exception as exc:
        print("无法停止已核验桥接监听: " + str(exc))
        return False


def stop_owned_process():
    path = os.path.join(BASE, "bridge.pid")
    try:
        with _pid_guard(path):
            if not os.path.exists(path):
                return True
            info, raw = _read_pid(path)
            live = _native_process(info["pid"])
            if _stale(info, live):
                _unlink_unchanged(path, raw)
                return True
            if live["status"] != "alive" or _canonical(live["executable"]) != _canonical(info["executable"]):
                raise ValueError("PID 所属进程无法核验，已保留元数据")
            _verify_command(info)
            _graceful_stop_verified(info)
            _terminate_verified(info)
            _unlink_unchanged(path, raw)
            return True
    except Exception as exc:
        print("无法安全停止: " + str(exc))
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["open", "url", "check", "stop"])
    action = parser.parse_args().action
    if action == "open":
        webbrowser.open(panel_url())
    elif action == "url":
        print(panel_url())
    elif action == "stop":
        raise SystemExit(0 if stop_owned_process() else 1)
    else:
        print("面板地址：" + panel_url() + "；只允许停止 PID 元数据核验通过的本项目进程。")
