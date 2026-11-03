"""Read-only WeFlow Moments feed with original XML and SNS database identities.

The timeline endpoint is a local cache: callers must refresh WeChat first. Display
names and generated UI comment IDs never establish an actor's identity.
"""
from __future__ import annotations

import math
import re
import time
from urllib.parse import quote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import config
import http_util
from stable_id import validate_username

MAX_XML_BYTES = 2 * 1024 * 1024
MAX_HTTP_BYTES = 8 * 1024 * 1024
MAX_DETAILS_COMMENTS = 500
MAX_DETAILS_LIKES = 1000


class FeedError(ValueError):
    pass


def real_id(value):
    """Retain an original opaque ID; never accept WeFlow's generated UI IDs."""
    if isinstance(value, bool):
        return ""
    value = str(value) if isinstance(value, (str, int)) else ""
    if (value in {"0", "-0", "unknown", "null", "None"} or value.startswith("cmt_")
            or not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}|-[1-9][0-9]{0,29})", value)):
        return ""
    return value


def _unsigned_post_id(value):
    # The details endpoint binds unsigned XML IDs to signed native SQLite tids.
    # Do not coerce a floating-point JSON number or guess aliases for opaque IDs.
    return (value if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value)
            and int(value) < 1 << 64 else "")


def _comment_detail_id(value, *, optional=False):
    if optional and value == "":
        return ""
    if (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value)
            or int(value) >= 1 << 64):
        raise FeedError("朋友圈原生评论 ID 无法核验")
    return value


def _text(node, *tags):
    if node is None:
        return ""
    allowed = {tag.lower() for tag in tags}
    for child in node:
        if child.tag.lower() in allowed and child.text:
            return child.text.strip()
    return ""


def _lists(root, names):
    return [node for node in root.iter() if isinstance(node.tag, str) and node.tag.lower() in names] if root is not None else []


def _timestamp(value):
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return 0
        # The documented API uses seconds. Milliseconds are not guessed here.
        return int(number) if number < 100_000_000_000 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _actor(value):
    try:
        return validate_username(value)
    except (TypeError, ValueError):
        return ""


def _profile(username, registry):
    if registry is None:
        return {}
    try:
        record = registry.get_private_by_session(username)
        if not isinstance(record, dict) or record.get("session_id") != username:
            return {}
        return record
    except Exception:
        return {}


def _names(username, nickname, registry):
    profile = _profile(username, registry)
    names = []
    for value in (profile.get("name"), profile.get("remark"), profile.get("nickname"), nickname):
        if isinstance(value, str) and value.strip() and value != username and value not in names:
            names.append(value.strip()[:256])
    return names


def _xml_root(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None
    if len(raw.encode("utf-8")) > MAX_XML_BYTES:
        raise FeedError("朋友圈原始 XML 超出限制")
    # ElementTree does not resolve external entities, but reject every DTD and
    # entity declaration as well to prevent expansion and ambiguous input.
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.I):
        raise FeedError("朋友圈原始 XML 包含不支持的声明")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as error:
        # Native sns exports may be a sequence of sibling fields rather than a
        # document. Add one fixed root, never rewrite or repair the field data.
        try:
            return ET.fromstring("<SnsFragment>" + raw + "</SnsFragment>")
        except ET.ParseError:
            raise FeedError("朋友圈原始 XML 无法解析") from error


def _decode_named_xml_entities(text):
    # The native timeline preserves XML escapes in some contentDesc values.
    # Decode exactly one layer of the five XML names, never arbitrary HTML or
    # repeated escapes. The caller additionally requires original XML proof.
    names = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
    return re.sub(r"&(amp|lt|gt|quot|apos);", lambda match: names[match[1]], text)


def _media_url(value, token="", *, video=False):
    """Apply WeFlow's native SNS URL contract without re-encoding signed fields."""
    # Native XML/JSON can retain one XML ampersand layer. Percent escapes and
    # existing signed query values must otherwise reach the CDN unchanged.
    value = value.replace("&amp;", "&")
    parsed = urlsplit(value)
    path = parsed.path if video else re.sub(r"/(?:150|200|480)$", "/0", parsed.path)
    query = parsed.query
    if token:
        fields = [field for field in query.split("&") if field and field.split("=", 1)[0] not in {"token", "idx"}]
        signed = "token=" + quote(str(token), safe="") + "&idx=1"
        query = "&".join([signed, *fields] if video else [*fields, signed])
    return urlunsplit(("https" if parsed.scheme == "http" else parsed.scheme, parsed.netloc, path, query, parsed.fragment))


