"""Local, extractive budgeting for a single untrusted memory input.

There is no accumulated session history or LLM summary here. Every retained
excerpt is a literal source range, so paraphrases cannot become evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re

_BOUNDARY = re.compile(r'[。！？!?；;]+["\'’”）)]*\s*|\r?\n|\.(?=\s|$)')
_DURABLE = re.compile(r'喜欢|不喜欢|讨厌|偏好|习惯|过敏|希望|以后|下次|约定|生日|住在|来自|朋友|家人|亲人|关系|一直|通常|改成|现在|以后|\b(?:prefer|like|dislike|allergic|birthday|promise|usually|always|next time)\b', re.I)
_SELF = re.compile(r'我|本人|自己|\b(?:I|my|me)\b', re.I)


@dataclass(frozen=True)
class MemoryInput:
    messages: list
    spans: tuple
    compressed: bool
    estimated_input_tokens: int
    context_cap: int
    output_reserved: int

    def accepts_evidence(self, evidence):
        return isinstance(evidence, str) and bool(evidence) and any(evidence in item[2] for item in self.spans)


def _messages(prompt, payload):
    return [{'role': 'system', 'content': prompt},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}]


def _cost(messages, configured_prompt):
    values = [dict(message) for message in messages]
    if configured_prompt and configured_prompt not in values[0]['content']:
        values[0]['content'] += '\n' + configured_prompt
    # UTF-8 bytes deliberately overestimate text tokens and include JSON
    # escaping plus role overhead. model_client therefore need not cut JSON.
    return sum(len(message['content'].encode('utf-8')) + 16 for message in values)


def _sentence_spans(text):
    start = 0
    result = []
    for boundary in _BOUNDARY.finditer(text):
        end = boundary.end()
        if text[start:end].strip():
            result.append((start, end, text[start:end]))
        start = end
    if start < len(text) and text[start:].strip():
        result.append((start, len(text), text[start:]))
    return result


def prepare_memory_input(text, prompt, profile, *, session_id, actor_id, message_id, source_truncated=False):
    """Return one bounded extraction input, or None if no intact block fits.

    IDs are opaque metadata copied unchanged. A compressed request contains
    separate original ranges, never a synthesized joined sentence/summary.
    """
    text = str(text)
    cap, output = int(profile.get('context_tokens', 8192)), int(profile.get('max_tokens', 1024))
    if not 1024 <= cap <= 2000000 or not 16 <= output < cap:
        raise ValueError('记忆模型上下文必须大于输出预算')
    options = profile.get('compression', {})
    options = options if isinstance(options, dict) else {}
    threshold = float(options.get('threshold', .82))
    if not math.isfinite(threshold) or not .5 <= threshold <= .95:
        raise ValueError('记忆原文压缩阈值无效')
    enabled = bool(options.get('enabled', True))
    configured = str(profile.get('prompt', ''))
    budget = cap - output - 128
    identity = {'session_id': session_id, 'actor_id': actor_id, 'message_id': message_id}
    if source_truncated:
        identity['source_truncated'] = True
        prompt += '\n原消息因长度上限被截取时，最后未结束的句子不能作为长期断言的证据。'
    candidates = _sentence_spans(text)
    if source_truncated and candidates:
        # Do not preserve a half-sentence as a durable preference: the omitted
        # suffix may be a negation or correction. Completed earlier units stay.
        last_boundary = list(_BOUNDARY.finditer(text))
        complete_end = last_boundary[-1].end() if last_boundary else 0
        candidates = [item for item in candidates if item[1] <= complete_end]
    if not candidates:
        return None
    messages = _messages(prompt, {**identity, 'untrusted_message': text})
    cost = _cost(messages, configured)
    if cost <= budget and (not enabled or cost <= int(budget * threshold)):
        evidence_end = candidates[-1][1] if source_truncated else len(text)
        return MemoryInput(messages, ((0, evidence_end, text[:evidence_end]),), False, cost, cap, output)
    if not enabled:
        return None

    target = int(budget * threshold)
    base = {**identity, 'untrusted_message_excerpts': [],
            'excerpt_policy': '每个片段是同一原消息的独立连续原文；不能把不相邻片段拼成证据。'}
    compressed_prompt = prompt + '\n输入是原文片段时，evidence必须逐字完整落在某一个text内；禁止跨片段拼接。start/end是原消息字符范围，不是新身份信息。'
    if _cost(_messages(compressed_prompt, base), configured) > target:
        return None
    # No new model call is used for selection. Favor first-person durable
    # statements and later corrections; keep original order in the payload.
    ranked = sorted(candidates, key=lambda item: (len(_DURABLE.findall(item[2])) * 10 + bool(_SELF.search(item[2])) * 3, item[0]), reverse=True)
    selected = []
    for span in ranked:
        if len(selected) >= 48:
            break
        proposed = sorted(selected + [span], key=lambda item: item[0])
        payload = {**base, 'untrusted_message_excerpts': [{'start': start, 'end': end, 'text': raw} for start, end, raw in proposed]}
        proposed_messages = _messages(compressed_prompt, payload)
        if _cost(proposed_messages, configured) <= target:
            selected = proposed
    if not selected:
        return None
    payload = {**base, 'untrusted_message_excerpts': [{'start': start, 'end': end, 'text': raw} for start, end, raw in selected]}
    messages = _messages(compressed_prompt, payload)
    return MemoryInput(messages, tuple(selected), True, _cost(messages, configured), cap, output)
