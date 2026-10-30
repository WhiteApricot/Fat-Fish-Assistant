# 肥鱼助手 / Fat-Fish-Assistant

可自行部署的 QQ AI 助手集成框架。AstrBot 是唯一聊天主体；本项目提供权限适配、必要兼容补丁、可复现安装与命令行管理。GitHub 仓库建议名称为 **Fat-Fish-Assistant**。

仓库只保存自有代码、最小补丁、依赖清单、模板、测试和文档。AstrBot、插件、Python 包、NapCat 与模型从固定官方/可信来源获取，放在仓库以外；不携带第三方源码 ZIP、wheel、模型、QQ 登录态或私人数据库。

## 一键检测与安装

Windows x64。在审阅并信任本项目源码后，从项目目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Fat-Fish-Assistant.ps1
```

该执行策略仅作用于这次脚本进程，不改系统策略。向导询问**你自己的 QQ 主人身份**，自动准备固定 CPython、独立环境、外部源码与补丁、插件及空白实例。进度持续显示；必要时给出错误码、日志和修复建议。QQ 客户端及登录、模型服务密钥由你手动配置，脚本不会操作 QQ 窗口或导入别人的身份。

默认安装 Chat 和已验收的三个功能插件，不强制下载 Embedding/语音模型或 NapCat。缺少 Microsoft x64 运行库时先提示用户安装；不会擅自修改系统。CPython NuGet 版没有升级 UI，升级需新版本目录，不替换系统 Python。[Python 官方说明](https://docs.python.org/3.12/using/windows.html#the-nuget-org-packages)

仅检测、启停或查看配置向导：

```powershell
.\Fat-Fish-Assistant.ps1 -Action check
.\Fat-Fish-Assistant.ps1 -Action start
.\Fat-Fish-Assistant.ps1 -Action health
.\Fat-Fish-Assistant.ps1 -Action stop
.\Fat-Fish-Assistant.ps1 -Action wizard
```

默认安装目录是当前用户 LocalAppData 下的 Fat-Fish-Assistant，可用 `-Root` 指定仓库外的新目录。初始管理密码在实例 `private/dashboard-password.txt`，不在终端输出。实例初始没有 QQ 平台、云模型密钥、群白名单、旧聊天和记忆。默认人格只更名，不重写既有风格和行为。

## 模块化与自动化

高级用户及未来 P6 调用同一个稳定 JSON 接口：

```powershell
python -I ffa.py --root ../assistant-local check --network
python -I ffa.py --root ../assistant-local install --components chat --owner YOUR_QQ_ID
python -I ffa.py --root ../assistant-local components
python -I ffa.py --root ../assistant-local component astrbot status
```

安装组件可选 `python,astrbot,plugins,napcat,embedding,speech`；`chat` 是前三项的快捷组合。`--plugins` 可选择固定清单中的子集，空值表示不装功能插件；安全适配始终安装。只下载安装模型资产时，可以暂不初始化实例，启动前仍需完成实例配置。受限外部资产须先审阅对应许可，再明确选择 `--accept-external-licenses`。这不构成对模型或 NapCat 的再分发授权。

重复安装复用通过校验的缓存和已完成环境，不覆盖实例。中断阶段留在内部日志/失败目录；只对带本项目构建标记的未完成环境归档重试。Windows 虚拟环境在最终版本路径构建，避免破坏命令启动器。两个仅有源码的 Python 包由固定 sdist 和固定构建工具在独立构建环境生成 wheel，产物和本地解析锁留在私密安装目录。[pip 哈希安装说明](https://pip.pypa.io/en/stable/topics/repeatable-installs/)

## 文档与验收

- [架构与兼容标识](docs/ARCHITECTURE.md)、[更名记录](docs/RENAMING.md)。
- [解耦、废弃入口与回滚记录](docs/GOVERNANCE.md)、[实际验证范围](docs/VALIDATION.md)。
- [管理契约、组件能力与错误码](docs/MANAGEMENT_API.md)。
- [安装、维护、备份、恢复及回滚](docs/OPERATIONS.md)。
- [依赖与补丁](patches/README.md)、[第三方声明](THIRD_PARTY_NOTICES.md)。
- [Windows 虚拟机安装表](docs/VM_INSTALL_CHECKLIST.md)、[跨主机迁移表](docs/MIGRATION_CHECKLIST.md)。

本轮不执行新的完整迁移或换盘模拟。已有迁移夹具与原生恢复工具保留，VM 项均需用户后续实际填写。第二真实群测试暂缓；普通私聊和两条连续文字合并已有用户确认，完整私聊图文/顺序/打断/工具专项仍未全覆盖。Dagu、Work 仅保留描述项，不开发 P3–P6 功能。

这是本地开源候选，没有自动推送/上传步骤。自有代码及必要派生补丁按 AGPL-3.0 组织，外部组件各自许可独立适用；许可证歧义与未分发资产见第三方声明，不能因使用下载器而忽略义务。
