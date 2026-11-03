"""
bootstrap.py — 环境自检与自动配置
==================================

在程序启动最早期调用，负责：
1. 检查 Python 版本
2. 检查并自动安装缺失的第三方依赖
3. 自动生成 config.json（从 config.example.json 复制）
4. 自动创建 data 等运行目录
5. 可选环境提示（如 VB-Cable 是否存在）

用法（main.py 最顶部）：
    import bootstrap
    bootstrap.ensure_environment()
"""

import importlib
import json
import os
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
EXAMPLE_FILE = os.path.join(BASE_DIR, "config.example.json")
DATA_DIR = os.path.join(BASE_DIR, "data")

# (import 名, pip 包名, 是否仅 Windows)
REQUIRED = [
    ("requests", "requests>=2.31,<3", False),
    ("websockets", "websockets>=12,<18", False),
    ("pygame", "pygame>=2.5,<3", False),
    ("mutagen", "mutagen>=1.47,<2", False),
    ("uiautomation", "uiautomation>=2.0.17,<3", True),
    ("pyperclip", "pyperclip>=1.8.2,<2", False),
    ("PIL", "Pillow>=10,<13", False),
    ("pycaw", "pycaw>=20240210,<20270000", True),
    ("dashscope", "dashscope>=1.26.7,<2", False),
    ("cryptography", "cryptography>=42,<51", False),
]

AUTO_INSTALL = os.environ.get("WECHAT_BOT_NO_AUTO_INSTALL", os.environ.get("AKASHA_NO_AUTO_INSTALL", "")).lower() not in ("1", "true", "yes")


# ============ 基础检查 ============

def check_python_version() -> bool:
    if sys.version_info < (3, 10):
        print(f"[bootstrap] Python 版本过低: {sys.version.split()[0]}，需要 3.10+")
        return False
    return True


def _is_missing(import_name: str, win_only: bool) -> bool:
    if win_only and sys.platform != "win32":
        return False
    try:
        importlib.import_module(import_name)
        return False
    except Exception:
        return True


def check_dependencies() -> list[str]:
    """返回缺失的 pip 包名列表。"""
    missing = []
    for import_name, pip_name, win_only in REQUIRED:
        if _is_missing(import_name, win_only):
            missing.append(pip_name)
    return missing


LOCAL_LIBS = os.path.join(BASE_DIR, "third_party")
if os.path.isdir(LOCAL_LIBS) and LOCAL_LIBS not in sys.path:
    sys.path.insert(0, LOCAL_LIBS)
    importlib.invalidate_caches()


def install_packages(packages: list[str]) -> bool:
    """尝试用当前 Python 的 pip 安装缺失包。

    先正常安装；如果因为权限/环境失败，自动改用 `--target` 安装到项目本地
    `third_party/` 目录，并加入 sys.path，避免需要管理员权限。
    """
    if not packages:
        return True
    if os.environ.get("WECHATBOT_PORTABLE_ROOT"):
        print("[bootstrap] 本包环境缺少依赖，请再次双击根目录一键启动，由锁定运行器修复。")
        return False
    print(f"[bootstrap] 检测到缺失依赖: {', '.join(packages)}")
    print("[bootstrap] 正在自动安装，请稍候...")

    # 方式一：正常 pip install
    cmd = [sys.executable, "-m", "pip", "install"] + packages
    try:
        proc = subprocess.run(cmd, timeout=300)
        if proc.returncode == 0:
            print("[bootstrap] 依赖安装完成")
            return True
        print(f"[bootstrap] 常规 pip 安装失败（退出码 {proc.returncode}），尝试本地安装...")
    except subprocess.TimeoutExpired:
        print("[bootstrap] 常规 pip 安装超时，尝试本地安装...")
    except Exception as e:
        print(f"[bootstrap] 常规 pip 安装异常: {e}，尝试本地安装...")

    # 方式二：安装到项目本地 third_party/，不需要管理员权限
    try:
        os.makedirs(LOCAL_LIBS, exist_ok=True)
        cmd_local = [sys.executable, "-m", "pip", "install", "--target", LOCAL_LIBS] + packages
        proc = subprocess.run(cmd_local, timeout=300)
        if proc.returncode == 0:
            sys.path.insert(0, LOCAL_LIBS)
            print(f"[bootstrap] 依赖已安装到本地目录: {LOCAL_LIBS}")
            return True
        print(f"[bootstrap] 本地安装也失败，退出码 {proc.returncode}")
        return False
    except subprocess.TimeoutExpired:
        print("[bootstrap] 本地安装超时")
        return False
    except Exception as e:
        print(f"[bootstrap] 本地安装异常: {e}")
        return False


