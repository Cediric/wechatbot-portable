"""
配置模块：加载 config.json，提供全局配置常量。
"""

import json
import os
import logging
import threading
import tempfile
import re
import stat
from urllib.parse import urlsplit
from config_model import CONFIG_SCHEMA, normalize_config, validate_model_budgets
from logging.handlers import RotatingFileHandler

# ============ 配置 ============

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")


def load_config():
    if not os.path.exists(CONFIG_FILE):
        example = os.path.join(BASE_DIR, "config.example.json")
        if os.path.exists(example):
            import shutil
            shutil.copyfile(example, CONFIG_FILE)
            print("[config] 未找到 config.json，已从 config.example.json 生成，请填写配置后重启。")
        else:
            raise FileNotFoundError("缺少 config.json 且无 config.example.json")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    if not isinstance(loaded, dict):
        raise ValueError("config.json 必须是配置对象")
    # 旧版的个人风格开关同时绑定了会话票据。升级只保留原有的发送来源设置。
    if "require_reply_ticket" not in loaded and (loaded.get("reply_style_no_letters") or loaded.get("reply_style_allowed_punctuation")):
        loaded["require_reply_ticket"] = True
    known = {field.key for group in CONFIG_SCHEMA for field in group.fields}
    loaded.update(normalize_config({key: value for key, value in loaded.items() if key in known}))
    validate_model_budgets(loaded)
    return loaded


config = load_config()
_config_lock = threading.RLock()


def _get(key, default=None):
    return config.get(key, default)


# JSON 字段名 → 本模块常量名（少数不是简单 upper）
_RUNTIME_KEY_MAP = {
    "weflow_base_url": "WE_FLOW_BASE_URL",
}


def apply_runtime_config(new_cfg: dict) -> None:
    """把 Web 面板保存的字段同步到本模块常量，尽量即时生效。"""
    import sys
    mod = sys.modules[__name__]
    for k, v in (new_cfg or {}).items():
        if k.startswith("_"):
            continue
        attr = _RUNTIME_KEY_MAP.get(k, k.upper())
        setattr(mod, attr, v)
        config[k] = v
    # 规范化列表字段
    refs = getattr(mod, "BOT_IMAGE_REFS", [])
    if isinstance(refs, str):
        mod.BOT_IMAGE_REFS = [p.strip().strip('"').strip("'") for p in refs.split(",") if p.strip()]
    nicks = getattr(mod, "BOT_NICKNAMES", [])
    if isinstance(nicks, str):
        mod.BOT_NICKNAMES = [x.strip() for x in nicks.replace("，", ",").split(",") if x.strip()]
    contacts = getattr(mod, "MOMENTS_INTERACTION_CONTACTS", [])
    if isinstance(contacts, str):
        mod.MOMENTS_INTERACTION_CONTACTS = normalize_config({"moments_interaction_contacts": contacts})["moments_interaction_contacts"]
    for key in ("STICKER_DIR", "TTS_OUTPUT_DIR", "MEMORY_DB_PATH", "MOMENTS_HISTORY_PATH"):
        path = getattr(mod, key, "")
        if path and not os.path.isabs(path):
            setattr(mod, key, os.path.join(BASE_DIR, path))