def _media_child(node, name):
    return next((child for child in node if child.tag.lower() == name), None)


def normalize_post(row, *, bot_id="", registry=None):
    """Return one verified post; malformed identity fields fail closed.

    Raw comments remain useful for context, but only an original XML cmtid plus
    username can produce a reply target. Nickname-only like arrays are ignored.
    """
    if not isinstance(row, dict):
        raise FeedError("朋友圈条目格式无效")
    post_id = real_id(row.get("id"))
    author_id = _actor(row.get("username"))
    created_at = _timestamp(row.get("createTime"))
    if not post_id or not author_id or not created_at:
        raise FeedError("朋友圈缺少真实动态 ID、作者微信 ID 或时间")
    root = _xml_root(row.get("rawXml"))
    timelines = _lists(root, {"timelineobject"})
    if len(timelines) > 1:
        raise FeedError("朋友圈 XML 存在多个动态对象")
    timeline = timelines[0] if timelines else root
    if timeline is not None:
        xml_id = _text(timeline, "id")
        xml_author = _text(timeline, "username")
        if (xml_id and xml_id != post_id) or (xml_author and xml_author != author_id):
            raise FeedError("朋友圈原始身份与缓存条目不一致")
    nickname = row.get("nickname") or row.get("displayName") or _text(timeline, "nickname")
    nickname = nickname if isinstance(nickname, str) else ""
    author_names = _names(author_id, nickname, registry)
    content = row.get("contentDesc")
    if content is None:
        content = _text(timeline, "contentDesc")
    if not isinstance(content, str) or len(content) > 32_000:
        raise FeedError("朋友圈正文格式无效或超出限制")
    if timeline is not None and _text(timeline, "id") == post_id and _text(timeline, "username") == author_id:
        original = _text(timeline, "contentDesc")
        if original != content and original == _decode_named_xml_entities(content):
            content = original

    comments, ids = [], set()
    native_extra = bool(_lists(root, {"localextrainfo"}))
    comment_lists = _lists(root, {"commentuserlist", "commentlist", "comment_user_list"})
    if root is not None:
        for node in [child for group in comment_lists for child in group
                     if child.tag.lower() in {"commentuser", "comment", "user_comment"}]:
            comment_id = (real_id(_text(node, "comment_64id")) if native_extra else "") or real_id(_text(node, "cmtid", "commentId", "comment_id", "id"))
            username = _actor(_text(node, "username"))
            text = _text(node, "content")
            if comment_id in ids:
                raise FeedError("朋友圈包含重复评论 ID，无法核验目标")
            if native_extra:
                # Full native SNS XML also contains deleted/local optimistic
                # interactions. Only the details API validates those flags and
                # canonical references; raw LocalExtraInfo stays context-only.
                if comment_id:
                    ids.add(comment_id)
                    comments.append({"comment_id": "", "author_id": "", "author_name": _text(node, "nickname"),
                        "author_names": [], "content": text[:4000], "created_at": 0,
                        "ref_author_id": "", "ref_comment_id": "", "ref_author_name": "",
                        "identity_verified": False, "source_comment_id": comment_id})
                continue
            if not comment_id or not username or not text:
                continue
            ids.add(comment_id)
            ref_username = _actor(_text(node, "ref_username"))
            name = _text(node, "nickname")
            names = _names(username, name, registry)
            comments.append({"comment_id": comment_id, "author_id": username,
                "author_name": names[0] if names else name, "author_names": names,
                "content": text[:4000], "created_at": _timestamp(_text(node, "createTime")) or created_at,
                "time_source": "comment" if _timestamp(_text(node, "createTime")) else "post",
                "ref_author_id": ref_username, "ref_comment_id": real_id(_text(node, "refCommentId", "replyCommentId", "ref_comment_id")),
                "ref_author_name": _text(node, "refNickname", "refNickName", "replyNickname"), "identity_verified": True})
    # Unverified cached comments are explicitly context-only. A generated cmt_...
    # ID is never retained as a stable identity or used for deduplication.
    raw_comments = row.get("comments", [])
    if isinstance(raw_comments, list):
        for item in raw_comments[:200]:
            if not isinstance(item, dict):
                continue
            cached_id = real_id(item.get("id"))
            if cached_id in ids:
                continue
            text = item.get("content")
            if isinstance(text, str) and text.strip():
                comments.append({"comment_id": "", "author_id": "", "author_name": str(item.get("nickname") or "")[:256],
                    "author_names": [], "content": text[:4000], "created_at": 0,
                    "ref_author_id": "", "ref_comment_id": "", "ref_author_name": "", "identity_verified": False,
                    "source_comment_id": cached_id})
    like_users = []
    like_lists = _lists(root, {"likeuserlist", "likelist", "like_user_list"})
    if root is not None:
        for node in [child for group in like_lists for child in group if child.tag.lower() in {"likeuser", "user_comment"}]:
            username = _actor(_text(node, "username"))
            if username and username not in like_users:
                like_users.append(username)
    if native_extra:
        like_users = []
    likes_verified = bool(like_lists) and not native_extra
    # Keep media associated with this exact verified post. The native XML is
    # authoritative when present; service-derived thumbnails alone must not be
    # described as having watched the original video.
    native_media = []
    post_video = str(row.get("type", "")) == "15"
    video_keys = [node.attrib.get("key", "") for node in _lists(root, {"enc"}) if node.attrib.get("key")]
    video_key = video_keys[0] if post_video and video_keys else ""
    if root is not None:
        for group in _lists(root, {"medialist", "media_list"}):
            for node in list(group)[:9]:
                if node.tag.lower() != "media":
                    continue
                url, thumb = _text(node, "url"), _text(node, "thumb", "thumbnail")
                if url or thumb:
                    original, preview = _media_child(node, "url"), _media_child(node, "thumb")
                    attrs = original.attrib if original is not None else {}
                    preview_attrs = preview.attrib if preview is not None else {}
                    item = {"id": _text(node, "id"), "type": _text(node, "type") or ("15" if post_video else ""),
                            "url": url, "thumb": thumb, "token": attrs.get("token", preview_attrs.get("token", "")),
                            "thumbToken": preview_attrs.get("token", "")}
                    if item["type"] == "4":
                        card = _media_child(timeline, "contentobject") if timeline is not None else None
                        item["title"] = _text(node, "title") or _text(card, "title")
                        item["description"] = _text(node, "description") or _text(card, "description")
                    key = video_key or attrs.get("key", preview_attrs.get("key", ""))
                    if key:
                        item["key"] = key
                    native_media.append(item)
    media = native_media or row.get("media") or []
    if not isinstance(media, list) or len(media) > 9:
        raise FeedError("朋友圈媒体列表格式或数量无效")
    normalized_media = []
    for item in media:
        if not isinstance(item, dict):
            raise FeedError("朋友圈媒体条目格式无效")
        url, thumb = item.get("url", ""), item.get("thumb", item.get("thumbnail", ""))
        if not isinstance(url, str) or not isinstance(thumb, str) or len(url) > 8192 or len(thumb) > 8192:
            raise FeedError("朋友圈媒体地址格式无效")
        kind = str(item.get("type", ""))[:32] or ("15" if post_video else "")
        if kind == "4":
            normalized_media.append({"id": str(item.get("id", ""))[:256], "type": kind, "url": url,
                                     "title": str(item.get("title") or "")[:512],
                                     "description": str(item.get("description") or "")[:1024]})
            continue
        token, thumb_token = item.get("token", ""), item.get("thumbToken", "")
        if not isinstance(token, str) or not isinstance(thumb_token, str):
            raise FeedError("朋友圈媒体签名格式无效")
        normalized = {"id": str(item.get("id", ""))[:256], "type": kind,
                      "url": _media_url(url, token, video=kind.lower() in {"6", "15", "video"}) if url else "",
                      "thumb": _media_url(thumb, thumb_token or token) if thumb else ""}
        key = item.get("key", "")
        if key not in ("", None):
            if isinstance(key, bool) or not isinstance(key, (str, int)) or not re.fullmatch(r"[0-9]{1,20}", str(key)):
                raise FeedError("朋友圈媒体原生解密密钥格式无效")
            normalized["key"] = str(key)
        normalized_media.append(normalized)
    return {"post_id": post_id, "author_id": author_id,
        "author_name": author_names[0] if author_names else nickname, "author_names": author_names,
        "content": content, "created_at": created_at, "comments": comments,
        "like_user_ids": like_users, "likes_verified": likes_verified,
        "liked_by_self": bool(bot_id and bot_id in like_users),
        "has_media": bool(normalized_media), "media": normalized_media, "identity_verified": True,
        "unverified_comment_count": sum(item.get("identity_verified") is not True for item in comments)}


