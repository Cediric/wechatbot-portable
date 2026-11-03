"""Package-local, resumable Windows bootstrap; never installs into global Python."""
from __future__ import annotations
import argparse
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BRIDGE = ROOT / "wechat-weflow-bridge-ob11"
RUNTIME = HERE / "runtime"
INSTANCE = RUNTIME / "astrbot-instance"
PLATFORM_ID = "wechatbot-portable"
PROVIDER_ID = "wechatbot-main"
STARTUP_STAGE = "读取启动参数"
_DIAGNOSTIC_SECRETS = set()
_SECRET_NAME = re.compile(r"(?:api[_-]?key|access[_-]?key|secret|token|password|passwd|authorization|credential|(?:^|[_-])key$|密码|密钥|令牌)", re.I)

def startup_stage(value):
    global STARTUP_STAGE
    STARTUP_STAGE = value

def remember_diagnostic_secrets(values):
    """Register already loaded secrets without opening another config or logging it."""
    if isinstance(values, Mapping):
        for name, value in values.items():
            if _SECRET_NAME.search(str(name)):
                candidates = value if isinstance(value, (list, tuple)) else (value,)
                for candidate in candidates:
                    if isinstance(candidate, str) and candidate:
                        _DIAGNOSTIC_SECRETS.add(candidate)
            if isinstance(value, (Mapping, list, tuple)):
                remember_diagnostic_secrets(value)
    elif isinstance(values, (list, tuple)):
        for value in values:
            remember_diagnostic_secrets(value)

def redact_diagnostic(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "")
    text = re.sub(r"(?im)^.*(?:\bcommand(?:\s|=|:)|--(?:api[_-]?key|token|password|secret|credential|authorization)(?:=|\s)).*$", "[子进程命令已隐藏]", text)
    text = re.sub(r"(?im)^(.*?)(?:\{|\[)[^\r\n]*(?:main_llm_|api[_-]?key|provider_settings|system_prompt|memory|password)[^\r\n]*$", r"\1[配置对象已隐藏]", text)
    text = re.sub(r"(?s)\{.*\}", "[对象内容已隐藏]", text)
    for secret in sorted(_DIAGNOSTIC_SECRETS, key=len, reverse=True):
        for representation in (secret, json.dumps(secret, ensure_ascii=True)[1:-1], repr(secret)[1:-1]):
            text = text.replace(representation, "[已脱敏]")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_.=~-]+", "Bearer [已脱敏]", text)
    text = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{8,}", "[已脱敏]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[已脱敏]@", text)
    text = re.sub(r"(?i)(https?://[^\s?'\"<>]+)\?[^\s'\"<>]+", r"\1?[查询参数已隐藏]", text)
    # Quoted JSON/Python fields, shell-style assignments and Chinese labels.
    field = r"(?:[\w.-]*(?:api[_-]?key|access[_-]?key|secret|token|password|passwd|authorization|credential)[\w.-]*|key|密码|密钥|令牌)"
    prefix = r"([\"']?" + field + r"[\"']?\s*[:=：]\s*)"
    text = re.sub(r"(?is)" + prefix + r"\[[^\]]*\]", r"\1[已脱敏]", text)
    text = re.sub(r"(?is)" + prefix + r'''(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$))''', r"\1[已脱敏]", text)
    text = re.sub(r"(?im)(\bauthorization\b[\"']?\s*[:=：]\s*)[^\r\n]*", r"\1[已脱敏]", text)
    text = re.sub(r"(?i)([\"']?" + field + r"[\"']?\s*[:=：]\s*)(?:\[[^\]\r\n]*\]|\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\}\]\r\n]+)", r"\1[已脱敏]", text)
    return text

def _bounded_diagnostic_lines(lines, line_limit, char_limit):
    important = [i for i, line in enumerate(lines) if line.startswith(("阶段：", "原因："))][-8:]
    selected, size = set(), 0
    for i in important + list(range(len(lines) - 1, -1, -1)):
        if i not in selected and len(selected) < line_limit and size + len(lines[i]) + 1 <= char_limit:
            selected.add(i)
            size += len(lines[i]) + 1
    return "\n".join(lines[i] for i in sorted(selected))

def diagnostic_excerpt(value):
    """Only error/traceback/progress lines are useful; opaque output stays private."""
    safe = redact_diagnostic(value)
    lines = []
    useful = re.compile(r"(?:Traceback|\bFile \"|\b\w*(?:Error|Exception)\b|\berror\b|\bfailed\b|\bfailure\b|not found|no such|denied|cannot|can't|timed? out|timeout|missing|invalid|requires|unsupported|启动未完成|阶段[：:]|原因[：:]|缺少|失败|无效|未找到|无法|超时|准备|检测|检查|修复|环境准备完成)", re.I)
    for line in safe.splitlines():
        stripped = line.strip()
        # Never replay an environment/config dump, traceback source line, or argv.
        if (not stripped or stripped.startswith(("{", "[", '"', "'", "Command ", "command=", "env=", "settings=", "config="))
                or re.search(r"(?:os\.environ|subprocess\.(?:run|Popen)|run_checked\(|Invoke-Checked|--api[_-]?key)", stripped)):
            continue
        if line[:1].isspace() and not stripped.startswith('File "'):
            continue
        if useful.search(stripped):
            lines.append(line[:1500])
    return _bounded_diagnostic_lines(lines, 32, 12000)

