# WeFlow 6.3.2 独立代码扩展

这份代码准备器针对本机**只读提取的代码快照**，本身不运行 Electron、不读取账号或聊天数据、不修改安装目录。准备产物是独立的修改后 `main.js`、可读扩展和操作清单。另经用户批准和独立交叉审查，已在本机完成 ASAR 重包、备份采用及真实只读接口验收，结果见文末；准备器本身仍只在工作目录生成文件。

## 版本与具体兼容差异

`package.json` 必须同时为 `name=weflow`、`version=6.3.2`、`main=dist-electron/main.js`。原始 `main.js` SHA-256 必须是：

```text
4d504f70c1c29041b3730df30a7aeac20fc213ce53d8d2585eb862c2f8ec36ec
```

公开 main 源码补丁并不是这个已安装构建的二进制补丁。6.3.2 的实际差异如下：

| 项目 | 6.3.2 已提取代码 | 此适配器处理 |
| --- | --- | --- |
| 服务绑定 | `X` ChatService、`B` WCDB、`hM` HTTP、`_M` SSE、`o` crypto、`c` fs、`t` path；原包 `saxes` | 精确 SHA 和唯一绑定检查；只对这份构建生效 |
| 账号配置 | `myWxid`，并有 `getCacheAccountScope`、`accountCacheEpoch` | 调用该构建现有账号范围函数，查询后核验范围和 epoch |
| 原始会话身份 | `username/user_name/userName/usrName/UsrName/talker/talker_id/talkerId` | 支持这份构建的全部原始别名；绝不从 displayName 猜 ID |
| 群成员 HTTP 字段 | `wxid` | 桥已兼容真实 `wxid/username/accountId`；本扩展不重写群成员 API |
| 联系人缓存 | `contactsMemoryCacheTtlMs=180000` | 新接口通过固定 `SELECT * FROM contact` 绕过 ChatService 名称缓存 |
| 会话名称 | 原始会话查询后从 `avatarCache` 取 displayName | 新接口按同次联系人结果填写名称，缺少联系人则显示真实 username |
| SSE 新消息 | 有 session/rawid/显示名，但无实际 sender/type/direction/localId | 保留原 payload，并补原始 `senderUsername/isSend/localType/localId` |

WCDB 服务已有 `execQuery(kind,path,sql,params)` 和 `getSessions()`；提取的 `wcdbWorker.js` 分派至相应 native 方法。固定 contact 查询会解析联系人数据库路径并返回实际查询行，原始 sessions 直接读 native 会话结果；这两条路径没有 ChatService 的名称缓存。准备脚本及模拟不执行真实 native 方法；本机采用后的只读 API 已实际返回完整合同数据，仍不能保证数据库尚未落盘的微信改动立刻可见。

## 可审阅修改范围

`weflow632_extension.js` 是依赖注入的纯 JavaScript 工厂。文件本身只定义函数；安装工厂只添加方法，不立即连接数据库、调用 UI 或发起网络。生成代码只做两处修改：

1. 在原 HTTP 请求的 token 验证之后，原 contacts 分支前添加身份快照、朋友圈原生记录、结构元信息及身份明细的 GET 分支；原路由、鉴权和 OPTIONS 行为保留。
2. 在 bundle 尾部追加可读工厂，绑定已核验的实际服务、文件与路径模块，以及原包已有的 `saxes` XML 解析器。同步模块求值结束前完成方法安装；原 `app.whenReady().then(...)` 回调在其后运行。

新接口复用 v1 快照合同，2.5 秒共享响应期限。挂起 native 调用超时后返回 503，保持同一个已完成失败响应，直到实际调用结束；持续轮询不会反复排队。账号范围变化、重复实际身份、查询错误不能返回 fresh 成功。只有完成新查询后才计算 ETag 或返回 304。原 contacts/sessions 路径不被改变。

`prepare_weflow_632.py` 只读取快照的 `package.json` 和 `dist-electron/main.js`，只在 review workspace 内的独立目录写三个文件。它拒绝源/输出重叠、外部输出、版本或 hash 不匹配、意外路由、已有不同内容；相同内容可复跑。写入使用 exclusive create，不删除或覆盖用户文件。

