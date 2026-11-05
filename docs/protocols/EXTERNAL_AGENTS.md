# 外部 Agent 调度与 Computer Use

P7 目标是通过获准的路径利用既有 GPT/Codex 与 Claude 订阅。QQ → AstrBot → Work → DSH → Provider → 外部应用。Work 持有 Task ID、执行尝试和结果，DSH 协调操作与证据收集，外部 Agent 执行工程任务，AstrBot 统一向 QQ 汇报。

## 接入门槛

按地区、账号计划、目标客户端及操作分别记录服务条款和明确许可，覆盖自动提交、状态读取、输出提取和代码上传。OpenAI 条款限制自动或程序化提取数据或输出，Anthropic 消费者条款限制自动访问，并列明 API Key 或明确许可的例外。P7 在真实账号接入前完成适用性核查。没有明确可用路径时用人工提交和结果交接，自动化链路可用模拟应用验收基础设施。外部订阅与 DSH 自身模型凭据分别核算。[OpenAI 使用条款](https://openai.com/policies/row-terms-of-use/) · [Anthropic 消费者条款 §3](https://www.anthropic.com/legal/consumer-terms)

## Provider 契约草案

| 能力 | 请求与返回 |
| --- | --- |
| 枚举与启动 | `list_windows` 返回应用、进程身份、窗口标识和会话；启动仅接受允许的应用标识。 |
| 定位与观测 | `focus_window`、`capture_window`、`get_ui_tree`。使用绑定窗口、超时和观测时间，UI 树不可用时显式报告。截图和界面文本按任务范围保存。 |
| 输入 | `click`、`type_text`、`press_key` 绑定目标窗口与操作 ID，执行前重验焦点和目标控件，回执区分已执行、拒绝及结果未知。 |

Provider 通过能力声明可替换。候选包括 Windows UI Automation 服务、MCP 工具、可桥接的 AstrBot 插件及未来 DSH 官方实现。AstrBot 插件作为载体时，需经认证 MCP 或 HTTP 将本机执行端接给 DSH，明确远端 Linux AstrBot 与 Windows 交互桌面的部署关系。Device Service 提供共享窗口和截图能力。Work 任务协议独立于 Provider；P7 对各候选比较窗口定位、锁屏/断开桌面会话、焦点争抢和 UI 版本变化。

## 提交、状态与证据

1. 校验应用和账号会话，分配独立 Git worktree 或分支，记录起始提交及项目根。并发写入默认隔离，共享工作区加互斥；并入和冲突处理需独立审查。
2. 激活目标窗口，确认项目路径，创建任务并提交。绑定应用会话或任务引用。提交结果未知时先核对目标应用，避免盲目重复点击。
3. Work 支持 `QUEUED → LAUNCHING → SUBMITTED → RUNNING`，审批、输入和额度分别为 `WAITING_APPROVAL`、`WAITING_USER_INPUT`、`USAGE_LIMIT_REACHED`。满足恢复条件后复核再继续。终态为 `FAILED`、`COMPLETED`、`CANCELLED`。审批及输入交还用户。无法确认外部取消时保留取消请求及观测状态。
4. Device Service 研究系统通知和 WASAPI Loopback 音效识别，将信号按应用、会话和时间关联并送 Work Controller。信号只触发确认，以 UI Automation、任务视图、截图及结果材料验证完成。漏报用低频检查兜底。仅在需要解释未知界面或冲突证据时唤醒 DSH。Windows 通知可访问性、音频归属和各应用实际事件待实测。
5. 收集基线到结果的 Git Diff、测试命令和结果、应用输出与失败证据。Work 保存授权引用并经 AstrBot 汇报。缺失测试记录明确标记，避免把应用完成提示等同于测试通过。

P7 以启动、项目选择、可靠提交、等待与限额识别、完成确认、结果回收为验证主线。Provider 契约、条款判定和状态识别分别记录应用版本与测试证据。
