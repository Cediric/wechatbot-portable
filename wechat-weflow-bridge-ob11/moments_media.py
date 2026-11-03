"""Read one post's original media; native encrypted bytes use WeFlow's proxy."""
from __future__ import annotations
import ipaddress
import os
import socket
import tempfile
import time
from urllib.parse import urlsplit

import config
import media_util

DEFAULT_HOSTS = ("qpic.cn", "qlogo.cn", "weixin.qq.com")
MEDIA_HEADERS = {"User-Agent": "MicroMessenger Client", "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}


class MediaDownloadError(ValueError):
    """A safe status for the caller's per-post retry policy; never contains URLs."""
    def __init__(self, message, *, http_status=None):
        super().__init__(message)
        self.http_status = http_status


def _cancelled(cancelled):
    if cancelled and (cancelled.is_set() if hasattr(cancelled, "is_set") else cancelled()):
        raise ValueError("朋友圈媒体读取已取消")


def _resolved_url(url):
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError("朋友圈媒体地址无效")
    parsed = urlsplit(media_util.validate_http_url(url))
    host = parsed.hostname.lower().rstrip(".")
    if parsed.fragment or parsed.port not in (None, 80, 443):
        raise ValueError("朋友圈媒体地址端口或片段无效")
    allowed = getattr(config, "MOMENTS_MEDIA_ALLOWED_HOSTS", DEFAULT_HOSTS)
    if not isinstance(allowed, (list, tuple)) or any(not isinstance(item, str) for item in allowed):
        raise ValueError("朋友圈媒体域名白名单无效")
    if not any(host == item.lower().rstrip(".") or host.endswith("." + item.lower().rstrip(".")) for item in allowed if item and ":" not in item and "/" not in item):
        raise ValueError("朋友圈媒体域名未获准")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("朋友圈媒体不能使用 IP 地址")
    addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("朋友圈媒体地址解析到非公网地址")
    return parsed, addresses[0][4][0]


def validate_media_url(url):
    _resolved_url(url)
    return url


def _write_media(chunks, path, cancelled, *, deadline, limit, declared=""):
    if declared and (not declared.isdigit() or int(declared) > limit):
        raise ValueError("朋友圈媒体超过大小上限")
    total = 0
    with open(path, "xb") as stream:
        for chunk in chunks:
            _cancelled(cancelled)
            total += len(chunk)
            if total > limit or time.monotonic() > deadline:
                raise ValueError("朋友圈媒体读取超时或超过大小上限")
            stream.write(chunk)
    if total == 0:
        raise ValueError("朋友圈媒体为空")
    return total


def _download_native(url, path, key, cancelled, *, deadline):
    """WeFlow already implements the native SNS cipher; retain its uint64 key."""
    import http_util
    import requests
    _cancelled(cancelled)
    _resolved_url(url)  # The proxy must only receive an allowed original CDN URL.
    base = str(getattr(config, "WE_FLOW_BASE_URL", "")).rstrip("/")
    parsed = urlsplit(media_util.validate_http_url(base))
    if parsed.query or parsed.fragment:
        raise ValueError("WeFlow 地址不能含查询参数或片段")
    session = http_util.new_session(1)
    session.trust_env = False
    response = None
    try:
        response = session.get(base + "/api/v1/sns/media/proxy", params={
            "url": url, "key": str(key), "access_token": getattr(config, "ACCESS_TOKEN", "")},
            timeout=(3, min(15, max(.1, deadline-time.monotonic()))), stream=True, allow_redirects=False)
        if response.status_code not in (200, 206):
            reason = "WeFlow 原生朋友圈媒体下载 HTTP " + str(response.status_code)
            if response.status_code == 502:
                body = next(iter(response.iter_content(1024)), b"").decode("utf-8", "replace")
                if "无法识别图片格式" in body:
                    reason += "（WeFlow 无法解密图片，请检查朋友圈媒体适配）"
            raise MediaDownloadError(reason, http_status=response.status_code)
        return _write_media(response.iter_content(65536), path, cancelled, deadline=deadline,
            limit=min(media_util.media_limit(), 30*1024*1024), declared=response.headers.get("Content-Length", ""))
    except requests.RequestException as error:
        raise MediaDownloadError("WeFlow 原生朋友圈媒体连接失败（" + type(error).__name__ + "）") from None
    finally:
        if response is not None:
            response.close()
        session.close()


def _download(url, path, cancelled, *, deadline):
    import requests
    import urllib3
    _cancelled(cancelled)
    parsed, address = _resolved_url(url)
    limit = min(media_util.media_limit(), 30*1024*1024)
    # Pin the checked public IP: a second DNS lookup cannot rebind it to LAN.
    # TLS still verifies the original CDN hostname and uses it for SNI/Host.
    # Independent pool has no environment proxies, cookies or authorization.
    kwargs = {"port": parsed.port or (443 if parsed.scheme == "https" else 80), "retries": False,
              "timeout": urllib3.Timeout(connect=3, read=min(10,max(.1,deadline-time.monotonic())))}
    if parsed.scheme == "https":
        pool = urllib3.HTTPSConnectionPool(address, assert_hostname=parsed.hostname,
            server_hostname=parsed.hostname, cert_reqs="CERT_REQUIRED", ca_certs=requests.certs.where(), **kwargs)
    else:
        pool = urllib3.HTTPConnectionPool(address, **kwargs)
    response = None
    try:
        target = (parsed.path or "/") + ("?"+parsed.query if parsed.query else "")
        response = pool.request("GET", target, headers={"Host": parsed.netloc, **MEDIA_HEADERS}, redirect=False,
                                preload_content=False, retries=False)
        if response.status not in (200, 206):
            raise MediaDownloadError("朋友圈媒体下载 HTTP " + str(response.status), http_status=response.status)
        if str(response.headers.get("X-Enc", "")).strip() == "1":
            raise MediaDownloadError("朋友圈原媒体需要原生解密，但原动态未提供密钥")
        total = _write_media(response.stream(65536, decode_content=True), path, cancelled, deadline=deadline,
            limit=limit, declared=response.headers.get("Content-Length", ""))
    except urllib3.exceptions.HTTPError as error:
        raise MediaDownloadError("朋友圈媒体连接失败（" + type(error).__name__ + "）") from None
    finally:
        if response is not None:
            response.close()
            response.release_conn()
        pool.close()
    return total


def post_parts(post, *, cancelled=None):
    """Stateless media parts for this single post; inability to view stops it."""
    if not isinstance(post, dict) or post.get("identity_verified") is not True:
        raise ValueError("朋友圈媒体必须来自已核验的原动态")
    items = post.get("media", [])
    if not isinstance(items, list) or len(items) > 9 or post.get("has_media") and not items:
        raise ValueError("该朋友圈媒体地址不可用，不能假称已看过")
    if not items:
        return []
    from multimodal import media_parts
    root = os.path.join(config.DATA_DIR, "moments_media")
    os.makedirs(root, exist_ok=True)
    deadline = time.monotonic()+45
    total, result = 0, []
    with tempfile.TemporaryDirectory(prefix="post-", dir=root) as directory:
        for index, item in enumerate(items, 1):
            _cancelled(cancelled)
            if not isinstance(item, dict):
                raise ValueError("朋友圈媒体条目无效")
            if item.get("type") == "4":
                details = " ".join(value for value in (item.get("title"), item.get("description")) if value)
                result.append({"type": "text", "text": "这是一张分享卡片。仅可见卡片文字，未打开链接。 " + details})
                continue
            kind = "video" if str(item.get("type", "")).lower() in {"6", "video", "15"} else "image"
            url = item.get("url")
            if not url:
                # A thumbnail is not the original video; do not silently decide
                # on content that the primary model was unable to see.
                raise ValueError("朋友圈缺少原媒体地址")
            path = os.path.join(directory, str(index)+( ".mp4" if kind == "video" else ".image"))
            if item.get("key") not in (None, ""):
                total += _download_native(url, path, item["key"], cancelled, deadline=deadline)
            else:
                total += _download(url, path, cancelled, deadline=deadline)
            if total > 60*1024*1024:
                raise ValueError("本条朋友圈媒体总量超过 60 MB")
            result.append({"type": "text", "text": "本条朋友圈第 " + str(index) + " 个媒体（" + kind + "）"})
            result.extend(media_parts(path, kind=kind, cancelled=cancelled))
    _cancelled(cancelled)
    return result
