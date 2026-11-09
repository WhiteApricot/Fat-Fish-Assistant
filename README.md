# 肥鱼助手 · Fat-Fish-Assistant

肥鱼助手当前交付 Windows 10/11 x64 上的 QQ 聊天能力，并规划演进为拥有独立 Work、统一治理与可扩展 Agent 生态的个人 AI 工作平台。当前 migration/snowluma-only 分支已完成 Windows SnowLuma S1 原型验收，通过 SnowLuma 的 OneBot 反向连接接入 AstrBot，并提供会话人格、个人与群记忆、提醒、群聊回复策略和受权限控制的管理指令。

`QQ ↔ SnowLuma ↔ OneBot ↔ AstrBot ↔ 肥鱼助手网关与插件 ↔ 模型及本地数据`

- 当前原型接入、已验收功能和已知限制见 [S1 Windows SnowLuma](docs/S1_WINDOWS_SNOWLUMA.md)。
- 根目录安装、启动和迁移入口仍含 P2 实现，尚未迁移为 SnowLuma 管理入口；[旧快速开始](docs/QUICK_START.md)不适用于 S1 新实例。
- 想了解能做什么，先读 [用户功能总览](docs/FEATURES.md)。
- 查看 [用户文档](docs/README.md)、[开发者文档](docs/protocols/README.md) 或 [工程路线图](docs/protocols/ROADMAP.md)。目标架构见 [Work 与统一治理](docs/protocols/WORK_ARCHITECTURE.md)。

初装需要获得所选组件并准备自己的账号与模型凭据。QQ 登录需要本人扫码。Linux 部署、QQ 电话和电脑控制仍未交付。外部组件固定版本及其许可见 [依赖](docs/DEPENDENCIES.md) 和 [第三方声明](THIRD_PARTY_NOTICES.md)。项目许可见 [LICENSE](LICENSE)。

正式源代码由 [GitHub 项目仓库](https://github.com/WhiteApricot/Fat-Fish-Assistant) 管理。测试及工程记录使用独立本地工作区，安装状态和运行数据由各自安装根管理。

开发维护入口见 [系统设计](docs/protocols/CONTRIBUTING.md#系统设计)。业务组件在 src/fat_fish，AstrBot 入口在 plugins，管理包在 fat_fish_management。
