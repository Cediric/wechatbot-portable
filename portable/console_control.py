"""Verify a dedicated Windows console before sending its graceful Ctrl+C."""
import ctypes
from ctypes import wintypes
import json
import math
import os
import sys
import psutil


def _canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _records_valid(records):
    if not isinstance(records, list) or not 1 <= len(records) <= 33:
        return False
    seen = set()
    for item in records:
        if not isinstance(item, dict):
            return False
        pid, created = item.get("pid"), item.get("created")
        if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or pid in seen
                or isinstance(created, bool) or not isinstance(created, (int, float))
                or not math.isfinite(created) or created < 0):
            return False
        if any(not isinstance(item.get(k), str) or not os.path.isabs(item[k]) for k in ("executable", "instance")):
            return False
        command = item.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
            return False
        seen.add(pid)
    return True


def _identities_match(records):
    for record in records:
        process = psutil.Process(record["pid"])
        if (process.create_time() != record["created"] or process.exe() != record["executable"]
                or process.cmdline() != record["command"]
                or process.environ().get("ASTRBOT_ROOT", "") != record["instance"]):
            return False
    return True


def _tree_snapshot(records):
    root = records[0]
    expected = {record["pid"] for record in records}
    children = psutil.Process(root["pid"]).children(recursive=True)
    if len(children) > 64:
        raise ValueError("process tree too large")
    host = _canonical(os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "System32", "conhost.exe"))
    parents, applications, snapshot = {}, {root["pid"]}, []
    for child in children:
        if child.pid == root["pid"] or child.pid in parents:
            raise ValueError("invalid child tree")
        created, executable, parent = child.create_time(), child.exe(), child.ppid()
        parents[child.pid] = parent
        snapshot.append((child.pid, created, executable, parent))
        if _canonical(executable) != host:
            if child.pid not in expected or created < root["created"]:
                raise ValueError("unverified child")
            applications.add(child.pid)
    if applications != expected:
        raise ValueError("application tree changed")
    for pid in applications - {root["pid"]}:
        current, visited = pid, set()
        while current != root["pid"]:
            if current in visited or current not in parents:
                raise ValueError("child parent chain changed")
            visited.add(current)
            current = parents[current]
    return tuple(sorted(snapshot))


def _native_identity(native, handle):
    if native.WaitForSingleObject(handle, 0) != 258:
        raise ValueError("original process exited")
    image, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
    if not native.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
        raise OSError("process image unavailable")
    times = [ctypes.c_uint64() for _ in range(4)]
    if not native.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        raise OSError("process birth identity unavailable")
    return native.GetProcessId(handle), times[0].value, _canonical(image.value)


def _members(native, expected):
    members = (wintypes.DWORD * 128)()
    count = native.GetConsoleProcessList(members, len(members))
    if not 0 < count <= len(members):
        raise ValueError("invalid console member count")
    actual = frozenset(members[:count])
    if len(actual) != count or actual != expected | {os.getpid()}:
        raise ValueError("console members changed")
    return actual


def main():
    if os.name != "nt": return 1
    records = json.loads(sys.argv[1])
    if not _records_valid(records): return 1
    native = ctypes.WinDLL("kernel32", use_last_error=True)
    native.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    native.OpenProcess.restype = wintypes.HANDLE
    native.CloseHandle.argtypes = [wintypes.HANDLE]
    native.GetProcessId.argtypes = [wintypes.HANDLE]
    native.GetProcessId.restype = wintypes.DWORD
    native.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    native.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    native.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.c_void_p] * 4
    native.AttachConsole.argtypes = [wintypes.DWORD]
    native.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    native.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    native.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
    held, attached = [], False
    try:
        # Keep the original Windows process objects open until the signal has
        # been sent. A PID lookup alone cannot pin a process across AttachConsole.
        for record in records:
            handle = native.OpenProcess(0x1000 | 0x00100000, False, record["pid"])
            if not handle: return 1
            held.append((handle, None))
            identity = _native_identity(native, handle)
            if identity[0] != record["pid"] or identity[2] != _canonical(record["executable"]): return 1
            held[-1] = (handle, identity)
        if not _identities_match(records): return 1
        tree = _tree_snapshot(records)
        native.FreeConsole()
        if not native.AttachConsole(records[0]["pid"]): return 1
        attached = True
        expected = {record["pid"] for record in records}
        members = _members(native, expected)
        # Attach may race an exit, interpreter change or tree replacement.
        # Repeat all identities and the complete tree while handles stay held.
        if not _identities_match(records) or _tree_snapshot(records) != tree: return 1
        if not native.SetConsoleCtrlHandler(None, True): return 1
        if not _identities_match(records) or _tree_snapshot(records) != tree: return 1
        if any(_native_identity(native, handle) != identity for handle, identity in held): return 1
        if _members(native, expected) != members: return 1
        return 0 if native.GenerateConsoleCtrlEvent(0, 0) else 1
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        return 1
    finally:
        if attached:
            native.FreeConsole()
        for handle, _ in held:
            native.CloseHandle(handle)

if __name__ == "__main__":
    try: raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, IndexError, psutil.Error): raise SystemExit(1)
