# 肥鱼助手 / Fat-Fish-Assistant

可自行部署的 Windows QQ AI 助手。AstrBot 是唯一聊天主体；项目提供权限适配、固定依赖、独立实例及统一管理接口。正式工程和 Git 根仅为本目录，现有远端为 [WhiteApricot/Fat-Fish-Assistant](https://github.com/WhiteApricot/Fat-Fish-Assistant)。本轮不改变仓库可见性，不进入 P2F/P3。

## 普通用户从这里开始

1. 首次安装：双击 **Install-Fat-Fish-Assistant.cmd**。确认位置、许可和功能，填写主人 QQ、机器人 QQ 及隐藏的 DeepSeek API Key。专用 QQ 组件自动准备，登录页自动填写 token，你只需手动扫码。分条回复和 24 张鲸鱼娘 GIF 表情随安装准备好，机器人会按语境适度选图。
2. 日常使用：只需双击 **Start-Fat-Fish-Assistant.cmd**。复用已有服务，按顺序启动已安装组件并检查健康；需要扫码时会打开 NapCat 页面。失败保留窗口，并给出原因和日志位置。
3. 换主机：双击 **Migrate-Fat-Fish-Assistant.cmd**，选择迁出或导入。迁出会正常停止并暂停源实例；导入选择一个加密包、密码和新的空安装位置，自动获取匹配依赖并恢复支持的数据。目标必须手动重新登录 QQ。

完整操作见 [快速开始](docs/QUICK_START.md)、[运维](docs/OPERATIONS.md) 和 [迁移清单](docs/MIGRATION_CHECKLIST.md)。AstrBot Dashboard 用户名为 astrbot，初始密码从启动日志安全采集，以 Windows 当前账户 DPAPI 加密保存；改密后首次打开设置可隐藏输入一次完成安全更新，以后自动登录。初始群白名单为空；安装、健康检查和迁移都不会扩大权限。

双击入口、实际服务测试与人工验收是不同层级。当前执行结果和待验收项以 [P2E-UX 报告](docs/P2E_UX_REPORT.md) 和 [验证记录](docs/VALIDATION.md) 为准；尚不能把同机独立实例恢复称作新 Windows 主机验收。

## 工程与依赖

工程边界及 Git、开发、测试规则统一见 [WORKFLOW](docs/WORKFLOW.md)。维护者的运行、缓存和测试仅写入相邻 Fat-Fish-Assistant.local；正式工程不启动服务、不下载依赖、不生成数据库。普通安装可使用用户 LocalAppData 或自行选择的仓库外目录。Backup 仅供历史恢复，不参与构建。

仓库只保存自有代码、最小补丁、依赖清单、模板、测试和文档。解释器、AstrBot、第三方插件、NapCat、模型和 Python 包从固定来源按 SHA256 获取；不携带登录态、真实 Key、聊天和私人数据库。不直接编辑上游核心，既有兼容补丁继续通过 patches 和前后哈希管理。

默认推荐 QQ 连接与长期记忆检索组件，语音可选；可跳过可选组件使用核心管理页面。缺少 Microsoft 运行库时，在明确接受外部许可后获取固定哈希的官方安装程序；只有此必要步骤可能请求 Windows 系统确认。不修改系统 Python 或全局 PowerShell 执行策略。

## 维护者资料

双击入口复用原有 JSON 管理 API，未建立第二套服务管理器。高级命令及兼容契约见 [MANAGEMENT_API](docs/MANAGEMENT_API.md)。代码升级、回滚和故障恢复见 [OPERATIONS](docs/OPERATIONS.md)。测试入口见 [VALIDATION](docs/VALIDATION.md)，历史 P2E 基线记录保留在同一文档的历史章节。

- [架构](docs/ARCHITECTURE.md)、[工程设计](docs/ENGINEERING_DESIGN.md)、[治理](docs/GOVERNANCE.md)、[补丁](patches/README.md)。
- [阶段规划](docs/ROADMAP.md)、[更名记录](docs/RENAMING.md)、[Windows VM 检查表](docs/VM_INSTALL_CHECKLIST.md)。
- [第三方声明](THIRD_PARTY_NOTICES.md)、[LICENSE](LICENSE)。自有代码及必要派生补丁按 AGPL-3.0 组织，外部组件独立遵守各自许可；下载能力不代表再分发授权。
