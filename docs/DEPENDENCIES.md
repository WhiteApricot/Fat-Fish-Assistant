# 外部依赖

| 组件 | 用途 | 版本和许可依据 |
| --- | --- | --- |
| Python | 独立管理及聊天环境 | 3.12.3，[解释器清单](../manifests/python-runtime.json) |
| AstrBot 与 Dashboard | 消息管线、模型和插件生命周期、本机管理页 | Dashboard 4.29.3，源码按提交固定，[源码清单](../manifests/sources.json)与[管理页清单](../manifests/dashboard.json) |
| SnowLuma / QQ | 唯一 QQ 通信端，使用官方发行包独立安装 | 官方 v2.4.2 Full，[Windows x64 发行锁](../manifests/snowluma/windows-x64.json) |
| LivingMemory、Continuous Message、Meme Manager、SelfReply 决策子集 | 记忆、消息合并、表情和主动参与判断 | [固定提交及许可证](../manifests/sources.json) |
| 嵌入与语音组件 | 可选本地记忆检索与语音识别 | [模型清单](../manifests/model-runtime-lock.json) |

模型服务需按实际配置提供凭据。分发条件、模型与素材声明见 [第三方声明](../THIRD_PARTY_NOTICES.md)。

FFA 受管依赖使用 [固定清单](../manifests/index.json)。Windows 的 Microsoft VC++ 运行库用于 Python 原生依赖检查；Python、AstrBot、Dashboard、插件、FFmpeg 和可选本地模型按自身清单管理。

SnowLuma 仅从官方 GitHub 仓库或官方 Release 获取。任何获准的传输加速都必须保留官方原始资产并核对 SHA256。Windows Full 包自带 Node，QQ 客户端按官方指引准备。FFA 发布物与缓存清理清单均不纳入 SnowLuma 原生组件。组件启停、设置、QQ 登录和数据管理通过官方 Launcher、WebUI 或已公开且明确支持的接口操作。禁止重打包、制作衍生发行包、修改原生组件、直接写入内部配置或数据库、注入进程和绕过官方流程。需要 GUI 时，由用户在官方界面完成。

本轮未增加AstrBot核心补丁，但P2已有11个补丁目标；升级仍需哈希、接口与权限回归，详见[S1升级边界](S1_WINDOWS_SNOWLUMA.md#延期项与升级边界)。
