# 消息和操作如何处理

## 聊天入口

QQ 消息由 SnowLuma 经反向 OneBot WebSocket 送到 AstrBot。飞书消息由 AstrBot 的 Lark 适配器接收。固定版补丁在 AstrBot 消息管线入口调用 `PlatformGateway.run(event, downstream)`。适配器转换原生事件为 `MessageEvent`，带上平台实例、机器人、私聊或群聊、账号、消息 ID、@、引用及媒体。私聊以发件人作对端，群聊使用原生群 ID，线程继续区分会话。[AstrBot 补丁](../../patches/astrbot/changes.json) · [事件转换](../../src/fat_fish/adapters/astrbot_communication.py)

## 身份、权限与回复

1. Gateway 先核对 Provider 与机器人，刷新需要的原生权限。QQ 私聊核对好友，群管理核对对应群的原生身份。飞书同组织用户的已验证私聊可注册普通成员。
2. 已有绑定用原生 Account 查询 Principal。私聊身份命令先走 Provider 账号验证，`/invite` 签发有时效的一次性邀请，另一个账号用 `/link` 确认。`/unlink` 受所有权、主账号保护和确认规则约束。
3. Runtime 检查会话准入并领取事件去重记录。群准入、群模式、封禁、@ 和当前权限影响能否进入后续处理。未经授权的消息或重复事件在 Gateway 停止。
4. `ChatTurns` 按会话与账号协调短时间内的多条消息及旧回复取消。显式命令在 `CommandRegistry` 分派，管理写入可要求确认。自然语言管理使用 `ManagementTurn`，在执行时复核权限。
5. Gateway 准备媒体、引用与记忆。媒体处理受原生能力与授权限制，失败时可发送适用的提示。普通群消息由 Participation 根据模式、是否被直接呼叫、冷却及决策结果判断是否插话。
6. Gateway 加载 FFA 会话偏好，随后调用 AstrBot 下游。AstrBot 选择基础人格和模型，FFA Guard 在模型请求中加入经当前会话授权的记忆。模型回复提交前再次校验权限及回复是否已被更新的消息取代。普通回复使用 4–6.5 秒的计划窗口，实际回复还受模型、媒体及发送耗时影响。
7. 回复通过 Runtime 账本记录提交结果。无法确认是否已发送时保留未知状态。已确认的发送可写入相应的记忆活动。

[Gateway](../../src/fat_fish/gateway/platform_gateway.py) · [插话决策](../../src/fat_fish/participation/participation.py) · [Guard](../../plugins/fat_fish_guard/main.py)

## 撤回与提醒

撤回由原生适配器核验来源，Runtime 再核对当前授权。Gateway 按原会话、作者和消息标识取消相关待发回复，交给记忆桥清理可追溯的活动、派生记录与上下文。清理能覆盖的范围取决于原消息证据和平台通知。

AstrBot 调度器触发提醒检查，FFA 保存在提醒记录中的完整会话键决定投递目标。依赖原生连接的权限刷新遇到离线时延后，无须占用授权失败重试次数。已知授权撤销会终止投递，状态未知的发送按账本核验。[提醒分派](../../src/fat_fish/reminders/dispatch.py) · [提醒存储](../../src/fat_fish/reminders/reminders.py)
