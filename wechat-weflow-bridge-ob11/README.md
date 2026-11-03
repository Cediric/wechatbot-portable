# 微信消息桥接

桥接从 WeFlow 的本地接口和 SSE 主动推送接收微信消息，将私聊和群聊事件通过 OneBot v11 反向 WebSocket 交给 AstrBot。收到回复后，桥接核对目标会话，通过微信桌面主窗口的 UI 自动化（UIA）发送。正常使用从根目录的 **一键启动.bat** 进入。[项目首页](../README.md) · [便携版使用指南](../portable/README.md)

## 默认连接

| 组件 | 默认地址 | 用途 |
| --- | --- | --- |
| WeFlow API | `http://127.0.0.1:5031` | 接收消息，读取消息所属账号与会话 |
| AstrBot OneBot v11 | `ws://127.0.0.1:11229/ws` | 桥接连接 AstrBot，传递事件和回复 |
| 桥接控制面板 | `http://127.0.0.1:8766` | 配置模型、朋友圈、记忆与发送设备 |
| AstrBot 控制面板 | `http://127.0.0.1:6185` | 管理 AstrBot 实例 |

如将 AstrBot 部署到另一台机器，还需安装随包的 `astrbot_plugin_wechatbot_profile` 插件，配置对应平台，并让双方都能读取附件路径。

## 配置与数据

桥接首次启动时会根据 `config.example.json` 创建 `config.json`。可在面板修改日常设置。主模型配置从下一次请求生效。修改 `web_port` 或 `memory_db_path` 后需完整重启。默认的 `data/` 保存会话记忆、身份映射、待发送记录和朋友圈互动状态。[完整配置字段](../docs/配置字段.md)

WeFlow、桥接和随包 AstrBot 默认在本机通信。生成回复时，所配置的模型服务会收到本次请求所需的聊天上下文和媒体。图片、动画和视频采样画面由主模型直接读取，对应模型需支持图像输入。启用生图、语音或联网检索时，相关内容也会按所用功能发送至相应服务。

主模型可以边生成边按完整短句分段发送。发送中断时，可在「会话工作台」查看运行状态，并与目标会话中的实际消息对照。[流式回复与上下文](../docs/streaming-context.md) · [微信 UIA 兼容性](../docs/微信UIA兼容性.md)

## 运行回归测试

在项目根目录打开 PowerShell，用已安装桥接依赖的 Python 运行：

```powershell
$env:PYTHONPATH = "wechat-weflow-bridge-ob11"
python -B -m unittest discover -s tests -q
```

只检查朋友圈可在命令末尾加上 `-p "test_moments_*.py"`。在已启动过的便携包中，可将 `python` 换成 `.\portable\runtime\bridge-venv\Scripts\python.exe`。
