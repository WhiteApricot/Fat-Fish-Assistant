# 清单与平台边界

index.json 索引 FFA 依赖锁、源码补丁、可选资源和 SnowLuma 独立发行锁。components.json 定义组件 ID、依赖与归属。Core 随 AstrBot 插件加载，Work 为保留组件。

assets.json 登记 FFA 自有安装流程会校验的外部资源。snowluma/windows-x64.json 记录官方 ZIP、来源提交、大小、SHA256 和 Launcher 名称，独立于 FFA 组装及迁移资产。Windows 适配器只读检查登记的发行布局。P3-E Linux 优先验证 Core、记忆及飞书。SnowLuma 发行锁仅用于 Windows 临时 QQ 兼容。

更新版本时校验官方来源、发行包摘要、补丁输入与输出 SHA256，运行 Windows 回归，再更新依赖记录。SnowLuma 的管理与许可边界见 [依赖说明](../docs/DEPENDENCIES.md)。
