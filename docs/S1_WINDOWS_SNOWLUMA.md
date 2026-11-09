# S1 · Windows SnowLuma 原型验证

2026-11-09：Windows 原型按用户确认的范围完成实机验收。本次交付是 SnowLuma-only 路线的 S1 源码与文档，S2 完整 Windows 安装管理迁移及 Linux 部署尚未开展。

## 架构与分支

`QQ ↔ SnowLuma ↔ OneBot v11 ↔ AstrBot ↔ Fat-Fish Core / 自有插件 ↔ 模型与本地数据`

AstrBot 是唯一聊天主体；SnowLuma 只提供 QQ 事件与动作。人格、权限、记忆与任务治理仍由 Core 统一管理。DSH 是 DeepSeek Harness；未来 Work、DSH 和外部 Agent 不直接绕过 AstrBot 操作 QQ。

- `migration/snowluma-only` 从 P2 main `ea3d8070232f636232200e5335c1d06de72c03b6` 开始，不包含 NapCat P3 提交的自动移植。
- main 保持 P2 基线；`backup/p3-napcat` 保留 `a0166f84cd55da48ccf9051fec67437eee3cefc1`。
- S1 不全面删除旧安装资产；保留的 P2 脚本不构成 NapCat 备用运行路线，也不支持双协议接入。

## 固定测试版本

