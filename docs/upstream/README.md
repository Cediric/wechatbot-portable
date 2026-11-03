# WeFlow 名称与身份扩展（待上游审查）

`WeFlow-identity-snapshot.patch` 是针对官方 WeFlow 源码的可审查补丁，本页保存它的初始准备说明。公开源码补丁仍待上游审查；另行提供的6.3.2精确构建适配已在先前用户授权下安装并验收，当前状态以[兼容说明](WeFlow-6.3.2-兼容说明.md)和[v5.3验收报告](../v5.3验收报告.md)为准。桥接会自动检测能力；未装扩展的原版 WeFlow 仍可正常接收消息，名称刷新受原版缓存限制。

官方来源：[chatService.ts](https://github.com/hicccc77/WeFlow/blob/main/electron/services/chatService.ts)、[httpService.ts](https://github.com/hicccc77/WeFlow/blob/main/electron/services/httpService.ts)、[messagePushService.ts](https://github.com/hicccc77/WeFlow/blob/main/electron/services/messagePushService.ts)。本补丁基于 2026-10-11 下载的公开文件；对应原始文件 SHA-256 在 `source-manifest.json`。官方 main 会变化，请先在独立源码 checkout 检查补丁，不要直接改安装目录。

本机提取的 **6.3.2** 构建另有精确版本/hash 限定的 [独立代码适配器与验证说明](WeFlow-6.3.2-兼容说明.md)。它使用核验过的构建适配器，并保留完整性验证。v5.3便携启动仅对核验构建自动备份适配，本轮便携测试没有再次修改正在运行的微信或WeFlow。

## 为什么需要扩展

当前公开源码的 `contactsMemoryCacheTtlMs` 是 3 分钟。`/api/v1/contacts` 没有 `forceRefresh` 参数；`/api/v1/sessions` 的 `displayName` 取自 avatar/name 缓存，该 HTTP 路径没有强制刷新名称。已有 `refreshSessionContactDisplayNames` 属于 Electron IPC 路径。桥每 3 秒请求缓存接口，不等于实际名称每 3 秒刷新；改成 1 秒会增加请求量，仍绕不过这些缓存。

补丁新增独立、只读、版本化接口 `GET /api/v1/identity-snapshot`，沿用现有 access_token 验证，其他 HTTP 方法返回 405。它执行固定的 `SELECT * FROM contact` 并读取原始 session 列表，不走联系人或头像名称缓存；每次请求完成新读取后才计算 revision / ETag，名称未变可返回 304。接口不接受客户端 SQL、不写数据库、不调用 UI 或收费模型。

整个查询是 singleflight，共用 2.5 秒响应期限。底层 native 调用挂起时返回 503；共享失败结果一直保留到实际底层调用结束，后续轮询不会反复排入 native RPC 或添加等待闭包。期限不声称能取消原生线程；若底层一直不恢复，需要修复或重启 WeFlow 数据服务。

返回合同：

```json
{
  "success": true,
  "version": 1,
  "fresh": true,
  "capabilities": {"freshIdentitySnapshot": 1},
  "snapshotAt": 0,
  "revision": "sha256",
  "contacts": [{"username": "wxid_example", "displayName": "备注", "remark": "备注", "nickname": "昵称", "alias": "微信号", "type": "friend"}],
  "sessions": [{"username": "wxid_example", "displayName": "备注", "type": 1}]
}
```

这里只提供身份与名称快照，`type` 用于区分 `@chatroom` 群和其他联系人，不替代上游联系人权限/好友分类。名称按 `remark → nickname → alias → username` 展示；删掉备注或 alias 后仍返回明确空字符串。session 名称必须来自同轮 contact 读取；缺少 contact 时只返回真实 username，绝不把旧昵称猜作微信 ID。账号切换期间的结果丢弃，数据库读取失败返回 503，不伪装成 fresh 成功。contact 和 sessions 是两次顺序读取，不能宣称跨库事务快照；微信更新尚未落入可读数据库时仍无法发现。

SSE 补充 `senderUsername`、`isSend`、`localType`、`localId` 四个原始字段。原版 SSE 不给实际群成员 ID 时，桥用原始 server ID 和真实群 ID 查询 `/messages`，只接受唯一匹配、明确对方方向及真实成员 username。0 不是有效 server ID；本地 system 消息可使用同时提供的 localId。普通消息仍按实际成员 ID 处理，昵称只作显示。

群批次保留标准 OneBot 信封首成员 `user_id`，模型可见的每条正文/媒体作者前缀带实际微信 ID，扩展 `wechat_identity.messages` 还逐条保留成员 user_id、原始 ID 字段和稳定群 ID。正文及记忆日志不会把后续成员署名替换为第一人；标准适配器即使忽略扩展 metadata，仍可按正文分辨同名作者。

原版 `localType=10000` 群系统消息可以通过 `message.new` 推送，但旧 SSE 没有暴露类型。桥对唯一匹配的原始 10000 行立即请求一次名称快照；不从聊天文本正则推断改名，也不把系统通知伪装成某个群成员的发言。其他改昵称、备注不保证有系统消息，仍依赖快照轮询。

## 生效和边界

桥自动识别上述完整 v1 能力。未安装扩展时，`get_sync_status()` 返回 `fresh_capability=false`、`name_source=legacy_cached` 和缓存限制；404 能力探测最多每 60 秒重试。已确认扩展后，故障期间保留最新身份名称，不退回旧缓存覆写。默认 `contact_refresh_seconds=3`；安装扩展后可选 1 秒。一次新名称可被发现的时间约为“微信数据进入可读库的时刻 + 轮询等待 + 本地查询/HTTP 时间”，没有数据库落盘前的即时保证。旧接口请求和查询失败可能延长检查周期。

应用前在 WeFlow 源码根目录运行：

```powershell
git apply --check <补丁绝对路径>
git apply <补丁绝对路径>
```

随后按 WeFlow 自身项目说明安装开发依赖、运行 TypeScript/项目测试及构建，在独立测试环境确认 token、账号切换和数据库兼容性，再决定是否采用。本交付没有替用户完成上游构建、安装或真实微信改名验收。

已完成：补丁在下载的原始文件上 `git apply --check` 通过；Node 24 解析 3 个完整修改文件；提取新方法并用纯 WCDB/HTTP mock 验证名称变更、备注删除、同名不同 ID、账号切换、失败返回、读后 ETag 和挂起 native 调用的 singleflight。挂起期间 20 次请求、超时后再 20 次请求均只创建一次底层查询，实际结束后才可启动下一次；桥核心 63 项回归通过。源码测试没有启动 Electron/native DLL、访问真实数据库或操作设备。

交付目录内的合成验证可用 Node 24 复跑：`node docs/upstream/verify_snapshot.mjs`。它从补丁提取新增方法、剥离 TypeScript 类型并注入纯 mock，不需要 Electron、微信或真实数据库。它不是完整 WeFlow 类型检查或实际数据库验收。
