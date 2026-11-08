# 外部依赖

| 组件 | 用途 | 版本和许可依据 |
| --- | --- | --- |
| Python | 独立管理及聊天环境 | 3.12.3，[解释器清单](../manifests/python-runtime.json) |
| AstrBot 与 Dashboard | 消息管线、模型和插件生命周期、本机管理页 | Dashboard 4.29.3，源码按提交固定，[源码清单](../manifests/sources.json)与[管理页清单](../manifests/dashboard.json) |
| NapCat / QQ / Node | QQ 登录、OneBot 连接 | [运行包](../manifests/napcat-runtime-lock.json)与[Node 清单](../manifests/napcat-node-lock.json) |
| LivingMemory、Continuous Message、Meme Manager、SelfReply 决策子集 | 记忆、消息合并、表情和主动参与判断 | [固定提交及许可证](../manifests/sources.json) |
| 嵌入与语音组件 | 可选本地记忆检索与语音识别 | [模型清单](../manifests/model-runtime-lock.json) |

模型服务需按实际配置提供凭据。分发条件、模型与素材声明见 [第三方声明](../THIRD_PARTY_NOTICES.md)。

[固定清单](../manifests) 是下载和组装的唯一依赖来源。Python、AstrBot 和 Dashboard 是核心。QQ 接入需安装 NapCat 组件。语音、嵌入模型及外部插件按安装选项启用。

## Linux 阶段 1 依赖锁

- [Ubuntu 24.04 x86_64 直接前置](../manifests/linux-x86_64/ubuntu-24.04.tsv)区分运行和构建包。安装器校验固定版本和缺件归档的 SHA256。传递系统依赖由 Ubuntu APT 签名索引管理，离线系统安装还需预备对应的索引和传递包。
- [Python 3.12 core 锁](../manifests/linux-x86_64/core.json)记录 P2 对应版本、Linux wheel ABI、下载来源和哈希。aiocqhttp 与 jieba 保留固定 sdist，构建工具复用 build.json。pywin32 属 Windows 平台。chat 扩展和可选语音环境另行验证。
- [QQ 与 NapCat Shell](../manifests/linux-x86_64/napcat.json)固定阶段 0 实机通过的 QQ、Shell 和启动器源码。QQ deb 的 SHA256 来自已验收的腾讯下载样本。组件组装和启动适配在后续批次接入 ffa.py。
- 系统前置脚本位于 scripts/linux/system-prerequisites.sh，支持 --check、--install 和 --offline。在线补包使用 curl 下载锁定的直接依赖，优先清华镜像，随后试用 Ubuntu 官方源。传递包使用系统已配置的 APT 源。verify_cache.py 在组装前验证平铺缓存。WSL 浏览器转发和软件渲染参数单独维护。
