"""Shared model roles; no configuration reads, network or application imports."""
PROFILES = {
    "main": ("main_llm", "主模型", "main_llm_prompt", 32768, 4096),
    "memory": ("memory_llm", "记忆抽取模型", "memory_llm_prompt", 8192, 1024),
    "group": ("group_reply_judge", "群聊判断模型", "group_reply_judge_prompt", 4096, 384),
    "autopilot": ("autopilot_llm", "朋友圈发布决策", "autopilot_prompt", 8192, 1024),
    "image_prompt": ("image_prompt_llm", "生图提示词子模型", "image_prompt_llm_prompt", 8192, 1024),
    "moments_decision": ("moments_decision_llm", "拟真与朋友圈决策", "moments_interaction_prompt", 8192, 768),
}
THINKING_OPTIONS = [("auto", "模型默认"), ("off", "关闭思考"), ("on", "开启思考")]
EFFORT_OPTIONS = [("auto", "模型默认"), ("minimal", "最低"), ("low", "低"), ("medium", "中"), ("high", "高"), ("max", "最高")]
FORMAT_OPTIONS = [("auto", "按模型与接口识别"), ("openai", "OpenAI reasoning_effort"), ("qwen", "千问 enable_thinking"), ("deepseek", "DeepSeek thinking"), ("none", "不支持思考参数")]
