# 当前架构

以 main 中的代码和锁定依赖为准。FFA Core 是由多个 Python 模块组成的进程内业务层。受管入口启动固定版本的 AstrBot。AstrBot 持有 OneBot 和 Lark 的原生连接，负责模型运行、原生会话历史和调度器。补丁把平台消息交给 FFA 处理，再由 AstrBot 完成下游执行。两个平台共用同一个 Core。

| 模块 | 职责 | 源码 |
| --- | --- | --- |
| PlatformGateway | 转换后接入、身份命令、会话准入、回复协调、上下文准备 | [platform_gateway.py](../../src/fat_fish/gateway/platform_gateway.py) |
| CommunicationRuntime | Provider 状态、入站授权与去重、回复和主动投递、撤回核验 | [runtime.py](../../src/fat_fish/communication/runtime.py) |
| Provider 与适配器 | 转换原生消息，提供原生权限、引用、撤回及发送能力 | [registry.py](../../src/fat_fish/communication/registry.py) · [适配器](../../src/fat_fish/adapters/) |
| Identity / Permission | 平台账号绑定到 Principal，计算全局角色和当前会话准入 | [identity.py](../../src/fat_fish/core/identity.py) · [service.py](../../src/fat_fish/permissions/service.py) |
| Persona | AstrBot 选择基础人格，FFA 加载全局、个人及群内附加偏好 | [persona](../../src/fat_fish/persona/) |
| Memory | 记录活动和事实，维护有来源的卡片、社交画像和 Bot 经历，按权限召回和清理 | [记忆架构](MEMORY_ARCHITECTURE.md) |
| Task 与 Plugin | 当前有提醒调度和 Agent 操作状态，宿主加载 AstrBot 插件 | [接口与扩展](EXTENDING_FFA.md) |

## 数据与状态

- `ProviderId` 由平台与实例组成，`Account.storage_key` 再加原生账号 ID。IdentityDirectory 持久化账号、已核验绑定、Principal、全局角色和群授权。QQ 与飞书可绑定同一 Principal。
- `Conversation.storage_key` 由平台、Provider 实例、Bot、会话类型、会话 ID 和线程组成。消息事件 ID 以完整会话键和原生消息 ID 哈希得到。消息去重、群权限、群人格、撤回和投递目标以会话为边界。
- FFA 私聊长期记忆跟随 Principal。群记忆跟随完整会话键。AstrBot 的原生会话历史仍按平台会话保存。授权、用途和来源决定记忆能否进入当前请求。
- 基础人格记录位于 AstrBot 的数据库，受管初始化将 `Persona_Master.md` 写入 `feiyu-v1`。FFA 的 ChatSettings 单独保存会话设置和偏好。个人偏好跨已绑定账号，群偏好作用于当前群。
- 会话准入返回 Principal、角色、模式和当前群管理权限。全局管理员身份由 FFA 管理。原生权限证据由适配器提供，并限定于对应平台与会话。
- Provider 包装层健康状态保存在进程内，去重和发送账本由 FFA 持久化。AstrBot 和原生 SDK 持有连接、心跳和重连。提醒保存任务状态、到期时间、目标和发送记录。
- 当前统一 Task、Work、Worker 和正式插件兼容层仍在[路线图](../ROADMAP.md)中。现有提醒状态和 Agent 操作记录是两个各自的机制。

[程序行为](MESSAGE_FLOW.md) · [Provider 契约](PROVIDER.md) · [数据与隐私](../DATA_AND_PRIVACY.md)
