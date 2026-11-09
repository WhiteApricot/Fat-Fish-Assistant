# 外部依赖

| 组件 | 用途 | 版本和许可依据 |
| --- | --- | --- |
| Python | 独立管理及聊天环境 | 3.12.3，[解释器清单](../manifests/python-runtime.json) |
| AstrBot 与 Dashboard | 消息管线、模型和插件生命周期、本机管理页 | Dashboard 4.29.3，源码按提交固定，[源码清单](../manifests/sources.json)与[管理页清单](../manifests/dashboard.json) |
| SnowLuma / QQ | S1唯一QQ通信端，用户手动安装 | 官方v2.4.2 Full，来源和校验见[S1说明](S1_WINDOWS_SNOWLUMA.md) |
| NapCat / QQ / Node（P2遗留） | 尚待S2替换的安装资产，不作备用运行路线 | [运行包](../manifests/napcat-runtime-lock.json)与[Node 清单](../manifests/napcat-node-lock.json) |
| LivingMemory、Continuous Message、Meme Manager、SelfReply 决策子集 | 记忆、消息合并、表情和主动参与判断 | [固定提交及许可证](../manifests/sources.json) |
| 嵌入与语音组件 | 可选本地记忆检索与语音识别 | [模型清单](../manifests/model-runtime-lock.json) |

模型服务需按实际配置提供凭据。分发条件、模型与素材声明见 [第三方声明](../THIRD_PARTY_NOTICES.md)。

受管组件的下载和组装使用[固定清单](../manifests)；S1手动安装的SnowLuma另按官方Release及本轮校验记录固定。Python、AstrBot 和 Dashboard 是核心。S1的QQ接入使用用户手动安装的官方SnowLuma；现有NapCat清单仍为待迁移资产，不用于S1实例。语音、嵌入模型及外部插件按安装选项启用。

本轮未增加AstrBot核心补丁，但P2已有11个补丁目标；升级仍需哈希、接口与权限回归，详见[S1升级边界](S1_WINDOWS_SNOWLUMA.md#延期项与升级边界)。
