#!/usr/bin/env python3
"""
cleanup_privacy.py — 开源发布前隐私清理脚本
============================================

删除运行过程中产生的敏感/临时文件：
- config.json（含 Token、wxid）
- bridge.log / bridge.pid
- data/（身份映射、记忆库、朋友圈历史、TTS 缓存）
- __pycache__ / *.pyc

用法：
    python scripts/cleanup_privacy.py [--dry-run]
"""

import argparse
import os
import shutil
import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_DIR = os.path.join(ROOT, "wechat-weflow-bridge-ob11")

TARGETS = [
    os.path.join(BRIDGE_DIR, "config.json"),
    os.path.join(BRIDGE_DIR, "bridge.log"),
    os.path.join(BRIDGE_DIR, "bridge.pid"),
    os.path.join(BRIDGE_DIR, "data"),
    os.path.join(BRIDGE_DIR, "third_party"),
]

GLOB_DIRS = ["__pycache__"]
GLOB_EXTS = [".pyc", ".pyo", ".db", ".sqlite", ".sqlite3"]


def remove_path(path, dry_run=False):
    path=os.path.realpath(path)
    if os.path.commonpath((os.path.realpath(ROOT),path))!=os.path.realpath(ROOT) or path==os.path.realpath(ROOT):
        raise ValueError("拒绝清理项目之外或项目根目录")
    if os.path.isfile(path):
        if dry_run:
            print(f"[dry-run] 删除文件: {path}")
        else:
            os.remove(path)
            print(f"已删除文件: {path}")
    elif os.path.isdir(path):
        if dry_run:
            print(f"[dry-run] 删除目录: {path}")
        else:
            shutil.rmtree(path, ignore_errors=True)
            print(f"已删除目录: {path}")


def walk_clean(root, dry_run=False):
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过 .git
        if ".git" in dirpath:
            continue
        for d in list(dirnames):
            if d in GLOB_DIRS:
                remove_path(os.path.join(dirpath, d), dry_run)
        for f in filenames:
            if f.startswith(("bridge.log","startup_bridge.log","startup_astrbot.log")) or any(f.endswith(ext) for ext in GLOB_EXTS):
                remove_path(os.path.join(dirpath, f), dry_run)


def main():
    parser = argparse.ArgumentParser(description="清理微信bot 隐私/运行数据")
    parser.add_argument("--dry-run", action="store_true", help="只列出将删除的内容，不实际删除")
    args = parser.parse_args()

    print("清理目标目录:", BRIDGE_DIR)
    for t in TARGETS:
        if os.path.exists(t):
            remove_path(t, args.dry_run)
    for t in glob.glob(os.path.join(BRIDGE_DIR,"bridge.log.*")):
        remove_path(t,args.dry_run)

    walk_clean(BRIDGE_DIR, args.dry_run)
    walk_clean(os.path.join(ROOT, "scripts"), args.dry_run)

    print("清理完成。" if not args.dry_run else "dry-run 结束，未删除任何内容。")


if __name__ == "__main__":
    main()
