"""The character's own viewing preferences, separate from contact memories."""
from contextlib import closing
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import random
import re
import sqlite3
import threading
import time
import unicodedata

from stable_id import validate_username


_DEFAULT_DIR = Path(__file__).resolve().parent / "data"
_HALF_LIFE = 14 * 86400
_PER_ROLE = 32
_TOTAL = 512
_EXPLORATION = .15
_ROW_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
# Only public hobby words may leave the machine in a search query. Other labels
# still work for local matching; no substring from a free-form seed is exported.
_PUBLIC = frozenset("""
剑术 剑法 武术 击剑 太极拳 射箭 传统武术 兵器 传统文化 非遗 传统工艺 古建筑 建筑 书法 国画 水墨画
古典文学 诗词 古诗词 历史 考古 博物馆 汉服 妆造 文学 小说 侦探小说 科幻 奇幻 仙侠 写作 阅读
摄影 街拍 街头摄影 风光摄影 风景摄影 人像摄影 纪实摄影 夜景摄影 微距摄影 胶片摄影 星空摄影 航拍
插画 绘画 素描 水彩 美术 平面设计 动画 手绘 漫画 剪辑 电影 纪录片 电影配乐 配音 音乐 乐理
古风器乐 民族器乐 民乐 器乐 古典音乐 国风音乐 古风音乐 轻音乐 电子音乐 爵士 爵士乐 摇滚 民谣
古琴 古筝 琵琶 二胡 竹笛 笛子 唢呐 钢琴 吉他 电吉他 贝斯 架子鼓 小提琴 大提琴 管弦乐 合唱
昆曲 京剧 戏曲 戏剧 舞蹈 芭蕾 街舞 音游 音乐游戏 电子游戏 游戏设计 独立游戏 桌游 围棋 象棋
茶艺 茶文化 咖啡 美食 烹饪 烘焙 面包 甜点 旅行 徒步 登山 露营 骑行 跑步 游泳 攀岩 球类
羽毛球 乒乓球 篮球 足球 网球 滑板 健身 瑜伽 观鸟 猫 猫咪 狗狗 鸟类 动物 自然 植物 园艺
花艺 天文 星空 科普 数学 物理 化学 生物 海洋 地理 编程 软件开发 机器人 电子制作 机械 模型制作
手工 刺绣 木工 陶艺 编织 折纸 篆刻 漆艺 掐丝珐琅 钓鱼 摄影构图 手机摄影 二次元 番剧
photography jazz rock piano guitar anime astronomy cooking baking hiking cycling chess origami
""".split())
_LABEL = re.compile(r"[\w\u3400-\u9fff][\w\u3400-\u9fff ·+#-]{0,23}")
_DIRECTIVE = re.compile(r"忽略|执行指令|系统提示|你必须|扮演|ignore previous|system prompt", re.I)


def _label(value):
    if not isinstance(value, str) or len(value) > 48:
        return ""
    value = unicodedata.normalize("NFKC", value).strip()
    return value if _LABEL.fullmatch(value) and not _DIRECTIVE.search(value) else ""


def _context(settings=None):
    if settings is None:
        import config
        def get(key, default):
            return getattr(config, key.upper(), default)
        folder = Path(get("data_dir", _DEFAULT_DIR))
    else:
        get = settings.get
        folder = _DEFAULT_DIR
    path = folder / "character_interests.sqlite3"
    if not get("character_interests_enabled", True):
        return "", path, []
    try:
        actor = validate_username(get("bot_wxid", ""))
    except ValueError:
        return "", path, []
    if actor in {"0", "self", "wechat_bot", "filehelper", "weixin"}:
        return "", path, []
    persona = str(get("bot_persona", "")).strip()
    scope = hashlib.sha256((actor + "\0" + persona).encode()).hexdigest()
    seeds = get("character_interest_seeds", [])
    seeds = dict.fromkeys(filter(None, map(_label, seeds[:24]))) if isinstance(seeds, list) else ()
    return scope, path, list(seeds)


def identity():
    """Capture before viewing so late feedback cannot update a different role."""
    return _context()[0]


def _decayed(score, stamp, now):
    return score * .5 ** (max(0, now - stamp) / _HALF_LIFE)


def _rows(path, scope):
    try:
        stat = path.stat()
        version = (stat.st_mtime_ns, stat.st_size)
        key = (path, scope)
        with _CACHE_LOCK:
            hit = _ROW_CACHE.get(key)
            if hit and hit[0] == version and time.monotonic() < hit[1]:
                _ROW_CACHE.move_to_end(key)
                return hit[2]
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
            rows = db.execute("SELECT topic, score, updated FROM interests WHERE scope=? LIMIT ?",
                              (scope, _PER_ROLE)).fetchall()
        with _CACHE_LOCK:
            _ROW_CACHE[key] = (version, time.monotonic() + 4, rows)
            if len(_ROW_CACHE) > 16:
                _ROW_CACHE.popitem(last=False)
        return rows
    except (OSError, sqlite3.Error):
        return ()