def combined_diagnostic_excerpt(stdout="", stderr=""):
    """Reserve a bounded share for each stream; a warning cannot hide a failure."""
    parts = []
    for value in (stdout, stderr):
        safe = diagnostic_excerpt(value)
        # Preserve component/exit/deadline summaries even if tracebacks fill the tail.
        bounded = _bounded_diagnostic_lines(safe.splitlines(), 16, 5900)
        if bounded:
            parts.append(bounded)
    return "\n".join(parts)

class BootstrapCommandError(RuntimeError):
    def __init__(self, stage, reason, detail="", kind="exit"):
        self.stage = stage
        self.kind = kind
        self.detail = diagnostic_excerpt(detail)
        message = "阶段：%s\n原因：%s" % (stage, redact_diagnostic(reason))
        if self.detail:
            message += "\n" + self.detail
        super().__init__(message)

def save_startup_failure(exc):
    stage = getattr(exc, "stage", STARTUP_STAGE)
    if isinstance(exc, subprocess.TimeoutExpired):
        reason = "子进程超时（%s 秒），已停止等待。" % exc.timeout
        details = combined_diagnostic_excerpt(exc.output, exc.stderr)
    else:
        reason = redact_diagnostic(str(exc))
        details = ""
    # Traceback locations remain useful; source lines, locals and command values do not.
    locations = ["  %s:%d (%s)" % (Path(frame.filename).name, frame.lineno, frame.name)
                 for frame in traceback.extract_tb(exc.__traceback__)]
    report = "微信bot 启动诊断（已脱敏）\n阶段：%s\n错误类型：%s\n原因：%s\n" % (stage, type(exc).__name__, reason)
    if details: report += details + "\n"
    previous = getattr(exc, "previous_diagnostic", "")
    if previous: report += "前序导入失败（修复前）：\n" + previous + "\n"
    if locations: report += "调用位置：\n" + "\n".join(locations) + "\n"
    report = redact_diagnostic(report)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    path = RUNTIME / "startup-error.txt"
    temporary = path.with_name(".startup-error-%s.tmp" % time.time_ns())
    try:
        temporary.write_text(report, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return report

def _mutable_config_paths():
    return (BRIDGE / "config.json", INSTANCE / "data" / "cmd_config.json",
            INSTANCE / "data" / "wechatbot-profile-location.json")


def _mutable_config_path(path):
    path = Path(os.path.abspath(path))
    if not any(os.path.normcase(str(path)) == os.path.normcase(os.path.abspath(item))
               for item in _mutable_config_paths()):
        raise RuntimeError("可变配置路径不属于本包固定白名单。")
    # Inspect the lexical path, so resolving a junction cannot hide its origin.
    for parent in reversed((path, *path.parents)):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("可变配置路径含有重解析点，已停止。")
        if parent != path and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("可变配置祖先不是目录，已停止。")
        if parent == path and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise RuntimeError("可变配置不是独立普通文件，已停止。")
    return path


def _config_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            getattr(info, "st_birthtime_ns", info.st_ctime_ns), info.st_nlink)


def _mutable_config_snapshot(path):
    path = _mutable_config_path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if before.st_size > 16 * 1024 * 1024:
        raise RuntimeError("可变配置超过核验上限，已停止。")
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        digest = hashlib.sha256(source.read(16 * 1024 * 1024 + 1)).digest()
        closed = os.fstat(source.fileno())
    _mutable_config_path(path)
    after = path.lstat()
    identity = _config_identity(before)
    if any(_config_identity(value) != identity for value in (opened, closed, after)):
        raise RuntimeError("可变配置在核验期间发生变化，已停止。")
    return {"path": path, "identity": identity, "digest": digest,
            "attributes": getattr(after, "st_file_attributes", 0)}


def _same_mutable_config(previous, current):
    return current is not None and all(previous[key] == current[key] for key in ("identity", "digest"))


def _writable_config_attributes(attributes):
    # Windows reports FILE_ATTRIBUTE_NORMAL (0x80), not zero, when clearing
    # READONLY leaves no other attributes. NORMAL is valid only on its own.
    writable = attributes & ~1
    return ((writable & ~0x80) or 0x80) if os.name == "nt" else writable


def _set_mutable_config_attributes(path, attributes):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel.SetFileAttributesW
    function.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    function.restype = wintypes.BOOL
    if not function(str(path), attributes):
        raise ctypes.WinError(ctypes.get_last_error())


def _restore_mutable_config_attributes(previous):
    current = _mutable_config_snapshot(previous["path"])
    if (not _same_mutable_config(previous, current) or
            current["attributes"] not in (previous["attributes"], _writable_config_attributes(previous["attributes"]))):
        raise RuntimeError("可变配置已被其他程序改变，未恢复属性或覆盖内容。")
    _set_mutable_config_attributes(previous["path"], previous["attributes"])
    restored = _mutable_config_snapshot(previous["path"])
    if not _same_mutable_config(previous, restored) or restored["attributes"] != previous["attributes"]:
        raise RuntimeError("可变配置原属性恢复未通过核验。")


