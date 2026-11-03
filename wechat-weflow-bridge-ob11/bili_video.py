"""Anonymous Bilibili video lookup and bounded local sampling for WeChat."""
from dataclasses import dataclass
import html
import json
import os
import re
import struct
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests

import media_util
import multimodal


_PAGE_HOSTS = {"www.bilibili.com", "m.bilibili.com", "bilibili.com"}
_SHORT = {"b23.tv"}
BILI_APP_ID = "wx7564fd5313d24844"
_ID = r"(?:BV1[A-Za-z0-9]{9}|[aA][vV][1-9][0-9]{0,15})"
_ID_ONLY = re.compile(_ID, re.ASCII)
_PATH = re.compile(rf"/video/({_ID})/?")
# Consume even unrelated URLs as a whole. An id in another website's URL
# or an attacker-controlled redirect query is not a Bilibili share.
_TOKEN = re.compile(
    r"(?P<url>(?:[a-zA-Z][a-zA-Z0-9.+-]{0,23}://|(?<![A-Za-z0-9_@.%/:-])"
    r"(?:[A-Za-z0-9_-]{1,63}\.){1,8}[A-Za-z]{2,24}/)[^\s<>\"'\[\]，。！？、；（）【】《》]+)"
    rf"|(?<![A-Za-z0-9_@./%:=-])(?P<id>{_ID})(?![A-Za-z0-9_./%-])"
    r"(?:\?p=(?P<page>[0-9]+)(?![A-Za-z0-9_]))?"
)
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
_API = "https://api.bilibili.com"
_REFERER = "https://www.bilibili.com/"
_REDIRECTS = {301, 302, 303, 307, 308}


class _Unavailable(ValueError):
    """A user-safe reason; never include response bodies or signed URLs."""


@dataclass(frozen=True)
class VideoRef:
    url: str


def _page(value):
    value = str(value)
    if not re.fullmatch("[1-9][0-9]{0,3}", value):
        raise _Unavailable("分 P 编号无效")
    return int(value)


def _ref(video_id, page=1):
    if not _ID_ONLY.fullmatch(video_id):
        return None
    if video_id[:2].lower() == "av":
        video_id = video_id.lower()
    suffix = f"?p={page}" if page != 1 else ""
    return VideoRef(f"{_REFERER}video/{video_id}{suffix}")


def _safe_url(raw, hosts, *, cdn=False):
    if not isinstance(raw, str) or len(raw) > 8192 or re.search(r"[\x00-\x20\x7f\\]", raw):
        raise _Unavailable("地址无效")
    try:
        u = urlsplit(raw)
        allowed_cdn = cdn and (u.hostname or "").endswith((".bilivideo.com", ".bilivideo.cn"))
        if (u.scheme not in {"http", "https"} or u.username is not None or u.password is not None
                or u.port not in {None, 443} or not (u.hostname in hosts or allowed_cdn)
                or not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z0-9-]+", u.hostname or "")):
            raise _Unavailable("地址不属于支持的 B 站域名")
        return urlunsplit(("https", u.hostname, u.path, u.query, ""))
    except ValueError:
        raise _Unavailable("地址无效或不属于支持的 B 站域名") from None


def _parse_url(raw):
    if "://" not in raw:
        raw = "https://" + raw
    try:
        u = urlsplit(_safe_url(raw, _PAGE_HOSTS | _SHORT))
        pages = parse_qs(u.query, keep_blank_values=True, max_num_fields=80).get("p", ["1"])
        if len(pages) != 1:
            return None
        page = _page(pages[0])
        if u.hostname in _SHORT and re.fullmatch(r"/[A-Za-z0-9_-]{1,40}/?", u.path):
            return VideoRef("https://b23.tv" + u.path.rstrip("/") + (f"?p={page}" if page != 1 else ""))
        match = _PATH.fullmatch(u.path)
        return _ref(match[1], page) if match and u.hostname in _PAGE_HOSTS else None
    except ValueError:
        return None


