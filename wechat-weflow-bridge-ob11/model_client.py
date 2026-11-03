"""Bounded stateless model requests. No hidden shared conversation or retries.

Each caller supplies the current conversation; reasoning text is never returned
as a chat answer. Context budgets are conservative estimates, not a tokenizer.
"""
from __future__ import annotations
import base64
import copy
import io
import json
import math
import re
import time
from urllib.parse import urlsplit

from model_catalog import PROFILES
from model_workflow import with_workflow_prompt, workflow_prompt


class ModelError(RuntimeError):
    pass


def _check_cancel(cancelled):
    if cancelled and (cancelled.is_set() if hasattr(cancelled, "is_set") else cancelled()):
        raise ModelError("模型请求已取消")


def persona_prompt(settings=None):
    """Trusted role card and native identity constraints, without a task format."""
    if settings is None:
        import config
        def get(key, default):
            return getattr(config, key.upper(), default)
    else:
        def get(key, default):
            return settings.get(key, default)
    persona = str(get("bot_persona", "")).strip()
    owners = get("bot_owner_wechat_ids", [])
    if not isinstance(owners, list) or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,256}", x) or x.endswith("@chatroom") or x in {"unknown", "null", "None"} for x in owners):
        raise ModelError("角色主人须填写真实微信 username 列表")
    if not persona:
        return ""
    owner_rule = ("角色主人的已绑定真实微信 ID：" + json.dumps(owners, ensure_ascii=False)
        if owners else "尚未绑定角色主人的真实微信 ID，不按昵称、备注或自称认定当前来人是角色主人。")
    owner_rule += "\n仅依据桥接提供的核验身份区分人与群成员；别名只属于角色设定，不是身份凭据。身份注记供内部判断，不复述到聊天正文。"
    return "[角色卡]\n" + persona + "\n\n[角色关系身份规则]\n" + owner_rule


def profile_settings(profile, settings=None):
    if profile not in PROFILES:
        raise ModelError("未知模型角色")
    prefix, label, prompt_key, context, output = PROFILES[profile]
    if settings is None:
        import config
        def get(key, default):
            return getattr(config, key.upper(), default)
    else:
        def get(key, default):
            return settings.get(key, default)
    values = {
        "profile": profile, "label": label, "api_base": str(get(prefix+"_api_base", "")).rstrip("/"),
        "api_key": str(get(prefix+"_api_key", "")), "model": str(get(prefix+"_model", "")),
        "prompt": str(get(prompt_key, "")), "context_tokens": int(get(prefix+"_context_tokens", context)),
        "max_tokens": int(get(prefix+"_max_tokens", output)), "thinking": get(prefix+"_thinking", "auto"),
        "thinking_format": get(prefix+"_thinking_format", "auto"),
        "thinking_budget": int(get(prefix+"_thinking_budget", 0)),
        "reasoning_effort": get(prefix+"_reasoning_effort", "auto"),
        "temperature": float(get(prefix+"_temperature", 0.6)), "timeout": float(get(prefix+"_timeout", 35)),
    }
    if profile == "memory":
        values["compression"] = {"enabled": bool(get("memory_context_compression_enabled", True)), "threshold": float(get("memory_context_compression_threshold", 0.82))}
    if profile == "main":
        values["top_p"] = float(get("main_llm_top_p", -1))
        if values["top_p"] != -1 and not 0 <= values["top_p"] <= 1:
            raise ModelError("主模型 Top P 必须为 -1 或 0 至 1")
        if values["context_tokens"] > 500000:
            raise ModelError("主模型上下文上限不能超过500000 token")
        values["compression"] = {
            "enabled": bool(get("main_context_compression_enabled", True)),
            "threshold": float(get("main_context_compression_threshold", 0.82)),
            "keep_recent_ratio": float(get("main_context_compression_keep_recent_ratio", 0.15)),
            "prompt": str(get("main_context_compression_prompt", "")),
        }
        values["streaming"] = {"enabled": bool(get("main_streaming_enabled", True)), "max_chars": int(get("main_stream_max_chars", 160))}

    if profile in {"main", "group", "autopilot", "moments_decision", "image_prompt"}:
        persona = persona_prompt(settings)
        if persona:
            values["prompt"] = persona + ("\n\n[本模型的任务要求]\n" + values["prompt"] if values["prompt"] else "")
    values["prompt"] = with_workflow_prompt(profile, values["prompt"])
    values["workflow_prompt"] = workflow_prompt(profile)
    return values


