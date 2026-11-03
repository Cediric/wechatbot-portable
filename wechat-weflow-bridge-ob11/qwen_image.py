"""Qwen Image 2.1/3.0 synchronous DashScope contract, with no paid retries.

Reference bytes are decoded locally before the prompt specialist is called.
This module never logs provider bodies, API keys, or signed result URLs.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import time
from urllib.parse import unquote, urlsplit

import config
import http_util
import media_util

MODELS = frozenset({"qwen-image-2.1-pro", "qwen-image-2.1-turbo", "qwen-image-3.0-pro", "qwen-image-3.0"})
MAX_REFERENCE_BYTES = 10 * 1024 * 1024
MAX_PROMPT_TOKENS = 4500
_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "BMP": "image/bmp",
         "TIFF": "image/tiff", "WEBP": "image/webp", "GIF": "image/gif"}


class ImageError(ValueError):
    pass


def _transport_error(error):
    """Keep useful TLS reasons without echoing URLs, credentials or bodies."""
    kind = type(error).__name__
    message = str(error).upper()
    reasons = (
        ("CERTIFICATE_VERIFY_FAILED", "服务器证书校验失败"),
        ("UNEXPECTED_EOF_WHILE_READING", "服务器或代理提前断开 TLS 连接"),
        ("WRONG_VERSION_NUMBER", "TLS 或代理协议不匹配"),
        ("TLSV1_ALERT_PROTOCOL_VERSION", "对端不接受当前 TLS 协议"),
        ("UNSUPPORTED_PROTOCOL", "TLS 协议不受支持"),
        ("SSLV3_ALERT_HANDSHAKE_FAILURE", "对端拒绝 TLS 握手"),
        ("TLSV1_ALERT_HANDSHAKE_FAILURE", "对端拒绝 TLS 握手"),
        ("TLSV1_ALERT_INTERNAL_ERROR", "对端 TLS 内部错误"),
        ("EOF OCCURRED IN VIOLATION OF PROTOCOL", "服务器或代理提前断开 TLS 连接"),
        ("TLS/SSL CONNECTION HAS BEEN CLOSED", "TLS 连接已被对端关闭"),
    )
    for code, explanation in reasons:
        if code in message:
            return f"{kind}（{code}：{explanation}）"
    # CPython/urllib3 may expose an OpenSSL reason which is absent from the
    # translated list. Keep the structured code, never the URL-bearing wrapper.
    openssl = re.search(r"\[SSL:\s*([A-Z][A-Z0-9_]{2,63})\]", message)
    if openssl:
        return f"{kind}（OpenSSL：{openssl[1]}）"
    if kind == "SSLError":
        return kind + "（TLS 连接失败，尚未解析出具体原因）"
    if kind == "ProxyError":
        return kind + "（连接代理失败）"
    if kind == "ConnectTimeout":
        return kind + "（建立连接超时）"
    if kind == "ReadTimeout" or "READ TIMED OUT" in message:
        return kind + "（等待服务端响应超时）"
    for markers, reason in (
        (("NAMERESOLUTIONERROR", "GETADDRINFO FAILED", "FAILED TO RESOLVE"), "域名解析失败"),
        (("WINERROR 10061", "CONNECTION REFUSED"), "目标端口拒绝连接"),
        (("WINERROR 10054", "CONNECTIONRESETERROR", "CONNECTION RESET BY PEER"), "连接被对端重置"),
        (("REMOTEDISCONNECTED", "REMOTE END CLOSED CONNECTION"), "对端未返回响应就断开连接"),
        (("WRITE OPERATION TIMED OUT",), "写入请求数据超时"),
        (("BROKEN PIPE", "BROKENPIPEERROR"), "写入连接已断开"),
    ):
        if any(marker in message for marker in markers):
            return f"{kind}（{reason}）"
    return kind


def checked_prompt(value):
    """Bound prompt text conservatively without slicing its instructions.

    UTF-8 byte length is a deliberately broad token upper estimate, not the
    provider's exact tokenizer. In particular, characters are not tokens.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 10000:
        raise ImageError("生图提示词为空或过长")
    value = value.strip()
    try:
        estimated = len(value.encode("utf-8"))
    except UnicodeError as error:
        raise ImageError("生图提示词包含无效的 Unicode 文本") from error
    if estimated > MAX_PROMPT_TOKENS:
        raise ImageError("千问生图提示词超过 4,500 token 的保守估算上限；请缩短后重试，未提交生图")
    return value


def request_deadline(timeout=600):
    if isinstance(timeout, bool):
        raise ImageError("生图时限无效")
    seconds = float(timeout)
    if not math.isfinite(seconds) or not 1 <= seconds <= 1800:
        raise ImageError("生图时限需为 1–1800 秒")
    return time.monotonic() + seconds


def check_active(cancelled=None, deadline=None):
    if cancelled and (cancelled.is_set() if hasattr(cancelled, "is_set") else cancelled()):
        raise ImageError("生图请求已取消")
    if deadline is not None and time.monotonic() >= deadline:
        raise ImageError("生图超过本次请求时限")