|组件|版本或来源|
|---|---|
|SnowLuma Full Windows x64|[官方 v2.4.2 Release](https://github.com/SnowLuma/SnowLuma/releases/tag/v2.4.2)，tag 对应源码 `f2ea3354f8bbe8672cbb4ae6a2b4e1fbeff44faf`|
|发行包 SHA256|`0912926c5e73260f35b000278a8658493fc9452f36782c32fa86f0e825bad81a`|
|AstrBot|4.29.3，`f61a7f42696da6df25dc7a249ec9b8d54ed9da18`，保留 P2 锁定补丁|
|Python / aiocqhttp|独立 Python 3.12.3 / aiocqhttp 1.4.4|
|QQ 客户端|本轮环境登记为 9.9.26-44725；根 QQ.exe 启动器文件版本不能代替实际客户端版本|

SnowLuma 由用户从官方渠道自行下载、解压、接受协议并启动。FFA 不附带其二进制，不自动下载部署，不改写其程序或 Hook。后续自动安装、再分发和配置自动化应按官方许可另行确认。

## 独立接入配置

使用独立机器人测试账号。QQ 登录、安全确认和 Hook 加载由用户亲自操作；避免自动注入到其他 QQ 账号，不在同账号同时运行不同通信端。

- SnowLuma WebUI 仅监听本机回环；POC 启动环境使用 `SNOWLUMA_HOOK_AUTOLOAD=0`、`SNOWLUMA_WEBUI_HOST=127.0.0.1`。
- AstrBot 保留 aiocqhttp 适配器：平台 ID `feiyu-onebot`，反向 WebSocket 监听 `127.0.0.1:6199`，路由 `/ws`。
- SnowLuma WS 客户端 URL 为 `ws://127.0.0.1:6199/ws`，role 为 `Universal`，messageFormat 为 `array`，重连间隔 5000 毫秒。
- 两端使用同一随机 OneBot Token。它与 WebUI 登录密码不同，均不进入源码、示例或提交。
- 关闭 `statusCommand.enabled` 和该 WS 客户端 `reportSelfMessage`；不同时启用占用 6199 的 SnowLuma WS 服务端。

这些是 POC 参数，不是新安装器。根目录现有安装、启动、迁移入口仍含 P2 通信管理，不能当作 SnowLuma 自动部署入口。运行副本应使用现有受管 `fat_fish_management/run_chat.py --instance <独立实例>` 正常启动；不要提前从开发源码目录导入 Core，开发树不包含发行副本组装的第三方决策组件。

## 已交付行为

- 普通 QQ 首回复随机窗口为 4–6 秒，已识别指令为 2–3 秒；从最近一条参与合并的消息进入 Core 时计算，收集、意图识别和模型耗时计入，后续分段不重复增加整段等待。模型及网络慢时总耗时仍可能更长。
- 先短暂收集并开始准备回复。媒体没有补充时使用 8–10 秒发送窗口，收到补充后切换为 4–6 秒。已有未发送草稿可安全取消时，复用消息收集器合并后重算。
- 自然语言意图不明确或识别服务不可用时保留原消息回到聊天，不执行含糊的管理动作。权限拒绝、身份验证及明确操作的确认机制保留。
- 明确索要表情包及“再来一个／重新发一下”使用当前会话、当前发送者的短时衔接状态。只从当前绑定图库的真实有图分类选择，交给原 Meme Manager 和 AstrBot 发送；普通自动配图策略保持。
- 授权后的小型纯文本附件可读取：txt/md/csv/json/log/yaml/yml，最多 3 份、单轮合计 64KiB，支持 UTF-8 与 GB18030。拒绝任意本地路径、非受信下载地址及不支持的二进制文档，失败明确说明。
- 黑名单提示由 Core 固定生成，不经过 LLM；同账号同会话每 60 秒最多一次。获批群仅真实 @ 时提示，业务访问仍被拒绝。
- 修复转发读取器漏传消息收集器版本参数、群记忆初始化阻塞聊天等问题；提醒等待与模型耗时共用窗口，发送前继续检查授权。

联网由回复模型按上下文选择。`/webresearch` 查看或切换当前会话开关，关闭时跳过搜索决策与工具注册。使用 [DeepSeek 原生 Search API](https://api-docs.deepseek.com/api/search/)。QQ 发送前过滤后台诊断，受管配置关闭工具状态、推理和分段间隔。

## S1 基线验收证据与范围

独立 Windows 实机验证通过登录/Hook、身份核对、反向连接、动作与错误响应、私群聊天、真实 @、唯一回复主体、去重、错误 Token 拒绝与恢复、正常停启、异常退出恢复及 AstrBot 重启。用户确认引用、嵌套转发、语音识别、图片、文本附件、提醒、人格、长期记忆、私群隔离、读空气、权限拒绝、表情续发和最终计时/聊天回退测试通过。

另执行 54 项 Local 离线回归，包含真实运行解释器的入口导入检查。真实只读动作中，好友历史 100 条约 0.7 秒；群历史请求 300 条、本次返回 200 条约 2.5 秒，无重复消息 ID，未观察到返回群字段不匹配；成员接口返回 owner/admin/member。保留有符号消息 ID，不按 ID 加减翻页。以上耗时是本次样本，不是性能保证。

详细测试脚本、用户填写表、脱敏报告和运行环境保存在独立 Local 工作区；正式仓库仅交付源码与维护文档，不包含原始聊天、二维码、凭据或登录态。

## 延期项与升级边界

- 按用户要求，语音发送、合并转发发送暂不实现；主动文件业务归后续 Work。本次不宣称这些发送能力已验收。
- P2 扩展表情回应、请求审批、群管理、视频及复杂媒体没有完整实机覆盖；撤权屏障等额外安全边界也不以离线测试冒充全量实机验证。
- Linux/WSL、腾讯云大陆服务器、正式一键安装、长期稳定性及完整备份恢复体系留待后续阶段。本次 Windows 结果不能替代这些验收。
- 本轮不新增 AstrBot 核心补丁，也不修改 SnowLuma 或 Meme Manager 源码。但 P2 已有 11 个 AstrBot 补丁目标，现有收集桥绑定 Continuous Message 2.9.3 的内部接口，表情适配读取 Meme Manager 的绑定图库接口。
- 依赖升级仍需固定新版本、验证补丁哈希和接口、在 Local 回归后再发布；不保证更新可以直接替换，也不为减少耦合绕过权限检查。

下一阶段仍按 Windows 完整迁移 → Linux SnowLuma 部署与管理适配 → WSL 实机 → 腾讯云大陆服务器验收推进；本次同步不自动开始 S2。
