# AstrBot 插件约定

组装器将每个自有插件目录放入 release/plugins，并由实例管理器部署到 data/plugins。插件保留 metadata.yaml、main.py 与相对资源路径，交由 AstrBot 原生发现。Guard 注册消息及模型钩子，Health 提供本机启动视图和 OneBot 健康检查，Forwards 注册合并转发读取，Reminders 向共享 cron 调度器注册任务并在终止时移除。

第三方插件选择和固定源码由 [组装器](../../scripts/dependencies.py) 处理，配置模板位于 [examples/config/plugins](../../examples/config/plugins)。固定插件程序通过停写后的事务切换更新。
