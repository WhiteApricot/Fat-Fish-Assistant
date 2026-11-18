# Provider 接口与生命周期

`ProviderRegistry` 从内置 JSON 清单与受信 Python 包加载定义。外部包使用 `fat_fish.communication_providers` entry point，发行包须列入 `trusted_packages`。实例配置为 schema 2，指定 provider、platform、instance、bot、enabled 与选用的 config。重复标识、未知能力、依赖缺失或无效配置会被拒绝或跳过，细节见注册实现。

| 接口 | 调用约定 |
| --- | --- |
| 工厂 | 接收 ProviderId、机器人、ProviderContext 与配置，返回符合 manifest 和能力定义的 Provider。 |
| `start()` / `stop()` | 异步打开或关闭 FFA 包装层及钩子。由 Runtime 串行化同一实例的生命周期。 |
| `healthy()` | 返回包装层健康判断。检查失败可产生 unhealthy 状态。`health_snapshot` 返回启用状态、错误类型、尝试次数和时间。 |
| `send_checked(target, message, allowed)` | 异步发送，实际提交前调用授权回调。提交前明确拒绝可报告 `NotSubmitted`。已提交且结果不明应交给账本核验。 |
| 核验、撤回和就绪探针 | 仅在相应能力可用时接入。核验要绑定当前账号、机器人和会话。投递就绪探针供提醒等主动发送逻辑调用。 |

Runtime 先检查目标地址、权限和声明能力，并按操作 ID 去重。`send` 对已领取的操作返回 `False`，新提交成功返回 `True`。权限缺失、能力不足或 Provider 不可用按错误类型反馈，发送异常按是否已提交维护账本。回复走 `reply`，直接复用当前原生事件的发送入口并复核授权。

启动和停止失败记录为 `start_failed` 或 `stop_failed`。失败隔离到对应实例。启动失败可重试，停止失败应先完成清理；QQ 与飞书的原生重连还需由 AstrBot 或平台 SDK 执行。FFA 的 enable/disable 操作包装层，宿主退出会停止两端。

[接口实现](../../src/fat_fish/communication/runtime.py) · [定义与配置](../../src/fat_fish/communication/registry.py) · [配置示例](../../examples/config/ffa-communication.json) · [双平台运维](../DUAL_PLATFORM.md)