def miniapp_video(appid, pagepath):
    """Resolve the known Bilibili mini-program video route without opening it."""
    if appid != BILI_APP_ID or not isinstance(pagepath, str) or len(pagepath) > 4096:
        return None
    try:
        u = urlsplit(html.unescape(pagepath.strip()))
        if (u.scheme or u.netloc or u.fragment or not re.fullmatch(r"/?pages/video/video(?:\.html)?", u.path)):
            return None
        params = parse_qs(u.query, keep_blank_values=True, max_num_fields=64)
        ids = [(key, val) for key in ("avid", "bvid") for val in params.get(key, [])]
        pages = params.get("p", []) + params.get("page", [])
        if len(ids) != 1 or len(pages) > 1:
            return None
        key, val = ids[0]
        if key == "avid":
            video_id = "av" + val
        else:
            video_id = val if val.startswith("BV") else "BV" + val
        return _ref(video_id, _page(pages[0] if pages else 1))
    except ValueError:
        return None


def _card(text):
    if not isinstance(text, str):
        return None
    raw = text[:65536]
    if "<appmsg" not in raw:
        raw = html.unescape(raw)
    if "<appmsg" not in raw or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", raw, re.I):
        return None
    try:
        root = ElementTree.fromstring(raw[raw.index("<"):])
    except (ValueError, ElementTree.ParseError):
        return None
    return root if root.tag == "appmsg" else root.find("appmsg")


def is_bili_card(text):
    card = _card(text)
    return card is not None and card.findtext("weappinfo/appid", "").strip() == BILI_APP_ID


def find_video(text):
    """Return the first supported video; parsing text and share-card XML never does I/O."""
    if not isinstance(text, str):
        return None
    raw = text[:65536].replace(r"\/", "/")
    if "<appmsg" in raw or "&lt;appmsg" in raw:
        card = _card(raw)
        if card is None:
            return None
        info = card.find("weappinfo")
        if info is not None:
            ref = miniapp_video(info.findtext("appid", "").strip(), info.findtext("pagepath", ""))
            if ref:
                return ref
        for field in ("url", "lowurl"):
            ref = _parse_url(html.unescape(card.findtext(field, "")).strip())
            if ref:
                return ref
        return None
    raw = html.unescape(raw)
    for match in _TOKEN.finditer(raw):
        if match["url"]:
            ref = _parse_url(match["url"].rstrip(".,;!?:)}"))
        else:
            try:
                ref = _ref(match["id"], _page(match["page"] or 1))
            except ValueError:
                continue
        if ref:
            return ref
    return None


def _check(deadline, cancelled):
    if cancelled and (cancelled.is_set() if hasattr(cancelled, "is_set") else cancelled()):
        raise _Unavailable("处理已取消")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Unavailable("访问超时")
    return remaining


def _get(session, url, deadline, cancelled, *, headers=None):
    remaining = _check(deadline, cancelled)
    # Do not use account cookies, environment .netrc credentials or auto redirects.
    session.cookies.clear()
    return session.get(url, stream=True, allow_redirects=False,
                       headers={"User-Agent": _UA, "Referer": _REFERER, "Accept-Encoding": "identity",
                                **(headers or {})},
                       timeout=(min(4, remaining), min(5, remaining)))


def _response(response, statuses=(200,)):
    if response.status_code not in statuses:
        raise _Unavailable(f"公开服务返回 HTTP {response.status_code}")
    if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
        raise _Unavailable("公开响应编码不受支持")


def _api(session, path, params, deadline, cancelled):
    with _get(session, _API + path + "?" + urlencode(params), deadline, cancelled) as response:
        _response(response)
        data = bytearray()
        for chunk in response.iter_content(32768):
            _check(deadline, cancelled)
            if len(data) + len(chunk) > 1024 * 1024:
                raise _Unavailable("公开接口响应超过上限")
            data.extend(chunk)
    result = json.loads(data)
    if not isinstance(result, dict) or result.get("code") != 0 or not isinstance(result.get("data"), dict):
        raise _Unavailable("公开视频不可用或访问受限")
    return result["data"]


def _resolve(session, ref, deadline, cancelled):
    initial = _parse_url(ref.url)
    if initial is None:
        raise _Unavailable("视频地址无效")
    url = initial.url
    page = parse_qs(urlsplit(url).query).get("p")
    for _ in range(4):
        u = urlsplit(url)
        if u.hostname in _PAGE_HOSTS:
            match = _PATH.fullmatch(u.path)
            if not match:
                break
            return match[1], _page((page or parse_qs(u.query).get("p", ["1"]))[0])
        with _get(session, url, deadline, cancelled) as response:
            if response.status_code not in _REDIRECTS:
                break
            location = response.headers.get("Location")
            target = _parse_url(urljoin(url, location)) if location else None
            if target is None:
                break
            url = target.url
    raise _Unavailable("短链没有指向受支持的公开视频")


