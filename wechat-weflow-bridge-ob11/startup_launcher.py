"""Unified launcher; no port-based termination and no unverified PID deletion."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import re
import socket
import subprocess
import stat
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import webbrowser

import startup_control
import start_astrbot

BASE = Path(__file__).resolve().parent
_LOG_NAMES = {"AstrBot": "startup_astrbot.log", "微信bot": "startup_bridge.log", "桥接": "startup_bridge.log"}
_DIAGNOSTICS = None
_ASTRBOT_LIFECYCLE = None


def _astrbot_lifecycle():
    global _ASTRBOT_LIFECYCLE
    if _ASTRBOT_LIFECYCLE is None:
        path = Path(__file__).resolve().parent.parent / "portable" / "astrbot_lifecycle.py"
        spec = importlib.util.spec_from_file_location("wechatbot_launcher_lifecycle", path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        _ASTRBOT_LIFECYCLE = helper
    return _ASTRBOT_LIFECYCLE


def _handover_previous_packages():
    """Release verified older portable services before starting either component."""
    path = BASE.parent / "portable" / "package_handover.py"
    spec = importlib.util.spec_from_file_location("wechatbot_package_handover", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    ports = {endpoint(startup_control.panel_url())[1], 11229}
    host, port = endpoint(astrbot_url())
    if host in {"127.0.0.1", "localhost", "::1"}:
        ports.add(port)
    print("阶段：切换旧版本微信bot", flush=True)
    count = helper.handover(BASE.parent, ports)
    if count:
        print("已退出旧版本微信bot组件，开始启动当前版本。", flush=True)


def _capture_bridge_spawn(process):
    pid = getattr(process, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return
    try:
        identity = startup_control._native_process(pid)
        if identity.get("status") != "alive":
            return
        value = {"pid": pid, "creation": identity["creation"], "executable": identity["executable"]}
        startup_control._verify_command(value)
        process._wechatbot_spawn_identity = value
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass  # No usable birth snapshot: never treat a later listener as ours.


def _bridge_interpreter_matches(root, child):
    canonical = startup_control._canonical
    if canonical(root["executable"]) == canonical(child["executable"]):
        return True
    selected = Path(bridge_python())
    if canonical(root["executable"]) != canonical(selected):
        return False
    config = selected.parent.parent / "pyvenv.cfg"
    try:
        if config.stat().st_size > 16384:
            return False
        match = re.search(r"(?m)^home\s*=\s*(.+?)\s*$", config.read_text(encoding="utf-8-sig"))
        if not match or not Path(match.group(1)).is_absolute():
            return False
        candidate = Path(match.group(1)) / "python.exe"
        if os.environ.get("WECHATBOT_PORTABLE_ROOT") and not candidate.resolve().is_relative_to((_astrbot_lifecycle().RUNTIME / "python").resolve()):
            return False
        return os.path.samefile(candidate, child["executable"])
    except (OSError, ValueError):
        return False


def _bridge_spawn_owns(process, info):
    value = getattr(process, "_wechatbot_spawn_identity", None)
    if not isinstance(value, dict) or value.get("pid") != process.pid:
        raise RuntimeError("无法核验本轮桥接启动器的创建身份。")
    def same_root():
        current = startup_control._native_process(value["pid"])
        if (current.get("status") != "alive" or current.get("creation") != value["creation"]
                or startup_control._canonical(current.get("executable", "")) != startup_control._canonical(value["executable"])):
            raise RuntimeError("本轮桥接启动器 PID、时间或解释器已改变。")
        startup_control._verify_command(value)
    same_root()
    if info["pid"] == value["pid"]:
        if info["creation"] != value["creation"]:
            raise RuntimeError("桥接监听 PID 的创建时间与本轮启动不符。")
    else:
        # A real Windows venv redirector starts a managed-Python child. Both
        # absolute main.py argv and native creation identities must be proven.
        if int(info["creation"]) < int(value["creation"]):
            raise RuntimeError("桥接监听进程早于本轮启动。")
        if not _bridge_interpreter_matches(value, info):
            raise RuntimeError("桥接子进程实际解释器与本轮已核验虚拟环境不符。")
        helper = _astrbot_lifecycle()
        try:
            root = helper.psutil.Process(value["pid"])
            if info["pid"] not in {p.pid for p in root.children(recursive=True)}:
                raise RuntimeError("桥接监听进程不属于本轮启动器的进程树。")
            same_root()
            if info["pid"] not in {p.pid for p in root.children(recursive=True)}:
                raise RuntimeError("桥接启动器父子进程关系在核验期间改变。")
        except helper.psutil.Error:
            raise RuntimeError("桥接启动器父子进程关系无法核验。") from None


def _bridge_listener_ready(process, url):
    info = startup_control.owned_running()
    if not info or info.get("panel_url") != url:
        raise RuntimeError("桥接面板端口没有本轮已核验的本包进程；保留其他进程与记录。")
    if process is not None:
        _bridge_spawn_owns(process, info)
    port = endpoint(url)[1]
    if startup_control._listener_pid(port) != info["pid"]:
        raise RuntimeError("桥接面板端口被其他进程占用；监听进程与本包 PID 不符。")
    # owned_running verifies creation time, executable and the absolute main.py argv.
    again = startup_control.owned_running()
    if again != info or startup_control._listener_pid(port) != info["pid"]:
        raise RuntimeError("桥接监听进程或本包创建身份在核验期间改变。")
    return info


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _AstrbotPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_title = False
        self.title = ""
        self.description = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            fields = dict(attrs)
            self.description |= fields.get("name") == "description" and fields.get("content") == "AstrBot Dashboard"

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


def _external_astrbot_ready(url):
    """Compatibility for nonportable external dashboards: product, never package ownership."""
    parsed = urlsplit(url)
    if parsed.query or parsed.fragment:
        raise ValueError("外部 AstrBot 面板核验地址不能包含查询参数或片段")
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(Request(url, headers={"Accept": "text/html"}), timeout=3) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                raise ValueError("unrecognized response")
        parser = _AstrbotPage()
        parser.feed(body.decode("utf-8", errors="replace"))
        if parser.title.strip() != "AstrBot Dashboard" or not parser.description:
            raise ValueError("unrecognized product")
        return True
    except (OSError, ValueError):
        raise RuntimeError("外部面板端口未能核验为 AstrBot；不会将任意监听服务视为成功。") from None


def _target_ready(name, process, url):
    if name in {"微信bot", "桥接"}:
        return _bridge_listener_ready(process, url)
    if name != "AstrBot":
        raise RuntimeError("未找到可核验的本包组件")
    host, port = endpoint(url)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        # An explicit remote URL is an external connection, not a local startup.
        return True
    if not os.environ.get("WECHATBOT_PORTABLE_ROOT"):
        return _external_astrbot_ready(url)
    listener = startup_control._listener_pid(port)
    _astrbot_lifecycle().ready_listener(listener, spawned_pid=process.pid if process is not None else None)
    if startup_control._listener_pid(port) != listener:
        raise RuntimeError("AstrBot 面板监听进程在核验期间改变。")
    return True


def _diagnostics():
    """Use the package's strong redactor, registering config without replaying it."""
    global _DIAGNOSTICS
    try:
        if _DIAGNOSTICS is None:
            path = Path(__file__).resolve().parent.parent / "portable" / "portable_bootstrap.py"
            spec = importlib.util.spec_from_file_location("wechatbot_launcher_diagnostics", path)
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            _DIAGNOSTICS = helper
        helper = _DIAGNOSTICS
        helper.remember_diagnostic_secrets(os.environ)
        config = BASE / "config.json"
        # A missing/unreadable config means opaque log credentials cannot be trusted.
        trusted = (config.resolve() == config.absolute() and config.is_file()
                   and config.stat().st_size <= 1024 * 1024)
        if trusted:
            helper.remember_diagnostic_secrets(helper.load_json(config))
        return helper, trusted
    except (OSError, ValueError, ImportError, AttributeError):
        return _DIAGNOSTICS, False


