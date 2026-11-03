# WeFlow 身份扩展

微信bot 用 WeFlow 接收消息，并用真实微信 ID 区分联系人和群成员。原版 WeFlow 的部分接口会复用名称缓存。扩展提供新读取的身份快照，便于在联系人改名或删除备注后更新显示名称。尚未写入微信本地数据库的变化，要等落盘后才能读到。

| 用途 | 对应文件 |
| --- | --- |
| 在 Windows 便携包中使用 WeFlow 6.3.2 | 启动器会识别核验过的安装构建并准备扩展。[6.3.2 兼容说明](WeFlow-6.3.2-兼容说明.md) |
| 在 WeFlow 源码中审阅联系人快照与消息推送改动 | [源码补丁](WeFlow-identity-snapshot.patch)和[原文件 SHA-256](source-manifest.json)。补丁基于 2026-10-11 获取的[上游 WeFlow 源码](https://github.com/hicccc77/WeFlow)。 |

## 源码补丁提供的接口

`GET /api/v1/identity-snapshot` 沿用本机 WeFlow 的令牌鉴权。成功时返回版本、`fresh` 标志、联系人、会话及内容修订值。联系人以 `username` 标识，按备注、昵称、微信号、原始 ID 的顺序显示名称。账号切换或读取失败会使本轮快照失效。完成新的读取后，才会用 ETag 判断内容是否变化。

消息推送补充 `senderUsername`、`isSend`、`localType` 和 `localId`，用于识别真实发言人、方向与消息类型。桥接自动探测扩展能力，使用原版缓存时，状态显示 `legacy_cached`。

## 核验补丁

从本仓库根目录运行以下命令，使用 Node.js 24 和模拟数据库、HTTP 服务检查快照与消息接口逻辑。

```shell
node docs/upstream/verify_snapshot.mjs
```

如需适配 WeFlow 源码，先对照上面的 SHA-256 清单，再从对应的 WeFlow 源码目录执行 `git apply --check <补丁路径>`。上游后续提交可能改变补丁上下文。完成应用后，还应在该源码版本上构建并测试。
