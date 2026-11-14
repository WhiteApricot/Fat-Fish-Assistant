# 外部依赖

| 组件 | 用途 | 版本和许可依据 |
| --- | --- | --- |
| Python | 独立管理及聊天环境 | 3.12.3，[解释器清单](../manifests/python-runtime.json) |
| AstrBot 与 Dashboard | 消息管线、模型和插件生命周期、本机管理页 | Dashboard 4.29.3，源码按提交固定，[源码清单](../manifests/sources.json)与[管理页清单](../manifests/dashboard.json) |
| 飞书 | AstrBot 内置原生适配器与固定 SDK 提供长连接 | [飞书接入](LARK.md) |
| SnowLuma / QQ | 当前 Windows QQ 临时兼容组件，使用官方发行包独立安装 | 官方 v2.4.2 Full，[Windows x64 发行锁](../manifests/snowluma/windows-x64.json) |
| LivingMemory、Continuous Message、Meme Manager、SelfReply 决策子集 | 记忆、消息合并、表情和主动参与判断 | [固定提交及许可证](../manifests/sources.json) |
| 嵌入与语音组件 | 可选本地记忆检索与语音识别 | [模型清单](../manifests/model-runtime-lock.json) |

模型服务需按实际配置提供凭据。分发条件、模型与素材声明见 [第三方声明](../THIRD_PARTY_NOTICES.md)。

FFA 受管依赖使用 [固定清单](../manifests/index.json)。Windows 的 Microsoft VC++ 运行库用于 Python 原生依赖检查；Python、AstrBot、Dashboard、插件、FFmpeg 和可选本地模型按自身清单管理。

SnowLuma 仅从官方 GitHub 仓库或官方 Release 获取。任何获准的传输加速都必须保留官方原始资产并核对 SHA256。Windows Full 包自带 Node，QQ 客户端按官方指引准备。FFA 发布物与缓存清理清单均不纳入 SnowLuma 原生组件。组件启停、设置、QQ 登录和数据管理通过官方 Launcher、WebUI 或已公开且明确支持的接口操作。禁止重打包、制作衍生发行包、修改原生组件、直接写入内部配置或数据库、注入进程和绕过官方流程。需要 GUI 时，由用户在官方界面完成。

## Windows 安装依赖范围

默认使用 Core 锁，141 个发行项。完整 Chat 锁 160 项，保留给显式选择。双击安装默认跳过两个本地模型组件。Speech 使用自己的 4 项锁和模型，Embedding 使用独立模型和引擎。构建期的 2 个工具只用于固定源码构建；应用只在确实缺少共享 VC++ 运行库时引导补齐。SnowLuma Full 自带 Node，QQ 客户端由腾讯官方提供。旧通信运行包、独立 Node 安装和额外 QQ 运行资产均不在 FFA 安装计划中。

PyPI 优先使用国内镜像匹配固定文件名，失败后回退官方 PyPI。同意使用的 GitHub 加速服务可设置 FFA_GITHUB_TRANSFER_BASE，地址需为 HTTPS。加速服务失败后回退固定官方 URL。SnowLuma 接入使用用户保存的原始官方 ZIP 或有效安装目录。本地 ZIP 校验大小与 SHA256。[Windows QQ 接入](SNOWLUMA_WINDOWS.md)