def _current_log_excerpt(process, log_name, helper):
    """Read only complete lines after this spawn's offset in the same owned file."""
    record = getattr(process, "_wechatbot_startup_log", None)
    path = BASE / log_name
    try:
        if (not isinstance(record, dict) or record.get("name") != log_name
                or record.get("path") != str(path.absolute())
                or path.resolve() != path.absolute()):
            return ""
        with path.open("rb") as source:
            status = os.fstat(source.fileno())
            offset = record.get("offset")
            if (not isinstance(offset, int) or offset < 0 or status.st_size < offset
                    or not status.st_ino or (status.st_dev, status.st_ino) != record.get("identity")):
                return ""
            prefix_size = min(offset, 512)
            source.seek(offset - prefix_size)
            boundary = source.read(prefix_size)
            if (record.get("prefix_size") != prefix_size or len(boundary) != prefix_size
                    or hashlib.sha256(boundary).hexdigest() != record.get("boundary_sha")):
                return ""
            start = max(offset, status.st_size - 32768)
            source.seek(start)
            raw = source.read(min(status.st_size - start, 32768))
            source.seek(offset - prefix_size)
            unchanged = source.read(prefix_size)
            after = os.fstat(source.fileno())
            if (unchanged != boundary or (after.st_dev, after.st_ino) != (status.st_dev, status.st_ino)
                    or after.st_size < status.st_size):
                return ""
        if start > offset or (offset and not record.get("terminated")):
            raw = raw.split(b"\n", 1)[1] if b"\n" in raw else b""
        # A still-being-written final line may contain only part of a private value.
        raw = raw.rsplit(b"\n", 1)[0] if b"\n" in raw else b""
        safe = helper.diagnostic_excerpt(raw)
        lines = safe.splitlines()[-12:]
        while lines and len("\n".join(lines)) > 5000:
            lines.pop(0)
        return "\n".join(lines)
    except (OSError, ValueError, RuntimeError):
        return ""


