"""Select local stickers from directory names and optional nested manifests."""
import json
import logging
import os
import random
import re
import threading
import time

import config

log = logging.getLogger("ob11-bridge")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_cache = {"dir": "", "expires": 0.0, "stamps": (), "manifest": {}, "files": (), "by_dir": {}}
_cache_lock = threading.RLock()


def _safe_image(root, value, relative_to=None):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        path = os.path.realpath(os.path.join(relative_to or root, value))
        inside = os.path.normcase(os.path.commonpath((root, path))) == os.path.normcase(root)
        if inside and path.lower().endswith(IMAGE_EXTS) and os.path.isfile(path):
            return path
    except (OSError, ValueError):
        pass
    return None


def _stamp(path):
    try:
        stat = os.stat(path)
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def _load_manifest(root, path, manifest):
    # A single malformed entry must not discard other keywords or nested lists.
    try:
        resolved = os.path.realpath(path)
        if os.path.normcase(os.path.commonpath((root, resolved))) != os.path.normcase(root):
            return
        if os.path.getsize(resolved) > 512 * 1024:
            raise ValueError("表情清单超过 512 KB")
        with open(resolved, encoding="utf-8-sig") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("表情清单需为关键词到文件列表的对象")
        for keyword, values in data.items():
            if not isinstance(keyword, str) or not keyword.strip():
                continue
            values = [values] if isinstance(values, str) else values
            if not isinstance(values, list):
                continue
            key = keyword.strip().casefold()
            for value in values:
                candidate = _safe_image(root, value, os.path.dirname(resolved))
                if candidate and candidate not in manifest.setdefault(key, []):
                    manifest[key].append(candidate)
    except (OSError, ValueError, TypeError) as error:
        log.warning("[sticker_selector] 清单无效 %s: %s", path, error)


def _ensure_cache(sticker_dir):
    """Return a coherent snapshot even if another thread selects a different root."""
    global _cache
    root = os.path.realpath(sticker_dir)
    now = time.monotonic()
    with _cache_lock:
        if _cache["dir"] == root and now < _cache["expires"] and all(_stamp(path) == stamp for path, stamp in _cache["stamps"]):
            return _cache
        files, known_files, manifest, by_dir, stamps = [], set(), {}, {}, []
        # Root mtime detects newly created/deleted children immediately. Nested
        # directory/file changes are refreshed within five seconds at most.
        stamps.append((root, _stamp(root)))
        try:
            for directory, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(directory, name))]
                manifest_path = os.path.join(directory, "manifest.json")
                # Include missing manifests in the signature so adding one is seen.
                stamps.append((manifest_path, _stamp(manifest_path)))
                if "manifest.json" in names:
                    _load_manifest(root, manifest_path, manifest)
                local = []
                for name in names:
                    candidate = _safe_image(root, name, directory)
                    if candidate and candidate not in known_files:
                        known_files.add(candidate)
                        files.append(candidate)
                        local.append(candidate)
                if local:
                    key = os.path.basename(directory).casefold()
                    by_dir.setdefault(key, []).extend(local)
        except OSError as error:
            log.warning("[sticker_selector] 扫描表情目录失败: %s", error)
        _cache = {"dir": root, "expires": now + 5, "stamps": tuple(stamps),
                  "manifest": {key: tuple(values) for key, values in manifest.items()},
                  "files": tuple(files), "by_dir": {key: tuple(values) for key, values in by_dir.items()}}
        return _cache


def _matches_keyword(keyword, key):
    """Exact labels win; phrase matches must carry a real, unnegated intent."""
    if keyword == key:
        return True
    if len(keyword) < 2 or len(key) < 2:
        return False
    for label, phrase in ((key, keyword), (keyword, key)):
        if re.fullmatch(r"[a-z0-9_ -]+", label):
            for match in re.finditer(r"(?<![a-z0-9])" + re.escape(label) + r"(?![a-z0-9])", phrase):
                if not re.search(r"(?:not|no|never|unhappy)\s+$", phrase[:match.start()]):
                    return True
        else:
            for match in re.finditer(re.escape(label), phrase):
                # '不开心' cannot match an '开心' sticker collection.
                if not re.search(r"不|别|非|不要|不是", phrase[max(0, match.start()-2):match.start()]):
                    return True
    return False


def select_sticker(keyword=""):
    """Choose only a labeled intent match; an unmatched request stays text."""
    if not config.STICKER_ENABLED:
        return None
    try:
        root = os.path.realpath(config.STICKER_DIR)
    except (OSError, ValueError, TypeError):
        return None
    if not os.path.isdir(root):
        return None
    snapshot = _ensure_cache(root)
    keyword = str(keyword or "").strip().casefold()
    if not keyword or keyword in {"表情", "表情包", "sticker", "stickers", "emoji", "随机", "随便"}:
        return None
    for collection in (snapshot["manifest"], snapshot["by_dir"]):
        exact = [path for path in collection.get(keyword, ()) if _safe_image(root, path)]
        if exact:
            return random.choice(list(dict.fromkeys(exact)))
        candidates = []
        for key, paths in collection.items():
            if _matches_keyword(keyword, key):
                candidates.extend(path for path in paths if _safe_image(root, path))
        if candidates:
            return random.choice(list(dict.fromkeys(candidates)))
    return None
