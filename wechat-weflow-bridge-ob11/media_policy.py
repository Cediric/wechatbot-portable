"""Conservative reply-media decisions, scoped to each verified conversation.

This module never calls a model or sends anything. The caller supplies the main
model's final reply; visual understanding stays with that main model. Media are
optional additions and cannot erase the complete text on a failed selection.
"""
from dataclasses import dataclass
import math
import re
import threading
import time


MODES = frozenset(("text", "sticker", "voice", "text_sticker", "text_voice"))
_SERIOUS = re.compile(r"去世|死亡|葬礼|自杀|自残|抑郁|重病|失恋|分手|受伤|事故|报警|诈骗|危险|悲伤|难过|崩溃|伤心|痛苦|urgent|bereave|suicid|grief", re.I)
_TECHNICAL = re.compile(r"代码|报错|错误日志|程序|编译|安装|卸载|命令|参数|密钥|密码|验证码|银行卡|接口|上下文|思维链|工作流|数据库|api\b|http\b|python\b|javascript\b|powershell\b|sql\b|debug\b", re.I)
_CODE = re.compile(r"```|~~~|`[^`\n]+`|^\s*(?:def |class |import |from \S+ import |function\b|const |let |SELECT\b|INSERT\b|\$[\w]+\s*=|(?:sudo |pip |npm |python |git )[\w-])|[{}]\s*[;\n]", re.I | re.M)
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_PRIVATE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+\S{12,}|\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|(?:密码|密钥|验证码|口令|token|api[_ -]?key)\s*[:：=]\s*\S+|(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{16,19}(?!\d)", re.I)
_REASONING = re.compile(r"</?(?:think|thinking|analysis|reasoning)>|\[/?(?:analysis|reasoning)\]|reasoning_content\s*[:=]", re.I)
_EMOTIONS = (
    ("开心", re.compile(r"哈哈|嘿嘿|笑死|太好了|好开心|开心|高兴|好耶|哈哈哈|\bhaha\b|\byay\b|[😄😆😂🤣🥳]", re.I)),
    ("感谢", re.compile(r"谢谢|多谢|感谢|辛苦啦|辛苦了|\bthanks?\b|[🙏]", re.I)),
    ("期待", re.compile(r"期待|等不及|好期待|盼着|[🤩]", re.I)),
    ("晚安", re.compile(r"晚安|睡啦|睡觉啦|good night", re.I)),
    ("抱抱", re.compile(r"抱抱|抱一下|给你个抱|[🫂]", re.I)),
)
_COMFORT_WORDS = frozenset(("悲伤", "安慰", "抱抱", "理解", "陪伴", "难过", "sad", "comfort"))


@dataclass(frozen=True)
class VoicePlan:
    allowed: bool
    text: str = ""
    reason: str = ""


def prepare_voice(text, *, max_chars=600):
    """Reject unsuitable full replies before any billable synthesis occurs."""
    if not isinstance(text, str) or not text.strip():
        return VoicePlan(False, reason="empty")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    try:
        limit = min(2000, max(40, int(max_chars)))
    except (ValueError, TypeError, OverflowError):
        limit = 600
    if len(text) > limit:
        return VoicePlan(False, reason="too_long")
    if _REASONING.search(text):
        return VoicePlan(False, reason="reasoning")
    if _PRIVATE.search(text):
        return VoicePlan(False, reason="private_data")
    if _CODE.search(text):
        return VoicePlan(False, reason="code")
    if _URL.search(text):
        return VoicePlan(False, reason="copyable_url")
    technical = _TECHNICAL.findall(text)
    if technical and (len(text) > 180 or len(technical) >= 3):
        return VoicePlan(False, reason="technical_detail")
    if len(re.findall(r"^\s*(?:[-*]|\d+[.)、])\s*\S", text, re.M)) >= 2:
        return VoicePlan(False, reason="structured_steps")
    # Speech should not read formatting marks or stage directions aloud. Never
    # shorten a refused reply: its complete original text remains the fallback.
    spoken = re.sub(r"\*\*|__", "", text).strip()
    if not spoken:
        return VoicePlan(False, reason="empty")
    return VoicePlan(True, spoken, "conversational")


def emotion_keyword(text):
    """Return an evidenced intent, rather than a random mood."""
    text = str(text or "")
    for keyword, expression in _EMOTIONS:
        if expression.search(text):
            return keyword
    return ""


class ReplyMediaPolicy:
    """One owned instance, with independent cooldowns per stable session ID.

    Call once for the complete reply, not for every SSE delta. The optional
    request_id makes accidental repeated decisions idempotent. Automatic voice
    is opt-in; an explicit voice/sticker request may bypass a frequency limit,
    but never bypass speech content checks or unsuitable sticker intent.
    """
    def __init__(self, *, auto_sticker=True, auto_voice=False, sticker_cooldown=120,
                 sticker_min_turns=4, voice_cooldown=180, voice_min_turns=6,
                 voice_min_chars=24, voice_max_chars=600, clock=time.monotonic, max_sessions=2048):
        self.auto_sticker, self.auto_voice = bool(auto_sticker), bool(auto_voice)
        self.sticker_cooldown = self._number(sticker_cooldown, 120)
        self.voice_cooldown = self._number(voice_cooldown, 180)
        self.sticker_min_turns = max(1, int(sticker_min_turns))
        self.voice_min_turns = max(1, int(voice_min_turns))
        self.voice_min_chars = max(1, int(voice_min_chars))
        self.voice_max_chars = voice_max_chars
        self.clock, self.max_sessions = clock, max(1, int(max_sessions))
        self._sessions, self._lock = {}, threading.RLock()

    @staticmethod
    def _number(value, default):
        try:
            result = float(value)
            return max(0, result) if math.isfinite(result) else default
        except (TypeError, ValueError, OverflowError):
            return default

    def decide(self, user_text, assistant_text, *, requested_mode="text",
               sticker_keyword="", explicit_sticker=False, explicit_voice=False,
               session_id="", request_id="", voice_enabled=True, sticker_enabled=True):
        user_text, assistant_text = str(user_text or ""), str(assistant_text or "")
        combined = user_text + "\n" + assistant_text
        serious, technical = bool(_SERIOUS.search(combined)), bool(_TECHNICAL.search(combined) or _CODE.search(combined) or _URL.search(combined))
        mode = requested_mode if requested_mode in MODES else "text"
        keyword = str(sticker_keyword or "").strip()
        plan = prepare_voice(assistant_text, max_chars=self.voice_max_chars)
        stable_session = str(session_id or "").strip()
        now = self.clock()
        with self._lock:
            # Missing identity must not invent shared frequency state for two
            # unrelated chats. Explicit content checks still apply below.
            state = self._sessions.get(stable_session) if stable_session else None
            if state is not None and request_id and state.get("request_id") == str(request_id):
                return dict(state["result"])
            if state is None:
                state = {"turn": 0, "sticker_at": None, "sticker_turn": 0,
                         "voice_at": None, "voice_turn": 0}
                if stable_session:
                    if len(self._sessions) >= self.max_sessions:
                        self._sessions.pop(next(iter(self._sessions)))
                    self._sessions[stable_session] = state
            state["turn"] += 1
            wanted_voice = mode in ("voice", "text_voice") or bool(explicit_voice)
            wanted_sticker = mode in ("sticker", "text_sticker") or bool(explicit_sticker)
            reason = "text_default"
            if wanted_voice:
                if not voice_enabled:
                    mode, reason = "text", "voice_disabled"
                elif not plan.allowed:
                    mode, reason = "text", "voice_" + plan.reason
                elif not explicit_voice and not self.auto_voice:
                    mode, reason = "text", "voice_auto_disabled"
                elif not explicit_voice and not stable_session:
                    mode, reason = "text", "voice_identity_unverified"
                elif not explicit_voice and not self._ready(state, "voice", now):
                    mode, reason = "text", "voice_frequency_limited"
                else:
                    mode = "voice"
                    reason = "requested_voice"
            elif wanted_sticker:
                keyword = keyword or emotion_keyword(assistant_text) or emotion_keyword(user_text)
                acceptable = bool(keyword) and (not serious or (explicit_sticker and keyword.casefold() in _COMFORT_WORDS))
                if sticker_enabled and acceptable and (explicit_sticker or not technical):
                    mode = "sticker" if mode == "sticker" else "text_sticker"
                    reason = "requested_sticker"
                else:
                    mode, keyword, reason = "text", "", "sticker_unsuitable"
            elif stable_session and not serious and not technical and not _PRIVATE.search(combined) and not _REASONING.search(combined):
                keyword = emotion_keyword(assistant_text)
                if self.auto_sticker and sticker_enabled and keyword and self._ready(state, "sticker", now):
                    mode, reason = "text_sticker", "matched_emotion"
                elif self.auto_voice and voice_enabled and plan.allowed and len(plan.text) >= self.voice_min_chars and self._ready(state, "voice", now):
                    mode, reason = "voice", "conversational_voice"
                else:
                    keyword = ""
            if mode in ("voice", "text_voice"):
                state["voice_at"], state["voice_turn"] = now, state["turn"]
            if mode in ("sticker", "text_sticker"):
                state["sticker_at"], state["sticker_turn"] = now, state["turn"]
            result = {"reply_type": mode, "sticker_keyword": keyword if mode in ("sticker", "text_sticker") else "",
                      "voice_text": plan.text if mode in ("voice", "text_voice") else "", "reason": reason}
            state["request_id"], state["result"] = str(request_id or ""), dict(result)
            return result

    def _ready(self, state, kind, now):
        previous = state[kind + "_at"]
        return previous is None or (now - previous >= getattr(self, kind + "_cooldown") and
                                    state["turn"] - state[kind + "_turn"] >= getattr(self, kind + "_min_turns"))

    def forget(self, session_id):
        with self._lock:
            self._sessions.pop(str(session_id), None)


class ReplyVoiceAccumulator:
    """Accumulate numbered text segments and permit a single final voice plan."""
    def __init__(self, *, max_chars=600):
        self.max_chars, self._pieces, self._finished = max_chars, {}, False
        self._overflow = False
        self._length = 0
        self._lock = threading.RLock()

    def add(self, sequence, text):
        with self._lock:
            if self._finished or type(sequence) is not int or sequence < 0 or not isinstance(text, str):
                return False
            if sequence in self._pieces:
                return False
            # Bound memory even for a malformed, endless stream. Refuse voice
            # rather than speak a truncated version of the final answer.
            if len(self._pieces) >= 4096 or self._length + len(text) > 20000:
                self._overflow = True
                return False
            self._pieces[sequence] = text
            self._length += len(text)
            return True

    def finish(self, *, cancelled=False):
        with self._lock:
            if self._finished:
                return VoicePlan(False, reason="already_finished")
            self._finished = True
            if cancelled:
                return VoicePlan(False, reason="cancelled")
            if self._overflow:
                return VoicePlan(False, reason="too_long")
            indices = sorted(self._pieces)
            if indices and indices != list(range(indices[-1] + 1)):
                return VoicePlan(False, reason="missing_segment")
            return prepare_voice("".join(self._pieces[i] for i in indices), max_chars=self.max_chars)
