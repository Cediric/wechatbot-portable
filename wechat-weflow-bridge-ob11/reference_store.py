"""Local, opaque-ID reference images. Uploading here never contacts a model."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import uuid
from pathlib import Path

import config
import media_util

_LOCK = threading.RLock()
_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_BYTES = 20 * 1024 * 1024
_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "BMP": ".bmp", "WEBP": ".webp"}
_BUILTIN_ID = "bd3715ab591f1f3d95d11f23dbb2bad1"
_BUILTIN = {"id": _BUILTIN_ID, "name": "通用构图与配色", "note": "仅参考几何布局和低饱和配色，不用于人物身份或角色外貌。", "suffix": ".png", "width":512,"height":512,"bytes":3387,
            "sha256":"bd3715ab591f1f3d95d11f23dbb2bad1e7edf9c6940e50f7caff892160a4abd3", "builtin":True}


def _root():
    return Path(config.DATA_DIR).resolve() / "reference_images"


def _image(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= min(_MAX_BYTES, media_util.media_limit()):
        raise ValueError("参考图为空或超过 20 MB / 媒体上限")
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        suffix = _FORMATS.get(image.format)
        width, height = image.size
        if not suffix or not (240 <= width <= 8000 and 240 <= height <= 8000 and width * height <= 40_000_000 and 1 / 8 <= width / height <= 8):
            raise ValueError("参考图格式、像素或长宽比无效")
        if "A" in image.getbands() or "transparency" in image.info:
            raise ValueError("参考图必须是不透明图片")
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
    return suffix, width, height


def _paths(ref_id):
    if not isinstance(ref_id, str) or not _ID.fullmatch(ref_id):
        raise ValueError("参考图 ID 无效")
    if ref_id == _BUILTIN_ID:
        path = Path(__file__).resolve().parent / "assets" / "references" / "neutral-composition.png"
        if not path.is_file() or path.is_symlink():
            raise ValueError("内置参考图文件缺失")
        return None, path, dict(_BUILTIN)
    root = _root()
    metadata = root / (ref_id + ".json")
    if metadata.is_symlink():
        raise ValueError("参考图记录不能为链接")
    if not 0 < metadata.stat().st_size <= 4096:
        raise ValueError("参考图记录大小无效")
    record = json.loads(metadata.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("id") != ref_id or record.get("suffix") not in _FORMATS.values():
        raise ValueError("参考图记录损坏")
    path = root / (ref_id + record["suffix"])
    if path.is_symlink() or path.resolve().parent != root or not path.is_file():
        raise ValueError("参考图文件缺失或路径无效")
    return metadata, path, record


def upload(data: bytes, *, name="参考图", note=""):
    if not isinstance(name, str) or not isinstance(note, str) or len(name) > 120 or len(note) > 1000:
        raise ValueError("参考图名称或用途说明过长")
    suffix, width, height = _image(data)
    with _LOCK:
        root = _root()
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink():
            raise ValueError("参考图库不能为链接")
        if sum(1 for _ in root.glob("*.json")) >= 100:
            raise ValueError("参考图库最多保存 100 张图片")
        ref_id = uuid.uuid4().hex
        record = {"id": ref_id, "name": name.strip() or "参考图", "note": note.strip(),
                  "suffix": suffix, "width": width, "height": height, "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()}
        path = root / (ref_id + suffix)
        try:
            media_util.write_bytes_atomic(path, data, max_bytes=_MAX_BYTES)
            media_util.write_bytes_atomic(root / (ref_id + ".json"), json.dumps(record, ensure_ascii=False).encode("utf-8"), max_bytes=4096)
        except BaseException:
            if path.exists():
                path.unlink()
            raise
        return dict(record, value="ref:" + ref_id)


def list_images():
    with _LOCK:
        root = _root()
        if not root.exists():
            return [dict(_BUILTIN, value="ref:"+_BUILTIN_ID)]
        if root.is_symlink():
            raise ValueError("参考图库不能为链接")
        result = [dict(_BUILTIN, value="ref:"+_BUILTIN_ID)]
        for metadata in sorted(root.glob("*.json")):
            _, _, record = _paths(metadata.stem)
            result.append(dict(record, value="ref:" + record["id"]))
        return result


def resolve(value):
    if not isinstance(value, str) or not value.startswith("ref:"):
        raise ValueError("参考图引用无效")
    with _LOCK:
        _, path, record = _paths(value[4:])
        if not 0 < path.stat().st_size <= min(_MAX_BYTES, media_util.media_limit()):
            raise ValueError("参考图文件超过大小上限")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != record.get("sha256") or len(data) != record.get("bytes"):
            raise ValueError("参考图校验失败，请重新上传")
        _image(data)
        return str(path)


def read(ref_id):
    path = resolve("ref:" + ref_id)
    return Path(path).read_bytes(), {".jpg": "image/jpeg", ".png": "image/png", ".bmp": "image/bmp", ".webp": "image/webp"}[Path(path).suffix]


def remove(ref_id):
    with _LOCK:
        metadata, path, _ = _paths(ref_id)
        if metadata is None:
            raise ValueError("内置参考图无需删除，取消选中即可")
        path.unlink()
        metadata.unlink()
        return True
