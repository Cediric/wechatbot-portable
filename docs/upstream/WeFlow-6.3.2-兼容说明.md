# WeFlow 6.3.2 便携适配

Windows 便携包可为匹配的 WeFlow 6.3.2 安装身份和朋友圈读取扩展。先在本机 WeFlow 中选中与微信相同的账号，开启 API 服务和「主动推送」，并生成连接令牌，再运行包根目录的 **一键启动.bat**。若启动器提示 WeFlow 仍在运行，通过托盘菜单「退出」后重新启动。[便携启动说明](../../portable/README.md)

## 构建与完整性

启动器核对 WeFlow 可执行文件、ASAR 归档和主脚本。安装扩展时先备份，只更新归档中的 `dist-electron/main.js`，重新计算其原有的完整性校验，并核对其余成员。已有本项目安装记录的旧扩展按已知哈希核验。遇到其他构建会报告不匹配。

| 文件 | SHA-256 |
| --- | --- |
| 原版 WeFlow 可执行文件 | `0e55aea3120d8c6c487af633542db9d79760b5ac90a1ecd9ae56219c890349e2` |
| 原版 `app.asar` | `3c20bfd34b57ceab7523a410b45dbda05529b21cd301681c1b0fce9dc31b09cd` |
| 原版 `dist-electron/main.js` | `4d504f70c1c29041b3730df30a7aeac20fc213ce53d8d2585eb862c2f8ec36ec` |
| 当前扩展 `dist-electron/main.js` | `4e8861ca3e78e91595c36a8bb583887804cb06fb632ca3e8641e7377a1a8f6dc` |
| 当前扩展 `app.asar` | `0a77290733a255731502d702207b9af80b44234d68d728260c0f9353a44687f3` |

这些值也是[启动器构建检查](../../portable/weflow_setup.py)的依据。

## 读取能力

| 本机 API | 用途 |
| --- | --- |
| `GET /api/v1/identity-snapshot` | 用当前账号的新读取结果更新联系人、会话和显示名称。 |
| `GET /api/v1/sns-identity-timeline` | 分页读取朋友圈原生动态，保留帖子 ID 与作者。 |
| `GET /api/v1/sns-identity-schema` | 查看当前账号朋友圈数据库的受支持表和列。 |
| `GET /api/v1/sns-identity-details` | 一次按最多 20 个帖子 ID 核验评论、点赞者及回复引用。 |

这些接口沿用 WeFlow 的 HTTP 令牌鉴权。评论明细保留真实发送者 ID 和原评论引用。读取不完整、账号切换或内容仍待本地提交时，桥接无法凭显示名确认互动。图片与视频的原生媒体密钥保留为十进制字符串，以免较大的 64 位密钥在转换时丢失精度。

## 复核方式

下列 PowerShell 命令从本仓库根目录执行。要复现离线检查，先准备匹配上述哈希的原版 `app.asar` 及其提取目录。示例将提取目录放在仓库旁的 `original-extracted`，生成文件写入同级独立目录 `wf632-reviewed`。将示例中的 ASAR 路径换成本机文件。源码准备器核对版本和输入哈希，并保留原始文件。

```powershell
$env:WEFLOW_ORIGINAL_ASAR = (Resolve-Path ..\original\app.asar).Path
$env:WEFLOW_SOURCE_DIR = (Resolve-Path ..\original-extracted).Path
python -B docs/upstream/prepare_weflow_632.py --source $env:WEFLOW_SOURCE_DIR --output ..\wf632-reviewed
node docs/upstream/verify_weflow_632.mjs ..\wf632-reviewed
python -B docs/upstream/test_prepare_weflow_632.py
```

验证运行中的 WeFlow 时，通过环境变量 `WECHAT_VERIFY_API_TOKEN` 提供已配置的令牌，将端口换成本机 WeFlow 的 API 端口。该检查只访问回环地址，验证健康状态、身份快照、条件请求和令牌鉴权。聊天推送和微信界面收发需另行验证。

```powershell
python -B docs/upstream/verify_live_identity_api.py --base-url http://127.0.0.1:5031
```