def _preferences(settings=None):
    scope, path, seeds = _context(settings)
    weights = {topic: 1.5 for topic in seeds}
    recent = {}
    now = time.time()
    for topic, score, stamp in _rows(path, scope) if scope else ():
        current = _decayed(score, stamp, now)
        if abs(current) >= .15:
            recent[topic] = current
            weights[topic] = weights.get(topic, 0) + current
    return seeds, recent, weights


def current_topics(settings=None):
    """Short local lists for the panel, independent of private contacts and chats."""
    seeds, recent, _ = _preferences(settings)
    recent = sorted(recent.items(), key=lambda item: -abs(item[1]))
    return {"seeds": seeds, "likes": [t for t, s in recent if s > 0][:5],
            "dislikes": [t for t, s in recent if s < 0][:3]}


def snapshot(settings=None):
    """A short internal note for the current main-model turn, without new inference."""
    topics = current_topics(settings)
    if not any(topics.values()):
        return ""
    state = {"基础爱好": topics["seeds"][:12], "最近更感兴趣": topics["likes"], "最近兴致较低": topics["dislikes"]}
    return "[角色自己的兴趣，仅在话题相关时自然体现]\n" + json.dumps(
        {k: v for k, v in state.items() if v}, ensure_ascii=False, separators=(",", ":"))


def _terms(value):
    terms = set()
    for token in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", unicodedata.normalize("NFKC", value).casefold()):
        if len(token) < 2 or token.isascii():
            terms.add(token)
        else:
            terms.update(token[i:i + 2] for i in range(len(token) - 1))
    return terms


def choose(candidates, *, rng=None):
    """Choose a (URL, title) from the caller's already eligible, unseen feed."""
    if not candidates:
        return None
    rng = rng or random
    weights = _preferences()[2]
    if not weights or rng.random() < _EXPLORATION:
        return rng.choice(candidates)
    interests = [(_terms(topic), weight) for topic, weight in weights.items()]
    scores = []
    for _, title in candidates:
        words = _terms(title)
        scores.append(sum(w * len(tags & words) / len(tags) for tags, w in interests if tags))
    best = max(scores)
    return rng.choice([item for item, score in zip(candidates, scores) if score == best])


def preferred_topic(*, rng=None):
    """Return one positive public hobby for discovery, or explore the unfiltered feed."""
    rng = rng or random
    if rng.random() < _EXPLORATION:
        return ""
    _, recent, weights = _preferences()
    cooling = {topic.casefold() for topic, score in recent.items() if score < 0}
    public = [(topic.casefold(), score) for topic, score in weights.items()
              if topic.casefold() in _PUBLIC and score > 0 and topic.casefold() not in cooling]
    if not public:
        return ""
    labels, scores = zip(*public)
    return rng.choices(labels, weights=scores, k=1)[0]


def record_reaction(topic, reaction, *, expected_identity=None):
    """Only the viewer calls this with its main model's own post-view reaction."""
    topic = _label(topic)
    scope, path, _ = _context()
    if (not scope or not topic or reaction not in ("like", "neutral", "dislike")
            or expected_identity is not None and expected_identity != scope):
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=1)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS interests (scope TEXT, topic TEXT COLLATE NOCASE, "
                       "score REAL NOT NULL, updated REAL NOT NULL, PRIMARY KEY(scope, topic))")
            now = time.time()
            old = db.execute("SELECT score, updated FROM interests WHERE scope=? AND topic=?", (scope, topic)).fetchone()
            score = _decayed(*old, now) if old else 0.
            if reaction == "neutral":
                score *= .75
            else:
                score = max(-4., min(4., score + (1 if reaction == "like" else -1)))
            if abs(score) < .15:
                db.execute("DELETE FROM interests WHERE scope=? AND topic=?", (scope, topic))
            else:
                db.execute("INSERT INTO interests(scope, topic, score, updated) VALUES(?,?,?,?) "
                           "ON CONFLICT(scope,topic) DO UPDATE SET score=excluded.score,updated=excluded.updated",
                           (scope, topic, score, now))
                db.execute("DELETE FROM interests WHERE rowid IN (SELECT rowid FROM interests WHERE scope=? "
                           "ORDER BY updated DESC,rowid DESC LIMIT -1 OFFSET ?)", (scope, _PER_ROLE))
                db.execute("DELETE FROM interests WHERE rowid IN (SELECT rowid FROM interests "
                           "ORDER BY updated DESC,rowid DESC LIMIT -1 OFFSET ?)", (_TOTAL,))
        with _CACHE_LOCK:
            _ROW_CACHE.clear()
        return True
    except (OSError, sqlite3.Error):
        return False
