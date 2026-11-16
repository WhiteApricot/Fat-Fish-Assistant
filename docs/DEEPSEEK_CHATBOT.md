# DeepSeek ChatBot 能力接入

## 调用链路

AstrBot 接收 QQ 或飞书的原生事件，FFA 的通信核心检查身份、会话权限和媒体准入。Main Agent 继续使用当前会话的 DeepSeek 4.1 Flash、人格、记忆和管理指令。普通聊天由 Main Agent 直接处理。Web 操作沿用 Main Agent 原有的意图和工具选择，受到 `/websearch`、会话授权、转发材料和敏感信息边界约束。

| 能力 | 通过准入后的执行链路 |
| --- | --- |
| 联网搜索 | Main Agent 选择 `web_search_deepseek` 的 `search` 模式。统一客户端调用 `POST /chat/completions`，仅声明 `deepseek_web_search`，关闭深度思考并启用内容安全检查。助手答复、引用和首选来源作为资料交还 Main Agent。 |
| 阅读链接 | 同一工具的 `read_url` 模式调用同一端点，仅声明 `deepseek_read_url`。链接须来自当前用户提交的公开地址。工具状态保留 DeepSeek 的原始 `server_tool_calls` 与签名，在同一会话、同一发言者再次选择阅读时回传。 |
| 文件输入 | 获取经平台下载边界验证的附件，使用 `POST /files` 上传。PDF、Word 和 PowerPoint 采用 `text_recognition`。纯文本、Excel 和电子书采用 `user_data`。AstrBot OpenAI Provider 在 Main Agent 当前请求的用户消息中加入 `{"type":"file","file_id":"…"}`。 |
| 语音输入 | QQ、飞书共用本地媒体定位和格式转换，调用 `POST /v1/tools/asr`。转写文本继续进入原有参与判断、记忆和 Main Agent。QQ SILK 使用 AstrBot 的解码能力。 |

DeepSeek 原生 Web 工具和 AstrBot 自定义函数工具需要分开调用。普通聊天只承担既有推理调用。联网轮次通常由主模型选工具、DeepSeek 原生 Web 调用和主模型生成答复组成。文件上传后的理解直接在主模型本轮完成。语音增加一次 ASR HTTP 请求。原生网页搜索、阅读、上传及 ASR 共用客户端、请求前后授权、超时和一次 HTTP 429 重试。模型中断、请求超时或部分生成失败由本轮反馈，避免重复提交可能已计费的调用。

## 配置和支持边界

