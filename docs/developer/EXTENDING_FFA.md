# 接口与扩展入口

| 范围 | 当前调用方式及边界 |
| --- | --- |
| Core | AstrBot 补丁调用 `PlatformGateway.run(event, downstream)`。正常请求进入下游，身份或权限未通过时停止事件。跨平台作用域与授权应由 Core 统一处理。 |
| Identity / Permission | `IdentityDirectory.resolve_principal(account)` 返回 Principal 与角色。`PermissionService.conversation(account, room, mentions=...)` 返回 Admission 或 None。原生证据由适配器提供，调用方在写入和发送前复核。 |
| Persona | `ChatSettings.prepare(event, message, admission)` 为当前请求设置偏好，`effective_notes(config, actor, group)` 读取有效附加说明。设置命令在受信 Gateway 调用并复核权限。 |
| Memory | 受信宿主创建 Access，存储服务接收范围、来源和授权信息。详见[记忆架构](MEMORY_ARCHITECTURE.md)。 |
| Provider | 通过清单、实例配置和受信工厂注册，输入、发送错误和生命周期见 [Provider 契约](PROVIDER.md)。 |
| Task、Work、Worker | 当前有提醒任务与 Agent 操作状态。统一 Task API、Windows Work、Task Router、远端 Worker API 和文件传输协议由 FFA-N03 至 FFA-N05 定义。 |
| Plugin、Agent | AstrBot 插件使用宿主生命周期。代码另有 `AgentRegistry` 的注册、启停、健康、观察、计划、审批、执行与对账接口。它是带授权和证据的连接器机制。跨服务的 Plugin Compatibility Layer 属于 FFA-N07。 |

新增平台时，先确定它提供聊天传输、活动来源还是独立工具。原生 SDK、事件转换、地址与权限证明放在适配层。统一身份、会话策略、记忆作用域和投递账本放在 Core。实现 Provider 时核对配置版本、能力声明、重复事件、撤回、重启及最后一次授权校验。

接入现有 Agent 连接器时，使用 `Manifest` 声明工具和风险，通过受信宿主注册连接器、Access 与允许的能力。输入和审批须绑定同一操作与参数。回执记录操作结果，未知的外部写入先对账再重试。当前仓库包含 `BiliWhaleObservations` 等观察和回执转换代码，正式 Bilibili 平台接入仍属 FFA-N02。[Agent 注册](../../src/fat_fish/agents/registry.py) · [Bilibili 观察入口](../../src/fat_fish/agents/bili_whale.py)

第三方插件应复用现有可用接口，扩展范围通过能力和权限声明。接入前检查许可证、固定依赖、历史数据与配置兼容、双平台隔离和恢复行为。Worker 扩展在统一协议建立后开展，需先确定身份认证、能力声明、任务状态、取消、日志、文件及失败重试契约。
