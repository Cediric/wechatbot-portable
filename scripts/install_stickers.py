#!/usr/bin/env python3
"""
install_stickers.py — 安装本地表情包到微信bot
==============================================

用法：
    python scripts/install_stickers.py <表情包文件夹或zip> [包名]

说明：
- 支持从文件夹或 zip 导入图片（png/jpg/jpeg/gif/webp/bmp）
- 安装到 wechat-weflow-bridge-ob11/data/stickers/<包名>/
- 如果源目录里有 manifest.json，会一起复制
- 如果没有 manifest.json，会根据文件名自动生成关键词映射
"""

import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import PurePosixPath

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_DIR = os.path.join(ROOT, "wechat-weflow-bridge-ob11")
STICKER_ROOT = os.path.join(BRIDGE_DIR, "data", "stickers")

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def contained(root, path):
    root, path = os.path.realpath(root), os.path.realpath(path)
    if os.path.commonpath((root, path)) != root:
        raise ValueError("表情路径超出导入目录")
    return path


def pack_destination(name):
    if not name or name in (".", "..") or any(char in name for char in '/\\:<>"|?*') or any(ord(char)<32 for char in name) or name[-1] in (".", " "):
        raise ValueError("包名必须是一个有效的文件夹名称")
    if name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("包名不能是 Windows 保留名称")
    return contained(STICKER_ROOT, os.path.join(STICKER_ROOT, name))


def extract_zip(zip_path: str, dest: str):
    with zipfile.ZipFile(zip_path, "r") as zf:
        entries = zf.infolist()
        if len(entries) > 10000 or sum(item.file_size for item in entries) > 200 * 1024 * 1024:
            raise ValueError("表情压缩包超过文件数量或大小限制")
        seen = set()
        for item in entries:
            path = PurePosixPath(item.filename.replace("\\", "/"))
            if not path.parts or path.is_absolute() or any(part in ("..", "") or any(char in part for char in ':<>"|?*') or any(ord(char)<32 for char in part) for part in path.parts) or (item.external_attr >> 16) & 0xf000 == 0xa000:
                raise ValueError("压缩包包含无效路径或符号链接")
            target = contained(dest, os.path.join(dest, *path.parts))
            key = target.casefold()
            if key in seen:
                raise ValueError("压缩包包含重复目标路径")
            seen.add(key)
        for item in entries:
            zf.extract(item, dest)


def scan_images(folder: str) -> list[str]:
    result = []
    for root, _, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(IMAGE_EXTS):
                path = os.path.join(root, f)
                if not os.path.islink(path):
                    result.append(contained(folder, path))
    return result


def build_manifest_from_filenames(files: list[str], base: str) -> dict:
    manifest = {}
    for path in files:
        rel = os.path.relpath(path, base).replace("\\", "/")
        name = os.path.splitext(os.path.basename(path))[0]
        # 用文件名作为关键词，支持 "开心", "开心_角色", "开心-角色" 等
        keywords = [k.strip() for k in name.replace("-", "_").split("_") if k.strip()]
        for kw in keywords:
            manifest.setdefault(kw, [])
            if rel not in manifest[kw]:
                manifest[kw].append(rel)
    return manifest


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    src = os.path.abspath(sys.argv[1])
    pack_name = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(os.path.basename(src))[0]

    if not os.path.exists(src):
        print(f"源路径不存在: {src}")
        sys.exit(1)

    contained(ROOT, STICKER_ROOT)
    os.makedirs(STICKER_ROOT, exist_ok=True)
    dest = pack_destination(pack_name)
    os.makedirs(dest, exist_ok=True)

    # 如果是 zip，先解压到临时目录
    tmp = None
    if zipfile.is_zipfile(src):
        tmp_owner = tempfile.TemporaryDirectory(prefix=".import-", dir=STICKER_ROOT)
        tmp = tmp_owner.name
        extract_zip(src, tmp)
        src = tmp

    files = scan_images(src)
    if not files:
        print("没有找到图片文件。")
        if tmp:
            tmp_owner.cleanup()
        sys.exit(1)

    # 复制图片
    copied = 0
    for path in files:
        rel = os.path.relpath(path, src)
        target = contained(dest, os.path.join(dest, rel))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(path, target)
        copied += 1

    # 复制源 manifest（如果有）
    src_manifest = os.path.join(src, "manifest.json")
    if os.path.exists(src_manifest):
        shutil.copy2(src_manifest, os.path.join(dest, "manifest.json"))
        print(f"已复制 manifest.json")
    else:
        manifest = build_manifest_from_filenames(files, src)
        if manifest:
            with open(os.path.join(dest, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            print(f"已根据文件名生成 manifest.json（{len(manifest)} 个关键词）")

    if tmp:
        tmp_owner.cleanup()

    print(f"[OK] Installed {copied} stickers to: {dest}")
    print("表情清单已更新，微信bot 会在下次使用时重新读取。")


if __name__ == "__main__":
    main()
