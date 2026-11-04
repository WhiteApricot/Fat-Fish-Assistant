# AstrBot 插件约定

组装器将每个自有插件目录放入 release/plugins，并由实例管理器部署到 data/plugins。插件保留 metadata.yaml、main.py 与相对资源路径，交由 AstrBot 原生发现。Guard 注册消息及模型钩子，Health 提供本机启动视图和 OneBot 健康检查，Forwards 注册合并转发读取，Reminders 向共享 cron 调度器注册任务并在终止时移除。

第三方插件选择和固定源码由 [组装器](../../fat_fish_management/dependencies.py) 处理，配置模板位于 [examples/config/plugins](../../examples/config/plugins)。固定插件程序通过停写后的事务切换更新。

仓库的 plugins/ 提供发布插件的唯一输入，发布 src/ 承载共享业务包。AstrBot 通过 fat_fish_gateway 桥接核心网关。

## 启用、依赖与数据

四个插件由同一固定 AstrBot 发行包组装。插件标识是安装和数据路径的一部分。

| 标识 | 依赖与职责 | 禁用或初始化失败的影响 |
| --- | --- | --- |
| fat_fish_guard | 核心必需。接入人格、记忆保护与消息钩子；长期记忆操作需要启用 LivingMemory。业务数据在实例与对应记忆插件存储中。 | 失去核心回复约束、人格及记忆适配。 |
| fat_fish_forwards | 可选能力。要求启用固定的 Continuous Message 2.9.3，并检查三项私有桥接方法签名。转发材料随请求处理。 | 停用合并转发桥接，其余插件可以独立运行。 |
| fat_fish_reminders | 可选能力。依赖核心网关和 AstrBot 调度器。数据库位于 data/plugin_data/fat_fish_reminders。 | 不再调度或发送，现有提醒数据库保留。 |
| fat_fish_health | 管理集成必需。输出只读 OneBot 健康证据和启动视图。 | 管理器无法依据插件证据确认 OneBot 已连接，启动视图不可用。 |

转发初始化先检查版本、同步或异步约定及参数签名，全部通过后才替换上游方法。终止时恢复原方法。
