"""Recognize explicit song requests and find official QQ Music share pages."""
from __future__ import annotations

import html
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class SongRequest:
    title: str
    artist: str = ""


_COMMAND = re.compile(
    r"^(?:[/／]?(?:点歌|music\b)|(?:请|麻烦)?(?:帮我|给我)?(?:点歌|点(?:播|一?首)|来一?首|放一?首|播(?:放)?一?首))"
    r"[\s:：]*", re.I)
_LISTEN = re.compile(r"^(?:(?:我)?想听|(?:请)?播放)(?:一首(?:歌)?)?[\s:：]*(?=[^《》]*《)")
_DISCUSSION = re.compile(
    r"^(?:功能|插件|系统|软件|命令|指令|教程|是什么意思|失败了|成功了|的时候|的人|"
    r"了[吗么]|[吗么]|台|机|单(?:里|上|怎么)|怎么(?:用|点|发)|如何(?:用|点|发))")
_SEP = re.compile(r"\s+(?:-|–|—)\s+")
_URL = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
_CACHE: OrderedDict[SongRequest, tuple[float, str]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_USAGE = "想听哪首歌？可以发「点歌 晴天」或「点歌 周杰伦的《晴天》」。"
_UNAVAILABLE = "暂时连不上 QQ 音乐的歌曲搜索，过一会儿再点吧。"
_MISSING = "QQ 音乐里没找到匹配的歌曲。可以核对歌名，或用「点歌 歌手的《歌名》」再试一次。"


def parse_song_request(text: str) -> SongRequest | None:
    """The caller checks the chat and removes a verified bot mention first."""
    line = text.strip()
    if len(line) > 160 or len(line.splitlines()) != 1:
        return None
    command = _COMMAND.match(line) or _LISTEN.match(line)
    if not command:
        return None
    body = line[command.end():].strip().rstrip("。！").strip()
    # Keep command-related discussion in the conversation. Explicitly quoted titles
    # still allow actual song names such as 《怎么用》.
    if (_DISCUSSION.match(body) or re.match(r"^(?:诗|唐诗|宋词)(?:[。，！？]|$)", body)
            or re.search(r"(?:[？?]|好听吗|怎么用|什么意思)$", body)):
        return None
    quoted = re.fullmatch(r"(?:([^《》]+?)\s*(?:的\s*)?)?《([^《》]+)》(?:\s*(?:吧|谢谢))?", body)
    if quoted:
        return SongRequest(quoted[2].strip(), (quoted[1] or "").strip())
    if "《" in body or "》" in body:
        return None
    # "的" can be part of the title. Resolve unquoted forms against real song and
    # artist names instead of guessing whether 隐形 is a performer.
    return SongRequest(body)


def _key(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _select(request: SongRequest, rows: list) -> str | None:
    wanted, artist = _key(request.title), _key(request.artist)
    parts = [_key(part) for part in _SEP.split(request.title, maxsplit=1)]
    best_rank = 0
    best_reply = None
    for row in rows:
        if not isinstance(row, dict) or row.get("switch") in (0, "0"):
            continue
        mid = row.get("songmid")
        if not isinstance(mid, str) or not re.fullmatch(r"[a-zA-Z0-9]{8,32}", mid):
            continue
        title = row.get("songname")
        singers = row.get("singer")
        if not isinstance(title, str) or not isinstance(singers, list):
            continue
        title = html.unescape(title).strip()
        names = [html.unescape(s["name"]).strip() for s in singers
                 if isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"].strip()]
        if not title or not names:
            continue
        keys = {_key(n) for n in names}
        song = _key(title)
        # Preserve search order for equal matches and prefer the exact recording.
        base = re.sub(r"\([^()]+\)$", "", song)
        rank = 0
        for candidate, weight in ((base, 1), (song, 2)):
            if artist:
                if wanted == candidate and artist in keys:
                    rank = weight
            elif len(parts) == 2 and any(parts == [n, candidate] or parts == [candidate, n] for n in keys):
                rank = weight
            elif wanted == candidate or any(wanted in (
                    name + "的" + candidate, name + candidate, candidate + name) for name in keys):
                rank = weight
        if rank > best_rank:
            pay = row.get("pay")
            hint = "（完整播放可能需要会员或购买）" if isinstance(pay, dict) and pay.get("payplay") in (1, "1") else ""
            best_reply = f"《{title}》 · {' / '.join(names)}{hint}\nhttps://y.qq.com/n/ryqq/songDetail/{mid}"
            best_rank = rank
    return best_reply


def lookup_song_reply(request: SongRequest) -> str:
    """Synchronous, for the bridge's existing worker pool. Sends only search terms."""
    if not request.title:
        return _USAGE
    with _CACHE_LOCK:
        cached = _CACHE.get(request)
        if cached and cached[0] > time.monotonic():
            _CACHE.move_to_end(request)
            return cached[1]
    query = " ".join(filter(None, (request.artist, _SEP.sub(" ", request.title))))
    try:
        response = requests.get(_URL, params={
            "w": query, "t": 0, "p": 1, "n": 10, "format": "json",
            "platform": "yqq.json", "outCharset": "utf-8",
        }, headers={"Referer": "https://y.qq.com/"}, timeout=(2.5, 4), allow_redirects=False)
        response.raise_for_status()
        data = response.json()
        if data["code"] != 0:
            return _UNAVAILABLE
        rows = data["data"]["song"]["list"]
        if not isinstance(rows, list):
            return _UNAVAILABLE
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return _UNAVAILABLE
    reply = _select(request, rows)
    with _CACHE_LOCK:
        _CACHE[request] = (time.monotonic() + (600 if reply else 30), reply or _MISSING)
        _CACHE.move_to_end(request)
        if len(_CACHE) > 128:
            _CACHE.popitem(last=False)
    return reply or _MISSING
