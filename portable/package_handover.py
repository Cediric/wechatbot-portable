"""Stop verified older portable services before starting this package."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

import psutil


def _canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def _listeners(ports):
    found = {port: set() for port in ports}
    try:
        connections = psutil.net_connections(kind="tcp")
    except (OSError, psutil.Error):
        raise RuntimeError("无法读取监听进程；请先退出旧微信bot后再启动本包。") from None
    for item in connections:
        if item.status != psutil.CONN_LISTEN or not item.laddr:
            continue
        address, port = item.laddr
        if port in found and address in {"127.0.0.1", "0.0.0.0", "::1", "::"}:
            found[port].add(item.pid)
    result = {}
    for port, pids in found.items():
        if pids:
            if len(pids) != 1 or None in pids:
                raise RuntimeError("端口 %s 的监听进程无法确定；请先退出旧微信bot或占用程序。" % port)
            result[port] = pids.pop()
    return result


def _occupied(port, pid, reason):
    return RuntimeError("端口 %s 被进程 %s 占用，%s；请先退出旧微信bot或占用程序，再启动本包。" % (port, pid, reason))


def _locate(pid):
    process = psutil.Process(pid)
    env = process.environ()
    command = process.cmdline()
    # Inspect only the actual Python entrypoint, never a filename inside -c text.
    args = command[1:]
    while args:
        if args[0] in {"-u", "-B", "-E", "-s", "-S", "-I"}:
            args = args[1:]
        elif len(args) > 1 and args[:2] == ["-X", "utf8"]:
            args = args[2:]
        else:
            break
    root = None
    if args and Path(args[0]).is_absolute():
        entry = Path(args[0])
        if entry.name.lower() == "main.py" and entry.parent.name.lower() == "wechat-weflow-bridge-ob11":
            root = entry.parent.parent
    kind = "bridge" if root is not None else "astrbot"
    if root is None:
        instance = env.get("ASTRBOT_ROOT", "")
        if instance and Path(instance).is_absolute():
            path = Path(instance)
            if path.name.lower() == "astrbot-instance" and path.parent.name.lower() == "runtime" and path.parent.parent.name.lower() == "portable":
                root = path.parent.parent.parent
    if root is None:
        raise ValueError("not a portable service")
    configured = env.get("WECHATBOT_PORTABLE_ROOT", "")
    if configured and (not Path(configured).is_absolute() or _canonical(configured) != _canonical(root)):
        raise ValueError("package identity differs")
    return kind, root.resolve()


def _load_current(current_root, kind, previous_root):
    """Use this delivery's verifier code with the previous package's data paths."""
    relative = "wechat-weflow-bridge-ob11/startup_control.py" if kind == "bridge" else "portable/astrbot_lifecycle.py"
    spec = importlib.util.spec_from_file_location("wechatbot_handover_" + kind, current_root / relative)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    if kind == "bridge":
        helper.BASE = str(previous_root / "wechat-weflow-bridge-ob11")
    else:
        helper.ROOT = previous_root
        helper.RUNTIME = previous_root / "portable" / "runtime"
        helper.INSTANCE = helper.RUNTIME / "astrbot-instance"
        helper.RECORD = helper.RUNTIME / "astrbot-process.json"
    return helper


def _verify(candidate):
    helper, kind, listeners = candidate["helper"], candidate["kind"], candidate["listeners"]
    if kind == "bridge":
        # The listener, native creation identity and actual main.py entry are
        # sufficient even when an older package lost its optional PID file.
        value = helper.owned_listener(next(iter(listeners)))
        if not value or any(pid != value["pid"] for pid in listeners.values()):
            raise ValueError("bridge listener ownership differs")
        if urlsplit(value.get("panel_url", "")).port not in listeners:
            raise ValueError("bridge listener differs from its verified panel")
        return value
    value = helper.verified_record(required=True)
    for pid in set(listeners.values()):
        helper.ready_listener(pid)
    return value


def _wait_released(listeners):
    deadline = time.monotonic() + 5
    while True:
        remaining = _listeners(listeners)
        if not remaining:
            return
        changed = next(((port, pid) for port, pid in remaining.items() if pid != listeners[port]), None)
        if changed:
            raise _occupied(*changed, "退出后端口已换成其他进程，本次交接停止")
        if time.monotonic() >= deadline:
            port, pid = next(iter(remaining.items()))
            raise _occupied(port, pid, "退出后端口仍被占用，本次交接停止")
        time.sleep(.1)


def handover(current_root, ports):
    """Return the number of older components stopped; never alter user data."""
    current_root = Path(current_root).resolve()
    ports = tuple(dict.fromkeys(ports))
    if any(isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 for port in ports):
        raise ValueError("交接检查端口无效")
    if not ports:
        return 0
    listeners = _listeners(ports)
    candidates = {}
    for port, pid in listeners.items():
        try:
            kind, previous_root = _locate(pid)
            if _canonical(previous_root) == _canonical(current_root):
                continue  # The normal launcher verifies/reuses this package.
            key = kind, _canonical(previous_root)
            if key not in candidates:
                candidates[key] = {"kind": kind, "listeners": {},
                                   "helper": _load_current(current_root, kind, previous_root)}
            candidates[key]["listeners"][port] = pid
        except (OSError, ValueError, TypeError, KeyError, psutil.Error):
            raise _occupied(port, pid, "无法核验为旧版微信bot") from None
    # Prove every candidate before stopping any service; a foreign listener must
    # not produce a half-finished switch from an otherwise valid old package.
    for candidate in candidates.values():
        port, pid = next(iter(candidate["listeners"].items()))
        try:
            candidate["record"] = _verify(candidate)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, psutil.Error) as exc:
            reason = ("旧桥接实时进程核验失败：%s" % exc if candidate["kind"] == "bridge"
                      else "旧 AstrBot 进程记录缺失或归属不匹配")
            raise _occupied(port, pid, reason) from None
    stopped = 0
    for candidate in sorted(candidates.values(), key=lambda item: item["kind"] != "bridge"):
        port, pid = next(iter(candidate["listeners"].items()))
        try:
            if _listeners(candidate["listeners"]) != candidate["listeners"] or _verify(candidate) != candidate["record"]:
                raise ValueError("listener or creation identity changed")
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, psutil.Error):
            raise _occupied(port, pid, "监听或创建身份已改变，未发送退出请求") from None
        helper = candidate["helper"]
        try:
            ok = helper.stop_listener(candidate["record"]) if candidate["kind"] == "bridge" else helper.stop_owned()
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, psutil.Error):
            ok = False
        if not ok:
            raise _occupied(port, pid, "旧微信bot未确认正常退出，本次交接停止")
        _wait_released(candidate["listeners"])
        stopped += 1
    return stopped
