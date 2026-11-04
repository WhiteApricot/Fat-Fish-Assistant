# 肥鱼助手 · Fat-Fish-Assistant

肥鱼助手是部署在 Windows 10/11 x64 的 QQ 聊天助手。它通过 NapCat 的 OneBot 反向连接接入 AstrBot，并提供会话人格、个人与群记忆、提醒、群聊回复策略和受权限控制的管理指令。

`QQ ↔ NapCat ↔ OneBot ↔ AstrBot ↔ 肥鱼助手网关与插件 ↔ 模型及本地数据`

- 首次安装双击 [安装入口](Install-Fat-Fish-Assistant.cmd)，按照 [快速开始](docs/QUICK_START.md) 初始化和扫码。
- 日常使用 [启动入口](Start-Fat-Fish-Assistant.cmd)，跨设备使用 [迁移入口](Migrate-Fat-Fish-Assistant.cmd)。
- 查看 [用户文档](docs/README.md)、[接口与数据协议](docs/protocols/README.md) 或 [工程路线图](docs/work/ROADMAP.md)。

初装需要获得所选组件并准备自己的账号与模型凭据。QQ 登录需要本人扫码。Linux 部署、QQ 电话和电脑控制仍未交付。外部组件固定版本及其许可见 [依赖](docs/DEPENDENCIES.md) 和 [第三方声明](THIRD_PARTY_NOTICES.md)。项目许可见 [LICENSE](LICENSE)。

正式源代码由 [GitHub 项目仓库](https://github.com/WhiteApricot/Fat-Fish-Assistant) 管理。测试及工程记录使用独立本地工作区，安装状态和运行数据由各自安装根管理。

开发维护入口见 [系统设计](docs/work/SYSTEM_DESIGN.md)。业务组件在 src/fat_fish，AstrBot 入口在 plugins，管理包在 fat_fish_management。