- 搜索、链接和语音优先复用当前会话的官方 DeepSeek 聊天凭据，接着读取旧配置 `provider_settings.websearch_deepseek_key` 或 `DEEPSEEK_API_KEY`。文件需当前会话本身采用官方 DeepSeek Flash Provider，文件上传和对话需使用可访问同一文件的账号。
- FFA 每轮最多上传 3 份、合计 8 MiB，单份纯文本限 64 KiB。支持 TXT、Markdown、CSV、JSON、LOG、YAML、PDF、DOC/DOCX、PPT/PPTX、XLS/XLSX、EPUB 和 MOBI。正文来自官方解析和模型上下文。官方 Files API 的最高限额依文件类别为 32 或 100 MiB，解析文档最多 1,000 页，聊天最多引用 20 个文档且正文合计最多 8 MiB。解析后的文件正文不提供下载接口。FFA 在本轮结束时请求删除文件，并设置一小时过期。后续普通对话可以利用已有回复和记忆，再查附件正文需重新发送。 [官方 Files API](https://api-docs.deepseek.com/guides/files_api/) · [文件解析](https://api-docs.deepseek.com/guides/document_parsing/)
- 官方语音接口接收 16 位 PCM WAV 或 Ogg Opus，每份至多 8 MiB、120 秒。请求一次返回完整文本和音频时长，以音频时长计费。现有可选本地 Speech 供合并转发中的语音和原生历史接入使用。 [官方 ASR](https://api-docs.deepseek.com/guides/asr/)
- 原生 Web 独立调用使用完整 JSON。DeepSeek 也支持 SSE，流式的工具调用、引用及最终 usage 分别在各增量到达。其链接读取仅检查最后一条用户消息的前十个不同链接。页面正文由服务端签名恢复，历史搜索在下一轮仅提供答复文本。FFA 的链接状态在进程内保留十分钟并随已验证的撤回失效。Main Agent 继续使用 AstrBot 自有的流式回复与会话上下文。 [官方 Web Tools](https://api-docs.deepseek.com/guides/web_tools/) · [官方 Demo](https://github.com/deepseek-ai/chat-demo)
- 当前没有启用图片搜索、图片生成、视频理解或新的多模态能力。现有普通图片等输入仍走原通道。

## 计费

Web 费用由 DeepSeek 返回的输入、缓存命中和输出 Token，加上计费搜索查询和网页读取次数组成。每 1,000 次成功搜索或读取为 US$0.75。文档解析按成功处理页计费，每千页 US$0.225。纯文本、电子表格和电子书的上传不收取解析费。文件随聊天传入后，按模型实际输入 Token 计费。语音每小时为 US$0.058 至 0.116，取决于计费时段。 [官方定价](https://api-docs.deepseek.com/quick_start/pricing/)

旧的独立 Search API、网页正文截取、Draft/Review 和补查流程已经移除。搜索等待提示、工具输出清理、管理命令权限、同用户群语音缓存和平台专用媒体获取都沿用既有边界。

## 2026-11-16 本地验收

在 `Fat-Fish-Assistant.local/deepseek-chatbot-20261116` 使用真实 DeepSeek API 和合成样本。基线为远端 `main` 的 `9ff9ac5`。两组联网基准用相同的 DeepSeek Flash 非思考设置和三个公开问题，依次检查 Python 3.14 的 PEP 750、SQLite WAL 的跨主机限制和 IANA 示例域名。测试驱动保留 FFA 的工具选择提示、实际工具处理代码和模型调用。延迟从提交问题到最终正文首个流式片段及结束计时，排除 IM 通道、实际客户端展示和回复节奏。

| 三题平均 | 基线 | 本次 |
| --- | ---: | ---: |
| 输入 Token（含缓存） | 20,652 | 11,213 |
| 输出 Token | 1,943 | 757 |
| 总 Token | 22,595 | 11,971 |
| 模型请求 | 5 | 3 |
| 独立 Search API 请求 | 4 | 0 |
| 计费搜索查询 | 3.67 | 3.33 |
| 首条正文 | 14.46 秒 | 6.68 秒 |
| 总耗时 | 15.59 秒 | 8.16 秒 |
| API 费用，美元每题 | 0.01070 | 0.00666 |

费用根据真实返回的 Token、缓存命中和计费搜索量，使用测量时段适用的官方高峰费率核算。该表未与账户账单流水对账，三题的小样本用于对比这条调用链。三题核心结论均与官方资料一致，新链路三题都在正文输出官方链接。SQLite 回复另外推断了文件锁和错误码，所引官方段落能直接证明的原因是进程需要共享内存。 [Python 3.14](https://docs.python.org/3.14/whatsnew/3.14.html#pep-750-template-string-literals) · [SQLite WAL](https://sqlite.org/wal.html) · [IANA](https://www.iana.org/help/example-domains) · [RFC 2606](https://www.rfc-editor.org/rfc/rfc2606)

开着联网开关的普通问候和关着开关的显式搜索各消耗一次主模型请求，均未执行 Web 工具。主模型选择阅读测试网址后，最终回答引用了页面第二句所述的两种用途并附上来源。真实链接读取回传了 `read_link` 状态和签名，追问沿用签名还原了页面内容，新增计费抓取次数为零。无效链接触发了“没有取得可用网页内容”。

TXT、CSV、标准 PDF、DOCX 都提取了预置值。锁定的 AstrBot Provider 实际发送了包含 `file_id` 的当前用户消息，PDF、DOCX 和 CSV 均只需一次主模型请求理解正文。损坏 PDF 和无效音频触发了网关的失败提示。极简手写 PDF 虽获上传成功响应，正文提取却为空，标准 PDF 的一次验证也出现空正文，后续复测通过。

本地 QQ SILK 和飞书 Ogg Opus 事件样本经过网关、真实 DeepSeek ASR 与 AstrBot Provider，最终都回答了语音中的数字 42。平台侧使用隔离的原生事件夹具，实机机器人登录、消息上传及客户端展示仍待现场验收。另完成 135 项核心回归和 22 项锁定 AstrBot 飞书及媒体契约测试，覆盖权限、管理指令、记忆、人格、联网开关和媒体边界。

## 修改范围

- 客户端与执行链路位于 `deepseek_chatbot.py`、`web_search.py`、`web_chat.py`、`text_files.py`、`voice_input.py`、`deepseek_tool_output.py`、`platform_gateway.py`、`media_ingress.py` 和 `fat_fish_guard/main.py`。
- 平台相关变更集中在 `group_media.py` 与 `lark_communication.py`。平台独有的旧 `lark_voice.py` 和联网复核模块 `web_answer_review.py` 已删除。
- 回归覆盖更新于 `test_communication.py`、`test_native_lark.py` 和 `test_native_media.py`。部署、配置与数据说明同步见 [开发参考](DEVELOPMENT.md)、[运维](OPERATIONS.md)、[快速开始](QUICK_START.md)、[功能](FEATURES.md)、[数据与隐私](DATA_AND_PRIVACY.md) 和 [故障排查](TROUBLESHOOTING.md)。