def reasoning_options(values):
    mode, style = values.get("thinking", "auto"), values.get("thinking_format", "auto")
    effort, budget = values.get("reasoning_effort", "auto"), values.get("thinking_budget", 0)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or not 0 <= budget <= 131072 or int(budget) != budget:
        raise ModelError("思考预算必须是0至131072的整数")
    if mode not in {"auto", "on", "off"} or style not in {"auto", "openai", "qwen", "deepseek", "none"}:
        raise ModelError("思考模式设置无效")
    if effort not in {"auto", "minimal", "low", "medium", "high", "max"}:
        raise ModelError("思考强度设置无效")
    if style == "auto":
        host = urlsplit(values.get("api_base", "")).hostname or ""
        model = values.get("model", "").lower()
        style = "qwen" if "qwen" in model else "deepseek" if "deepseek" in model or host == "api.deepseek.com" else "openai"
    if style == "none":
        if mode != "auto" or effort != "auto" or budget:
            raise ModelError("该接口不支持已指定的思考参数，请改用模型默认")
        return {}
    options = {}
    if style == "qwen":
        if mode != "auto":
            options["enable_thinking"] = mode == "on"
        if mode != "off" and budget:
            options["thinking_budget"] = int(budget)
        if effort != "auto":
            raise ModelError("千问使用思考预算，请将思考强度设为模型默认")
    elif style == "deepseek":
        if budget:
            raise ModelError("DeepSeek 使用思考强度，不支持千问思考预算")
        if mode != "auto":
            options["thinking"] = {"type": "enabled" if mode == "on" else "disabled"}
        if mode != "off" and effort != "auto":
            options["reasoning_effort"] = effort
    else:
        if budget:
            raise ModelError("OpenAI 兼容接口使用思考强度，不支持千问思考预算")
        if mode == "off":
            options["reasoning_effort"] = "none"
        elif effort != "auto":
            options["reasoning_effort"] = effort
        elif mode == "on":
            options["reasoning_effort"] = "medium"
    return options


def sampling_options(values, reasoning):
    """Only emit sampling controls supported by this role, model and mode."""
    model = values["model"].lower()
    if re.match(r"^(?:o[1-9](?:-|$)|gpt-5(?:[.-]|$))", model):
        return {}
    off = values["thinking"] == "off"
    temperature = {"temperature": values["temperature"]}
    if values["profile"] != "main":
        return temperature if off or not reasoning else {}
    top_p = values["top_p"]
    if model == "deepseek-flash":
        if off:
            return temperature
        return {"top_p": max(.95, top_p)} if top_p >= 0 else {}
    if model == "deepseek-v4-pro" and not off:
        return {}
    if reasoning and not off and "qwen" not in model:
        return {}
    if top_p >= 0:
        temperature["top_p"] = top_p
    return temperature


def estimate_tokens(content):
    if isinstance(content, list):
        return sum(estimate_tokens(p.get("text", "")) if p.get("type") == "text" else 1536 for p in content if isinstance(p, dict))
    text = str(content or "")
    # Reserve one token per non-ASCII character; ASCII receives a 3-char estimate.
    ascii_count = len(text) if text.isascii() else len(text.encode("ascii", "ignore"))
    return len(text)-ascii_count + (ascii_count+2)//3 + 8


def _shorten(content, budget):
    if isinstance(content, str):
        while content and estimate_tokens(content) > budget:
            content = content[:max(0, len(content)-(estimate_tokens(content)-budget))]
        return content
    if isinstance(content, list):
        result = copy.deepcopy(content)
        media_cost = sum(1536 for p in result if isinstance(p, dict) and p.get("type") != "text")
        if media_cost >= budget:
            raise ModelError("本条媒体超过上下文预算，请提高主模型上下文长度或减少采样帧")
        texts = [p for p in result if isinstance(p, dict) and p.get("type") == "text"]
        remaining = budget-media_cost
        for part in texts:
            part["text"] = _shorten(str(part.get("text", "")), max(0, remaining))
            remaining -= estimate_tokens(part["text"])
        return result
    raise ModelError("模型消息格式无效")


def _message_cost(message):
    cost = estimate_tokens(message.get("content"))
    for key in ("tool_calls", "function_call", "name", "tool_call_id"):
        if key in message:
            cost += estimate_tokens(json.dumps(message[key], ensure_ascii=False))
    return cost