def endpoint(base_url):
    value = str(base_url).strip().rstrip("/")
    parsed = urlsplit(media_util.validate_http_url(value))
    if parsed.scheme != "https" or parsed.query or parsed.fragment or not parsed.path.endswith("/api/v1"):
        raise ImageError("千问生图需使用 HTTPS DashScope /api/v1 地址")
    return value + "/services/aigc/multimodal-generation/generation"


def image_size(value):
    """DashScope uses '*'; auto is represented by omitting size entirely."""
    size = str(value or "1024*1024").strip().upper()
    if size == "AUTO":
        return None
    if size in {"1K", "2K"}:
        side = 1024 if size == "1K" else 2048
        return f"{side}*{side}"
    match = re.fullmatch(r"(\d+)\s*[*X×]\s*(\d+)", size)
    if not match:
        raise ImageError("千问生图尺寸需为 auto、1K、2K 或宽*高")
    width, height = map(int, match.groups())
    if (width < 1 or height < 1 or not 512**2 <= width * height <= 2048**2
            or not 1 / 8 <= width / height <= 8):
        raise ImageError("千问输出面积需在 512²–2048²，宽高比需在 1:8–8:1")
    return f"{width}*{height}"


def parameters(size, *, has_refs=False, prompt_extend=True,
               prompt_extend_mode="direct", enable_thinking=True, watermark=False):
    for value in (prompt_extend, enable_thinking, watermark):
        if not isinstance(value, bool):
            raise ImageError("生图改写、思考和水印设置必须为布尔值")
    if prompt_extend_mode not in {"direct", "agent"}:
        raise ImageError("生图提示词改写模式需为 direct 或 agent")
    if has_refs and prompt_extend and prompt_extend_mode == "agent":
        raise ImageError("千问参考图模式不支持 agent 提示词改写")
    values = {"n": 1, "watermark": watermark, "prompt_extend": prompt_extend}
    if prompt_extend:
        values.update(prompt_extend_mode=prompt_extend_mode, enable_thinking=enable_thinking)
    if size is not None:
        values["size"] = size
    return values