它不打包 ASAR、不写 EXE、不修改 Electron fuse 或完整性配置，也不替换安装目录。后续重包应保留除目标 `main.js` 之外每个文件的原始字节，并正常重算该文件已有的 ASAR integrity；不能为运行修改副本而关闭、绕过完整性验证。

本版主窗口 Close/Alt+F4 根据 `windowCloseBehavior` 执行 `ask/tray/quit`，关闭窗口可能只是隐藏到托盘。明确的应用退出入口是托盘菜单“退出”，或关闭确认对话框的“退出”。它们进入应用自己的 `before-quit` 清理流程。实际安装前应等待该进程完成退出，再复核 EXE 和原 ASAR 身份；本版 `autoInstallOnAppQuit=true`，若先前下载过更新，退出可能采用更新，此时旧版本/hash 补丁必须拒绝继续。这里没有操作真实托盘、退出进程或修改退出设置。

## 准备与模拟验证

在项目根目录执行，所有路径可改成对应绝对路径：

```powershell
# 把源和 ASAR 改成你自己的原始 6.3.2 文件路径；输出必须是项目旁的新独立目录。
python -B docs/upstream/prepare_weflow_632.py --source ..\original-extracted --output ..\wf632-reviewed
$env:WEFLOW_ORIGINAL_ASAR = 'C:\review\WeFlow-original\app.asar'
node docs/upstream/verify_weflow_632.mjs ..\wf632-reviewed
$env:WEFLOW_SOURCE_DIR = (Resolve-Path ..\original-extracted).ProviderPath
python -B docs/upstream/test_prepare_weflow_632.py
node --check ..\wf632-reviewed\modified\dist-electron\main.js
```

验证脚本必须通过 `WEFLOW_ORIGINAL_ASAR` 或第三个命令行位置参数提供原 ASAR，第二个位置参数或 `WEFLOW_PREPARED_DIR` 提供准备目录，没有任何本机安装路径默认值。例如 `node docs/upstream/verify_weflow_632.mjs ..\wf632-reviewed C:\review\WeFlow-original\app.asar`。脚本直接从该原包只读取得原 `main.js`、worker 源码和四个纯 JS XML 解析模块，检查 ASAR 头、文件边界、每个文件 integrity 与原 main SHA；不执行 Electron、worker 或 native 模块，不依赖临时的提取目录。ASAR 和真实账号数据不包含在源码交付中。

准备器默认源仍为项目旁的 `installed_weflow_code`；应优先明确 `--source/--output`。`operations.json` 记录当前输入、扩展和输出 SHA，及精确 route_old/route_new，便于重包前再次核验。新内容不能覆盖先前的不同准备产物，变更后应选择另一个独立目录。

当前 42 项 Node 模拟验证包括实际 6.3.2 `handleRequest` 的孤立路由执行、原 service/worker 方法合同，以及真实 `saxes` 解析模块的 XML 边界、64 位评论与回复引用、删除和本地待发状态验证；其他 bundle 代码不被执行。7 项 Python 验证检查独立副本、字节级修改范围、只读源保持、版本/hash 拒绝、路径边界及已有用户文件保护，原代码快照必须由 `WEFLOW_SOURCE_DIR` 明确指定。没有启动真实 Electron、native DLL、HTTP 服务或设备，没有读取真实聊天。语法和模拟通过仍不代替完整应用的真实运行验收。

采用后，桥会自动探测 v1 能力。默认 3 秒轮询可调为 1 秒；发现时间是“微信改动进入可读数据库 + 轮询等待 + 查询时间”，没有落盘前的即时保证。真实 `localType=10000` 系统消息会额外触发一次异步刷新；其他昵称和备注改动依赖轮询。未采用扩展时仍报告 `legacy_cached`，不能宣称 3 秒或 1 秒即时同步。

## 安装后的只读接口验收

`docs/upstream/verify_live_identity_api.py` 只请求本机 HTTP 的 health、v1 身份快照、条件 ETag 请求和无 Token 的鉴权拒绝检查。默认 `http://127.0.0.1:5031`；端口须与既有 `httpApiPort` 相符。Token 只能经环境变量 `WECHAT_VERIFY_API_TOKEN` 或 `--token-stdin` 提供，不放入 URL、命令行参数或报告；脚本本身不读取配置、不解密或修改任何密钥。

