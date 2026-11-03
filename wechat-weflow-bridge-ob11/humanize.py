"""Conversational layout without losing or randomly altering content."""
import re
import math
import time
import config

_STYLE_HINT = "（请用自然微信口语回答：先回应对方重点，结合上下文与偏好；不重复寒暄，避免客服套话。简单问题简短回答，复杂问题保留完整解释、代码、链接与必要步骤。不要编造现实经历或未完成的操作。）"

def enabled():
    return bool(getattr(config, "HUMAN_LIKE_ENABLED", True))

def clean_ai_text(text):
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()

def split_bubbles(text):
    text = clean_ai_text(text)
    if not text:
        return []
    max_bubbles = max(1, int(getattr(config, "HUMAN_MAX_BUBBLES", 4)))
    # Structured answers and links stay copyable.
    if "```" in text or re.search(r"https?://|^\s*(?:[-*]|\d+[.)])\s", text, re.M):
        return [text]
    pieces = re.findall(r"[^。！？!?\n]*[。！？!?\n]+|[^。！？!?\n]+$", text)
    # Length belongs in the model prompt; only complete sentences/newlines form bubbles.
    # Keep the remaining answer intact when the bubble count limit is reached.
    if len(pieces) > max_bubbles:
        return pieces[:max_bubbles-1] + ["".join(pieces[max_bubbles-1:])]
    return pieces or [text]

def style_prefix():
    return _STYLE_HINT+"\n" if enabled() else ""

def voice_script(text):
    return clean_ai_text(text)

def prepare_bubbles(text):
    if enabled():
        return split_bubbles(text)
    text = clean_ai_text(text)
    return [text] if text else []

def wait_remaining(started_monotonic, minimum_seconds, cancelled, *, clock=time.monotonic, sleeper=time.sleep):
    """Wait only an unspent response floor; poll cancellation at most 50 ms apart."""
    try:
        start, minimum = float(started_monotonic), float(minimum_seconds)
        now = clock()
        if not all(math.isfinite(x) for x in (start, minimum, now)) or start > now:
            return not cancelled()
        remaining = max(0.0, min(10.0, minimum) - (now - start))
    except (ValueError, TypeError):
        return not cancelled()
    deadline = now + remaining
    while not cancelled():
        remaining = deadline - clock()
        if remaining <= 0:
            return True
        sleeper(min(0.05, remaining))
    return False

def wait_response_floor(started_monotonic, cancelled):
    return wait_remaining(started_monotonic, getattr(config, "RESPONSE_MIN_SECONDS", 0.8), cancelled)

def wait_bubble_gap(cancelled):
    return wait_remaining(time.monotonic(), getattr(config, "HUMAN_BUBBLE_INTERVAL_SECONDS", 0.25), cancelled)