def prepare_mutable_config(path):
    """Clear only an owned configuration's Windows read-only bit, preserving bytes."""
    previous = None
    attempted = False
    step = "读取配置快照"
    try:
        previous = _mutable_config_snapshot(path)
        if previous is None or os.name != "nt" or not previous["attributes"] & 1:
            return previous
        step = "核验修改前的配置"
        current = _mutable_config_snapshot(path)
        if not _same_mutable_config(previous, current) or current["attributes"] != previous["attributes"]:
            raise RuntimeError("可变配置在修改属性前发生变化，已停止。")
        attempted = True
        step = "清除配置只读属性"
        writable_attributes = _writable_config_attributes(previous["attributes"])
        _set_mutable_config_attributes(previous["path"], writable_attributes)
        step = "核验配置可写属性"
        current = _mutable_config_snapshot(path)
        if not _same_mutable_config(previous, current) or current["attributes"] != writable_attributes:
            raise RuntimeError("可变配置可写属性未通过核验。")
        return previous
    except BaseException as error:
        restore_failed = False
        if attempted:
            try:
                _restore_mutable_config_attributes(previous)
            except BaseException:
                restore_failed = True
        if not isinstance(error, Exception):
            raise
        winerror = getattr(error, "winerror", None)
        raise RuntimeError("阶段：可变配置准备失败；步骤：%s；原因：%s%s；原属性恢复：%s。" %
                           (step, type(error).__name__, "；WinError：%s" % winerror if winerror is not None else "",
                            "需检查" if restore_failed else "无需或已完成")) from None


