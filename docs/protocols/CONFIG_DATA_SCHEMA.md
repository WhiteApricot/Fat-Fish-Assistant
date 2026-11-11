# 配置与数据约定

下列为现行持久化约定。P3 跨平台记忆的兼容和迁移设计见 [Memory Service](MEMORY_SERVICE.md)。

| 相对位置 | 用途 |
| --- | --- |
| 安装根 .ffa-managed.json、installation.json | 受管理标记及组件登记 |
| instance/instance.json | 实例标识及活动发布指针 |
| instance/data/cmd_config.json | AstrBot 原生平台、模型和 Dashboard 配置 |
| instance/data/config | 插件配置 |
| instance/private/instance-identity.json、p2c-access.json | 固定主人与作用域权限、白黑名单及相关设置 |
| instance/data/data_v4.db、data/plugin_data | 聊天及插件持久化数据，包括记忆索引 |

管理响应字段以 [JSON Schema](../../manifests/management-response.schema.json) 为准。备份、软件版本检查与恢复以 [实例管理器](../../fat_fish_management/instance.py) 为准。数据库、路径和配置兼容键随实例迁移。

`p2c-access.json` 中的 `naturalcommand` 与 `webresearch` 分别保存自然语言管理和联网搜索开关。两者都是可选对象，使用 `private:<QQ>` 或 `group:<群号>` 作为键，值为 JSON 布尔值。缺失键按开启处理。开关沿用会话权限、命令历史和撤销规则。关闭 `webresearch` 后当前会话的回复请求不提供搜索工具。
