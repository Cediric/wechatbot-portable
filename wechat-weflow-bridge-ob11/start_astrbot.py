"""Start AstrBot through its documented CLI, preserving instance lock checks.

Reference: https://docs.astrbot.app/en/use/cli.html . AstrBot must already be
installed/initialized; this wrapper never imports private CLI startup functions.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def _find_astrbot_root():
    configured = os.environ.get("ASTRBOT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    portable = Path(__file__).resolve().parents[1] / "portable" / "runtime" / "astrbot-instance"
    if (portable / ".astrbot").is_file():
        return portable
    home = Path.home()
    for candidate in (home / "AstrBot", home / "astrbot", home / ".astrbot"):
        if candidate.is_dir():
            return candidate.resolve()
    return home / "AstrBot"


def find_astrbot_cli(python=None):
    configured = os.environ.get("ASTRBOT_CLI_EXECUTABLE")
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError("ASTRBOT_CLI_EXECUTABLE 指定的 CLI 不存在")
        return str(path)
    interpreter = Path(python or sys.executable).resolve()
    candidates = [interpreter.with_name("astrbot.exe"), interpreter.parent / "Scripts" / "astrbot.exe"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    # Explicit interpreter selection must not silently invoke another environment.
    if python is None:
        cli = shutil.which("astrbot")
        if cli:
            return cli
    raise FileNotFoundError("未找到官方 astrbot CLI；请在选定环境安装 AstrBot，或填写 astrbot_py.txt")


def build_command(port=None, python=None):
    command = [find_astrbot_cli(python), "run"]
    if port is not None:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("AstrBot WebUI 端口无效")
        command.extend(["--port", str(port)])
    return command


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--python", help="只探测该解释器对应的官方 CLI")
    args = parser.parse_args(argv)
    root = _find_astrbot_root()
    if not root.is_dir():
        print("AstrBot 工作目录不存在，请先在该目录执行官方 astrbot init，或设置 ASTRBOT_ROOT。")
        return 1
    try:
        command = build_command(args.port, args.python)
        print(f"[AstrBot] 使用官方 CLI：{command[0]}；工作目录：{root}")
        return subprocess.call(command, cwd=str(root), env=os.environ.copy())
    except (OSError, ValueError) as exc:
        print("AstrBot 未启动：" + str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