def save_config(updates: dict) -> dict:
    """Validate before mutation; atomically replace the file under one lock."""
    normalized = normalize_config(updates)
    for key in ("weflow_base_url", "astrbot_ob_url", "ollama_base_url", "image_caption_api_base", "memory_llm_api_base", "group_reply_judge_api_base", "autopilot_llm_api_base", "dashscope_base_url"):
        if key in normalized and normalized[key]:
            url = urlsplit(normalized[key])
            allowed = ("ws", "wss") if key == "astrbot_ob_url" else ("http", "https")
            if url.scheme not in allowed or not url.hostname or url.username or url.password:
                raise ValueError(f"{key} 必须是有效的 {allowed} 地址，凭据请单独填写")
    with _config_lock:
        merged = dict(config)
        merged.update(normalized)
        validate_model_budgets(merged)
        fd, tmp = tempfile.mkstemp(prefix="config-", suffix=".json", dir=BASE_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            previous = None
            if os.name == "nt" and os.path.exists(CONFIG_FILE):
                previous = os.stat(CONFIG_FILE)
                if previous.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
                    # Windows chmod changes only the read-only bit. This file
                    # is mutable settings, even when an extractor marked it read-only.
                    os.chmod(CONFIG_FILE, stat.S_IWRITE)
            try:
                os.replace(tmp, CONFIG_FILE)
            except OSError:
                if previous is not None and previous.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
                    try:
                        current = os.stat(CONFIG_FILE)
                    except FileNotFoundError:
                        current = None
                    if current is not None and (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) == (
                            previous.st_dev, previous.st_ino, previous.st_size, previous.st_mtime_ns):
                        os.chmod(CONFIG_FILE, stat.S_IREAD)
                raise
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        apply_runtime_config(merged)
        return dict(merged)


# ============ 基础连接 ============

WE_FLOW_BASE_URL = _get("weflow_base_url", "http://127.0.0.1:5031")
ACCESS_TOKEN = _get("access_token", "")
ASTRBOT_ATTACHMENTS = _get("astrbot_attachments", "")
BOT_NICKNAMES = _get("bot_nicknames", [])
BOT_WXID = _get("bot_wxid", "")
BUFFER_SECONDS = _get("buffer_seconds", 5)
WEB_PORT = _get("web_port", 8766)
GROUP_REPLY_MODE = _get("group_reply_mode", "mention")  # "mention" / "all" / "batch"
ASTRBOT_OB_URL = _get("astrbot_ob_url", "ws://127.0.0.1:11229/ws")
REQUIRE_REPLY_TICKET = _get("require_reply_ticket", False)

# 真人感：短气泡、先读后打、夜里更慢
HUMAN_LIKE_ENABLED = _get("human_like_enabled", True)
HUMAN_MAX_BUBBLES = _get("human_max_bubbles", 4)

# 共享角色卡供主聊天及辅助决策使用；记忆抽取仅处理原文事实证据。
BOT_PERSONA = _get("bot_persona", "你是微信里的 AI 助手。用自然口语回应，不编造现实经历。")

# ============ 图片描述配置 ============

IMAGE_CAPTION_PROVIDER = _get("image_caption_provider", "ollama")  # "ollama" / "openai"
IMAGE_CAPTION_MODEL = _get("image_caption_model", "llava:7b")
IMAGE_CAPTION_API_KEY = _get("image_caption_api_key", "")
IMAGE_CAPTION_API_BASE = _get("image_caption_api_base", "")
IMAGE_CAPTION_PROMPT = _get("image_caption_prompt", "请用中文简短描述这张图片的内容")
OLLAMA_BASE_URL = _get("ollama_base_url", "http://127.0.0.1:61000")
OLLAMA_TIMEOUT = _get("ollama_timeout", 60)

# ============ 发送增强 ============

# 发图片方式：cf_hdrop = 文件剪贴板（保留 GIF/WebP 动画）；clipboard_image = 旧版 SetImage
IMAGE_SEND_METHOD = _get("image_send_method", "cf_hdrop")
# 微信表情包发送开关
STICKER_ENABLED = _get("sticker_enabled", True)
# 自定义 OneBot face → 微信表情文本映射文件（JSON），留空使用内置映射
FACE_MAPPING_FILE = _get("face_mapping_file", "")
# 本地表情包目录（图片文件 + manifest.json）
STICKER_DIR = _get("sticker_dir", os.path.join(DATA_DIR, "stickers"))

# ============ 语音发送（TTS + VB-Cable + UIA） ============

VOICE_ENABLED = _get("voice_enabled", True)
TTS_PROVIDER = _get("tts_provider", "dashscope")  # dashscope
DASHSCOPE_API_KEY = _get("dashscope_api_key", "")
DASHSCOPE_BASE_URL = _get("dashscope_base_url", "https://dashscope.aliyuncs.com/api/v1")
TTS_MODEL = _get("tts_model", "cosyvoice-v2")
TTS_VOICE = _get("tts_voice", "")
TTS_OUTPUT_DIR = _get("tts_output_dir", os.path.join(DATA_DIR, "tts_cache"))
VB_CABLE_DEVICE = _get("vb_cable_device", "CABLE Input")
VOICE_HOLD_MS = _get("voice_hold_ms", 3000)
VOICE_EXTRA_PADDING_MS = _get("voice_extra_padding_ms", 500)

# ============ 共享记忆库 ============

MEMORY_ENABLED = _get("memory_enabled", True)
MEMORY_DB_PATH = _get("memory_db_path", os.path.join(DATA_DIR, "memory.db"))
MEMORY_TOP_K = _get("memory_top_k", 5)
# 记忆抽取/检索用的 LLM（OpenAI 兼容接口；也兼容 DashScope compatible-mode）
MEMORY_LLM_API_BASE = _get("memory_llm_api_base", "https://dashscope.aliyuncs.com/compatible-mode/v1")
MEMORY_LLM_API_KEY = _get("memory_llm_api_key", "")
MEMORY_LLM_MODEL = _get("memory_llm_model", "qwen-plus")

# ============ 内置工具（联网搜索 / 生图 / 图片搜索） ============

TOOLS_ENABLED = _get("tools_enabled", True)
TOOLS_TAVILY_KEY = _get("tools_tavily_key", "")
TOOLS_IMAGE_MODEL = _get("tools_image_model", "qwen-image-2.1-pro")
TOOLS_IMAGE_SIZE = _get("tools_image_size", "1024*1024")
TOOLS_SEARCH_MAX_RESULTS = _get("tools_search_max_results", 5)

# Bot 形象图（以图生图参考图）
BOT_IMAGE_MODEL = _get("bot_image_model", "qwen-image-2.1-pro")
BOT_IMAGE_REFS = _get("bot_image_refs", [])
if isinstance(BOT_IMAGE_REFS, str):
    BOT_IMAGE_REFS = [p.strip().strip('"').strip("'").strip() for p in BOT_IMAGE_REFS.split(",") if p.strip()]
else:
    BOT_IMAGE_REFS = [p.strip().strip('"').strip("'").strip() for p in BOT_IMAGE_REFS if str(p).strip()]
BOT_IMAGE_REF_INDEX = _get("bot_image_ref_index", 0)
BOT_IMAGE_REF_STRENGTH = _get("bot_image_ref_strength", 0.8)

# ============ 群消息回复判断（小模型过滤） ============

GROUP_REPLY_JUDGE_ENABLED = _get("group_reply_judge_enabled", False)
GROUP_REPLY_JUDGE_PROVIDER = _get("group_reply_judge_provider", "openai")  # "openai" / "ollama"
GROUP_REPLY_JUDGE_API_BASE = _get("group_reply_judge_api_base", "https://dashscope.aliyuncs.com/compatible-mode/v1")
GROUP_REPLY_JUDGE_API_KEY = _get("group_reply_judge_api_key", "")
GROUP_REPLY_JUDGE_MODEL = _get("group_reply_judge_model", "qwen-turbo")
GROUP_REPLY_JUDGE_TIMEOUT = _get("group_reply_judge_timeout", 10)
GROUP_REPLY_JUDGE_PROMPT = _get("group_reply_judge_prompt", "")

# ============ AI 自主朋友圈 ============

AUTOPILOT_ENABLED = _get("autopilot_enabled", False)
AUTOPILOT_INTERVAL_MIN = _get("autopilot_interval_min", 45)
AUTOPILOT_LLM_API_BASE = _get("autopilot_llm_api_base", "https://dashscope.aliyuncs.com/compatible-mode/v1")
AUTOPILOT_LLM_API_KEY = _get("autopilot_llm_api_key", "")
AUTOPILOT_LLM_MODEL = _get("autopilot_llm_model", "qwen-plus")
AUTOPILOT_PROMPT = _get("autopilot_prompt", (
    "根据当前时间、近期聊天、记忆库和朋友圈历史，只决定是否发布及是否配图，不写正文或画面提示词。"
    '只输出 JSON：{"should_post": true/false, "form": "text 或 image", "reason": "决策理由"}。'
    "主题、文案及画面由主模型创作。"
))
MOMENTS_HISTORY_PATH = _get("moments_history_path", os.path.join(DATA_DIR, "moments_history.json"))

# Every schema field has a runtime constant, including optional performance settings.
for _group in CONFIG_SCHEMA:
    for _field in _group.fields:
        globals()[_RUNTIME_KEY_MAP.get(_field.key, _field.key.upper())] = _get(_field.key, _field.default)
for _path_key in ("STICKER_DIR", "TTS_OUTPUT_DIR", "MEMORY_DB_PATH", "MOMENTS_HISTORY_PATH"):
    if not os.path.isabs(globals()[_path_key]):
        globals()[_path_key] = os.path.join(BASE_DIR, globals()[_path_key])

# ============ 日志 ============

LOG_FILE = os.path.join(BASE_DIR, "bridge.log")
if os.name == "nt" and os.path.exists(LOG_FILE) and os.stat(LOG_FILE).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
    os.chmod(LOG_FILE, stat.S_IWRITE)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        RotatingFileHandler(
            LOG_FILE,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ob11-bridge")

def redact_text(value):
    value = str(value)
    value = re.sub(r"(?i)(access_token|api_key|authorization|token)([=:\s]+)([^\s&\"']+)", r"\1\2[已隐藏]", value)
    sensitive = {field.key for group in CONFIG_SCHEMA for field in group.fields if field.sensitive}
    for key, secret in config.items():
        if (key in sensitive or re.search(r"api_key|token|password", key, re.I)) and isinstance(secret, str) and secret:
            value = value.replace(secret, "[已隐藏]")
    return value

class _RedactFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True

for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RedactFilter())
    class _RedactFormatter(logging.Formatter):
        def format(self, record):
            return redact_text(super().format(record))
    _handler.setFormatter(_RedactFormatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))


def ensure_data_dirs():
    """确保 data 相关目录存在。"""
    for d in (DATA_DIR, TTS_OUTPUT_DIR, os.path.dirname(MEMORY_DB_PATH), os.path.join(DATA_DIR, "wechat_images")):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as exc:
            raise OSError(f"无法创建运行目录: {d}") from exc