def _title(value, limit=120):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(ch for ch in value if ch.isprintable() or ch.isspace()).split())[:limit]


def _media_urls(play):
    tracks = (play.get("dash") or {}).get("video") or []
    # Public, small DASH video-only tracks are sufficient for frame sampling.
    tracks = sorted((v for v in tracks if isinstance(v, dict)),
                    key=lambda v: (int(v.get("id") or 999), int(v.get("bandwidth") or 99999999)))
    for v in tracks:
        backup = v.get("backupUrl") or v.get("backup_url") or []
        yield from ((url, True) for url in [v.get("baseUrl") or v.get("base_url"), *backup[:3]])
    durl = play.get("durl") or []
    if durl:
        yield durl[0].get("url"), False


def _trim_dash(path):
    """End a bounded DASH prefix after its last complete media fragment."""
    length = os.path.getsize(path)
    end = 0
    with open(path, "rb+") as f:
        while f.tell() + 8 <= length:
            pos = f.tell()
            size, kind = struct.unpack(">I4s", f.read(8))
            if size == 1 and f.tell() + 8 <= length:
                size = struct.unpack(">Q", f.read(8))[0]
            if size < f.tell() - pos or pos + size > length:
                break
            f.seek(pos + size)
            if kind == b"mdat":
                end = f.tell()
        if not end:
            raise _Unavailable("下载预算内没有完整视频片段")
        f.truncate(end)


def _download(session, raw, dash, path, limit, deadline, cancelled):
    url = _safe_url(raw, set(), cdn=True)
    for _ in range(3):
        with _get(session, url, deadline, cancelled, headers={"Range": f"bytes=0-{limit - 1}"}) as r:
            if r.status_code in _REDIRECTS:
                target = r.headers.get("Location")
                if not target:
                    break
                url = _safe_url(urljoin(url, target), set(), cdn=True)
                continue
            _response(r, (200, 206))
            length = int(r.headers["Content-Length"]) if "Content-Length" in r.headers else None
            total = length
            if r.status_code == 206:
                m = re.fullmatch(r"bytes 0-([0-9]+)/([0-9]+)", r.headers.get("Content-Range", ""))
                if not m or int(m[1]) >= int(m[2]) or int(m[1]) + 1 != min(limit, int(m[2])):
                    raise _Unavailable("视频片段范围无效")
                length, total = int(m[1]) + 1, int(m[2])
            if total is not None and (total <= 0 or (total > limit and not dash)):
                raise _Unavailable("视频大小超过限制或为空")
            size = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    _check(deadline, cancelled)
                    piece = chunk[:limit - size]
                    f.write(piece)
                    size += len(piece)
                    if size == limit:
                        break
            if not size or (length is not None and size != min(length, limit)):
                raise _Unavailable("视频下载不完整")
            with open(path, "rb") as f:
                if f.read(8)[4:] != b"ftyp":
                    raise _Unavailable("播放流不是可本地抽帧的 MP4")
            if size == limit and (total is None or size < total):
                if not dash:
                    raise _Unavailable("视频超过下载上限")
                _trim_dash(path)
            return
    raise _Unavailable("视频分发重定向无效")