def trim_messages(messages, context_tokens, max_tokens):
    if not isinstance(messages, list) or not messages or not 1024 <= context_tokens <= 2000000 or not 16 <= max_tokens < context_tokens:
        raise ModelError("上下文长度必须大于最大输出长度")
    if any(not isinstance(m, dict) or m.get("role") not in {"system", "user", "assistant", "tool"} for m in messages):
        raise ModelError("模型消息角色无效")
    values = copy.deepcopy(messages)
    budget = context_tokens-max_tokens-128
    if budget < 128:
        raise ModelError("模型输入预算不足")
    # Keep system instructions and the current input; discard oldest complete
    # history first. Do not merge inputs from unrelated moments or sessions.
    costs = [_message_cost(message) for message in values]
    total = sum(costs)
    removed = set()
    index = 0
    while len(values)-len(removed) > 2 and total > budget:
        while index < len(values)-1 and values[index]["role"] == "system":
            index += 1
        if index >= len(values)-1:
            break
        removed.add(index)
        total -= costs[index]
        index += 1
        # Removing an old user turn must also remove the assistant/tool
        # continuation. Keep tool-call responses attached to their request.
        while index < len(values)-1 and values[index]["role"] in {"assistant", "tool"}:
            removed.add(index)
            total -= costs[index]
            index += 1
    if removed:
        values = [message for i, message in enumerate(values) if i not in removed]
    if total > budget:
        for index in reversed(range(len(values))):
            if values[index]["role"] != "system":
                others = total - estimate_tokens(values[index].get("content"))
                values[index]["content"] = _shorten(values[index].get("content", ""), budget-others)
                total = others + estimate_tokens(values[index]["content"])
                break
    if total > budget:
        raise ModelError("系统提示词超过上下文预算，请缩短提示词")
    return values


def image_part(path, *, max_pixels=4000000, max_bytes=8*1024*1024):
    """Encode a verified local image for the main model, never accept URLs here."""
    from pathlib import Path
    from PIL import Image, ImageOps
    target = Path(path)
    if not target.is_file() or target.stat().st_size > 30*1024*1024:
        raise ModelError("主模型图片不存在或超过大小限制")
    with Image.open(target) as image:
        if image.width*image.height > 40000000:
            raise ModelError("主模型图片像素超限")
        image.seek(0)
        picture = ImageOps.exif_transpose(image).convert("RGB")
        picture.thumbnail((1536,1536))
        with io.BytesIO() as stream:
            picture.save(stream, format="JPEG", quality=85)
            raw = stream.getvalue()
        if picture.width*picture.height > max_pixels or len(raw) > max_bytes:
            raise ModelError("主模型图片超过输入限制")
    return {"type":"image_url", "image_url":{"url":"data:image/jpeg;base64,"+base64.b64encode(raw).decode("ascii")}}


def _response_notes(document, choice, message):
    """Only bounded enum/count metadata is safe to include in an error."""
    finish = choice.get("finish_reason")
    allowed = {"stop", "length", "content_filter", "tool_calls", "insufficient_system_resource", "aborted"}
    finish = finish if isinstance(finish, str) and finish in allowed else "unknown"
    content = message.get("content")
    kind = ("missing" if "content" not in message else "null" if content is None else
        "string" if isinstance(content, str) else "text_parts" if isinstance(content, list) else
        "object" if isinstance(content, dict) else "boolean" if isinstance(content, bool) else
        "number" if isinstance(content, (int, float)) else "other")
    notes = ["finish="+finish, "content="+kind]
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        notes.append("reasoning=present")
    usage = document.get("usage")
    if isinstance(usage, dict):
        details = usage.get("completion_tokens_details")
        counts = [("completion_tokens", usage.get("completion_tokens"))]
        if isinstance(details, dict):
            counts.append(("reasoning_tokens", details.get("reasoning_tokens")))
        for name, value in counts:
            if type(value) is int and 0 <= value <= 1_000_000_000:
                notes.append(name+"="+str(value))
    return "；".join(notes)


