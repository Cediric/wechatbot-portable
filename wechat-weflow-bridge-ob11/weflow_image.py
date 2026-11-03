"""
weflow_image.py — 微信 V2 .dat 图片解码与缓存
==============================================
WeFlow 的 /api/v1/messages?media=1 对微信新版 V2 .dat 图片经常导不出来，
桥接改用本地解码：从微信 xwechat_files 附件目录找到 .dat，用派生出的
AES/XOR 密钥解密成 JPEG/PNG 并缓存到 data/wechat_images。

密钥派生算法（与 WeFlow / WeChatDaily 一致）：
  - wxid 目录后 4 位 hex == md5(str(uin))[:4]
  - xor_key = uin & 0xFF
  - aes_key = md5(str(uin) + cleaned_wxid).hexdigest()[:16]（ASCII 16 字节）
密钥派生较慢（单进程约 45 秒），首次在后台线程执行并缓存到
data/wechat_image_key.json；后续直接读缓存。
"""

import hashlib
import io
import json
import logging
import os
import struct
import threading
import time
import uuid
import re

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except Exception:  # pragma: no cover - 缺依赖时桥接仍可运行，只是不能解码图片
    Cipher = algorithms = modes = None

import config

log = logging.getLogger("ob11-bridge")

V2_MAGIC = bytes.fromhex("070856320807")
KEY_FILE = os.path.join(config.DATA_DIR, "wechat_image_key.json")
CACHE_DIR = os.path.join(config.DATA_DIR, "wechat_images")

_IMAGE_MAGICS = (
    b"\xff\xd8\xff",
    b"\x89\x50\x4e\x47",
    b"GIF",
    b"RIFF",
    b"wxgf",
)

_key_lock = threading.Lock()
_key_info = None
_key_derived = False
_key_account = ""
_weflow_meta = {"mtime": None, "attach": None, "wxid": ""}


# ---------- 路径定位 ----------

def _weflow_config_path() -> str:
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "weflow", "WeFlow-config.json") if appdata else ""


def _load_weflow_meta() -> tuple[str | None, str]:
    """读取 WeFlow-config.json，按 mtime 缓存 attach 路径和 wxid。"""
    cfg_path = _weflow_config_path()
    if not cfg_path or not os.path.exists(cfg_path):
        return None, ""
    try:
        mt = os.path.getmtime(cfg_path)
    except OSError:
        mt = -1
    if _weflow_meta["mtime"] == mt:
        return _weflow_meta["attach"], _weflow_meta["wxid"]
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        db_path = cfg.get("dbPath", "")
        wxid = (cfg.get("myWxid", "") or "").strip()
        attach = None
        if db_path and wxid:
            candidate = os.path.join(db_path, wxid, "msg", "attach")
            if os.path.isdir(candidate):
                attach = candidate
        _weflow_meta.update({"mtime": mt, "attach": attach, "wxid": wxid})
        return attach, wxid
    except Exception as e:
        log.warning(f"[weflow_image] 读取 WeFlow 配置失败: {e}")
        return None, ""


def get_account_attach_dir() -> str | None:
    """从 WeFlow-config.json 读取微信账号目录，返回 msg/attach 路径。"""
    attach, _ = _load_weflow_meta()
    return attach


# ---------- 模板与 XOR key ----------

def _iter_recent_dat(attach_dir: str, suffix: str = "_t.dat", max_files: int = 24):
    """只扫最近改过的若干子目录，避免全盘 glob。"""
    try:
        level1 = [e for e in os.scandir(attach_dir) if e.is_dir()]
    except OSError:
        return
    level1.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    n = 0
    for d1 in level1[:6]:
        try:
            level2 = [e for e in os.scandir(d1.path) if e.is_dir()]
        except OSError:
            continue
        level2.sort(key=lambda e: e.stat().st_mtime, reverse=True)
        for d2 in level2[:4]:
            img_dir = os.path.join(d2.path, "Img")
            if not os.path.isdir(img_dir):
                continue
            try:
                files = [f for f in os.scandir(img_dir) if f.name.endswith(suffix)]
            except OSError:
                continue
            files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            for f in files:
                yield f.path
                n += 1
                if n >= max_files:
                    return


def find_v2_templates(attach_dir: str, max_templates: int = 3) -> list[bytes]:
    """从 V2 _t.dat 提取 AES 密文模板（用于密钥验证）。"""
    out, seen = [], set()
    for f in _iter_recent_dat(attach_dir, "_t.dat", max_files=80):
        try:
            with open(f, "rb") as fp:
                data = fp.read(0x20)
        except OSError:
            continue
        if len(data) >= 0x1F and data[:6] == V2_MAGIC:
            ct = data[0xF:0x1F]
            if ct not in seen:
                seen.add(ct)
                out.append(ct)
                if len(out) >= max_templates:
                    return out
    return out


