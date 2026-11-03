# 配置与数据约定

| 相对位置 | 用途 |
| --- | --- |
| 安装根 .ffa-managed.json、installation.json | 受管理标记及组件登记 |
| instance/instance.json | 实例标识及活动发布指针 |
| instance/data/cmd_config.json | AstrBot 原生平台、模型和 Dashboard 配置 |
| instance/data/config | 插件配置 |
| instance/private/instance-identity.json、p2c-access.json | 固定主人与作用域权限、白黑名单及相关设置 |
| instance/data/data_v4.db、data/plugin_data | 聊天及插件持久化数据，包括记忆索引 |

管理响应字段以 [JSON Schema](../../manifests/management-response.schema.json) 为准。备份、软件版本检查与恢复以 [实例管理器](../../scripts/instance.py) 为准。数据库、路径和配置兼容键随实例迁移。