def _failure_reason(error):
    # str(SubprocessError) contains its entire argv. Preserve the useful status
    # without asking the redactor to hide the whole reason line.
    if isinstance(error, subprocess.TimeoutExpired):
        seconds = error.timeout
        seconds = seconds if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else "未提供"
        return "TimeoutExpired：子进程查询超过 %s 秒；命令参数已隐藏。" % seconds
    if isinstance(error, subprocess.CalledProcessError):
        return "CalledProcessError：子进程退出码 %s；命令参数已隐藏。" % error.returncode
    return "%s：%s" % (type(error).__name__, error)


def _report_failure(name, reason, *, process=None, timeout=20, url=None):
    helper, trusted = _diagnostics()
    safe_reason = helper.redact_diagnostic(reason) if helper else "无法安全摘取具体原因"
    try:
        if not url:
            url = astrbot_url() if name == "AstrBot" else startup_control.panel_url() if name in {"微信bot", "桥接"} else None
        port = str(endpoint(url)[1]) if url else "未核验"
    except (OSError, ValueError, RuntimeError):
        port = "未核验"
    log_name = _LOG_NAMES.get(name)
    log_path = str(BASE / log_name) if log_name else "无已核验的本包启动日志"
    log_label = "本轮日志" if process is not None else "预期日志位置（本轮未新建服务日志）"
    print("阶段：%s：服务启动失败" % name, flush=True)
    print("原因：%s；面板端口：%s；等待上限：%s 秒；%s：%s" %
          (safe_reason, port, timeout, log_label, log_path), flush=True)
    excerpt = _current_log_excerpt(process, log_name, helper) if trusted and process is not None and log_name else ""
    if excerpt:
        print(excerpt, flush=True)


def _read_hint(name):
    path = BASE / name
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return lines[0].strip() if lines else ""