```powershell
python -B docs/upstream/verify_live_identity_api.py --base-url http://127.0.0.1:5031
```

每次请求默认 4 秒硬超时、最大 4 MiB 响应，只接受数值 loopback 地址（localhost 规范化为 127.0.0.1），不跟随重定向、不用环境代理。报告只含 HTTP 状态、合同版本、fresh 能力、revision 哈希、联系人/会话数量及字段和实际 ID 的合法性摘要，不显示真实名称、微信 ID、聊天正文或 Token。脚本不请求 `/messages` 或真实 SSE；SSE 的成员/方向字段仅列出适配器静态预期，明确 `runtime_fields_verified=false`，不冒充实时聊天验收。8 项纯 HTTP stub 验证不需要启动真实服务。

## 本机采用结果：2026-10-11

用户批准后，安装 ASAR 已替换为仅修改 `dist-electron/main.js` 的扩展构建，54,847 个 packed 文件逐项核验。原包及相邻备份 SHA256 为 `3c20bfd34b57ceab7523a410b45dbda05529b21cd301681c1b0fce9dc31b09cd`；采用包为 `57907b69d09f6ca1fb355c4091c29638033b8b320f5b592d4ea2ab5102832568`。EXE 不变；完整性开关未修改。

重启后真实只读验收通过：health 200、fresh v1 snapshot 200、169 联系人和 44 会话的实际 ID 合法唯一，ETag 条件请求 304，未鉴权请求 401。没有读取历史或订阅 SSE，没有改联系人、备注或群名测真实变化时间。原 API 关闭/空 Token 状态只在验收期间临时改为有强令牌的 loopback 服务，验收后已恢复原 API 字段及托盘关闭行为，并保留其他设置重启；正式使用 bot 仍需配置启用原本的本地 API。

## 朋友圈身份扩展采用结果：2026-10-12

经用户授权、另一智能体审阅及本机只读验收，当前采用的 ASAR SHA-256 为 `361314d9f7ded19e91ee310add9fec3035c5c66300704c5dbbeffcbd5192dedd`，修改后 main SHA-256 为 `74c96b1fb106dc2b6683d5ef87b4201851b316b53449c47fe2a9688a0ab86d2e`。从原始 ASAR 重新打包，54,847 个 packed 文件逐项比较，唯一修改文件是 `dist-electron/main.js`。原包、先前身份扩展以及本轮替换前的包均保留备份；EXE、unpacked 文件和 fuse 均未改变。

以下接口经过原 HTTP Token 护栏，仅接受 GET；合同 `version` 和 capability 都是精确整数 1。

| 接口 | 参数与返回 | 可核验范围 |
| --- | --- | --- |
| `/api/v1/sns-identity-timeline` | `limit=1..100`、`offset=0..200`、可选 `usernames`；`capabilities.snsIdentityTimeline=1`、`timeline` | 直接保留原生 timeline 字段，不经过 `fixCommentRefs` 或显示名转换；原生缓存评论仍缺发送者 ID，不能据此认定发送者 |
| `/api/v1/sns-identity-schema` | 无数据参数；`capabilities.snsIdentitySchema=1`、表名及固定白名单列名/类型 | 当前账号 SNS 数据库结构；不返回 SQL、默认值、数据库路径或内容 |
| `/api/v1/sns-identity-details` | `post_ids` 为最多 20 个唯一、标准正十进制 unsigned64 字符串；`capabilities.snsIdentityDetails=1`、`timeline` | 固定查询 `SnsTimeLine` 的 tid、user_name、content，按数据库、XML 帖子 ID 和作者 ID 交叉核验，再返回真实评论和点赞身份 |

真实 SNS 表为 `SnsTimeLine`，列为 `tid INTEGER`、`user_name TEXT`、`content TEXT`、`pack_info_buf TEXT`；本扩展不读取 `pack_info_buf`。一般 metadata 调用在此版本返回空表时，只使用服务端当前选中账号目录下固定的 `db_storage/sns/sns.db`，核验 root/file realpath、相对路径和文件类型；客户端不能传 SQL、表名或路径。帖子 unsigned64 ID 与数据库 signed64 tid 通过 BigInt 精确映射，不经 JavaScript Number。每步读取前后核验账号范围和 epoch；朋友圈各接口共享单飞限制，实际 native 调用挂起时不会在响应超时后继续堆 RPC。

