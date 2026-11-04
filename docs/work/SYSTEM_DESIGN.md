# 系统设计

```text
Fat-Fish-Assistant/
├── ffa.py、*.cmd、Fat-Fish-Assistant.ps1
├── src/fat_fish/          机器人业务组件
├── plugins/              AstrBot 自有入口、元数据和视图
├── fat_fish_management/  Python 管理与实例操作
├── scripts/              Windows 进程、桌面、服务、构建及诊断
├── manifests/、patches/  固定来源、校验和及补丁
├── packaging/requirements/
├── examples/
└── docs/
```

| 修改内容 | 入口与实现 |
| --- | --- |
| QQ 消息、命令权限、会话隔离 | AstrBot 的 fat_fish_gateway 桥接调用 src/fat_fish/gateway，权限在 permissions，命令意图在 commands |
| 长期记忆、人格及群聊参与 | Guard 注册 AstrBot 钩子，memory/commands.py 执行记忆指令；memory、persona、participation 各自处理数据与策略 |
| 合并转发、提醒、健康证据 | plugins 中的 Forwards、Reminders、Health 注册原生生命周期；业务分别位于 integrations、reminders、adapters |
| 管理请求、安装与构建 | ffa.py 进入 controller.py；dependencies.py 获取固定依赖、验证补丁、组装和封存发布 |
| 实例启停、升级与回滚 | manage.py 负责运行状态和切换事务，run_chat.py 执行原生生命周期；instance.py 负责数据和发布完整性、初始化及冷备份 |
| 跨机迁移与配置 | migration.py 编排源停写及目标恢复，migration_package.py 处理包格式；configure_* 负责原生配置 |
| Windows 操作 | scripts/desktop 负责操作界面，scripts/services 调用受管模型与 NapCat，scripts/process 检查进程身份和私密权限 |

NapCat 管理 QQ 接入，OneBot 提供传输与平台动作，AstrBot 管理事件、模型、调度和插件。发布的 src/ 装载核心包，plugins/ 每个自有插件仅组装一次。实例初始化将发布的插件材料部署到 data/plugins，由 AstrBot 原生发现。

## 兼容标识与退出条件

- private/p2c-access.json、群会话前缀 p2c、数据库元数据 p2c_kind/p2c_authors、feiyu-* 平台、模型及人格 ID 属于持久化约定。迁移路径和会话身份需要继续读取这些值。退出前须完成数据转换、旧备份恢复和双向升级验证。
- FEIYU_* 环境变量及旧发布中的 AstrBot feiyu_p2c、旧语音服务路径供受管旧发布运行及回滚。当前发布使用 fat_fish_gateway 和新包路径。旧发布退出支持后才移除相应分支。
- 历史审计前缀及转发材料标记保留给既有记录和内容识别。退役前须确认相关记录和模型上下文的兼容性。
- 兼容检查仅接受固定来源的已知描述符变更，仍验证上游版本、归档、仪表盘及其他字段。数据库版本与路径约定沿用原实现。

参见 [插件协议](../protocols/PLUGIN_CONTRACT.md)、[管理 API](../protocols/MANAGEMENT_API.md) 和 [运维指南](../OPERATIONS.md)。