def _reference_bytes(value, *, cancelled=None, deadline=None):
    check_active(cancelled, deadline)
    value = str(value).strip().strip('"').strip("'")
    if value.startswith("ref:"):
        import reference_store
        value = reference_store.resolve(value)
    limit = min(MAX_REFERENCE_BYTES, media_util.media_limit())
    if value.startswith("data:"):
        match = re.fullmatch(r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)", value)
        if not match or len(match[2]) > (limit + 2) // 3 * 4 + 256:
            raise ImageError("参考图 Base64 数据格式无效或超过 10 MB")
        try:
            data = base64.b64decode(re.sub(r"\s", "", match[2]), validate=True)
        except (ValueError, TypeError) as error:
            raise ImageError("参考图 Base64 数据无效") from error
        return data, match[1].lower()
    if value.startswith(("https://", "http://")):
        url = media_util.validate_http_url(value)
        # Fetch without provider credentials; validating the bytes here also
        # prevents a broken public reference from consuming a paid prompt call.
        remaining = max(0.001, deadline - time.monotonic()) if deadline else 60
        try:
            with http_util.session().get(url, timeout=(min(5, remaining), min(30, remaining)),
                                         stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise ImageError(f"参考图下载失败，HTTP {response.status_code}")
                return _response_bytes(response, limit, cancelled, deadline), None
        except ImageError:
            raise
        except Exception as error:
            raise ImageError("参考图下载异常: " + type(error).__name__) from error
    if value.startswith("file://"):
        parsed = urlsplit(value)
        if parsed.netloc and not re.fullmatch(r"[A-Za-z]:", parsed.netloc):
            raise ImageError("参考图仅支持本地文件及公开 HTTP(S) URL")
        value = unquote((parsed.netloc + parsed.path) if parsed.netloc else parsed.path)
        if re.match(r"^/[A-Za-z]:", value):
            value = value[1:]
    elif "://" in value:
        raise ImageError("参考图地址格式不支持")
    if not os.path.isabs(value):
        value = os.path.join(config.BASE_DIR, value)
    if not os.path.isfile(value) or not 0 < os.path.getsize(value) <= limit:
        raise ImageError("参考图不存在、为空或超过 10 MB/媒体上限")
    with open(value, "rb") as stream:
        return stream.read(limit + 1), None


def _response_bytes(response, limit, cancelled, deadline):
    length = response.headers.get("Content-Length", "")
    try:
        if length and not 0 <= int(length) <= limit:
            raise ImageError("图片接口响应超过大小限制")
    except (TypeError, ValueError) as error:
        raise ImageError("图片接口响应长度无效") from error
    body = bytearray()
    for chunk in response.iter_content(65536):
        check_active(cancelled, deadline)
        if len(body) + len(chunk) > limit:
            raise ImageError("图片接口响应超过大小限制")
        body.extend(chunk)
    check_active(cancelled, deadline)
    if not body:
        raise ImageError("图片接口响应为空")
    return bytes(body)


def reference_input(value, *, cancelled=None, deadline=None):
    from PIL import Image
    if deadline is None:
        deadline = request_deadline(60)
    data, claimed_mime = _reference_bytes(value, cancelled=cancelled, deadline=deadline)
    if not 0 < len(data) <= min(MAX_REFERENCE_BYTES, media_util.media_limit()):
        raise ImageError("参考图为空或超过 10 MB/媒体上限")
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = _MIME.get(image.format)
            width, height = image.size
            if (not mime or not 1 <= width <= 8000 or not 1 <= height <= 8000
                    or not 1 / 10 <= width / height <= 10):
                raise ImageError("参考图格式不支持，边长需为 1–8000，宽高比需在 1:10–10:1")
            if claimed_mime and claimed_mime != mime:
                raise ImageError("参考图声明的 MIME 与实际内容不符")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            # Keep the exact dimensions, color profile, and every selected reference.
            # Alpha, animation and orientation-dependent files retain their original encoding.
            if (len(data) >= 512 * 1024 and image.mode in {"RGB", "L"}
                    and "transparency" not in image.info and not getattr(image, "is_animated", False)
                    and image.getexif().get(274, 1) == 1):
                encoded = io.BytesIO()
                image.save(encoded, "JPEG", quality=95, subsampling=0, icc_profile=image.info.get("icc_profile"))
                if encoded.tell() < len(data):
                    data, mime = encoded.getvalue(), "image/jpeg"
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ImageError("参考图内容不完整或无法解码") from error
    check_active(cancelled, deadline)
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def reference_limit(model):
    return 10 if model in {"qwen-image-2.1-pro", "qwen-image-2.1-turbo"} else 3


def prepare_references(values, *, model="qwen-image-3.0-pro", cancelled=None, deadline=None):
    maximum = reference_limit(model)
    if not isinstance(values, list) or len(values) > maximum:
        raise ImageError(f"当前千问模型最多支持 {maximum} 张参考图")
    return [reference_input(value, cancelled=cancelled, deadline=deadline) for value in values]


def request_image(model, prompt, refs, params, *, api_key, base_url,
                  cancelled=None, deadline=None):
    if deadline is None:
        deadline = request_deadline()
    if model not in MODELS:
        raise ImageError("不支持的千问图像模型")
    prompt = checked_prompt(prompt)
    if not isinstance(refs, list) or len(refs) > reference_limit(model):
        raise ImageError(f"当前千问模型最多支持 {reference_limit(model)} 张参考图")
    url = endpoint(base_url)
    check_active(cancelled, deadline)
    remaining = max(0.001, deadline - time.monotonic()) if deadline else 600
    content = [{"image": ref} for ref in refs] + [{"text": prompt}]
    body = {"model": model, "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": params}
    # The shared session has retries disabled. Reject redirects so the bearer
    # token can never follow a provider redirect to an image CDN.
    # urllib3 keeps connect_timeout on the socket while writing the request body.
    # Five seconds is fine for text, but sync I2I embeds several MiB of base64.
    upload_window = min(120, 5 + sum(len(ref) for ref in refs) / (128 * 1024))
    stage = "提交请求及等待响应头"
    try:
        with http_util.session().post(url, headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json"}, json=body,
                 timeout=(min(upload_window, remaining), remaining), stream=True, allow_redirects=False) as response:
            stage = "读取服务端响应正文"
            check_active(cancelled, deadline)
            if response.status_code != 200:
                reason = {400: "请求参数未获接受", 401: "API 密钥认证失败", 403: "该请求未获授权",
                          413: "请求体过大", 429: "请求受限或额度不足"}.get(response.status_code, "")
                detail = f"（{reason}）" if reason else ""
                raise ImageError(f"千问生图失败，HTTP {response.status_code}{detail}，未自动重试")
            raw = _response_bytes(response, 1024 * 1024, cancelled, deadline)
    except ImageError:
        raise
    except Exception as error:
        raise ImageError(f"千问生图请求异常: {_transport_error(error)}；{stage}期间失败，未自动重试") from None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise ImageError("千问生图响应不是有效 JSON") from error
    if not isinstance(data, dict) or data.get("code") or data.get("error"):
        raise ImageError("千问生图接口返回错误，未自动重试")
    output = data.get("output")
    choices = output.get("choices", []) if isinstance(output, dict) else []
    for choice in choices if isinstance(choices, list) else []:
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        content = message.get("content", []) if isinstance(message, dict) else []
        for item in content if isinstance(content, list) else []:
            image = item.get("image") if isinstance(item, dict) else None
            if isinstance(image, str) and image:
                check_active(cancelled, deadline)
                return media_util.validate_http_url(image)
    raise ImageError("千问生图响应没有有效图片")