def enrich_post(post, detail, *, cached_row=None, bot_id="", registry=None):
    """Join a verified SNS DB detail by exact post/actor/comment identity only.

    The server reads fixed SNS columns, verifies DB username and original XML,
    and clears any incomplete comment/like list. Names remain display metadata.
    """
    if not isinstance(detail, dict) or detail.get("identityVerified") is not True:
        raise FeedError("朋友圈原生互动身份未通过核验")
    post_id, tid = _unsigned_post_id(detail.get("id")), detail.get("tid")
    if (not post_id or post_id != post.get("post_id") or not isinstance(tid, str)
            or not re.fullmatch(r"(?:0|[1-9][0-9]{0,18}|-[1-9][0-9]{0,18})", tid)
            or not -(1 << 63) <= int(tid) < 1 << 63 or int(tid) % (1 << 64) != int(post_id)):
        raise FeedError("朋友圈原生数据库 ID 与动态 ID 不一致")
    author = _actor(detail.get("username"))
    timestamp = detail.get("createTime")
    if (not author or author != post.get("author_id") or detail.get("contentDesc") != post.get("content")
            or type(timestamp) is not int or not _timestamp(timestamp)
            or abs(timestamp - post.get("created_at", 0)) > 1):
        # This exact native build has a proven one-second row/XML timestamp
        # difference; larger differences or any body/actor change are refused.
        raise FeedError("朋友圈原生正文、作者或时间与缓存不一致")
    if type(detail.get("commentsVerified")) is not bool or type(detail.get("likesVerified")) is not bool:
        raise FeedError("朋友圈原生互动完整性标记无效")
    comments, likes = detail.get("commentsDetail"), detail.get("likesDetail")
    if (not isinstance(comments, list) or len(comments) > MAX_DETAILS_COMMENTS
            or not isinstance(likes, list) or len(likes) > MAX_DETAILS_LIKES
            or not detail["commentsVerified"] and comments or not detail["likesVerified"] and likes):
        raise FeedError("朋友圈原生互动列表无效或不完整")

    result = dict(post)
    verified, ids = [], set()
    alias_map, aliases_by_id = {}, {}
    for item in comments:
        if not isinstance(item, dict):
            raise FeedError("朋友圈原生评论格式无效")
        canonical = _comment_detail_id(item.get("id"))
        legacy = _comment_detail_id(item.get("commentId", ""), optional=True)
        full = _comment_detail_id(item.get("comment64Id", ""), optional=True)
        if ("commentId" in item or "comment64Id" in item) and canonical != (full or legacy):
            raise FeedError("朋友圈原生评论 32/64 位 ID 不一致")
        aliases = {canonical, *([legacy] if legacy else []), *([full] if full else [])}
        for alias in aliases:
            if alias in alias_map and alias_map[alias] != canonical:
                raise FeedError("朋友圈原生评论 ID 别名重复或冲突")
            alias_map[alias] = canonical
        aliases_by_id[canonical] = (aliases, legacy)
    cached = cached_row.get("comments", []) if isinstance(cached_row, dict) else []
    cached = cached if isinstance(cached, list) else []
    for item in comments:
        if not isinstance(item, dict):
            raise FeedError("朋友圈原生评论格式无效")
        comment_id = _comment_detail_id(item.get("id"))
        username, text = _actor(item.get("username")), item.get("content")
        if (not username or not isinstance(text, str) or len(text) > 4000 or comment_id in ids):
            raise FeedError("朋友圈原生评论发送者、正文或唯一 ID 无法核验")
        ids.add(comment_id)
        ref_id = _comment_detail_id(item.get("refCommentId"), optional=True)
        if "refCommentId32" in item or "refCommentId64" in item:
            ref32 = _comment_detail_id(item.get("refCommentId32", ""), optional=True)
            ref64 = _comment_detail_id(item.get("refCommentId64", ""), optional=True)
            mapped_ref = alias_map.get(ref32, ref32)
            if ref_id != (ref64 or mapped_ref) or ref64 and ref32 and mapped_ref != ref32 and mapped_ref != ref64:
                raise FeedError("朋友圈原生评论引用 32/64 位 ID 不一致")
        ref_raw = item.get("refUsername")
        ref_user = _actor(ref_raw) if ref_raw != "" else ""
        if not isinstance(ref_raw, str) or ref_raw and not ref_user:
            raise FeedError("朋友圈原生评论引用身份无效")
        comment_time = item.get("createTime")
        if type(comment_time) is not int or comment_time < 0 or comment_time and not _timestamp(comment_time):
            raise FeedError("朋友圈原生评论时间无效")
        nickname, ref_name = item.get("nickname"), item.get("refNickname")
        if not isinstance(nickname, str) or not isinstance(ref_name, str):
            raise FeedError("朋友圈原生评论展示字段无效")
        aliases, legacy_id = aliases_by_id[comment_id]
        matches = [old for old in cached if isinstance(old, dict) and real_id(old.get("id")) in aliases]
        if len(matches) > 1:
            raise FeedError("朋友圈缓存包含重复评论 ID，无法核验目标")
        for old in matches:
            if not isinstance(old.get("content"), str) or old["content"].strip() != text.strip():
                raise FeedError("朋友圈原生评论正文与缓存冲突")
            for key, expected, actor_field in (("username", username, True), ("refUsername", ref_user, True),
                                               ("refCommentId", ref_id, False)):
                value = old.get(key)
                actual = _actor(value) if actor_field else alias_map.get(real_id(value), real_id(value))
                if value not in (None, "", 0, "0") and actual != expected:
                    raise FeedError("朋友圈原生评论身份或引用与缓存冲突")
        for old in post.get("comments", []):
            if old.get("identity_verified") is True and old.get("comment_id") in aliases:
                if (old.get("author_id") != username or str(old.get("content", "")).strip() != text.strip()
                        or old.get("ref_comment_id") and alias_map.get(old["ref_comment_id"], old["ref_comment_id"]) != ref_id
                        or old.get("ref_author_id") and old["ref_author_id"] != ref_user):
                    raise FeedError("朋友圈原始 XML 与数据库评论身份冲突")
        names = _names(username, nickname, registry)
        verified.append({"comment_id": comment_id, "author_id": username,
            "author_name": names[0] if names else nickname, "author_names": names,
            "content": text, "created_at": comment_time or post["created_at"],
            "time_source": "comment" if comment_time else "post", "ref_author_id": ref_user,
            "ref_comment_id": ref_id, "ref_author_name": ref_name, "identity_verified": True,
            "source_comment_id": legacy_id if legacy_id != comment_id else "",
            "native_alias_verified": bool(legacy_id)})
    # A complete native list is the current authority, including deletions. Old
    # cache entries (often generated UI IDs) must not create phantom duplicate
    # comments or ambiguity beside those verified records. Preserve context and
    # observation baselines only when the native list is incomplete.
    context = []
    for old in post.get("comments", []) if not detail["commentsVerified"] else []:
        old_id = old.get("comment_id") or old.get("source_comment_id", "")
        if old_id and alias_map.get(old_id, old_id) in ids:
            continue
        context.append({"comment_id": "", "author_id": "", "author_name": old.get("author_name", ""),
            "author_names": [], "content": old.get("content", ""), "created_at": 0,
            "ref_author_id": "", "ref_comment_id": "", "ref_author_name": "", "identity_verified": False,
            "source_comment_id": real_id(old_id)})
    result["comments"] = verified + context
    like_users = []
    for item in likes:
        username = _actor(item.get("username")) if isinstance(item, dict) else ""
        if not username or username in like_users:
            raise FeedError("朋友圈原生点赞身份缺失或重复")
        like_users.append(username)
    if detail["likesVerified"]:
        result.update(like_user_ids=like_users, likes_verified=True,
                      liked_by_self=bool(bot_id and bot_id in like_users))
    # Incomplete details cannot prove absence of a previous known self-like.
    result["unverified_comment_count"] = len(context)
    result["interaction_identity_verified"] = detail["commentsVerified"]
    return result