def bridge_python():
    portable = os.environ.get("WECHATBOT_PORTABLE_BRIDGE_PYTHON")
    if portable:
        candidate = Path(portable).resolve()
        if not candidate.is_file():
            raise FileNotFoundError("本包桥接解释器不存在，请再次双击一键启动")
        return str(candidate)
    for name in (".venv", "venv"):
        candidate = BASE / name / "Scripts" / "python.exe"
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def astrbot_python():
    portable = os.environ.get("WECHATBOT_PORTABLE_ASTRBOT_PYTHON")
    if portable:
        candidate = Path(portable).resolve()
        if not candidate.is_file():
            raise FileNotFoundError("本包 AstrBot 解释器不存在，请再次双击一键启动")
        return str(candidate)
    explicit = _read_hint("astrbot_py.txt")
    if explicit:
        candidate = Path(os.path.expandvars(explicit.strip('"'))).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError("astrbot_py.txt 指定的解释器不存在")
        return str(candidate.resolve())
    cli = shutil.which("astrbot")
    if cli:
        candidate = Path(cli).with_name("python.exe")
        if candidate.is_file():
            return str(candidate.resolve())
    roots = [Path(value) / "uv" / "tools" / "astrbot" for value in
             (os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")) if value]
    roots += [Path.home() / ".local" / "pipx" / "venvs" / "astrbot", Path.home() / "pipx" / "venvs" / "astrbot"]
    for root in roots:
        candidate = root / "Scripts" / "python.exe"
        if candidate.is_file():
            return str(candidate.resolve())
    return None  # Official CLI on PATH remains a valid fallback.


def astrbot_url():
    url = _read_hint("astrbot_url.txt") or os.environ.get("WECHATBOT_PORTABLE_ASTRBOT_URL") or "http://127.0.0.1:6185"
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("astrbot_url.txt 必须是无凭据的 HTTP/HTTPS 面板地址")
    # Force urllib's port validation before opening a browser or starting a process.
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("AstrBot 面板端口无效")
    return url


def endpoint(url):
    parsed = urlsplit(url)
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def port_open(url):
    try:
        with socket.create_connection(endpoint(url), timeout=0.3):
            return True
    except OSError:
        return False


def _spawn(command, log_name, cwd=None):
    # The explicit .bat foreground command is the interactive terminal option.
    # One-click helpers/services run without additional visible windows.
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    startup = None
    portable_astrbot = os.name == "nt" and log_name == "startup_astrbot.log" and os.environ.get("WECHATBOT_PORTABLE_ROOT")
    if portable_astrbot:
        flags = subprocess.CREATE_NEW_CONSOLE
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
    if log_name not in set(_LOG_NAMES.values()):
        raise ValueError("启动日志必须是本包固定日志文件")
    log_path = BASE / log_name
    if os.name == "nt" and log_path.exists() and log_path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
        os.chmod(log_path, stat.S_IWRITE)
    with log_path.open("ab+") as output:
        output.seek(0, os.SEEK_END)
        offset = output.tell()
        prefix_size = min(offset, 512)
        output.seek(offset - prefix_size)
        boundary = output.read(prefix_size)
        output.seek(0, os.SEEK_END)
        status = os.fstat(output.fileno())
        record = {"name": log_name, "path": str(log_path.absolute()), "offset": offset,
                  "identity": (status.st_dev, status.st_ino), "prefix_size": prefix_size,
                  "boundary_sha": hashlib.sha256(boundary).hexdigest(),
                  "terminated": not offset or boundary.endswith(b"\n")}
        if status.st_size != offset or len(boundary) != prefix_size:
            record = {}  # Concurrent change: path-only diagnostics, never guess the boundary.
        process = subprocess.Popen(command, cwd=str(cwd or BASE), stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, creationflags=flags, startupinfo=startup)
    process._wechatbot_startup_log = record
    if log_name == "startup_bridge.log":
        _capture_bridge_spawn(process)
    if portable_astrbot:
        sys.path.insert(0, str(BASE.parent / "portable"))
        from astrbot_lifecycle import record_process
        record_process(process.pid)
    return process


def _activate_existing_bridge(info, url):
    """An idle panel is still a live process; resume it through its owned API."""
    def verify():
        if _bridge_listener_ready(None, url) != info:
            raise RuntimeError("桥接面板创建身份已改变；未继续启动操作。")

    def read_status(request):
        try:
            value = json.loads(startup_control._panel_response(session, request))
        except (ValueError, UnicodeError):
            raise RuntimeError("本包面板启动状态不是有效 JSON。") from None
        if (not isinstance(value, dict) or type(value.get("running")) is not bool
                or type(value.get("paused")) is not bool):
            raise RuntimeError("本包面板启动状态格式无效。")
        return value

    verify()
    session = startup_control._panel_session()
    parser = startup_control._CsrfParser()
    parser.feed(startup_control._panel_response(session, Request(url + "/")).decode("utf-8"))
    status = read_status(Request(url + "/api/status"))
    verify()
    if status["running"] and not status["paused"]:
        return
    if len(parser.tokens) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", parser.tokens[0]):
        raise RuntimeError("本包面板操作凭证无法核验；未发送启动请求。")
    action = "/start" if not status["running"] else "/resume"
    result = read_status(Request(url + action, data=b"{}", headers={
        "Content-Type": "application/json", "Origin": url, "X-Panel-Token": parser.tokens[0]}, method="POST"))
    verify()
    if result.get("ok") is False or not result["running"] or result["paused"]:
        raise RuntimeError("本包面板未确认恢复机器人；请查看面板服务操作错误后重试。")
    print("已通过本包面板恢复机器人，继续等待连接。", flush=True)


def launch_bridge(foreground=False):
    live = startup_control.owned_running()
    url = startup_control.panel_url()
    if live:
        actual = live.get("panel_url", url)
        if port_open(actual):
            _activate_existing_bridge(live, actual)
        if actual != url:
            print("配置端口已修改；当前进程仍使用启动时端口，请使用重启桥接。")
        print("微信bot 已运行。")
        return None, actual
    if port_open(url):
        raise RuntimeError("配置的面板端口被占用；不会终止其他进程，请更改 web_port 或核对占用程序")
    command = [bridge_python(), "-u", str(BASE / "main.py")]
    if foreground:
        raise SystemExit(subprocess.call(command, cwd=str(BASE)))
    print("启动微信bot；启动日志：" + str(BASE / "startup_bridge.log"))
    return _spawn(command, "startup_bridge.log"), url


def launch_astrbot():
    url = astrbot_url()
    host, port = endpoint(url)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("AstrBot 使用显式配置的外部远程面板；不代表本包已启动。")
        return None, url
    if os.environ.get("WECHATBOT_PORTABLE_ROOT"):
        _astrbot_lifecycle().finish_pending_stop(timeout=8)
    if port_open(url):
        _target_ready("AstrBot", None, url)
        print("本包 AstrBot 已核验面板连接；跳过重复启动。" if os.environ.get("WECHATBOT_PORTABLE_ROOT")
              else "已核验外部 AstrBot 面板产品；不代表本包已启动。")
        return None, url
    if os.environ.get("WECHATBOT_PORTABLE_ROOT") and _astrbot_lifecycle().verified_record() is not None:
        print("本包 AstrBot 进程已核验，继续等待面板初始化。")
        return None, url
    python = astrbot_python()
    command = start_astrbot.build_command(port, python)
    root = start_astrbot._find_astrbot_root()
    if not root.is_dir():
        raise FileNotFoundError("AstrBot 工作目录不存在，请先按官方文档初始化，或设置 ASTRBOT_ROOT")
    print("通过官方 astrbot run 启动；启动日志：" + str(BASE / "startup_astrbot.log"))
    return _spawn(command, "startup_astrbot.log", cwd=root), url


def _bridge_connection_ready(url, session, deadline):
    """Read the owned panel's connection state; never replay its log or config."""
    status = None
    for path in ("/", "/api/status"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        with session.open(Request(url + path), timeout=min(2, remaining)) as response:
            raw = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 1024 * 1024:
                raise RuntimeError("本包桥接连接状态响应无效。")
        if path == "/api/status":
            try:
                status = json.loads(raw)
            except (ValueError, UnicodeError):
                raise RuntimeError("本包桥接连接状态不是有效 JSON。") from None
    if not isinstance(status, dict):
        raise RuntimeError("本包桥接连接状态格式无效。")
    return status.get("running") is True and status.get("ob_connected") is True


def open_when_ready(targets, timeout=20, require_bridge_connection=False):
    waiting = list(targets)
    failed = False
    deadline = time.monotonic() + timeout
    sessions, ready_urls = {}, []
    while waiting and time.monotonic() < deadline:
        for item in list(waiting):
            name, process, url = item
            exit_code = process.poll() if process is not None else None
            if exit_code is not None:
                _report_failure(name, "子进程退出码 %s" % exit_code, process=process, timeout=timeout, url=url)
                waiting.remove(item)
                failed = True
            elif port_open(url):
                try:
                    _target_ready(name, process, url)
                    if require_bridge_connection and name in {"微信bot", "桥接"}:
                        if url not in sessions:
                            sessions[url] = startup_control._panel_session()
                        connected = _bridge_connection_ready(url, sessions[url], deadline)
                        _target_ready(name, process, url)
                        if not connected:
                            continue
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ImportError) as exc:
                    _report_failure(name, _failure_reason(exc), process=process, timeout=timeout, url=url)
                    waiting.remove(item)
                    failed = True
                else:
                    if require_bridge_connection:
                        ready_urls.append(url)
                    else:
                        webbrowser.open(url)
                    waiting.remove(item)
        if waiting:
            time.sleep(0.15)
    for name, process, url in waiting:
        reason = ("等待超时，本包桥接仍未确认连接 AstrBot OneBot（默认端口 11229）；请检查适配器和桥接连接日志"
                  if require_bridge_connection and name in {"微信bot", "桥接"}
                  else "等待超时，面板仍未就绪；仍在初始化或未能启动")
        _report_failure(name, reason, process=process, timeout=timeout, url=url)
    if not waiting and not failed and require_bridge_connection:
        print("本包桥接已确认连接 AstrBot OneBot。", flush=True)
        for url in ready_urls:
            webbrowser.open(url)
    return not waiting and not failed


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["all", "open", "bridge", "restart"])
    parser.add_argument("--foreground", action="store_true")
    args = parser.parse_args(argv)
    targets, errors = [], []
    timeout = 180 if os.environ.get("WECHATBOT_PORTABLE_ROOT") else 20
    if args.action == "open":
        for name, get_url in (("微信bot", startup_control.panel_url), ("AstrBot", astrbot_url)):
            try:
                webbrowser.open(get_url())
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ImportError) as exc:
                _report_failure(name, _failure_reason(exc), timeout=timeout)
                errors.append(name)
        return 1 if errors else 0
    if args.action == "restart":
        try:
            stopped = startup_control.stop_owned_process()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ImportError) as exc:
            _report_failure("微信bot", _failure_reason(exc), timeout=timeout)
            return 1
        if not stopped:
            _report_failure("微信bot", "未能核验或正常停止本包进程；重启失败", timeout=timeout)
            return 1
    if args.action == "all" and os.environ.get("WECHATBOT_PORTABLE_ROOT"):
        try:
            _handover_previous_packages()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ImportError) as exc:
            _report_failure("版本切换", _failure_reason(exc), timeout=timeout,
                            url=startup_control.panel_url())
            return 1
    # Start independent services before waiting for either panel to become ready.
    if args.action == "all":
        try:
            process, url = launch_astrbot()
            targets.append(("AstrBot", process, url))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ImportError) as exc:
            errors.append(("AstrBot", exc))
    try:
        process, url = launch_bridge(args.foreground)
        targets.append(("微信bot", process, url))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ImportError) as exc:
        errors.append(("微信bot", exc))
    for name, error in errors:
        _report_failure(name, _failure_reason(error), timeout=timeout)
    if targets:
        ready = (open_when_ready(targets, timeout=timeout, require_bridge_connection=args.action == "all")
                 if os.environ.get("WECHATBOT_PORTABLE_ROOT") else open_when_ready(targets))
        if not ready:
            return 1
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