真实数据库 XML 的外层是唯一 `SnsDataItem`，包含 `TimelineObject` 与 `LocalExtraInfo`。严格 SAX 解析保留 text 和 CDATA，对标准 XML 实体只解码一次；拒绝 DTD、实体声明、额外 processing instruction、重复来源、重复标量和 root/nested 双重 LocalExtraInfo。帖子返回 `id/username/contentDesc/createTime/identityVerified`，评论返回 `id/username/content/refCommentId/refUsername/createTime` 等字段。评论非零 `comment_64id` 优先作为 canonical `id`，同时保留原 `commentId/comment64Id/refCommentId32/refCommentId64`；仅按同帖真实 ID 建立 32→64 引用映射，引用冲突或发送者冲突不能返回已核验评论列表。昵称仅作显示。

`commentsVerified/likesVerified` 表示显式原生列表完整且身份可核验。缺少列表不能当作明确空列表。`b_deleted=1` 的对象不参与确认；`is_local_added=1`、缺失或未知状态不能确认发送，整组 verified=false 并清空身份明细。只有 `b_deleted=0` 且 `is_local_added=0` 的对象可参与确认，未知字段值不会透出诊断。空正文的原生表情评论可保留真实身份，但不凭空生成文字内容。

此轮本机只读验收得到 20 个帖子，20 个帖子 ID/作者交叉核验通过，13 条评论携带真实发送者 ID，32 个点赞携带真实账号 ID。实际四类删除/本地待发标记均为 0；当前样本非零 64 位评论 ID 为 0，64 位和回复引用边界另有模拟验证。既有实测评论及测试号留下的回复目标各找到一次，分别核验为当前账号发送者与唯一测试联系人真实 ID，且位于当前账号帖子；没有按昵称推断评论身份。未鉴权、额外 SQL 参数及 POST 分别返回 401、400、405。42 项 Node 模拟、7 项 Python 准备器验证和语法检查通过。

接口读取的是微信已落盘的数据，不能保证服务器尚未同步或尚未落盘的互动立即可见；每次发送确认必须进行新读取，不能把本地乐观状态当作已发送。本节只记录身份接口采用和只读核验，不代替 UIA 点赞、评论及回复的真机验收。真实账号响应和私有 API 令牌不包含在交付源码或压缩包中；本轮临时本地 API 会在全部真机测试结束后由主流程恢复原配置。

## 朋友圈原媒体兼容补充：2026-10-17

原生身份接口直接返回 WCDB 行，未经过 WeFlow 的 `SnsService.getTimeline` 媒体整理。因此桥接需要从原 XML 保留媒体地址的 `token/key` 属性，补齐原版下载参数、原图尺寸地址及视频类型，原签名查询字段保持原样。直连 CDN 使用原版的 `MicroMessenger Client` 请求头；需要密钥的原媒体交给已配置 WeFlow 的固定 `/api/v1/sns/media/proxy` 下载与解密，不用缩略图冒充原视频。桥接令牌只发送到配置的 WeFlow 服务。

原版 HTTP 适配器把十进制媒体密钥转换成 JavaScript `Number`，会舍入大于安全整数范围的原生六十四位密钥。本扩展保留十进制字符串，继续交给原版 `SnsService` 解密；身份快照增加 `capabilities.snsMediaKey=1`。新扩展 main SHA-256 为 `4e8861ca3e78e91595c36a8bb583887804cb06fb632ca3e8641e7377a1a8f6dc`，按既有重包规则从已核验原 ASAR 生成的 SHA-256 为 `0a77290733a255731502d702207b9af80b44234d68d728260c0f9353a44687f3`。

启动器保留上节旧版扩展的准确哈希，仅对原版或有本项目记录的旧版包执行升级。旧扩展去掉尾部代码并逆向恢复路由后，必须重新匹配原版 main 哈希，再生成新扩展；其他成员保持逐字节一致，替换前备份，正常退出后才写入。旧接口即使已有身份查询能力，也不能跳过本次媒体能力升级。本次验收为离线源码、模拟接口和语法验证，没有替换或启动本机 WeFlow，也没有访问真实媒体地址；不能把这里的模拟通过当作真机媒体读取成功。
