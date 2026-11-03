"""Bounded local media sampling for the main model. No caption-model calls."""
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import config

_PRUNE_LOCK = threading.Lock()
_PRUNE_TIMES = {}
_FRAME_NAME = re.compile(r"wechat_[0-9a-f]{32}_[0-9]{2}\.jpg\Z")
log = logging.getLogger("ob11-bridge")

class MediaError(RuntimeError):
    pass

def _cancel(cancelled):
    if cancelled and (cancelled.is_set() if hasattr(cancelled, "is_set") else cancelled()):
        raise MediaError("媒体处理已取消")

def _setting(name, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(getattr(config, name, default))))
    except (TypeError, ValueError):
        return default

def _input(path):
    value = Path(path).resolve(strict=True)
    limit = _setting("MEDIA_MAX_MB", 30, 1, 200)*1024*1024
    if not value.is_file() or not 0 < value.stat().st_size <= limit:
        raise MediaError("本条媒体不存在、为空或超过大小限制")
    return value

def _tools():
    configured = str(getattr(config, "FFMPEG_PATH", "") or "").strip()
    ffmpeg = Path(configured).resolve() if configured else Path(shutil.which("ffmpeg") or "")
    if not ffmpeg.is_file():
        raise MediaError("缺少 FFmpeg，请运行便携包环境检测或设置 FFmpeg 路径")
    ffprobe = ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe")
    if not ffprobe.is_file():
        found = shutil.which("ffprobe")
        if not found:
            raise MediaError("缺少 FFprobe，视频抽帧无法核验时长")
        ffprobe = Path(found).resolve()
    return str(ffmpeg), str(ffprobe)

def _run(arguments, cancelled, timeout):
    _cancel(cancelled)
    # Avoid unbounded communicate() buffers if a corrupt file produces repeated
    # decoder errors. Private temporary streams are capped while the child runs.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=output,
            stderr=errors, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic()+timeout
        try:
            while True:
                _cancel(cancelled)
                if any(os.fstat(stream.fileno()).st_size > 65536 for stream in (output, errors)):
                    raise MediaError("本条媒体解码输出超限")
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise MediaError("媒体抽帧超时")
                try:
                    process.wait(timeout=min(0.05, remaining))
                    if process.returncode or any(os.fstat(stream.fileno()).st_size > 65536 for stream in (output, errors)):
                        raise MediaError("本条媒体无法解码")
                    _cancel(cancelled)
                    output.seek(0)
                    return output.read(65536)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

def _frame_prefix():
    directory = Path(getattr(config, "ASTRBOT_ATTACHMENTS", "") or config.DATA_DIR)/"wechat_frames"
    directory.mkdir(parents=True, exist_ok=True)
    _prune_frame_cache(directory)
    return directory/("wechat_"+uuid.uuid4().hex+"_")

def _prune_frame_cache(directory, *, force=False):
    """Expire only owned frame JPEGs, preserving buffered, processing and durable references."""
    root = os.path.realpath(os.fspath(directory))
    with _PRUNE_LOCK:
        now = time.monotonic()
        if not force and root in _PRUNE_TIMES and now-_PRUNE_TIMES[root] < 300:
            return 0
        if len(_PRUNE_TIMES) >= 8:
            _PRUNE_TIMES.clear()
        _PRUNE_TIMES[root] = now
    try:
        import media_util
        # A buffer cannot release its lease between this outbox snapshot and
        # unlink: the shared lock also covers the durable-reference handoff.
        with media_util._LEASE_LOCK:
            box = getattr(sys.modules.get("state"), "outbox", None)
            protected = {os.path.normcase(os.path.realpath(p)) for p in box.referenced_files()} if box else set()
            days = _setting("CACHE_RETENTION_DAYS", 7, 1, 365)
            cutoff = time.time()-days*86400
            removed = 0
            with os.scandir(root) as entries:
                for entry in entries:
                    if not _FRAME_NAME.fullmatch(entry.name) or os.path.normcase(os.path.realpath(entry.path)) in protected:
                        continue
                    if media_util.remove_cached_file(entry.path, root, older_than=cutoff):
                        removed += 1
            return removed
    except Exception:
        # If a durable reference cannot be read, defer cleanup rather than guess.
        log.debug("抽帧缓存年龄清理暂缓", exc_info=True)
        return 0

def _remove(paths):
    for path in paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