def find_new_self_comment(source, fresh, *, bot_id, text, reply_comment_id="", previous_comment_ids=None):
    """Read-only evidence for one already submitted comment; never retries it.

    Both before/after lists must come from complete native details. A name,
    editor disappearance or an old matching comment cannot acknowledge a send.
    """
    if (not isinstance(source, dict) or not isinstance(fresh, dict) or not _actor(bot_id)
            or not _unsigned_post_id(source.get("post_id")) or not _actor(source.get("author_id"))
            or not isinstance(text, str) or not text or source.get("interaction_identity_verified") is not True
            or fresh.get("interaction_identity_verified") is not True
            or source.get("identity_verified") is not True or fresh.get("identity_verified") is not True
            or any(source.get(key) != fresh.get(key) for key in ("post_id", "author_id", "content", "created_at"))):
        return None
    source_comments, fresh_comments = source.get("comments"), fresh.get("comments")
    if not isinstance(source_comments, list) or not isinstance(fresh_comments, list):
        return None
    old_ids = set()
    if previous_comment_ids is not None:
        if not isinstance(previous_comment_ids, (list, tuple, set, frozenset)):
            return None
        for value in previous_comment_ids:
            try:
                old_ids.add(_comment_detail_id(value))
            except FeedError:
                return None
    for item in source_comments:
        if not isinstance(item, dict):
            return None
        values = [item.get("comment_id") or item.get("source_comment_id", "")]
        # A later native read can fall back from a 64-bit canonical ID to its
        # established 32-bit alias. Both already-observed identities must remain
        # excluded; losing the 64-bit field cannot make an old comment new.
        if item.get("native_alias_verified") is True:
            values.append(item.get("source_comment_id", ""))
        for value in values:
            if value:
                try:
                    old_ids.add(_comment_detail_id(value))
                except FeedError:
                    return None
    target = None
    if reply_comment_id:
        targets = [item for item in source_comments if item.get("comment_id") == reply_comment_id
                   and item.get("identity_verified") is True and _actor(item.get("author_id"))]
        if len(targets) != 1:
            return None
        target = targets[0]
    matches, seen = [], set()
    for item in fresh_comments:
        if not isinstance(item, dict):
            return None
        if item.get("identity_verified") is not True:
            continue
        try:
            comment_id = _comment_detail_id(item.get("comment_id"))
        except FeedError:
            return None
        if comment_id in seen or not _actor(item.get("author_id")):
            return None
        seen.add(comment_id)
        alias = item.get("source_comment_id", "") if item.get("native_alias_verified") is True else ""
        if comment_id in old_ids or alias in old_ids or item.get("author_id") != bot_id or item.get("content") != text:
            continue
        if target is not None:
            if (item.get("ref_comment_id") != reply_comment_id
                    or item.get("ref_author_id") and item["ref_author_id"] != target["author_id"]):
                continue
        elif item.get("ref_comment_id") or item.get("ref_author_id"):
            continue
        matches.append(item)
    return dict(matches[0]) if len(matches) == 1 else None


