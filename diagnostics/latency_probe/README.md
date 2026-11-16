# 飞书延迟观测

本目录是临时开发资料。把本目录复制到本地测试根目录，在该根目录下准备独立 FFA 实例与飞书测试应用。测试应用使用独立连接，避免与正在运行的机器人争用同一个 WebSocket 订阅。

## 启动

在本地测试根目录打开 PowerShell，将 $source、$instance 和 $testPython 指向开发工作区、本地测试实例及该实例的 Python。

    $localRoot = (Get-Location).Path
    $worker = Join-Path $source 'fat_fish_management/run_chat.py'
    $probe = Join-Path $localRoot 'latency_probe'
    New-Item -ItemType File (Join-Path $instance 'private/ffa-lark-latency') -Force
    & $testPython (Join-Path $probe 'launch.py') --worker $worker --instance $instance --local-root $localRoot

启动器沿用受管理实例的校验、生命周期锁、停止文件与清理流程。在测试应用内完成主人初始化和测试群准入，使用合成附件及公开 URL。见 [飞书验收清单](FEISHU_TEST_MATRIX.md)。

另开终端启动计时器。每个输入轮次使用唯一编号。

    $client = Join-Path $localRoot 'latency-client.jsonl'
    $server = Join-Path $instance 'private/latency-traces.jsonl'
    python (Join-Path $probe 'client.py') --client $client window
    & $testPython (Join-Path $probe 'client.py') list $server
    & $testPython (Join-Path $probe 'client.py') --client $client link CHAT-01 $traceId
    & $testPython (Join-Path $probe 'client.py') --client $client show $server CHAT-01

| 观测点 | 客户端操作 |
| --- | --- |
| T0 | 消息在客户端显示发送成功时按 Ctrl+Alt+F9 |
| T21 状态 | 首条「正在查」类状态提示实际显示时按 Ctrl+Alt+F10 |
| T21 正文 | 首条实质性回答实际显示时按 Ctrl+Alt+F11 |

窗口使用带 Tk 的桌面 Python。管理实例附带的 Python 可执行其他子命令。无 Tk 时可提前在终端输入 `client.py --client <日志> mark CHAT-01 outbound` 并在观察到事件时按 Enter，状态和正文用 `status`、`answer`。终端方式还包含启动解释器的时间。Windows 也可点窗口按钮，其他平台使用按钮。

一次只发一个测试轮次，使用 list 按时间、会话类型、消息类型选择 Trace ID，再执行 link。多轮用不同测试编号。两条相邻消息合并时保留各自入站 Trace ID，并为实际发出的回复标记被采用的轮次。出现歧义时，隔离测试流量后重新测量。

T0 和 T21 是操作员记录，包含人工反应和界面轮询时间。T1 来自飞书消息的 create_time。T2 来自锁定 SDK 的 WebSocket 接收处理入口，分片事件按交给事件派发的最后一帧计时。Webhook 的 T2 标为 unobserved。T20 表示飞书发送接口成功响应。流式卡片区分壳与正文更新。飞书私聊的已读事件记录已读行为，无法提供客户端首次显示时间。[飞书消息事件](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1)

## 分析

show 为 T0–T21 各输出一行 JSON，附带该阶段记录。未进入的服务端阶段标为 not_applicable，缺失的客户端或外部时间标为 unobserved。服务器 JSONL 记录随机 Trace ID、阶段、操作 ID、父操作 ID、UTC 毫秒时间、相对单调时间、状态及耗时。比较跨客户端、飞书和 FFA 的时间差前先检查时钟同步。单进程内用 monotonic_ms 与操作起止时间或父子关系判断重叠。

| 阶段 | 探针 |
| --- | --- |
| T1–T4 | 服务器创建时间、SDK 帧入口、AstrBot 转换及解析、Gateway 入口 |
| T5–T7 | Admission 身份/权限/去重、撤回登记和事务、批次入场和队列等待 |
| T8–T10 | 记忆准备/召回/写入、媒体下载及预处理、显式命令与参与路由、联网及管理可用性 |
| T11–T15 | Agent 开始/首个正文输出/结束，工具开始事件、执行结果及每次模型或原生 API 请求 |
| T16–T17 | 回复随机等待、授权复核与 Lark 路由 |
| T18–T20 | 飞书发送 SDK 操作、拒绝或异常与可识别的重试、成功回执 |

T14 区分主模型、流式请求、搜索、链接、文件上传/删除及 ASR。HTTP 尝试记录到响应或流头的时间，外层流操作记录到消耗结束。比较 T16 与 T11–T15 的时间区间可确认等待是否重叠。检查 SQLite 写竞争时比较 memory_writer_lock_wait、sqlite_begin_immediate 与 database_busy。SQLite 事务探针的时长包含调用开销。日志记录固定操作名、状态和数字，省略正文、异常消息、账号 ID、凭据、附件内容、URL、路径及 SQL。

写线程以有界队列异步追加。队列过载时，进程退出报告丢弃数，该轮样本需舍弃。进程中止或后台任务延续可能留下无结束行的操作，或在汇总之后追加记录。查看工具会读取同一 Trace ID 的全部行。

## 关闭和清理

用实例的停止文件结束受管理诊断进程，等待 RUNNING 消失，再移除 private/ffa-lark-latency。测试完成后清理本目录及本地副本，删除本地日志 private/latency-traces.jsonl 和 latency-client.jsonl。全部临时探针由 probe.py 在该诊断进程内挂载。