def ensure_dependencies(auto_install: bool = True) -> bool:
    """自检依赖；缺失时自动安装。返回是否全部就绪。"""
    missing = check_dependencies()
    if not missing:
        return True

    if not auto_install or not AUTO_INSTALL:
        print(f"[bootstrap] 缺少依赖: {', '.join(missing)}")
        print("[bootstrap] 请手动运行: pip install " + " ".join(missing))
        return False

    ok = install_packages(missing)
    if not ok:
        print("[bootstrap] 自动安装失败，请手动安装后重试。")
        return False

    still_missing = check_dependencies()
    if still_missing:
        print(f"[bootstrap] 仍有依赖缺失: {', '.join(still_missing)}")
        return False
    return True


# ============ 配置文件 / 目录 ============

def ensure_config() -> bool:
    """没有 config.json 时从 config.example.json 自动生成。"""
    if os.path.exists(CONFIG_FILE):
        return True
    if os.path.exists(EXAMPLE_FILE):
        shutil.copyfile(EXAMPLE_FILE, CONFIG_FILE)
        print("[bootstrap] 未找到 config.json，已从 config.example.json 自动生成。")
        print("[bootstrap] 请打开 Web 面板「全部设置」填写配置。")
        return True
    print("[bootstrap] 缺少 config.json 且无 config.example.json")
    return False


def ensure_data_dirs() -> bool:
    """自动创建 data 等运行目录。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        return True
    except Exception as e:
        print(f"[bootstrap] 创建数据目录失败: {e}")
        return False


# ============ 可选环境提示 ============

def check_optional_environment():
    """检查可选项并给出提示，不阻断启动。"""
    # VB-Cable 检测（需要 pycaw 已安装）
    try:
        from pycaw.pycaw import AudioUtilities
        from pycaw.constants import DEVICE_STATE, EDataFlow
        devices = AudioUtilities.GetAllDevices(
            data_flow=EDataFlow.eRender.value,
            device_state=DEVICE_STATE.ACTIVE.value,
        )
        names = [getattr(d, "FriendlyName", "") or "" for d in devices]
        has_cable = any("CABLE" in n for n in names)
        if has_cable:
            print("[bootstrap] 检测到 VB-Cable 虚拟声卡")
        else:
            print("[bootstrap] 未检测到 VB-Cable（发语音功能需要时请安装 VB-Cable）")
    except Exception:
        print("[bootstrap] 未检测到 VB-Cable（未安装 pycaw 或无法枚举音频设备）")


def service_endpoints():
    """Inspect configured endpoints without importing the runtime or exposing tokens."""
    settings = {}
    if os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as stream:
            settings = json.load(stream)
        if not isinstance(settings, dict):
            raise ValueError("config.json 必须是配置对象")
    endpoints = []
    for name, key, default in (
        ("WeFlow", "weflow_base_url", "http://127.0.0.1:5031"),
        ("AstrBot", "astrbot_ob_url", "ws://127.0.0.1:11229/ws"),
    ):
        parsed = urlsplit(settings.get(key, default))
        if not parsed.hostname or parsed.scheme not in ("http", "https", "ws", "wss"):
            raise ValueError(f"{name} 连接地址无效")
        port = parsed.port or (443 if parsed.scheme in ("https", "wss") else 80)
        endpoints.append((name, parsed.hostname, port))
    return endpoints


def check_services():
    """检查 WeFlow / AstrBot 是否在线，给出明确提示（不阻断启动）。"""
    import socket

    def port_open(port: int, host: str = "127.0.0.1") -> bool:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except Exception:
            return False

    try:
        endpoints = service_endpoints()
    except (OSError, ValueError, TypeError):
        print("[bootstrap] 无法读取服务地址，请检查 config.json")
        return
    for name, host, port in endpoints:
        status = "端口可连接" if port_open(port, host) else "端口未连接"
        print(f"[bootstrap] {name} {status} ({host}:{port})；服务健康以面板连接状态为准")


# ============ 总入口 ============

def ensure_environment(auto_install: bool = True) -> bool:
    """完整环境自检 + 自动修复。返回是否就绪。"""
    ok = True

    if not check_python_version():
        ok = False

    if not ensure_config():
        ok = False

    if not ensure_data_dirs():
        ok = False

    if not ensure_dependencies(auto_install=auto_install):
        ok = False

    check_optional_environment()
    check_services()

    if ok:
        print("[bootstrap] 环境检查通过")
    else:
        print("[bootstrap] 环境检查未通过，请根据上方提示处理")
    return ok


if __name__ == "__main__":
    if ensure_environment():
        print("[bootstrap] 环境就绪")
    else:
        sys.exit(1)
