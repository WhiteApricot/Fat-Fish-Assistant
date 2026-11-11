# Fat-Fish-Assistant（肥鱼助手）

肥鱼助手是独立于通信平台的 AI 助手。AstrBot 承担统一聊天前端。FFA 的目标架构通过 Communication Provider、Social Agent Provider 和 Work / Tool Provider 扩展能力，共享身份、权限、人格、记忆及行为记录。飞书是当前选定的主要通信平台，接入待 P3-C 验证。

## 当前交付

SnowLuma 基线在 Windows 10/11 x64 通过 OneBot 接入 QQ，提供会话人格、个人与群记忆、提醒、群聊回复策略和受权限控制的管理指令。SnowLuma 是临时 Windows QQ 兼容组件。当前 Windows 向导和聊天链路仍使用 QQ 身份。QQ 登录需用户本人在官方界面完成。

- [快速开始](docs/QUICK_START.md)、[用户功能总览](docs/FEATURES.md)和[用户文档](docs/README.md)
- [外部依赖](docs/DEPENDENCIES.md)、[第三方声明](THIRD_PARTY_NOTICES.md)和[项目许可](LICENSE)

## 开发规划

P3 依次完成平台解耦、统一记忆与最小插件治理、多平台联合验证、安装器和 Linux 部署。飞书、跨平台记忆、B 站实验插件、Linux 部署及 Work/DSH 均属后续计划。

- [工程路线图](docs/protocols/ROADMAP.md)、[目标架构](docs/protocols/WORK_ARCHITECTURE.md)和[开发者文档](docs/protocols/README.md)
- [工作区与提交规范](AGENTS.md)和[现行系统设计](docs/protocols/CONTRIBUTING.md#系统设计)