def derive_xor_key(attach_dir: str) -> int | None:
    """从 V2 _t.dat 末字节投票反推 xor_key（假设 JPEG EOI=0xD9）。"""
    keys = []
    for f in _iter_recent_dat(attach_dir, "_t.dat", max_files=24):
        try:
            with open(f, "rb") as fp:
                head = fp.read(6)
                if head != V2_MAGIC:
                    continue
                fp.seek(0, os.SEEK_END)
                if fp.tell() < 7:
                    continue
                fp.seek(-1, os.SEEK_END)
                last = fp.read(1)
            if len(last) == 1:
                keys.append(last[0] ^ 0xD9)
        except (OSError, ValueError):
            continue
    if not keys:
        return None
    return max(set(keys), key=keys.count)


def _aes_matches(aes_bytes: bytes, ciphertext: bytes) -> bool:
    if Cipher is None:
        return False
    try:
        cipher = Cipher(algorithms.AES(aes_bytes), modes.ECB())
        decryptor = cipher.decryptor()
        dec = decryptor.update(ciphertext) + decryptor.finalize()
    except Exception:
        return False
    return any(dec.startswith(m) for m in _IMAGE_MAGICS)


def _clean_wxid(wxid: str) -> str:
    wxid = (wxid or "").strip()
    if wxid.lower().startswith("wxid_"):
        # wxid_xxx_后四位  → wxid_xxx
        return wxid.split("_")[0] + "_" + wxid.split("_")[1] if wxid.count("_") >= 2 else wxid
    return wxid


def _brute_force_key(xor_key: int, suffix: str, wxid_try: str, templates: list[bytes]):
    """单进程枚举 uin，约 30~60 秒，命中后返回 (uin, aes_key)。"""
    suffix_bytes = bytes.fromhex(suffix)
    wxid_bytes = wxid_try.encode("ascii")
    total = 1 << 24
    md5 = hashlib.md5
    for i in range(total):
        uin = (i << 8) | xor_key
        uin_bytes = str(uin).encode("ascii")
        if md5(uin_bytes).digest()[:2] != suffix_bytes:
            continue
        aes_hex = md5(uin_bytes + wxid_bytes).hexdigest()[:16]
        aes_bytes = aes_hex.encode("ascii")
        if all(_aes_matches(aes_bytes, ct) for ct in templates):
            return uin, aes_hex
    return None


def derive_and_cache_key(attach_dir: str, wxid_full: str) -> bool:
    """派生 V2 图片密钥并缓存。成功返回 True。"""
    global _key_info, _key_derived
    templates = find_v2_templates(attach_dir)
    if not templates:
        log.warning("[weflow_image] 没有 V2 .dat 模板，无法派生图片密钥")
        return False
    xor_key = derive_xor_key(attach_dir)
    if xor_key is None:
        log.warning("[weflow_image] 无法从 .dat 反推 xor_key")
        return False
    suffix = None
    parts = wxid_full.rsplit("_", 1)
    if len(parts) == 2 and len(parts[1]) == 4 and all(c in "0123456789abcdefABCDEF" for c in parts[1]):
        suffix = parts[1].lower()
    if not suffix:
        log.warning(f"[weflow_image] wxid 目录缺少 4 位 hex 后缀: {wxid_full}")
        return False
    for wxid_try in (_clean_wxid(wxid_full), wxid_full):
        log.info(f"[weflow_image] 开始派生 V2 图片密钥（wxid={wxid_try}，约 30~60 秒）...")
        t0 = time.time()
        found = _brute_force_key(xor_key, suffix, wxid_try, templates)
        log.info(f"[weflow_image] 枚举耗时 {time.time() - t0:.1f}s")
        if found:
            uin, aes_key = found
            if _get_weflow_wxid() != wxid_full:
                log.warning("[weflow_image] 账号已切换，丢弃旧账号密钥派生结果")
                return False
            _key_info = {"uin": uin, "xor_key": xor_key, "aes_key": aes_key, "wxid": wxid_try}
            try:
                os.makedirs(config.DATA_DIR, exist_ok=True)
                temporary = KEY_FILE + "." + uuid.uuid4().hex + ".tmp"
                try:
                    with open(temporary, "x", encoding="utf-8") as f:
                        json.dump(_key_info, f, ensure_ascii=False, indent=2)
                    os.replace(temporary, KEY_FILE)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                log.info(f"[weflow_image] V2 图片密钥已缓存: {KEY_FILE}")
            except Exception as e:
                log.warning(f"[weflow_image] 缓存密钥失败: {e}")
            _key_derived = True
            return True
    return False


