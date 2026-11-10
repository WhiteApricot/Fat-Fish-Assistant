# Work 与统一治理架构

本文记录 P4–P8 的目标设计。当前交付能力见[功能说明](../FEATURES.md)，阶段门槛见[路线图](ROADMAP.md)。

## 组件边界

| 组件 | 责任与权威状态 |
| --- | --- |
| AstrBot / SnowLuma | AstrBot 是唯一 QQ 聊天主体，解析消息、识别用户和会话、呈现人格与发送回复。SnowLuma 提供 OneBot 通信。聊天继续使用实例配置的模型服务，DeepSeek 聊天模型与 DSH 分别配置。 |
| Fat-Fish Core | 统一人格、记忆访问、授权和消息治理。现有聊天边界继续由 AstrBot 网关与插件落实，跨组件能力随 P6 接入。 |
| Work | 拥有任务 ID、计划、调度、状态、事件、审计及结果。确定性请求直接走 Device Service。需要推理、动态规划或跨工具协调时才选择 DSH。 |
| Device Service | 独立、无需模型的设备能力。管理进程、文件与传输、桌面及窗口截图、窗口操作、进程和日志观测、系统通知及音频事件。AstrBot 经 Work 通道申请操作，DSH 经受控工具适配器复用同一服务。 |
| DSH | DeepSeek Harness。为复杂任务规划并动态调用工具，运行获准插件，协同外部 Agent，结果交回 Work。DSH 会话是任务的关联记录。 |
| GPT/Codex、Claude | 外部高能力 Agent，负责具体工程工作，经获准的 [Computer Use Provider](EXTERNAL_AGENTS.md) 或人工交接。 |

确定性链路为 QQ → AstrBot → Work → Device Service → Work → AstrBot → QQ。复杂链路在 Work 与设备工具或外部 Agent 之间加入 DSH。P3 的 Linux 聊天和 P4 的 Windows Work 可各自运行。P5 引入认证传输，P6 的 Bridge 在其上增加跨来源的通知治理。

## Work、设备与远端协作

- Work 先持久化任务、调用者、目标设备、授权范围与幂等键，再回执 Task ID。相同调用者和幂等键只映射到同一任务，参数冲突时拒绝。执行尝试单独编号，重启先对账再决定是否重试。任务取消需底层确认，已完成的外部副作用留审计。
- 设备请求包含任务、调用主体、能力和资源范围。路径以规范化后的实际目标授权，防止路径穿越及链接逃逸。进程绑定启动身份以防 PID 重用，截图绑定具体窗口和所属进程，桌面能力要求正确的交互会话。执行与监控返回结构化事件；Work 按事件 ID 去重并校验状态转换。
- P5 候选为由 Work 发起的认证加密连接。协议形状先定义版本、关联 Task ID、事件 ID、任务内序号与确认游标。发送侧保存未确认事件，重连按游标补发，接收侧幂等处理并可请求状态快照对账。结果和文件使用授权引用、大小及摘要校验，限制接收路径和可见范围。具体传输、凭据轮换和离线缓存期限待 P5 验证。
- P4 先以本机身份和资源策略执行，P5 扩展到设备认证及远程主体映射。P6 迁移至 Core 权限模型。任务和设备接口以部署无关的设备标识寻址。

## Core 治理

| 服务 | 设计和验收 |
| --- | --- |
| Persona Registry | 权威人格带版本、发布人与变更记录，定义身份、核心价值、行为底线和用户关系。按 AstrBot、DSH、插件生成带版本指纹的配置投影。保留基础/全局/会话的表达层级，将核心约束置于可定制层之上。发布后触发同步并核验配置回执与样例行为，检测漂移，失败时回退到上一通过版本或暂停对应投影。 |
| Memory Service | 共享用户记忆保存偏好、事实和长期目标，按用户及授权范围隔离。领域记忆留在插件存储，如观看记录和日记。QQ、Work、DSH 会话记忆以各自会话隔离，跨域只交换显式授权的摘要或引用。共享写入记录来源和版本，并支持纠正、删除及审计。现有 LivingMemory 的 SQLite/FAISS 与群记忆边界通过适配沿用，先验证迁移和隔离再开放写入。 |
| Permission Service | 认证 QQ 身份、设备、Work、DSH 与插件服务主体。按用户角色、任务、工具、文件/设备、平台操作授予最小范围和时效。入口授权后在执行边界再次检查，默认拒绝。高影响动作绑定目标、内容及有效期，撤销后阻断后续执行，日志脱敏。运行时用进程/账号、目录与网络隔离约束旁路工具。 |
| Communication Bridge | DSH 或插件 → Fat-Fish Bridge → AstrBot → QQ。Bridge 认证来源并校验事件类型、接收人和会话权限，按事件 ID 去重、限流、记录投递结果和重试。群聊或私聊目标由策略解析。外部平台评论和私信走独立发布授权，QQ 通知许可与外部发布许可分别管理。 |

## DSH 接入与证据

P6 评估由 Work 管理的 DSH 子进程与 SDK 适配，映射任务 ID 到尝试及会话 ID。接口语义包括启动、状态/事件、取消、恢复核对和结果回传，具体 SDK 调用以选定版本验证为准。每个任务只暴露所需工具和上下文。限制原生 shell、文件与网络旁路，再做越权测试。DSH 故障仅影响对应复杂任务。

截至 2026-11-05，官方 Python SDK 文档列出 Windows x64、Python 3.10+、配套原生 runtime wheel 以及兼容模型端点与凭据。SDK 启动 `dsh` 并支持 `sdk` 与 `sdk-minimal`，自定义 Profile 需保留 JSON-RPC 服务行。插件通过 Profile 组合及 bundle 层加载，官方提醒本地工具的工作目录并非访问边界。此处为文档核实。P6 仍需固定发布版本并实测启动、事件、取消、重启恢复、受限工具与最小权限。插件管理依赖和 Windows GUI 支持需分别核实。[官方 Python SDK 指南](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/python-sdk.md) · [官方架构](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)

## 现状差异与决策记录

- **架构决策**：Work 拥有任务权威状态，设备能力由 Device Service 统一供给，DSH 作为可选引擎，AstrBot 保有唯一 QQ 出口。P5 的旧生产 Linux 聊天目标及单消费者、灾备、容量要求归 P3。旧 P6 桌面集成管理作为独立里程碑由新 P6 取代。凭据、脱敏与人工 QQ 登录保留在 P3/P5，管理协议复用与桌面能力评估归 P4/P7。
- **Linux 风险**：当前交付入口为 CMD/PowerShell，进程和私密数据保护有 Windows 实现，Windows 构建与 SnowLuma 官方发行来源分别由 manifests 固定。P3 应盘点路径、锁、服务账号和 Linux 二进制，沿用已验证的停写、迁移与健康检查语义。[现行迁移规范](../MIGRATION.md)
- **治理差距**：当前人格合并、群及记忆权限、消息去重与提醒调度位于 `src/fat_fish` 和 AstrBot 插件。它们可供复用，跨进程治理及 Work 任务协议仍属规划。现有本机管理及原生进程脚本在 P4 评估，现有聊天提醒继续由 AstrBot 调度。[消息处理](../BOT_LOGIC.md) · [管理协议](MANAGEMENT_API.md)
- **运行时决策**：先验证 Work 所需 SDK 能力。P8 若需不同宿主服务，允许独立 Extension Runtime，以不同账号或进程边界、home、凭据与权限运行，经 Core 接入。是否合并留待兼容及隔离证据。
