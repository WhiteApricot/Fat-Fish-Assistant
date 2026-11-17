# 开发参考

## 通信与身份

平台适配层把可信原生事件转换为 `MessageEvent`，其中包含独立的 Provider、发送人、会话和消息标识。Registry 从 schema 2 配置生成 Provider，Runtime 校验来源、授权和去重，Gateway 调用共用的命令、记忆和提醒服务。飞书通过内置 Lark 适配器接入，OneBot 接口供独立 Windows QQ 接入使用。适配器分别承担原生路由、引用读取和发信。[通信配置示例](../examples/config/ffa-communication.json)

AstrBot 持有原生平台连接。FFA 的启动入口在同一事件循环中加载宿主，以便飞书 SDK 的收包任务随宿主运行。更换依赖时校验 [固定来源](../manifests/sources.json) 和现有补丁。飞书字段与 SDK 行为在适配层处理，消息准入与身份策略在通用层处理。

身份目录通过统一身份关联多个平台账号。新人工绑定在另一平台的私聊中兑换邀请码。会话授权以平台实例和会话标识为边界。主动投递与即时回复均在发信前重新核对授权。引用只接受适配器验证的同会话消息，历史中的用户内容作为模型资料。

## Agent 与回复投递

主 Agent 处理对话和按需工具调用。`/naturalcommand` 控制管理工具，`/websearch` 控制公开搜索。管理工具由 Gateway 绑定到原始事件，命令注册中心验证参数，指令处理器核对权限和待确认状态。一个轮次可提交一项写操作，另可执行只读查找。命令执行与回执投递分别记录。投递状态不明时可用 `/history` 核对近期结果。显式命令直接进入注册中心。带自然语言参数的显式提醒仅提供其绑定工具。

系统回执提交后，主 Agent 的最终人格回应按句末标点和非空换行分段。Gateway 每段提交一条消息，并沿用 Runtime 的发送授权和去重检查。人格修改当轮使用更新后的表达要求。

主 Agent 沿用原有意图选择，通过受控工具进入 DeepSeek 原生公开搜索或链接阅读。该工具返回资料和来源给原 Agent，用于按现有人格生成最终答复。搜索、链接、附件和转发内容作为外部资料处理。需要组合管理与联网时，管理操作须独立于联网结果。[DeepSeek 能力与限制](../docs/DEEPSEEK_CHATBOT.md)

Gateway 在首条普通文字通过准入后立即启动处理。同会话、同发言者的后续文字通过准入后更新轮次版本和原生接收时间，取消尚未提交的旧处理。最终发送和管理写操作在事件循环内取得提交资格，等待中的输入获准后会重新生成。超过单轮合并上限的输入进入下一轮。正式模型计算和剩余回复节奏可重叠。平台诊断区分服务器创建、原生入口、授权、预处理、Agent、工具、每次 IM API 请求及最终回执。受支持的 Agent 回传可记录 Token。客户端展示需要客户端来源的观测。[临时延迟探针](latency_probe/README.md)

Provider 定义可用 `platform_capabilities` 为指定平台追加能力，运行时以实例清单校验。飞书主动发送支持本地图片与独立文件，经 AstrBot 原生组件和上传方法处理。图片可与文本及群提及合并成一条富文本消息。通过 `Outgoing.media` 传给 Provider 的本地路径应由可信调用方选定。文件上传失败或格式不支持时，Provider 在提交 IM 消息前返回失败。

## 测试与运行

从项目根目录设置 `PYTHONPATH=src` 后运行 `python -m unittest discover -s tests`。Windows 与 Ubuntu CI 分别运行语法检查和离线测试。飞书原生契约测试需要锁定的 AstrBot 和 Lark SDK。真实模型、账号、WebSocket 与实际客户端投递通过独立测试实例验证。依赖构建和软件升级见[运维说明](../docs/OPERATIONS.md)。

维护早期记忆快照时，可在受保护的本机实例中运行 `python -m fat_fish.communication.operator --private <实例的-private-目录> export-memory-erasures <新文件>` 导出删除账本，再通过恢复选项 `--erasure-ledger` 应用。快照与删除账本均保存在本地。
