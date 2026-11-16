# 架构清理与飞书延迟观测验收

## A. Git 交付

本轮工作分支为 `work/cleanup-and-latency-probe`，从远端 `develop` 的 `32b0176f600172f0e5d28f41c7f79f7fd961cd0c` 创建。交付提交及远端分支状态见 [工作分支](https://github.com/WhiteApricot/Fat-Fish-Assistant/tree/work/cleanup-and-latency-probe)。

## B. P0–P3 整改

| 优先级 | 原问题 | 改动及验收 |
| --- | --- | --- |
| P0 | 旧 AstrBot Hook 白名单与当前工具不一致，Runner 会捕获开始 Hook 的异常后继续进入执行器。 | 从 `patches/astrbot/changes.json` 移除过时 Hook 改动，更新 `manifests/sources.json` 的哈希与计数。 `commands/agent_tool.py` 绑定本轮实际注册的工具实例及其授权回调。Hook 分派原生 OnUsingLLMToolEvent，执行器消费同一次调用的许可并重新检查。事件停止、处理器异常、未注册、会话撤权或联网撤权均不会进入工具实现。锁定 Runner 契约覆盖新工具事件、原生结果回传及拒绝路径。 |
| P1 Speech | 安装与维护仍暴露已退役的本地语音识别。 | 删除 `integrations/speech_server.py` 和 `manifests/speech.json`，清除组件、安装/启动/诊断、模型文件清单、许可依赖和 STT Provider 上下文。 `configure_local_models.py` 与 `Invoke-LocalModels.ps1` 继续管理 Embedding。普通语音与 QQ 转发的获准语音使用 `integrations/voice_input.py`。 |
| P1 模型 | 安装默认值和文件输入使用不同模型列表。 | `src/fat_fish/model_config.py` 用官方 `deepseek-flash` 统一安装、原生 Web 和 Flash 文件输入；安装预设沿用非思考模式。真实 PDF、DOCX、CSV 通过 Files 上传后作为当前请求的 file_id 进入主模型。  [官方模型和定价](https://api-docs.deepseek.com/quick_start/pricing/) |
| P2 通用协议 | DSML、输出过滤和原生工具协议依赖联网模块。 | 输出清理、原生调用过滤、错误恢复集中在 `integrations/deepseek_tool_output.py`。 `integrations/web_search.py` 保留搜索、链接、权限和签名状态管理。 |
| P2 退役工具 | 历史兼容分支、示例人格和种子仍使用旧工具标识。 | 删除历史名集合和按旧名修剪消息的逻辑，清理 Prompt 和默认注册。继续省略空 tool_calls，保留有效工具调用与结果配对。以当前请求实际提供的工具对未知调用做通用拒绝。 |
| P3 公告和资源 | 退役的检索核验还留有第三方许可与过时 API 文档。 | 对照现存源码及提示词后，删除原 `src/fat_fish/integrations/node_deepresearch.LICENSE` 并更新 `THIRD_PARTY_NOTICES.md`。修订指令、功能、依赖、部署和接入文档，清除退役资源引用。 |

## C. 范围和保留组件

- 语音清理覆盖 `fat_fish_management/` 下的控制器、下载缓存、实例、迁移、健康检查和端口管理，以及 `scripts/desktop/`、`scripts/services/` 和 `scripts/diagnostics/`。本地音频格式转换和 QQ SILK 解码继续供 DeepSeek ASR 使用。
- 删除旧 Speech 模型及其文件校验、独立环境与安装资源记录。Embedding 的 GGUF、运行引擎和许可信息随实际功能保留。AstrBot、SurfCore、SelfReply 决策子集及仍用到的上游依赖归属见 [第三方声明](../THIRD_PARTY_NOTICES.md)。
- 网页业务与通用输出协议各自有一个入口。搜索状态提示、自然语言管理、工具回执、身份与权限复核、人格及记忆保留原有行为。原先独立搜索和双重核验在基线已移除，本轮清掉该实现残存的调用兼容、提示与归属记录。
- 清单变更包括 `manifests/` 下的 `assets.json`、`components.json`、`index.json`、`license-evidence.json`、`model-runtime-lock.json` 和 `sources.json`。用 `git diff --name-status 32b0176f600172f0e5d28f41c7f79f7fd961cd0c HEAD` 查看完整清单。[提交历史](https://github.com/WhiteApricot/Fat-Fish-Assistant/commits/work/cleanup-and-latency-probe/)

## D. 调用关系

飞书或 QQ 事件由 AstrBot 接收并转换。平台适配器交付标准化消息与附件，Gateway 执行准入、去重、撤回登记、媒体准备和回复授权。显式指令交给 Command Registry。对话进入原 Main Agent，继续使用当前人格和记忆。Main Agent 可选择本轮实际提供的管理、搜索、链接工具。管理请求进入同一 Command Registry。工具在原生 Hook 和执行器边界核对本轮授权。

搜索、链接阅读、文件上传及删除、ASR 共用 DeepSeek ChatBot 客户端。搜索和阅读结果回到主 Agent。上传文件的 ID 进入主模型的当前请求。转写文本进入既有语义处理。Gateway 完成投递准备，通过飞书或 OneBot 调用平台发送逻辑。详见 [DeepSeek ChatBot 调用链](DEEPSEEK_CHATBOT.md)。

## E. 临时诊断

[诊断使用说明](../diagnostics/latency_probe/README.md) 包含启动、标记、关联、查询及删除步骤。临时清单为 `diagnostics/latency_probe/` 下的 `probe.py`、`launch.py`、`client.py`、`README.md` 和 `FEISHU_TEST_MATRIX.md`。测试实例使用独立的开关标记及本地 JSONL 日志，发布构建排除该目录。T0 和 T21 由实际操作者标记。SDK 成功响应和客户端显示有各自的观测点。

## F. 实机清单

[飞书实机清单](../diagnostics/latency_probe/FEISHU_TEST_MATRIX.md) 为九类链路各准备两至三组可执行消息，包含账号、会话、开关、附件、内部路径及可填结果。飞书应用需使用独立测试凭据。客户端实测结果由执行人员记录，合成验收日志只用于校验探针及链路。

## G. 测试

- Windows 使用锁定的 AstrBot `f61a7f42696da6df25dc7a249ec9b8d54ed9da18` 和 Core 依赖，全量 160 项通过，包含 25 项固定宿主的飞书、工具和媒体契约。仓库的 Core offline tests 只安装核心测试依赖，缺少宿主时固定版用例明确跳过。[分支 CI](https://github.com/WhiteApricot/Fat-Fish-Assistant/actions/workflows/core-tests.yml?query=branch%3Awork%2Fcleanup-and-latency-probe)
- Ubuntu 24.04（WSL2）覆盖同一套 160 项，154 项通过，6 项为仅适用 Windows 的 SnowLuma 或安装测试。既有记忆并发用例隔离复跑通过。两项首次因 PATH 缺少 FFmpeg 失败的媒体契约，在加上 Linux FFmpeg 7.0.2 后补验通过。25 项锁定 AstrBot 契约均已执行。
- 真实 DeepSeek 联网复验中，打开联网的普通问候和关闭联网的明确搜索均零 Web 调用。三个公开问题完成搜索，最终回答分别输出 2、2、3 条链接。脚本的首正文耗时为 4.843–5.328 秒，总耗时 5.515–7.016 秒，计时范围为测试驱动和真实 API。
- 原生 URL 阅读取得 IANA 页面引用。追问沿用返回的签名状态，服务端未报告新增抓取。两次请求分别为 6.110 秒和 0.936 秒。
- PDF、DOCX、CSV 分别通过上传、Flash 请求和删除，回答了各自的合成编号。损坏 PDF 和无效音频收到明确失败反馈。以 QQ 和飞书原生音频夹具请求真实 ASR，两次返回 HTTP 200，Main Agent 均答出 42。夹具未经过飞书或 QQ 的真实账号收发。
- 锁定飞书适配器使用合成消息和受控发送响应通过观测烟测。首个聊天路由被拒绝后，备用路由成功并各自记录毫秒耗时。客户端标记和测试编号合并后共有 22 个阶段。离线发布打包、1499 条 payload 校验、清单和补丁哈希核对通过。封装产物未包含临时目录。
- 独立诊断 Worker 的面板返回 HTTP 200，受管理停止验证通过。探针记录了真实 DeepSeek 普通请求、流式请求和原生链接阅读的三次 HTTP 200 响应。

## H. 下一轮观测

按链路和功能开关组合统计 T0–T21 的 p50 与 p95。分别核算服务器到 Gateway 的入站时间、主模型首输出与完整生成、工具等待、随机等待、首次投递回执和用户真实可见时间。对网络失败与重试单独分组，比较 SQLite 事务开始时间与 Memory writer 等待，检查模型生成和随机等待的重叠。端到端采用同步时钟，人工标记的误差单独注明。
