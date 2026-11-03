"""Stop only this package's recorded AstrBot; prefer its graceful console exit."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import subprocess
import stat
import sys
import time
import psutil

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "portable" / "runtime"
INSTANCE = RUNTIME / "astrbot-instance"
RECORD = RUNTIME / "astrbot-process.json"

def normalized(value):
    return os.path.normcase(str(Path(value).resolve()))

def fingerprint(process):
    return {"pid": process.pid, "created": process.create_time(), "executable": process.exe(),
        "command": process.cmdline(), "cwd": process.cwd(), "instance": process.environ().get("ASTRBOT_ROOT", "")}

def owned(value):
    expected = RUNTIME / "astrbot-venv" / "Scripts"
    executables = {normalized(expected / "astrbot.exe"), normalized(expected / "python.exe")}
    executables.update(normalized(p) for p in (RUNTIME / "python").glob("*/python.exe"))
    return normalized(value.get("instance", "")) == normalized(INSTANCE) and normalized(value["executable"]) in executables and any(
        normalized(c) == normalized(expected / "astrbot.exe") for c in value["command"] if c.lower().endswith("astrbot.exe"))

def record_process(pid):
    value = fingerprint(psutil.Process(pid))
    if not _ready_owned(value):
        raise RuntimeError("AstrBot 进程不属于本包，拒绝登记。")
    temporary = RECORD.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, RECORD)

def _ready_owned(value, *, child=False):
    """Strict official entrypoints shared by registration, readiness and stop."""
    scripts = RUNTIME / "astrbot-venv" / "Scripts"
    cli = normalized(scripts / "astrbot.exe")
    python = {normalized(scripts / "python.exe")}
    python.update(normalized(p) for p in (RUNTIME / "python").glob("*/python.exe"))
    executable = normalized(value.get("executable", ""))
    command = value.get("command")
    if (not value.get("instance") or normalized(value["instance"]) != normalized(INSTANCE)
            or executable not in python | {cli} or not isinstance(command, list)
            or not command or any(not isinstance(arg, str) for arg in command)):
        return False
    # distlib's Windows CLI can hand its command to the managed interpreter.
    if normalized(command[0]) == cli:
        return len(command) > 1 and command[1] == "run"
    if executable not in python or normalized(command[0]) not in python:
        return False
    args = command[1:]
    while args:
        if args[0] in {"-u", "-B", "-E", "-s", "-S", "-I"}:
            args = args[1:]
        elif len(args) > 1 and args[0] == "-X" and args[1] == "utf8":
            args = args[2:]
        else:
            break
    if len(args) > 1 and normalized(args[0]) == cli and args[1] == "run":
        return True
    if not child:
        return False
    if len(args) > 2 and args[:2] in (["-m", "astrbot.cli"], ["-m", "astrbot.cli.__main__"]):
        return args[2] == "run"
    entry = RUNTIME / "astrbot-venv" / "Lib" / "site-packages" / "astrbot" / "cli" / "__main__.py"
    return len(args) > 1 and os.path.isabs(args[0]) and normalized(args[0]) == normalized(entry) and args[1] == "run"

def _same_identity(left, right):
    return all(left.get(key) == right.get(key) for key in ("pid", "created", "executable", "command", "instance"))

def verified_record(spawned_pid=None, *, required=False):
    """Read-only snapshot; unknown/stale records never authorize another listener."""
    try:
        status = RECORD.stat()
    except FileNotFoundError:
        if required:
            raise RuntimeError("AstrBot 面板端口占用归属核验失败：端口已有监听，但当前包没有可核验的进程记录；可能来自另一个包，不会接管该服务。") from None
        return None
    except OSError:
        raise RuntimeError("AstrBot 本包进程记录无法读取；已保留记录。") from None
    try:
        if not stat.S_ISREG(status.st_mode):
            raise ValueError("record is not a regular file")
        with RECORD.open("rb") as source:
            raw = source.read(16385)
        if len(raw) > 16384:
            raise ValueError("record too large")
        value = json.loads(raw)
        if (not isinstance(value, dict) or isinstance(value.get("pid"), bool)
                or not isinstance(value.get("pid"), int) or value["pid"] <= 0
                or isinstance(value.get("created"), bool) or not isinstance(value.get("created"), (int, float))
                or not math.isfinite(value["created"]) or value["created"] <= 0 or not _ready_owned(value)):
            raise ValueError("record identity invalid")
        if spawned_pid is not None and value["pid"] != spawned_pid:
            raise RuntimeError("AstrBot 面板端口占用归属核验失败：记录中的根进程与本轮启动进程不匹配；已保留记录，不会接管监听服务。")
        process = psutil.Process(value["pid"])
        current = fingerprint(process)
        if not _same_identity(current, value) or not _ready_owned(current):
            raise ValueError("record identity changed")
        return value
    except psutil.NoSuchProcess:
        if required:
            raise RuntimeError("AstrBot 面板端口占用归属核验失败：本包记录中的根进程已经退出；已保留记录，不会接管当前监听服务。") from None
        return None
    except (OSError, ValueError, TypeError, KeyError, psutil.Error):
        raise RuntimeError("AstrBot 本包 PID、创建时间、解释器、命令或实例归属无法核验；已保留进程记录。") from None

def ready_listener(listener_pid, spawned_pid=None):
    """Verify the root or an official same-instance child; never signal/clean up."""
    value = verified_record(spawned_pid, required=True)
    try:
        root = psutil.Process(value["pid"])
        children = root.children(recursive=True)
        target = root if listener_pid == root.pid else next((p for p in children if p.pid == listener_pid), None)
        if target is None:
            raise ValueError("foreign listener")
        snapshot = fingerprint(target)
        if (snapshot.get("created", -1) < value["created"] or not _ready_owned(snapshot, child=target.pid != root.pid)
                or not _same_identity(fingerprint(root), value)):
            raise ValueError("unknown child")
        if target.pid != root.pid and target.pid not in {p.pid for p in root.children(recursive=True)}:
            raise ValueError("child tree changed")
        if not _same_identity(fingerprint(target), snapshot):
            raise ValueError("listener identity changed")
        # Also require the same package record after the process/tree queries.
        if verified_record(spawned_pid, required=True) != value:
            raise ValueError("record changed")
        return True
    except (OSError, ValueError, TypeError, KeyError, psutil.Error):
        raise RuntimeError("AstrBot 面板端口被其他或未核验进程占用；监听服务不属于本轮本包官方进程树。") from None

def _remove_unchanged_record(raw):
    try:
        if RECORD.read_bytes() != raw:
            return
        if os.name == "nt" and RECORD.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
            os.chmod(RECORD, stat.S_IWRITE)
        if RECORD.read_bytes() == raw:
            RECORD.unlink(missing_ok=True)
    except FileNotFoundError:
        pass


def _save_stopping_members(value, snapshots, raw):
    """Keep verified children recoverable if their launcher exits first."""
    updated = json.dumps(dict(value, shutdown_members=snapshots)).encode("utf-8")
    if RECORD.read_bytes() != raw:
        raise RuntimeError("AstrBot 进程记录已改变，请重试停止。")
    if os.name == "nt" and RECORD.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
        os.chmod(RECORD, stat.S_IWRITE)
    temporary = RECORD.with_suffix(".stop.tmp")
    temporary.write_bytes(updated)
    try:
        if RECORD.read_bytes() != raw:
            raise RuntimeError("AstrBot 进程记录已改变，请重试停止。")
        os.replace(temporary, RECORD)
    finally:
        temporary.unlink(missing_ok=True)
    return updated


def _remaining_members(snapshots):
    remaining = []
    for item in snapshots:
        try:
            process = psutil.Process(item["pid"])
            # A reused PID means our original process is gone, not permission
            # to inspect or terminate the replacement application.
            if process.create_time() == item["created"] and process.is_running():
                remaining.append((process, item))
        except psutil.NoSuchProcess:
            pass
    return remaining


def _wait_members(snapshots, timeout):
    deadline = time.monotonic() + max(0, timeout)
    while True:
        remaining = _remaining_members(snapshots)
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(.15)


def _terminate_remaining(remaining, root_pid):
    # Validate every survivor before terminating any. This is the captured
    # package tree only, never a scan by Python name or listening port.
    verified = []
    for process, item in remaining:
        try:
            current = fingerprint(process)
        except psutil.NoSuchProcess:
            continue
        if current["created"] != item["created"]:
            continue
        if not _same_identity(current, item) or not _ready_owned(current, child=process.pid != root_pid):
            raise RuntimeError("AstrBot 残留进程身份已改变，未结束该进程；请检查本包运行状态。")
        verified.append(process)
    for process in reversed(verified):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass


def finish_pending_stop(timeout=8):
    """Finish a recorded partial stop before launch; leave normal instances alone."""
    try:
        value = json.loads(RECORD.read_bytes())
    except FileNotFoundError:
        return True
    if not isinstance(value, dict):
        raise RuntimeError("AstrBot 本包进程记录无效，未启动新进程。")
    if "shutdown_members" in value:
        return stop_owned(timeout=timeout)
    return True


def stop_owned(timeout=25):
    try:
        raw = RECORD.read_bytes()
    except FileNotFoundError:
        return True
    value = json.loads(raw)
    snapshots = value.get("shutdown_members")
    if "shutdown_members" in value:
        if (not isinstance(snapshots, list) or not 1 <= len(snapshots) <= 33
                or any(not isinstance(item, dict) for item in snapshots)
                or not _same_identity(snapshots[0], value)
                or len({item.get("pid") for item in snapshots if isinstance(item, dict)}) != len(snapshots)
                or any(not isinstance(item, dict) or not _ready_owned(item, child=item.get("pid") != value["pid"])
                       for item in snapshots)):
            raise RuntimeError("AstrBot 停止记录无效，未操作其他进程。")
        remaining = _remaining_members(snapshots)
    else:
        try:
            process = psutil.Process(value["pid"])
            current = fingerprint(process)
        except psutil.NoSuchProcess:
            _remove_unchanged_record(raw)
            return True
        # The official Windows launcher may change cwd after spawning Python.
        if not _same_identity(current, value) or not _ready_owned(current):
            raise RuntimeError("AstrBot PID 已改变或进程身份不匹配；不会停止其他服务。")
        snapshots = []
        for target in [process] + process.children(recursive=True):
            item = fingerprint(target)
            if normalized(item["executable"]) == normalized(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/conhost.exe"):
                continue
            if not _ready_owned(item, child=target.pid != process.pid):
                raise RuntimeError("AstrBot 控制台含有未核验进程，拒绝发送退出信号。")
            snapshots.append(item)
        raw = _save_stopping_members(value, snapshots, raw)
        helper = Path(__file__).with_name("console_control.py")
        try:
            result = subprocess.run([sys.executable, "-X", "utf8", str(helper), json.dumps(snapshots)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            signalled = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            signalled = False
        remaining = _wait_members(snapshots, timeout if signalled else 0)
    if remaining:
        _terminate_remaining(remaining, value["pid"])
        if _wait_members(snapshots, 3):
            raise RuntimeError("本包 AstrBot 仍有进程未退出；已保留停止记录，可再次点击停止重试。")
    _remove_unchanged_record(raw)
    return True