def atomic_json(path: Path, data: dict):
    path = Path(path)
    owned = any(os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(item))
                for item in _mutable_config_paths())
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    previous = prepare_mutable_config(path) if owned else None
    temporary = None
    replaced = False
    step = "写入临时配置"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if owned:
            _mutable_config_path(path)
        descriptor, name = tempfile.mkstemp(prefix=".wechatbot-config-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if owned:
            step = "核验保存前的配置"
            current = _mutable_config_snapshot(path)
            if ((previous is None and current is not None) or
                    (previous is not None and
                     (not _same_mutable_config(previous, current) or
                      current["attributes"] != _writable_config_attributes(previous["attributes"])))):
                raise RuntimeError("可变配置在保存前发生变化，未覆盖内容。")
        replacement_info = temporary.lstat()
        replacement_identity = _config_identity(replacement_info)
        step = "替换本包配置"
        os.replace(temporary, path)
        replaced = True
        if owned and previous is not None and os.name == "nt":
            step = "核验保存后的配置"
            current = _mutable_config_snapshot(path)
            if (current["identity"] != replacement_identity or current["digest"] != hashlib.sha256(payload).digest() or
                    current["attributes"] != getattr(replacement_info, "st_file_attributes", 0)):
                raise RuntimeError("可变配置在保存后发生变化，未修改其他程序的属性。")
            writable_attributes = _writable_config_attributes(previous["attributes"])
            _set_mutable_config_attributes(path, writable_attributes)
            after = _mutable_config_snapshot(path)
            if not _same_mutable_config(current, after) or after["attributes"] != writable_attributes:
                raise RuntimeError("可变配置保存后的属性未通过核验。")
    except BaseException as error:
        restore_failed = False
        if previous is not None and previous["attributes"] & 1 and not replaced:
            try:
                _restore_mutable_config_attributes(previous)
            except BaseException:
                restore_failed = True
        if not owned or not isinstance(error, Exception):
            raise
        winerror = getattr(error, "winerror", None)
        raise RuntimeError("阶段：可变配置保存失败；步骤：%s；原因：%s%s；原属性恢复：%s。" %
                           (step, type(error).__name__, "；WinError：%s" % winerror if winerror is not None else "",
                            "需检查" if restore_failed else "无需或已完成")) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def load_json(path: Path, default=None):
    if not path.is_file():
        return {} if default is None else default
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(path.name + " 必须为配置对象")
    return data

def run_checked(command, *, cwd=ROOT, env=None, timeout=900, capture=False, stage=None):
    if stage: startup_stage(stage)
    remember_diagnostic_secrets(os.environ if env is None else env)
    try:
        # Raw child output must never bypass redaction, even for install progress.
        result = subprocess.run([str(x) for x in command], cwd=str(cwd), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except subprocess.TimeoutExpired as exc:
        raise BootstrapCommandError(STARTUP_STAGE, "子进程超时（%s 秒），已停止等待。" % timeout,
                                    combined_diagnostic_excerpt(exc.output, exc.stderr), kind="timeout") from None
    except OSError as exc:
        raise BootstrapCommandError(STARTUP_STAGE, "%s：%s" % (type(exc).__name__, exc.strerror or "无法启动子进程"), kind="spawn") from None
    if result.returncode:
        detail = combined_diagnostic_excerpt(getattr(result, "stdout", ""), getattr(result, "stderr", ""))
        raise BootstrapCommandError(STARTUP_STAGE, "子进程退出码 %d。" % result.returncode, detail)
    if not capture:
        progress = combined_diagnostic_excerpt(getattr(result, "stdout", ""), getattr(result, "stderr", ""))
        if progress: print(progress, flush=True)
    return result

def managed_paths():
    uv = Path(os.environ.get("WECHATBOT_PORTABLE_UV", str(RUNTIME / "uv" / "uv.exe"))).resolve()
    python = Path(os.environ.get("WECHATBOT_PORTABLE_PYTHON", sys.executable)).resolve()
    if not uv.is_file() or not python.is_file():
        raise RuntimeError("请双击根目录一键启动.bat，先准备本包运行器。")
    if not uv.is_relative_to(RUNTIME.resolve()) or not python.is_relative_to((RUNTIME / "python").resolve()):
        raise RuntimeError("便携启动仅使用本包 uv 与 managed Python。")
    return uv, python

def install_long_path_hook(target: Path):
    """Install only our private, byte-stable hook in a verified local venv."""
    runtime = RUNTIME.resolve()
    expected = runtime / target.name
    site_packages = target / "Lib" / "site-packages"
    if (target.name not in {"bridge-venv", "astrbot-venv"} or target.resolve() != expected
            or runtime != RUNTIME.absolute() or site_packages.resolve() != expected / "Lib" / "site-packages"
            or not site_packages.is_dir() or not (HERE / "runtime-manifest.json").is_file()):
        raise RuntimeError("长路径兼容钩子仅安装到本包已核验的独立运行环境。")
    source = HERE / "windows_long_paths.py"
    if not source.is_file() or source.resolve() != HERE / "windows_long_paths.py":
        raise RuntimeError("便携包缺少长路径兼容模块。")
    contents = {
        "_wechatbot_long_paths.py": source.read_bytes(),
        "wechatbot_private_long_paths.pth": b"import _wechatbot_long_paths; _wechatbot_long_paths.activate()\n",
    }
    for name, raw in contents.items():
        path = site_packages / name
        if path.is_symlink() or path.resolve() != expected / "Lib" / "site-packages" / name:
            raise RuntimeError("长路径兼容钩子文件位置无效。")
        if path.is_file() and path.read_bytes() == raw:
            continue
        temporary = path.with_name(".wechatbot-long-paths-" + str(time.time_ns()) + ".tmp")
        try:
            temporary.write_bytes(raw)
            os.replace(temporary, path)
        finally:
            if temporary.exists(): temporary.unlink()

def import_check_environment(name):
    env = os.environ.copy()
    env.pop("ASTRBOT_DASHBOARD_INITIAL_PASSWORD", None)
    env.pop("ASTRBOT_DASHBOARD_RESET_PASSWORD", None)
    env.pop("ASTRBOT_RESET_DASHBOARD_PASSWORD", None)
    if name == "astrbot":
        # astrbot.api imports initialize config/SQLite. Never do that in the
        # formal instance or an inherited host root before the real CLI init.
        sandbox = RUNTIME / "astrbot-import-check"
        expected = RUNTIME.absolute() / "astrbot-import-check"
        if RUNTIME.resolve() != RUNTIME.absolute() or sandbox.resolve() != expected:
            raise RuntimeError("AstrBot 导入检查目录超出本包，已停止检查。")
        sandbox.mkdir(parents=True, exist_ok=True)
        for entry in sandbox.rglob("*"):
            if entry.is_symlink() or entry.is_junction():
                raise RuntimeError("AstrBot 导入检查目录含链接，已停止检查。")
        env["ASTRBOT_ROOT"] = str(sandbox)
    return env

def sync_environment_dependencies(uv, executable, lock, name, *, reinstall=False):
    label = "桥接" if name == "bridge" else "AstrBot"
    stage = label + ("环境：修复锁定依赖" if reinstall else "环境：安装并核验锁定依赖")
    command = [uv, "pip", "sync", "--python", executable, "--require-hashes", "--no-build", "--no-config", lock]
    if reinstall: command += ["--reinstall"]
    wheels = HERE / "vendor" / "wheels"
    if wheels.is_dir():
        # An online index can prefer an sdist over --find-links. This release
        # contains every locked wheel; use only those verified binaries.
        command += ["--find-links", wheels, "--no-index"]
    elif os.environ.get("WECHATBOT_OFFLINE") == "1":
        command += ["--no-index"]
    try:
        return run_checked(command, stage=stage)
    except BootstrapCommandError as exc:
        if exc.kind == "exit" and "--no-index" in command:
            exc.args = (str(exc) + "\n本包需要完整的内置预编译 wheel；如果提示缺少依赖文件，请重新获取完整便携包。",)
        raise

def ensure_environment(uv: Path, python: Path, name: str, *, full_check=False):
    label = "桥接" if name == "bridge" else "AstrBot"
    startup_stage(label + "环境：读取依赖锁与已有环境")
    target = RUNTIME / (name + "-venv")
    marker = target / ".wechatbot-environment.json"
    lock = HERE / ("requirements-" + name + ".lock")
    if not lock.is_file():
        raise RuntimeError("交付包缺少已锁定依赖：" + lock.name)
    desired = {"root": str(ROOT.resolve()), "python": str(python), "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest()}
    current = load_json(marker)
    executable = target / "Scripts" / "python.exe"
    environment_reused = executable.is_file() and current == desired
    if target.exists() and current.get("root") and current.get("root") != desired["root"]:
        # Python venv launchers embed paths. Move only our marked, exact package-local env.
        if target.is_symlink() or not target.resolve().is_relative_to(RUNTIME.resolve()):
            raise RuntimeError("运行环境路径超出本包，拒绝修复。")
        backup = target.with_name(target.name + ".before-move-" + str(time.time_ns()))
        target.rename(backup)
        current = {}
    if not executable.is_file():
        print("阶段：%s环境：创建独立虚拟环境" % label, flush=True)
        run_checked([uv, "venv", "--python", python, "--no-config", target], stage=label + "环境：创建独立虚拟环境")
    if current != desired:
        print("阶段：%s环境：安装并核验锁定依赖" % label, flush=True)
        sync_environment_dependencies(uv, executable, lock, name)
        atomic_json(marker, desired)
    startup_stage(label + "环境：安装本包长路径兼容")
    install_long_path_hook(target)
    startup_stage(label + "环境：准备独立导入检查目录")
    smoke_env = import_check_environment(name)
    smoke = "import _wechatbot_long_paths; assert _wechatbot_long_paths.activate(); "
    smoke += "import requests,websockets,pygame,mutagen,uiautomation,pyperclip,PIL,pycaw,dashscope,cryptography,imageio_ffmpeg" if name == "bridge" else (
        "import astrbot,astrbot.cli,astrbot.api,anthropic; from Crypto.Cipher import AES; "
        "_aes_key=bytes.fromhex('000102030405060708090a0b0c0d0e0f'); "
        "_aes_plain=bytes.fromhex('00112233445566778899aabbccddeeff'); "
        "_aes_ciphertext=bytes.fromhex('69c4e0d86a7b0430d8cdb78070b4c55a'); "
        "assert AES.new(_aes_key,AES.MODE_ECB).encrypt(_aes_plain)==_aes_ciphertext; "
        "assert AES.new(_aes_key,AES.MODE_ECB).decrypt(_aes_ciphertext)==_aes_plain"
    )
    smoke_timeout = 180 if name == "astrbot" else 75
    smoke_marker = target / ".wechatbot-import-check.json"
    smoke_record = {"environment": desired, "probe_sha256": hashlib.sha256(smoke.encode()).hexdigest()}
    if name == "astrbot":
        # A successful full import is reusable for an unchanged environment.
        # Normal restarts only locate the SDK entry points and exercise AES;
        # importing astrbot.api again builds a disposable application instance.
        previous_smoke = {}
        if environment_reused and not full_check:
            try:
                previous_smoke = load_json(smoke_marker)
            except (OSError, ValueError):
                pass  # A disposable success cache is rebuilt by the full check.
        if environment_reused and not full_check and previous_smoke == smoke_record:
            quick_smoke = smoke.replace(
                "import astrbot,astrbot.cli,astrbot.api,anthropic; ",
                "import importlib.util; from pathlib import Path; "
                "_sdk=importlib.util.find_spec('astrbot'); "
                "assert _sdk and _sdk.submodule_search_locations; "
                "_sdk_root=Path(next(iter(_sdk.submodule_search_locations))); "
                "assert all((_sdk_root/part).exists() or (_sdk_root/(part+'.py')).is_file() for part in ('cli','api')); "
                "assert importlib.util.find_spec('anthropic'); ",
            )
            try:
                print("阶段：AstrBot环境：复用已验证环境，快速检查", flush=True)
                run_checked([executable, "-X", "utf8", "-B", "-c", quick_smoke],
                            env=smoke_env, capture=True, timeout=30, stage="AstrBot环境：快速检查")
                return executable
            except RuntimeError as quick_error:
                if isinstance(quick_error, BootstrapCommandError) and quick_error.kind != "exit":
                    raise
                print("AstrBot快速检查未通过，改为完整导入检查。", flush=True)
        # A failed or interrupted full check must never retain an older success.
        smoke_marker.unlink(missing_ok=True)
    try:
        print("阶段：%s环境：导入检查" % label, flush=True)
        run_checked([executable, "-X", "utf8", "-B", "-c", smoke], env=smoke_env, capture=True, timeout=smoke_timeout, stage=label + "环境：导入检查")
    except RuntimeError as first_error:
        if isinstance(first_error, BootstrapCommandError) and first_error.kind != "exit":
            raise
        print("%s环境导入检查未通过，正在按依赖锁修复。" % label, flush=True)
        if isinstance(first_error, BootstrapCommandError): print(str(first_error), flush=True)
        # A partial/missing library is repaired from the exact lock, in this venv only.
        try:
            sync_environment_dependencies(uv, executable, lock, name, reinstall=True)
            startup_stage(label + "环境：重装长路径兼容")
            install_long_path_hook(target)
            print("阶段：%s环境：修复后导入检查" % label, flush=True)
            run_checked([executable, "-X", "utf8", "-B", "-c", smoke], env=smoke_env, capture=True, timeout=smoke_timeout, stage=label + "环境：修复后导入检查")
        except Exception as repair_error:
            repair_error.previous_diagnostic = str(first_error) if isinstance(first_error, BootstrapCommandError) else diagnostic_excerpt(type(first_error).__name__ + ": " + str(first_error))
            raise
    if name == "astrbot":
        atomic_json(smoke_marker, smoke_record)
    return executable

def prepare_bridge_config():
    path = BRIDGE / "config.json"
    prepare_mutable_config(path)
    if not path.exists():
        atomic_json(path, load_json(BRIDGE / "config.example.json"))
    settings = load_json(path)
    # Resolve moved-package paths only when previously owned; leave user paths intact.
    state = load_json(RUNTIME / "package-state.json")
    old_root = state.get("package_root", "")
    changes = False
    for key in ("astrbot_attachments",):
        value = settings.get(key, "")
        if not value or old_root and value.startswith(old_root + os.sep):
            settings[key] = str(INSTANCE / "data" / "attachments")
            changes = True
    previous_ffmpeg = str(Path(old_root) / "portable" / "runtime" / "bin" / "ffmpeg.exe") if old_root else ""
    if not settings.get("ffmpeg_path") or previous_ffmpeg and os.path.normcase(os.path.normpath(settings["ffmpeg_path"])) == os.path.normcase(os.path.normpath(previous_ffmpeg)):
        settings["ffmpeg_path"] = str(RUNTIME / "bin" / "ffmpeg.exe")
        changes = True
    if changes: atomic_json(path, settings)
    atomic_json(RUNTIME / "package-state.json", {"package_root": str(ROOT), "bootstrap_schema": 1})
    return settings

def prepare_astrbot(python: Path, settings: dict):
    remember_diagnostic_secrets(settings)
    INSTANCE.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["ASTRBOT_ROOT"] = str(INSTANCE)
    # Ambient reset/initial credentials must never change an existing account.
    env.pop("ASTRBOT_DASHBOARD_INITIAL_PASSWORD", None)
    env.pop("ASTRBOT_DASHBOARD_RESET_PASSWORD", None)
    env.pop("ASTRBOT_RESET_DASHBOARD_PASSWORD", None)
    password_file = INSTANCE / "dashboard-login.txt"
    path = INSTANCE / "data" / "cmd_config.json"
    prepare_mutable_config(path)
    # The official CLI touches .astrbot *before* writing cmd_config.json. A failed
    # initialization must retry the CLI without deleting that marker or user data.
    first_initialization = not path.is_file()
    if first_initialization:
        if not password_file.exists():
            password = secrets.token_urlsafe(18)
            try:
                # Persist before CLI init; exclusive creation never replaces an
                # existing account password, including after a failed first run.
                with password_file.open("x", encoding="utf-8") as output:
                    output.write("AstrBot 用户名：astrbot\n首次登录密码：" + password + "\n请登录后更改密码。不要分享这个文件。\n")
            except FileExistsError:
                pass
        login_lines = password_file.read_text(encoding="utf-8-sig").splitlines()
        passwords = [line[len("首次登录密码："):] for line in login_lines if line.startswith("首次登录密码：")]
        if len(passwords) != 1 or not passwords[0] or len(passwords[0]) > 256:
            raise RuntimeError("AstrBot 初次配置缺失，现有登录口令文件无法核验；已保留它，不会生成替换口令。")
        password = passwords[0]
        remember_diagnostic_secrets({"password": password})
        env["ASTRBOT_DASHBOARD_INITIAL_PASSWORD"] = password
        print("阶段：AstrBot：初始化本包实例", flush=True)
        run_checked([python.with_name("astrbot.exe"), "init", "--yes"], cwd=INSTANCE, env=env, timeout=120, stage="AstrBot：初始化本包实例")
        if not path.is_file():
            raise RuntimeError("AstrBot 初始化命令已结束，但本包 cmd_config.json 仍缺失；首次口令及现有数据已保留。")
    elif not (INSTANCE / ".astrbot").is_file():
        # The official CLI reuses existing config and only restores its root
        # marker/directories. No initial/reset credential is supplied here.
        print("阶段：AstrBot：修复本包实例标记", flush=True)
        run_checked([python.with_name("astrbot.exe"), "init", "--yes"], cwd=INSTANCE, env=env, timeout=120, stage="AstrBot：修复本包实例标记")
        if not (INSTANCE / ".astrbot").is_file():
            raise RuntimeError("AstrBot 实例标记仍缺失；现有配置与密码已保留。")
    startup_stage("AstrBot：同步本包平台与模型配置")
    config = load_json(path)
    config.setdefault("config_version", 4)
    dashboard = config.setdefault("dashboard", {})
    if first_initialization:
        dashboard.update({"host": "127.0.0.1", "port": 6185, "enable": True})
    platform = {"id": PLATFORM_ID, "type": "aiocqhttp", "enable": True,
        "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 11229, "ws_reverse_token": ""}
    entries = config.setdefault("platform", [])
    entries[:] = [p for p in entries if p.get("id") != PLATFORM_ID] + [platform]
    provider_settings = config.setdefault("provider_settings", {})
    if first_initialization:
        provider_settings.update({"enable": True, "default_image_caption_provider_id": "", "display_reasoning_text": False})
        config.setdefault("platform_settings", {}).update({"enable_id_white_list": False, "reply_with_mention": False, "reply_with_quote": False})
        # Only a new isolated instance uses this as its default. The plugin selects
        # the owned provider per event, preserving other platforms' future defaults.
        agent = config.setdefault("agent_runner", {}).setdefault("config", {})
        agent.setdefault("model", {})["provider_id"] = PROVIDER_ID
        # The owned profile plugin performs semantic compression with output,
        # media and tool-budget checks. The SDK may only trim on later tool
        # iterations; a second summarizer must not run on the same request.
        agent["compression"] = {"max_turns": -1, "trim_turns": 1,
            "overflow_strategy": "truncate_by_turns", "instruction": "",
            "keep_recent_ratio": .15, "provider_id": "",
            "fallback_max_tokens": int(settings.get("main_llm_context_tokens", 128000))}
    plugin_source = HERE / "astrbot_plugin_wechatbot_profile"
    plugin_target = INSTANCE / "data" / "plugins" / "astrbot_plugin_wechatbot_profile"
    plugin_target.mkdir(parents=True, exist_ok=True)
    for source in plugin_source.iterdir():
        if source.is_file() and source.suffix in {".py", ".yaml", ".md"}:
            shutil.copyfile(source, plugin_target / source.name)
    profile_path = INSTANCE / "data" / "wechatbot-profile-location.json"
    atomic_json(profile_path, {"bridge_config": str(BRIDGE / "config.json"), "package_root": str(ROOT), "platform_id": PLATFORM_ID})
    # Actual provider contract lives in the bundled plugin policy, shared with tests.
    sys.path.insert(0, str(plugin_source))
    from policy import provider_config
    provider = provider_config(settings, PROVIDER_ID)
    providers = config.setdefault("provider", [])
    providers[:] = [p for p in providers if p.get("id") != PROVIDER_ID] + [provider]
    config.setdefault("provider_sources", [])
    atomic_json(path, config)
    return env

def verified_download(url, target, expected_sha):
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected_sha:
        return target
    if os.environ.get("WECHATBOT_OFFLINE") == "1": raise RuntimeError("离线包缺少或损坏：" + target.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=30) as response, partial.open("wb") as output:
        total = 0
        while chunk := response.read(1024*1024):
            total += len(chunk)
            if total > 250*1024*1024: raise RuntimeError("下载文件超过允许大小")
            digest.update(chunk); output.write(chunk)
    if digest.hexdigest() != expected_sha:
        partial.unlink(missing_ok=True)
        raise RuntimeError("下载文件 SHA-256 校验失败：" + target.name)
    os.replace(partial, target)
    return target

def prepare_ffmpeg(bridge_python: Path):
    manifest = load_json(HERE / "runtime-manifest.json")
    archive = verified_download(manifest["ffmpeg_url"], HERE / "vendor" / manifest["ffmpeg_archive"], manifest["ffmpeg_sha256"])
    directory = RUNTIME / "bin"
    directory.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            matches = [e for e in bundle.infolist() if e.filename.endswith("/bin/" + name)]
            if len(matches) != 1 or matches[0].file_size > 180*1024*1024: raise RuntimeError("FFmpeg 归档结构不匹配")
            raw = bundle.read(matches[0]); target = directory / name
            if not target.exists() or hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(raw).digest(): target.write_bytes(raw)
            run_checked([target, "-version"], capture=True, timeout=15, stage="FFmpeg：可执行文件检查")
    return directory

def native_diagnostics(bridge_python: Path):
    script = "import json; from pycaw.pycaw import AudioUtilities; print(json.dumps({'virtual_cable':any('CABLE' in (getattr(d,'FriendlyName','') or '').upper() for d in AudioUtilities.GetAllDevices())}))"
    try:
        result = run_checked([bridge_python, "-X", "utf8", "-c", script], capture=True, timeout=15, stage="语音：本机环境检查")
        cable = json.loads(result.stdout.strip()).get("virtual_cable", False)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        cable = False
    print("语音设备：" + ("已检测到虚拟声卡。" if cable else "未检测到 VB-Cable；文字、图片和控制面板可以启动，语音录制需要安装驱动并重启。"))
    return {"virtual_cable": cable, "voice_driver_url": "https://vb-audio.com/Cable/", "vc_runtime_url": "https://aka.ms/vs/17/release/vc_redist.x64.exe"}

def prepare_voice_driver(settings, diagnostics, action):
    """Requested voice can install a driver; cancellation keeps other services usable."""
    report = dict(diagnostics)
    if settings.get("voice_enabled") is not True or report.get("virtual_cable") or action not in {"all", "bridge", "restart"}:
        return report
    print("语音已启用但声卡缺失；打开官方安装器，请允许UAC并点击Install Driver。取消后其他功能仍继续启动。")
    executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    report["voice_driver_setup_requested"] = True
    try:
        run_checked([executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", HERE / "install_voice_driver.ps1"], timeout=900, stage="语音：官方驱动安装器")
        # Installer exit alone does not prove that Install Driver was clicked.
        report.update(voice_driver_setup_state="installer_closed", voice_restart_required=True)
        print("语音安装器已退出；如已完成安装，请按官方要求重启电脑后检查设备。当前继续启动文字、图片和面板。")
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        report.update(voice_driver_setup_state="cancelled_or_failed", voice_restart_required=False)
        print("语音驱动未完成安装；当前继续启动其他功能，稍后可双击安装语音驱动.bat。")
    atomic_json(RUNTIME / "voice-driver-setup.json", {"schema": 1, **report})
    return report

def stop_astrbot(bridge_python: Path, env: dict):
    """The base interpreter has no third-party modules; use the owned venv.

    The child still performs the PID/time/command/instance checks and graceful
    console shutdown. Pass the module directory as an argv value, never as code.
    """
    script = "import sys; sys.path.insert(0, sys.argv[1]); from astrbot_lifecycle import stop_owned; stop_owned()"
    run_checked([bridge_python, "-X", "utf8", "-B", "-c", script, HERE],
        env=env, timeout=40, stage="AstrBot：核验归属并正常退出")

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["all", "open", "bridge", "restart", "restart-astrbot", "prepare", "check"], default="all", nargs="?")
    args = parser.parse_args(argv)
    remember_diagnostic_secrets(os.environ)
    startup_stage("核验本包 uv 与 managed Python")
    uv, python = managed_paths()
    print("准备独立桥接环境…", flush=True)
    bridge_python = ensure_environment(uv, python, "bridge")
    print("准备独立 AstrBot 4.28.2 环境…", flush=True)
    astrbot_python = ensure_environment(uv, python, "astrbot", full_check=args.action in {"prepare", "check"})
    startup_stage("读取并适配现有桥接配置")
    settings = prepare_bridge_config()
    remember_diagnostic_secrets(settings)
    startup_stage("AstrBot：准备本包实例")
    env = prepare_astrbot(astrbot_python, settings)
    startup_stage("FFmpeg：核验本包运行器")
    ffmpeg_dir = prepare_ffmpeg(bridge_python)
    settings = load_json(BRIDGE / "config.json")
    if not settings.get("ffmpeg_path") or str(settings.get("ffmpeg_path")).startswith(str(RUNTIME)):
        settings["ffmpeg_path"] = str(ffmpeg_dir / "ffmpeg.exe")
        atomic_json(BRIDGE / "config.json", settings)
    env["PATH"] = str(ffmpeg_dir) + os.pathsep + env.get("PATH", "")
    env["WECHATBOT_PORTABLE_ROOT"] = str(ROOT)
    env["WECHATBOT_PORTABLE_BRIDGE_PYTHON"] = str(bridge_python)
    env["WECHATBOT_PORTABLE_ASTRBOT_PYTHON"] = str(astrbot_python)
    dashboard = load_json(INSTANCE / "data" / "cmd_config.json").get("dashboard", {})
    panel_port = dashboard.get("port", 6185)
    if isinstance(panel_port, bool) or not isinstance(panel_port, int) or not 1 <= panel_port <= 65535:
        raise ValueError("AstrBot 本包面板端口无效")
    env["WECHATBOT_PORTABLE_ASTRBOT_URL"] = "http://127.0.0.1:" + str(panel_port)
    env["WECHAT_BOT_NO_AUTO_INSTALL"] = "1"
    startup_stage("语音：核验本机环境与驱动")
    diagnostics = prepare_voice_driver(settings, native_diagnostics(bridge_python), args.action)
    startup_stage("保存本包环境验收结果")
    atomic_json(RUNTIME / "environment-report.json", {"schema": 1, "bridge_ready": True, "astrbot_ready": True,
        "python": "3.12.12", "astrbot": "4.28.2", "ffmpeg_ready": True, **diagnostics})
    if args.action in {"prepare", "check"}:
        print("本包环境准备完成。此模式不启动或修改微信、WeFlow。")
        return 0
    if args.action == "restart-astrbot":
        stop_astrbot(bridge_python, env)
        print("阶段：启动本包桥接、AstrBot 与控制面板", flush=True)
        return run_checked([bridge_python, "-X", "utf8", "-B", BRIDGE / "startup_launcher.py", "all"], cwd=BRIDGE, env=env, timeout=220, stage="启动本包桥接、AstrBot 与控制面板").returncode
    if args.action != "open":
        startup_stage("WeFlow：核验现有程序与桥接")
        print("阶段：WeFlow：核验现有程序与桥接", flush=True)
        from weflow_setup import ensure_weflow
        ensure_weflow(BRIDGE / "config.json", RUNTIME)
    print("阶段：启动本包桥接、AstrBot 与控制面板", flush=True)
    return run_checked([bridge_python, "-X", "utf8", "-B", BRIDGE / "startup_launcher.py", args.action], cwd=BRIDGE, env=env, timeout=220, stage="启动本包桥接、AstrBot 与控制面板").returncode

def bootstrap_entrypoint(argv=None):
    try:
        return main(argv)
    except Exception as exc:
        # Unexpected import/config/CLI failures deserve the same private-safe report.
        try:
            report = save_startup_failure(exc)
            print("启动未完成：\n" + report, flush=True)
            print("详细错误已保存：" + str(RUNTIME / "startup-error.txt"), flush=True)
        except OSError:
            if isinstance(exc, subprocess.TimeoutExpired):
                safe = "子进程超时（%s 秒），已停止等待。" % exc.timeout
                detail = combined_diagnostic_excerpt(exc.output, exc.stderr)
                if detail: safe += "\n" + detail
            else:
                safe = redact_diagnostic("%s：%s" % (type(exc).__name__, str(exc)))
            print("启动未完成：\n阶段：%s\n原因：%s\n无法写入本包诊断文件。" % (getattr(exc, "stage", STARTUP_STAGE), safe), flush=True)
        return 1

if __name__ == "__main__":
    raise SystemExit(bootstrap_entrypoint())