def video_frame_paths(path, cancelled=None):
    """Sample evenly within the configured initial clip; preserve source identity."""
    source = _input(path)
    ffmpeg, ffprobe = _tools()
    raw = _run([ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "v:0",
        "-show_entries", "stream=codec_type,width,height,duration:format=duration", "-of", "json", str(source)], cancelled, 8)
    try:
        detail = json.loads(raw.decode("utf-8"))
        streams = detail["streams"]
        if len(streams) != 1 or streams[0].get("codec_type") != "video":
            raise ValueError()
        width, height = int(streams[0]["width"]), int(streams[0]["height"])
        duration = float(detail.get("format", {}).get("duration") or streams[0].get("duration"))
        if width < 1 or height < 1 or width*height > 40_000_000 or not math.isfinite(duration) or duration <= 0:
            raise ValueError()
    except (KeyError, ValueError, TypeError, UnicodeError):
        raise MediaError("视频尺寸或时长无法核验") from None
    count = _setting("CHAT_VIDEO_MAX_FRAMES", 4, 1, 8)
    clip = min(duration, _setting("CHAT_VIDEO_MAX_SECONDS", 120, 1, 600))
    # Force a first frame even for clips shorter than a frame sampling interval.
    interval = max(0.01, clip/count)
    prefix = _frame_prefix()
    pattern = str(prefix)+"%02d.jpg"
    paths = []
    try:
        _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file,pipe",
            "-i", str(source), "-t", str(clip), "-an", "-sn", "-dn", "-vf",
            f"fps=1/{interval:.6f}:start_time=0,scale=1280:1280:force_original_aspect_ratio=decrease",
            "-frames:v", str(count), "-q:v", "3", pattern], cancelled, 20)
        paths = [str(prefix)+f"{index:02d}.jpg" for index in range(1, count+1) if os.path.isfile(str(prefix)+f"{index:02d}.jpg")]
        if not paths:
            raise MediaError("视频未生成可用画面")
        from media_util import validate_image_file
        for frame in paths:
            _cancel(cancelled)
            validate_image_file(frame)
        return paths
    except BaseException:
        _remove(str(prefix)+f"{index:02d}.jpg" for index in range(1, count+1))
        raise

def sticker_frame_paths(path, cancelled=None):
    """Preserve a bounded animation's changing expression as 2–4 still frames."""
    from PIL import Image, ImageOps
    source = _input(path)
    paths = []
    with Image.open(source) as image:
        if image.width*image.height > 40_000_000:
            raise MediaError("表情像素超限")
        if not getattr(image, "is_animated", False):
            return [str(source)]
        if image.width*image.height > 4_000_000:
            raise MediaError("表情动画尺寸超限，请缩小后重发")
        deadline = time.monotonic()+4
        count = 1
        for index in range(1, 201):
            _cancel(cancelled)
            if time.monotonic() > deadline:
                raise MediaError("表情动画解码超时")
            try:
                image.seek(index)
                count = index+1
            except EOFError:
                break
        if count > 200:
            raise MediaError("表情动画过长，请缩短后重发")
        samples = min(4, count)
        indexes = sorted({round(index*(count-1)/(samples-1)) for index in range(samples)})
        prefix = _frame_prefix()
        try:
            for index, frame_number in enumerate(indexes, 1):
                _cancel(cancelled)
                if time.monotonic() > deadline:
                    raise MediaError("表情动画解码超时")
                image.seek(frame_number)
                picture = ImageOps.exif_transpose(image).convert("RGB")
                picture.thumbnail((1280, 1280))
                target = str(prefix)+f"{index:02d}.jpg"
                paths.append(target)
                with open(target, "xb") as stream:
                    picture.save(stream, format="JPEG", quality=85)
            _cancel(cancelled)
            return paths
        except BaseException:
            _remove(paths)
            raise

def media_parts(path, kind="image", cancelled=None):
    """Produce main-model OpenAI image parts, with local temporary frames cleaned."""
    import model_client
    if kind not in ("image", "video", "sticker"):
        raise MediaError("媒体类型无效")
    source = str(_input(path))
    _cancel(cancelled)
    frames = video_frame_paths(source, cancelled) if kind == "video" else sticker_frame_paths(source, cancelled) if kind == "sticker" else [source]
    try:
        parts = []
        for index, frame in enumerate(frames, 1):
            _cancel(cancelled)
            if len(frames) > 1 or kind == "video":
                parts.append({"type": "text", "text": f"{'视频' if kind == 'video' else '表情'}画面 {index}/{len(frames)}；抽帧只展示部分画面，未包含声音。"})
            parts.append(model_client.image_part(frame))
        _cancel(cancelled)
        return parts
    finally:
        _remove(frame for frame in frames if os.path.normcase(os.path.realpath(frame)) != os.path.normcase(os.path.realpath(source)))
