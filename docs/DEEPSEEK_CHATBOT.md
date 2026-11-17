# DeepSeek 联网、文件与语音

| 功能 | 使用条件 |
| --- | --- |
| 联网搜索与阅读链接 | 全局及当前会话的联网开关都开启，并有可用的 DeepSeek 凭据。用 `/websearch` 查看会话状态。可要求查公开资料，或提交一个 DeepSeek 能访问的公开 HTTP(S) 链接。 |
| 文件 | 聊天模型使用 DeepSeek 官方 `deepseek-flash`，且有权上传和读取本次文件。支持 PDF、Word、PowerPoint、Excel、EPUB、MOBI 和常见纯文本。每轮最多 3 份、合计 8 MiB，单份纯文本限 64 KiB。再次核对原文时可以重发附件。 |
| 语音 | QQ 或飞书发送语音条，肥鱼会通过 DeepSeek ASR 转写。单份最多 8 MiB、120 秒。群语音的接续方式见 [飞书接入](LARK.md)。 |

联网和转写优先采用当前会话的官方 DeepSeek 凭据，部署时也可配置 `DEEPSEEK_API_KEY`。模型和工具按服务商口径计费。网页可达性受 DeepSeek 服务范围限制，文件解析也受格式和内容质量影响。[DeepSeek 定价](https://api-docs.deepseek.com/quick_start/pricing/) · [文件支持](https://api-docs.deepseek.com/guides/document_parsing/) · [语音转写](https://api-docs.deepseek.com/guides/asr/)

[聊天指令](COMMAND_REFERENCE.md) · [故障排查](TROUBLESHOOTING.md) · [数据与隐私](DATA_AND_PRIVACY.md)