def get_key_info():
    """返回 (xor_key, aes_key)；缓存缺失时触发一次后台派生。"""
    global _key_info, _key_derived, _key_account
    with _key_lock:
        current_account = _get_weflow_wxid()
        if current_account != _key_account:
            _key_account = current_account
            _key_info, _key_derived = None, False
        if _key_info is not None:
            return _key_info["xor_key"], _key_info["aes_key"]
        if _key_derived:
            return None, None
        if os.path.exists(KEY_FILE):
            try:
                with open(KEY_FILE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if (current_account and cached.get("wxid") in (current_account, _clean_wxid(current_account))
                        and isinstance(cached.get("xor_key"), int) and 0 <= cached["xor_key"] <= 255
                        and isinstance(cached.get("aes_key"), str) and len(cached["aes_key"]) == 16):
                    _key_info = cached
                    return cached["xor_key"], cached["aes_key"]
            except Exception as e:
                log.warning(f"[weflow_image] 读取密钥缓存失败: {e}")
        _key_derived = True  # 只尝试一次自动派生
        attach = get_account_attach_dir()
        wxid_full = _get_weflow_wxid()
        if attach and wxid_full:
            threading.Thread(target=derive_and_cache_key, args=(attach, wxid_full), daemon=True).start()
        return None, None


def _get_weflow_wxid() -> str:
    _, wxid = _load_weflow_meta()
    return wxid


# ---------- V2 解码 ----------

def decode_v2(data: bytes, xor_key: int, aes_key_ascii: str) -> bytes:
    if len(data) < 15 or data[:6] != V2_MAGIC:
        raise ValueError("不是 V2 .dat 文件")
    aes_size = struct.unpack_from("<L", data, 6)[0]
    xor_size = struct.unpack_from("<L", data, 10)[0]
    offset = 15
    # WeFlow's decryptDatV4WithJs uses a full PKCS7 block even for aligned data:
    # https://github.com/hicccc77/WeFlow/blob/main/electron/services/imageDecryptService.ts
    aligned = (aes_size // 16 + 1) * 16
    if aligned + xor_size > len(data) - offset:
        raise ValueError("V2 AES/XOR 声明长度超过实际数据")
    if not isinstance(xor_key, int) or not 0 <= xor_key <= 255:
        raise ValueError("无效的 XOR 密钥")
    aes_data = data[offset:offset + aligned]
    if Cipher is None:
        raise RuntimeError("缺少 cryptography 依赖，无法解码 V2 图片")
    key = aes_key_ascii.encode("ascii")
    if len(key) != 16:
        raise ValueError("AES 密钥必须为 16 字节")
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    dec = decryptor.update(aes_data) + decryptor.finalize()
    pad = dec[-1]
    if not 1 <= pad <= 16 or not dec.endswith(bytes([pad]) * pad):
        raise ValueError("V2 AES 填充无效")
    dec = dec[:-pad]
    if len(dec) != aes_size:
        raise ValueError("V2 AES 原文长度与声明不符")
    offset += aligned
    raw_size = len(data) - offset - xor_size
    raw_data = data[offset:offset + raw_size]
    offset += raw_size
    xor_data = data[offset:]
    dec_xor = bytes(b ^ xor_key for b in xor_data)
    return dec + raw_data + dec_xor


def _verified_image_ext(data: bytes) -> str:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError("解码图片像素数量超出上限")
        ext = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp", "BMP": ".bmp"}.get(image.format)
        if not ext:
            raise ValueError("解码图片格式不支持")
        image.verify()
    # JPEG verify alone does not decode its scan data. Require a readable image.
    with Image.open(io.BytesIO(data)) as image:
        image.load()
    return ext


def _guess_ext(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:4] == b"RIFF":
        return ".webp"
    if data[:4] == b"wxgf":
        return ".wxgf"
    return ".bin"


def find_latest_v2_dat(attach_dir: str) -> str | None:
    """只扫最近改过的若干子目录，避免全盘 glob。"""
    try:
        level1 = [e for e in os.scandir(attach_dir) if e.is_dir()]
    except OSError:
        return None
    level1.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    best = None
    best_mtime = 0.0
    for d1 in level1[:6]:
        try:
            level2 = [e for e in os.scandir(d1.path) if e.is_dir()]
        except OSError:
            continue
        level2.sort(key=lambda e: e.stat().st_mtime, reverse=True)
        for d2 in level2[:4]:
            img_dir = os.path.join(d2.path, "Img")
            if not os.path.isdir(img_dir):
                continue
            try:
                for f in os.scandir(img_dir):
                    name = f.name
                    if not name.endswith(".dat"):
                        continue
                    mt = f.stat().st_mtime
                    # 优先缩略图 _t.dat
                    score = mt + (1000 if name.endswith("_t.dat") else 0)
                    if score > best_mtime:
                        best_mtime = score
                        best = f.path
            except OSError:
                continue
    return best


def find_dat_for_message(attach_dir: str, talker: str, message: dict) -> str | None:
    """Resolve only message-bound files inside the account attachment root.

    WeFlow's exact local path is acceptable evidence. Hash-based discovery additionally
    requires a talker directory so identical/global latest files cannot cross chats.
    """
    attach_path = os.path.realpath(attach_dir)
    attach_real = os.path.normcase(attach_path)
    def inside(path):
        try:
            return os.path.commonpath((attach_real, os.path.normcase(os.path.realpath(path)))) == attach_real
        except ValueError:
            return False
    def unredirected(path):
        return os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.abspath(path))
    for key in ("datPath", "localPath", "filePath", "imagePath", "mediaPath"):
        value = message.get(key)
        if not isinstance(value, str) or not value:
            continue
        candidate = os.path.realpath(value if os.path.isabs(value) else os.path.join(attach_dir, value))
        if inside(candidate) and candidate.lower().endswith(".dat") and os.path.isfile(candidate):
            return candidate
    hashes = {str(message.get(k, "")).lower() for k in ("md5", "imageMd5", "mediaMd5")
              if re.fullmatch(r"[a-fA-F0-9]{32}", str(message.get(k, "")))}
    if not hashes or not talker:
        return None
    talker_names = {talker, hashlib.md5(talker.encode("utf-8")).hexdigest()}
    roots = [os.path.join(attach_path, name) for name in talker_names
             if os.path.basename(name) == name and inside(os.path.join(attach_path, name))
             and unredirected(os.path.join(attach_path, name)) and os.path.isdir(os.path.join(attach_path, name))]
    matches = []
    for root in roots:
        for directory, directories, names in os.walk(root):
            directories[:] = [name for name in directories
                              if inside(os.path.join(directory, name)) and unredirected(os.path.join(directory, name))]
            for name in names:
                stem = name[:-4] if name.lower().endswith(".dat") else ""
                if stem.endswith(("_t", "_h")):
                    stem = stem[:-2]
                if stem.lower() in hashes:
                    candidate = os.path.join(directory, name)
                    if inside(candidate) and unredirected(candidate) and os.path.isfile(candidate):
                        matches.append(os.path.realpath(candidate))
    if not matches:
        return None
    matches.sort(key=lambda path: (path.endswith("_t.dat"), path.endswith("_h.dat")))
    return matches[0]


def decode_latest_for_talker(talker: str, message: dict | None = None) -> str | None:
    """Compatibility name; decode the original message only, otherwise fail safely."""
    attach = get_account_attach_dir()
    if not attach:
        log.warning("[weflow_image] 找不到微信 attach 目录")
        return None
    if not message:
        log.warning("[weflow_image] 无原始媒体标识，拒绝猜测最新图片")
        return None
    message_talker = message.get("sessionId") or message.get("talkerId")
    if message_talker and str(message_talker) != str(talker):
        return None
    dat_path = find_dat_for_message(attach, talker, message)
    if not dat_path:
        log.warning("[weflow_image] 没有可明确归属本条消息的 .dat 文件")
        return None
    xor_key, aes_key = get_key_info()
    if xor_key is None or not aes_key:
        log.warning("[weflow_image] 缺少 V2 图片密钥（缓存未命中，派生中或失败）")
        return None
    try:
        max_bytes = int(getattr(config, "MEDIA_MAX_MB", 30) * 1024 * 1024)
        if os.path.getsize(dat_path) > max_bytes:
            return None
        with open(dat_path, "rb") as f:
            data = f.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None
        decoded = decode_v2(data, xor_key, aes_key)
        ext = _verified_image_ext(decoded)
        os.makedirs(CACHE_DIR, exist_ok=True)
        out = os.path.join(CACHE_DIR, f"wechat_{uuid.uuid4().hex}{ext}")
        with open(out, "xb") as f:
            f.write(decoded)
        log.info(f"[weflow_image] 已解码缓存微信图片: {out} (from {os.path.basename(dat_path)})")
        return out
    except Exception as e:
        log.warning(f"[weflow_image] 解码失败: {e}")
        return None
