"""Bounded HTTP downloads and owned temporary media."""
import math
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit
import config
import http_util

_LEASE_LOCK = threading.RLock()
_LEASED_FILES = {}


@contextmanager
def lease_file(path):
    """Keep an in-use local file out of this process's age-based cleanup."""
    key = os.path.normcase(os.path.realpath(os.fspath(path)))
    with _LEASE_LOCK:
        _LEASED_FILES[key] = _LEASED_FILES.get(key, 0) + 1
    try:
        yield path
    finally:
        with _LEASE_LOCK:
            count = _LEASED_FILES[key] - 1
            if count:
                _LEASED_FILES[key] = count
            else:
                del _LEASED_FILES[key]


def remove_cached_file(path, root, *, older_than=None):
    """Remove only an unleased regular file within its explicitly owned cache."""
    with _LEASE_LOCK:
        try:
            root = os.path.realpath(os.fspath(root))
            resolved = os.path.realpath(os.fspath(path))
            if (not os.path.isdir(root) or os.path.islink(path) or not os.path.isfile(path)
                    or os.path.commonpath((root, resolved)) != root or resolved == root
                    or _LEASED_FILES.get(os.path.normcase(resolved))):
                return False
            if older_than is not None and os.path.getmtime(path) >= older_than:
                return False
            os.unlink(path)
            return True
        except (OSError, ValueError, TypeError):
            return False


def media_limit():
    value = float(getattr(config, "MEDIA_MAX_MB", 30))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("附件大小上限无效")
    return int(value * 1024 * 1024)


def validate_http_url(url):
    parsed = urlsplit(str(url))
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("附件地址必须是无账号密码的 HTTP(S) 地址")
    return str(url)


def validate_image_file(path):
    """Check supported image bytes and decode before any desktop operation."""
    if not os.path.isfile(path) or not 0 < os.path.getsize(path) <= media_limit():
        raise ValueError("图片不存在、为空或超过大小限制")
    from PIL import Image
    try:
        with Image.open(path) as source:
            suffix = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp", "BMP": ".bmp"}.get(source.format)
            width, height = source.size
            if not suffix or width < 1 or height < 1 or width * height > 40_000_000:
                raise ValueError("图片格式不支持或像素超过 4000 万限制")
            source.verify()
        # JPEG.verify() checks headers only. Reopen and decode the first frame
        # to reject truncated scan data before it reaches an image picker.
        with Image.open(path) as source:
            source.load()
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("图片内容不完整或无法解码") from error
    return suffix


def response_bytes(response, max_bytes=None, timeout=60):
    """Bound decoded response chunks, including servers without Content-Length."""
    limit = media_limit() if max_bytes is None else int(max_bytes)
    if limit < 1:
        raise ValueError("附件大小上限无效")
    length = response.headers.get("Content-Length", "")
    if length:
        try:
            declared = int(length)
        except (TypeError, ValueError):
            raise ValueError("附件长度无效")
        if declared < 0 or declared > limit:
            raise ValueError("附件超过大小限制")
    data = bytearray()
    deadline = time.monotonic() + timeout
    for chunk in response.iter_content(65536):
        if time.monotonic() > deadline or len(data) + len(chunk) > limit:
            raise ValueError("附件超过大小或下载时限")
        data.extend(chunk)
    if not data:
        raise ValueError("附件为空")
    return bytes(data)


def write_bytes_atomic(output_path, data, max_bytes=None):
    """Leave existing destinations untouched if validation or staging fails."""
    limit = media_limit() if max_bytes is None else int(max_bytes)
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > limit:
        raise ValueError("附件为空、类型无效或超过大小限制")
    output_path = os.path.abspath(output_path)
    directory = os.path.dirname(output_path)
    os.makedirs(directory, exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix=".akasha-write-", suffix=".part", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(data)
            stream.flush()
        os.replace(staging, output_path)
        return output_path
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(staging):
            os.unlink(staging)

def download(url, directory, suffix=".bin", image=False):
    validate_http_url(url)
    if not isinstance(suffix, str) or not suffix.startswith(".") or any(c in suffix for c in "/\\"):
        raise ValueError("附件扩展名无效")
    os.makedirs(directory, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="media-", suffix=".part", dir=directory)
    final_path = path[:-5] + suffix
    limit = media_limit()
    try:
        with http_util.session().get(url, timeout=(5,30), stream=True) as response:
            response.raise_for_status()
            declared = int(response.headers.get("Content-Length", 0))
            if declared < 0 or declared > limit:
                raise ValueError("附件超过大小限制")
            total = 0
            with os.fdopen(fd, "wb") as f:
                fd = None
                deadline = time.monotonic()+60
                for chunk in response.iter_content(65536):
                    total += len(chunk)
                    if total > limit or time.monotonic() > deadline:
                        raise ValueError("附件超过大小或下载时限")
                    if chunk:
                        f.write(chunk)
        if not total:
            raise ValueError("附件为空")
        if image:
            final_path = path[:-5] + validate_image_file(path)
        os.replace(path, final_path)
        path = final_path
        return path
    except BaseException:
        if fd is not None:
            os.close(fd)
        if os.path.exists(path):
            os.unlink(path)
        raise

def prune_cache(directory, days=7, prefix=None):
    root = os.path.realpath(directory)
    cutoff = time.time()-max(1,days)*86400
    if not os.path.isdir(root):
        return
    with os.scandir(root) as entries:
        for entry in entries:
            try:
                if prefix is not None and not entry.name.startswith(prefix):
                    continue
                # The helper checks leases and removes under one lock shared
                # by age cleanup and callers that enforce a cache file count.
                remove_cached_file(entry.path, root, older_than=cutoff)
            except OSError:
                # Concurrent consumers/cleanup can remove a file between stat and unlink.
                continue