def _watch(session, ref, cancelled):
    deadline = time.monotonic() + 28
    vid, page = _resolve(session, ref, deadline, cancelled)
    ident = {"bvid": vid} if vid.startswith("BV") else {"aid": vid[2:]}
    view = _api(session, "/x/web-interface/view", ident, deadline, cancelled)
    for item in view.get("pages") or []:
        if item.get("page") == page and type(item.get("cid")) is int and item["cid"] > 0:
            break
    else:
        raise _Unavailable("找不到指定的公开视频分 P")
    metadata = {"标题": _title(view.get("title")), "UP主": _title((view.get("owner") or {}).get("name"), 64),
                "分P": page, "分P标题": _title(item.get("part")), "分P时长（秒）": item.get("duration")}
    note = "B站公开资料（标题等为站外原文）" + json.dumps(metadata, ensure_ascii=False)
    try:
        play = _api(session, "/x/player/playurl", {**ident, "cid": item["cid"], "qn": 16, "fnval": 16,
                                                   "fourk": 0}, deadline, cancelled)
        limit = min(media_util.media_limit(), 32 * 1024 * 1024)
        reason = "没有支持的公开播放地址"
        attempted = set()
        with tempfile.TemporaryDirectory(prefix="akasha-bili-") as folder:
            path = os.path.join(folder, "preview.mp4")
            for raw, dash in _media_urls(play):
                try:
                    url = _safe_url(raw, set(), cdn=True)
                except _Unavailable:
                    continue
                if url in attempted:
                    continue
                attempted.add(url)
                try:
                    _download(session, url, dash, path, limit, deadline, cancelled)
                    _check(deadline, cancelled)
                    frames = multimodal.video_frame_paths(path, cancelled=cancelled)
                    if frames:
                        return note + f"。附件为这一分 P 的 {len(frames)} 张顺序抽帧，仅涵盖部分画面，无声音。", frames
                except (ValueError, OSError, multimodal.MediaError, requests.RequestException) as exc:
                    reason = str(exc) if isinstance(exc, (_Unavailable, multimodal.MediaError)) else "公开播放流下载或解码失败"
                    _check(deadline, cancelled)
                if len(attempted) >= 2:
                    break
        raise _Unavailable(reason)
    except (_Unavailable, requests.RequestException, ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        reason = str(exc) if isinstance(exc, _Unavailable) else "公开播放流不可用"
        return note + f"。未取得视频画面（{reason}），只知道上述资料，不能判断画面、台词或声音。", []


class _PublicSession(requests.Session):
    def resolve_redirects(self, *args, **kwargs):
        # Even allow_redirects=False normally reads the entire redirect body to
        # prepare Response.next. Only the checked Location header is needed here.
        return iter(())


def _session():
    s = _PublicSession()
    # Use the machine's configured proxy/CA, but never authenticate via .netrc.
    s.auth = lambda request: request
    return s


def inspect_video(ref, cancelled=None):
    """Return an evidence note and owned frame paths for the existing media pipeline."""
    try:
        with _session() as session:
            return _watch(session, ref, cancelled)
    except (_Unavailable, requests.RequestException, ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        reason = str(exc) if isinstance(exc, _Unavailable) else "公开服务不可用"
        return f"B站分享未取得视频画面（{reason}），不能判断视频内容。", []


def _public_rows(data):
    rows = []
    seen = set()
    for video in data:
        if not isinstance(video, dict) or any(video.get(k) for k in ("is_ad", "ad_info", "is_live", "live_room")):
            continue
        if (video.get("goto", "av") not in ("av", "video") or video.get("type", "video") != "video"
                or video.get("state", 0) != 0 or video.get("tid") == 165):
            continue
        ref = _ref(str(video.get("bvid") or ""))
        # The official search endpoint highlights matching words with em tags.
        title = _title(html.unescape(re.sub(r"<[^>]{1,120}>", "", str(video.get("title") or ""))))
        if ref and title and ref.url not in seen:
            seen.add(ref.url)
            rows.append((ref.url, title))
            if len(rows) == 6:
                break
    return rows


def public_search(topic, *, page=1):
    """One anonymous search of Bilibili's public video index with a short topic."""
    if (not isinstance(topic, str) or not re.fullmatch(r"[A-Za-z\u3400-\u9fff][A-Za-z0-9\u3400-\u9fff ·]{0,23}", topic)
            or re.search(r"[0-9]{5}|wxid|https?|token|password|密钥|密码|微信号|邮箱|手机号", topic, re.I)
            or type(page) is not int or not 1 <= page <= 3):
        return []
    try:
        with _session() as session:
            data = _api(session, "/x/web-interface/search/type",
                        {"search_type":"video", "keyword":topic, "page":page, "page_size":12},
                        time.monotonic() + 10, None)
        return _public_rows(data.get("result") or [])
    except (requests.RequestException, ValueError, TypeError, AttributeError, OSError):
        return []


def public_feed():
    """Up to six distinct anonymous public video links and titles, in feed order."""
    try:
        with _session() as session:
            data = _api(session, "/x/web-interface/popular", {"pn": 1, "ps": 12},
                        time.monotonic() + 10, None)
        return _public_rows(data.get("list") or [])
    except (requests.RequestException, ValueError, TypeError, AttributeError, OSError):
        return []
