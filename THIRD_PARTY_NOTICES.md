# 第三方来源、许可与分发边界

本轮仅在既有私有仓库维护自有集成代码、必要派生补丁、清单/模板/测试和文档，不公开发布。根 LICENSE 为 AGPL-3.0；不是对整个外部依赖栈重新授权。外部源码、wheel、模型、NapCat、QQ 与个人素材均不捆绑，也不进入本工程 Git 历史。

基础小鱼表情由本项目自有绘图代码在实例中生成，使用项目许可，不使用私人素材或外部图片；通过现有表情包导入接口安装。

|依赖|固定版本与来源|许可与处理|
|---|---|---|
|AstrBot|f61a7f42696da6df25dc7a249ec9b8d54ed9da18，[原文](https://github.com/AstrBotDevs/AstrBot/tree/f61a7f42696da6df25dc7a249ec9b8d54ed9da18)|AGPL-3.0；保留必要补丁及修改说明|
|AstrBot WebUI|官方 v4.29.3 发布包，dashboard.json 固定 SHA|AGPL 及内含前端依赖声明；不捆绑，安装时校验并保留原资源|
|LivingMemory|e95a5a9fa5f8a47f4d0b7274c1b646ea322b3d0b，[来源](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)|AGPL-3.0，3 个适配文件|
|continuous_message|4a2a52ba90544d1527a72602762ae5ce3ae27dca，[来源](https://github.com/aliveriver/astrbot_plugin_continuous_message)|AGPL-3.0，4 个适配文件|
|meme_manager|5cec301481b8b99682ba2a0021a432656a3a92ec，[来源](https://github.com/anka-afk/astrbot_plugin_meme_manager)|MIT，原版获取；不代表表情素材授权|
|selfreply 判断子集|11be8206a6bb26b9ebeccf301213e2654087d512，[来源](https://github.com/chengzhi-c/astrbot_plugin_self_initiated_reply)|GPL-3.0；运行时只取三份判断文件与 LICENSE，不复制到本仓库|
|Python/包|CPython 3.12.3、Chat 160 项、Speech 4 项，见 manifests|PSF 及各包独立许可，保留元数据/来源；158 个既有 wheel 与两个固定源码构建|
|shipyard-python-sdk|0.2.4，源码匹配提交 126718566de2d83648637e225c472b9a44e303f9|wheel 原缺许可证；8 文件换行规范化匹配，上游根 Apache-2.0，证据清单保留|
|aiocqhttp/jieba|1.4.4 / 0.42.1，官方 PyPI sdist|MIT；固定哈希及构建工具，生成 wheel 只留本机|
|llama.cpp / Sherpa|b11946 / 1.12.40，见 assets 和 speech 清单|代码许可与权重分开，二进制 NOTICE 需按下载版本保留；不捆绑|
|NapCat|4.18.41，[固定 LICENSE](https://github.com/NapNeko/NapCatQQ/blob/v4.18.41/LICENSE)|Limited Redistribution License 含非商业/修改分发限制，不称 OSI 开源；仅外部用户自行获取并审阅|
|QQ / Node.js|NapCat 官方 Windows Node v4.18.41 发布包：QQ 9.9.31-49738、Node 22.11.0；版本及包哈希见 assets.json|QQ 专有条款、Node MIT 及附带声明；用户接受后外部下载，不捆绑到本仓库、不修改系统 QQ、不操作扫码或设备认证|
|Microsoft runtime|固定官方安装程序，版本与哈希见 assets.json|用户接受条款后仅在缺失时启动官方安装程序；必要的系统确认由用户完成|

AstrBot 同时含 [AGPL LICENSE](https://github.com/AstrBotDevs/AstrBot/blob/f61a7f42696da6df25dc7a249ec9b8d54ed9da18/LICENSE) 与 [EULA](https://github.com/AstrBotDevs/AstrBot/blob/f61a7f42696da6df25dc7a249ec9b8d54ed9da18/EULA.md)。其中用途及不可转让/不可再许可表述的适用关系仍需解释。本轮不删除原文、不作法律效力裁定，也不把一般解释歧义当作停止工程的理由。分发方式已改为自有代码+必要补丁+外部获取，未发现已证实必须通过削弱安全或更换内核才能解决的重大冲突；实际公开前仍须维护者复核适用义务。下载器不能规避条款。

BGE 原模型为 MIT，但固定 GGUF 转换条件仍有核实项；SenseVoice 转换来自 ASLP-lab/WSYue-ASR 的 sensevoice_small_yue，不能只引用旧 SenseVoiceSmall 或 Sherpa 代码许可推导权重授权。它们和私人表情/头像/素材都不分发。可选安装必须自行审阅外部条件并明确选择，不表示本项目保证其适用于任何用途。

本轮仅准备候选，不公开推送。许可元数据不是完整法律权利链证明；风险保留记录，不虚称全部第三方及模型许可已无条件通过。