def _details_by_id(data, requested_ids):
    caps = data.get("capabilities") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get("success") is not True
            or type(data.get("version")) is not int or data["version"] != 1
            or not isinstance(caps, dict) or type(caps.get("snsIdentityDetails")) is not int
            or caps["snsIdentityDetails"] != 1):
        raise FeedError("WeFlow 朋友圈原生互动接口能力或版本无法核验")
    details = data.get("timeline")
    if (not isinstance(details, list) or len(details) != len(requested_ids)
            or type(data.get("count")) is not int or data["count"] != len(details)):
        raise FeedError("WeFlow 朋友圈原生互动条目不完整")
    by_id = {}
    for detail in details:
        post_id = detail.get("id") if isinstance(detail, dict) else None
        if not isinstance(post_id, str) or post_id not in requested_ids or post_id in by_id:
            raise FeedError("WeFlow 朋友圈原生互动 ID 重复或与请求不符")
        by_id[post_id] = detail
    return by_id


def _base_url():
    base = str(getattr(config, "WE_FLOW_BASE_URL", "")).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise FeedError("WeFlow 地址无效")
    return base


class MomentsFeedClient:
    def __init__(self, registry=None):
        self.registry = registry
        self.last_skipped = 0
        self.identity_available = False
        self._capability_key, self._native_retry_at = None, 0.0
        self._native_supported = None
        self.details_identity_available = False
        self._details_supported, self._details_retry_at = None, 0.0

    @property
    def details_endpoint_missing(self):
        return self._details_supported is False

    def fetch(self, *, limit=20, usernames=None, bot_id="", exclude_author=None, cancel_event=None):
        """Bounded paged local-cache read; never claims a remote WeChat refresh."""
        limit = max(1, min(100, int(limit)))
        base = _base_url()
        usernames = list(usernames or [])
        if any(not _actor(value) for value in usernames):
            raise FeedError("朋友圈联系人筛选必须使用真实微信 ID")
        if exclude_author is not None and not _actor(exclude_author):
            raise FeedError("朋友圈排除作者必须使用真实微信 ID")
        key = (base, getattr(config, "ACCESS_TOKEN", ""))
        if key != self._capability_key:
            self._capability_key, self._native_supported = key, None
            self._native_retry_at = 0.0
            self._details_supported, self._details_retry_at = None, 0.0
        self.details_identity_available = False
        rows, seen, offset = [], {}, 0
        cached_rows = {}
        self.last_skipped = 0
        deadline = time.monotonic() + 12
        while len(rows) < limit and offset < 200:
            if cancel_event is not None and cancel_event.is_set():
                raise FeedError("朋友圈读取已取消")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FeedError("朋友圈读取超时")
            size = min(20, limit - len(rows))
            params = {"access_token": getattr(config, "ACCESS_TOKEN", ""),
                      "limit": size, "offset": offset, "media": 0}
            if usernames:
                params["usernames"] = ",".join(usernames)
            native = self._native_supported is not False or time.monotonic() >= self._native_retry_at
            data = self._read_page(base, params, deadline, cancel_event, native=native)
            if data is None:
                self._native_supported = False
                self._native_retry_at = time.monotonic() + 60
                native = False
                data = self._read_page(base, params, deadline, cancel_event, native=False)
            if not isinstance(data, dict) or data.get("success") is not True:
                raise FeedError("WeFlow 朋友圈接口未返回有效结果")
            if native:
                caps = data.get("capabilities")
                if (type(data.get("version")) is not int or data["version"] != 1
                        or not isinstance(caps, dict) or type(caps.get("snsIdentityTimeline")) is not int
                        or caps["snsIdentityTimeline"] != 1):
                    raise FeedError("WeFlow 朋友圈身份接口能力或版本无法核验")
                self._native_supported = True
            self.identity_available = native
            page = data.get("timeline")
            if not isinstance(page, list):
                raise FeedError("WeFlow 朋友圈分页格式无效")
            if len(page) > size:
                raise FeedError("WeFlow 朋友圈分页超出请求限制")
            for item in page:
                try:
                    post = normalize_post(item, bot_id=bot_id, registry=self.registry)
                except FeedError:
                    self.last_skipped += 1
                    continue
                if usernames and post["author_id"] not in usernames:
                    continue
                if exclude_author and post["author_id"] == exclude_author:
                    continue
                signature = (post["author_id"], post["created_at"], post["content"])
                if post["post_id"] in seen:
                    if seen[post["post_id"]] != signature:
                        raise FeedError("朋友圈分页中同一动态 ID 内容或身份冲突")
                    continue
                seen[post["post_id"]] = signature
                rows.append(post)
                cached_rows[post["post_id"]] = item
            offset += len(page)
            if len(page) < size or not page:
                break
        if cancel_event is not None and cancel_event.is_set():
            raise FeedError("朋友圈读取已取消")
        self._enrich_details(rows, cached_rows, base, deadline, cancel_event, bot_id)
        if cancel_event is not None and cancel_event.is_set():
            raise FeedError("朋友圈读取已取消")
        return rows[:limit]

    def _enrich_details(self, rows, cached_rows, base, deadline, cancel_event, bot_id):
        if self._details_supported is False and time.monotonic() < self._details_retry_at:
            return
        requested = [post for post in rows if _unsigned_post_id(post["post_id"])]
        for start in range(0, len(requested), 20):
            batch = requested[start:start + 20]
            params = {"access_token": getattr(config, "ACCESS_TOKEN", ""),
                      "post_ids": ",".join(post["post_id"] for post in batch)}
            data = self._read_json(base, "/api/v1/sns-identity-details", params, deadline, cancel_event,
                                   capability=True)
            if data is None:
                self._details_supported = False
                self._details_retry_at = time.monotonic() + 60
                self.details_identity_available = False
                return
            ids = {post["post_id"] for post in batch}
            by_id = _details_by_id(data, ids)
            rejected = set()
            for post in batch:
                try:
                    enriched = enrich_post(post, by_id[post["post_id"]],
                        cached_row=cached_rows[post["post_id"]], bot_id=bot_id, registry=self.registry)
                except FeedError:
                    # A conflict disqualifies this exact post. Unrelated valid
                    # native identities remain usable; protocol/HTTP failures
                    # above still abort the whole scan.
                    rejected.add(post["post_id"])
                    self.last_skipped += 1
                    continue
                post.update(enriched)
            if rejected:
                rows[:] = [post for post in rows if post["post_id"] not in rejected]
            self._details_supported = self.details_identity_available = True

    def read_post_details(self, source, *, bot_id="", cancel_event=None, timeout=4.0):
        """Always read one fresh verified native post, for post-submit evidence.

        This never uses timeline/cache fallback or initiates a desktop action.
        A missing capability or incomplete native comment list remains an error
        for the caller to treat as an uncertain result.
        """
        if (not isinstance(source, dict) or source.get("identity_verified") is not True
                or not _unsigned_post_id(source.get("post_id")) or not _actor(source.get("author_id"))):
            raise FeedError("朋友圈单帖核验需要真实动态 ID 和作者微信 ID")
        if isinstance(timeout, bool) or not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 8:
            raise FeedError("朋友圈单帖核验超时设置无效")
        params = {"access_token": getattr(config, "ACCESS_TOKEN", ""), "post_ids": source["post_id"]}
        data = self._read_json(_base_url(), "/api/v1/sns-identity-details", params,
                               time.monotonic() + float(timeout), cancel_event, capability=True)
        if data is None:
            raise FeedError("WeFlow 缺少朋友圈原生互动核验能力")
        by_id = _details_by_id(data, {source["post_id"]})
        result = enrich_post(source, by_id[source["post_id"]], bot_id=bot_id, registry=self.registry)
        if result.get("interaction_identity_verified") is not True:
            raise FeedError("朋友圈原生评论身份不完整，无法确认刚才提交的结果")
        if cancel_event is not None and cancel_event.is_set():
            raise FeedError("朋友圈单帖核验已取消")
        return result

    def _read_page(self, base, params, deadline, cancel_event, *, native):
        endpoint = "/api/v1/sns-identity-timeline" if native else "/api/v1/sns/timeline"
        request_params = {key: value for key, value in params.items() if not native or key != "media"}
        return self._read_json(base, endpoint, request_params, deadline, cancel_event, capability=native)

    def _read_json(self, base, endpoint, params, deadline, cancel_event, *, capability):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or cancel_event is not None and cancel_event.is_set():
            raise FeedError("朋友圈读取已取消或超时")
        response = http_util.session().get(base + endpoint, params=params,
            timeout=(min(2, remaining), min(4, remaining)), stream=True)
        try:
            if capability and response.status_code in (404, 405, 501):
                return None
            if response.status_code != 200:
                raise FeedError(f"WeFlow 朋友圈接口 HTTP {response.status_code}")
            import json
            payload, total = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                if cancel_event is not None and cancel_event.is_set():
                    raise FeedError("朋友圈读取已取消")
                if time.monotonic() >= deadline:
                    raise FeedError("朋友圈读取超时")
                total += len(chunk)
                if total > MAX_HTTP_BYTES:
                    raise FeedError("WeFlow 朋友圈响应超出限制")
                payload.append(chunk)
            return json.loads(b"".join(payload).decode("utf-8"))
        finally:
            response.close()
