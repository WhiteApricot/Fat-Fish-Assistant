# Fat-Fish-Assistant（肥鱼助手）开发约定

- 正式仓库为 `E:\AI_Chat\Fat-Fish-Assistant`。源码、锁文件、正式文档、提交及发布在此维护。正式开发从 `main` 开始，`backup/snowluma` 和 `backup/napcat` 是历史备份。
- 沙箱实例、测试环境、运行日志、实验脚本、测试数据、构建产物和 Git 恢复材料放在 `E:\AI_Chat\Fat-Fish-Assistant.local`。正式修改同步至正式仓库。提交前检查暂存内容和测试结果，排除凭据、用户数据、日志、缓存、安装包和运行文件。
- 维护现有用户数据、人格、权限、LivingMemory、插件配置和迁移兼容。遇到未知数据先查归属及用途。修改启动、权限、迁移或持久化时验证正常入口、拒绝、失败恢复和旧数据。
- AstrBot 与第三方组件按固定清单及补丁维护。升级需说明必要性、兼容风险和验证结果。SnowLuma 是临时 Windows QQ 适配，本阶段只验证现有部署。P3-D 设计仅接受用户指定的本地包或有效安装目录。稳定 QQ 组件选定前暂停 QQ 组件 Linux 移植。
- 按[路线图](docs/protocols/ROADMAP.md)的阶段范围交付，按[目标架构](docs/protocols/WORK_ARCHITECTURE.md)保持平台逻辑在适配器边界。区分已实现、实验性和待开发。贡献、验证与发布细节见[贡献指南](docs/protocols/CONTRIBUTING.md)。
