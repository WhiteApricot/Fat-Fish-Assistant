# 外部依赖

| 组件 | 用途 | 版本和许可依据 |
| --- | --- | --- |
| Python | 独立管理及聊天环境 | 3.12.3，[解释器清单](../manifests/python-runtime.json) |
| AstrBot 与 Dashboard | 消息管线、模型和插件生命周期、本机管理页 | Dashboard 4.29.3，源码按提交固定，[源码清单](../manifests/sources.json)与[管理页清单](../manifests/dashboard.json) |
| NapCat / QQ / Node | QQ 登录、OneBot 连接 | [运行包](../manifests/napcat-runtime-lock.json)与[Node 清单](../manifests/napcat-node-lock.json) |
| LivingMemory、Continuous Message、Meme Manager、SelfReply 决策子集 | 记忆、消息合并、表情和主动参与判断 | [固定提交及许可证](../manifests/sources.json) |
| 嵌入与语音组件 | 可选本地记忆检索与语音识别 | [模型清单](../manifests/model-runtime-lock.json) |

模型服务需按实际配置提供凭据。分发条件、模型与素材声明见 [第三方声明](../THIRD_PARTY_NOTICES.md)。

[依赖约束及锁文件](../packaging/requirements) 集中归档。实际下载和组装以 [固定清单](../manifests) 为准。
