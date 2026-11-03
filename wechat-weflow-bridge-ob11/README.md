# 微信bot 桥接 v6.32

WeFlow SSE 接收，OneBot v11 反向WebSocket连接AstrBot，主窗口UIA发送。启动与完整功能见[项目说明](../README.md)、[便携启动](../portable/README.md)。默认WeFlow5031、桥接8766、AstrBotWebUI6185、OneBot11229。

聊天流程不调用额外caption模型。主模型须视觉能力；视频/动画采用有界采样，不含视频音轨。独立/远端AstrBot需自行安装随包配置插件、使用本包平台配置并保证附件路径双方可访问，不能仅改WebSocket地址使远端读取Windows文件。

首次运行时从示例创建缺失配置。保存设置时，留空的 API Key 会保留原值。主模型热更新只对本包管理的平台生效。修改 `web_port` 或 `memory_db_path` 后须完整重启，更新连接和身份设置会触发重连。[完整字段](../docs/配置字段.md)

主模型使用真实SSE，完整短句逐步回复，推理保留在内部请求中、不发送到微信。普通补充消息沿用同会话缓冲，等当前回复交付结束再处理，明确停止要求仍取消旧轮；流式序号去重，发送结果不确定时停止。上下文上限包含系统提示、角色卡、媒体、工具和输出预留，接近预算时摘要完整旧轮次。记忆抽取独立处理单条原消息，默认8,000字符、最大32,000；需要缩减时本机选择完整原文片段，证据必须逐字对应，未完成的截断尾句不作长期证据。详见[流式与上下文设置](../docs/streaming-context.md)。

主要模块：bridge_core顺序/身份，multimodal采样，model_client独立角色/思考/预算，memory_store证据/归属，moments_interactions逐条查看/决策/执行，tools调用主模型创作生图提示词，reference_store图库，voice_cloning音色，uia_sender主窗口，web_panel本机登录态和CSRF。

运行数据在data内，包括记忆、身份、outbox、朋友圈核对和素材。干净包排除data、config.json、日志、录音、私人人设、AstrBot密码；自用包仅加入明确选择的配置、角色资料与素材，不复制已有聊天数据库或登录数据。自用配置可能含API密钥，应仅供本人使用。完整测试数、ZIP内容审查与交付SHA见本版验收记录。