def _final_content(document):
    if not isinstance(document, dict):
        raise ModelError("模型响应结构无效（根节点不是对象）")
    choices = document.get("choices")
    if not isinstance(choices, list):
        raise ModelError("模型响应结构无效（choices缺失或不是列表）")
    if not choices:
        raise ModelError("模型响应结构无效（choices为空）")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ModelError("模型响应结构无效（首个候选缺少message对象）")
    message = choice["message"]
    notes = _response_notes(document, choice, message)
    # A nonempty prefix can still be cut in the middle of JSON or a sentence.
    # Do not treat that prefix, or reasoning in its place, as a final answer.
    if choice.get("finish_reason") == "length":
        raise ModelError("模型输出达到长度上限，正文可能被截断（"+notes+"）")
    content = message.get("content")
    if isinstance(content, list):
        parts = [part for part in content if isinstance(part, dict) and part.get("type") == "text"]
        if any(not isinstance(part.get("text"), str) for part in parts):
            raise ModelError("模型正文文本分段格式无效（"+notes+"）")
        # Compatible text parts only: never copy reasoning/refusal/unknown parts.
        content = "".join(part["text"] for part in parts)
    elif content is not None and not isinstance(content, str):
        raise ModelError("模型正文格式不受支持（"+notes+"）")
    if not isinstance(content, str) or not content.strip():
        reason = message.get("reasoning_content")
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            description = "模型返回拒绝信息，未返回最终正文"
        elif isinstance(reason, str) and reason.strip():
            description = "模型仅返回推理，未返回最终正文"
        else:
            description = "模型返回空正文"
        raise ModelError(description+"（"+notes+"）")
    if len(content) > 100000:
        raise ModelError("模型正文超过长度限制（"+notes+"）")
    return content.strip()


def request(profile, messages, *, json_mode=False, cancelled=None, timeout=None, prompt_override=None):
    import http_util
    _check_cancel(cancelled)
    values = profile_settings(profile)
    if prompt_override is not None:
        if not isinstance(prompt_override, str):
            raise ModelError("模型任务提示词必须是文本")
        # Callers sharing credentials may have distinct output contracts. The
        # override is trusted local configuration, never model/user data.
        values["prompt"] = with_workflow_prompt(profile, prompt_override)
    if profile == "main":
        import config
        if getattr(config, "CHARACTER_INTERESTS_ENABLED", False):
            from character_interests import snapshot
            interests = snapshot()
            if interests:
                values["prompt"] += "\n\n" + interests
    parsed = urlsplit(values["api_base"])
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelError(values["label"]+"地址未配置或无效")
    if not values["model"]:
        raise ModelError(values["label"]+"名称未配置")
    if not values["api_key"] and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ModelError(values["label"]+"密钥未配置")
    options = reasoning_options(values)
    if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) for m in messages):
        raise ModelError("模型消息格式无效")
    # trim_messages owns the deep copy. Only copy the outer list and any system
    # message we edit here, so large media/history are not copied twice.
    messages = list(messages)
    if values["prompt"]:
        if messages and messages[0].get("role") == "system" and isinstance(messages[0].get("content"), str):
            if values["prompt"] not in messages[0]["content"]:
                messages[0] = dict(messages[0])
                messages[0]["content"] += "\n"+values["prompt"]
        else:
            messages.insert(0, {"role":"system", "content":values["prompt"]})
    payload = {"model":values["model"], "messages":trim_messages(messages, values["context_tokens"], values["max_tokens"]),
        "max_tokens":values["max_tokens"], **options}
    payload.update(sampling_options(values, options))
    if json_mode:
        payload["response_format"] = {"type":"json_object"}
    limit = float(values["timeout"] if timeout is None else timeout)
    if not 0.1 <= limit <= 300:
        raise ModelError("模型超时设置无效")
    deadline = time.monotonic()+limit
    response = None
    try:
        headers = {"Content-Type":"application/json"}
        if values["api_key"]:
            headers["Authorization"] = "Bearer "+values["api_key"]
        _check_cancel(cancelled)
        response = http_util.session().post(values["api_base"]+"/chat/completions", headers=headers,
            json=payload, timeout=(min(5,limit),limit), stream=True)
        _check_cancel(cancelled)
        if response.status_code != 200:
            raise ModelError(values["label"]+" HTTP "+str(response.status_code))
        if time.monotonic() >= deadline:
            raise ModelError(values["label"]+"读取超时")
        body, size = [], 0
        for chunk in response.iter_content(chunk_size=16384):
            _check_cancel(cancelled)
            if time.monotonic() >= deadline:
                raise ModelError(values["label"]+"读取超时")
            size += len(chunk)
            if size > 2*1024*1024:
                raise ModelError("模型响应超过限制")
            body.append(chunk)
        _check_cancel(cancelled)
        if time.monotonic() >= deadline:
            raise ModelError(values["label"]+"读取超时")
        try:
            document = json.loads(b"".join(body).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ModelError("模型响应不是有效JSON") from None
        return _final_content(document)
    except ModelError:
        raise
    except Exception as error:
        raise ModelError(values["label"]+"调用失败（"+type(error).__name__+"）") from None
    finally:
        if response is not None:
            response.close()
