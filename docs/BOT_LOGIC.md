# 消息处理

AstrBot 是唯一 QQ 聊天主体。SnowLuma 提供 OneBot 通信，身份、权限、人格、记忆和工具由 Fat-Fish Core 管理。DSH（DeepSeek Harness）和 Work 通过统一接口接入 Core。

1. SnowLuma 将 QQ 事件经本机 OneBot 反向连接交给 AstrBot。网关从事件读取真实发送者、会话和群号，执行去重、白名单及黑名单检查。
2. 明确的斜杠命令由指令入口解析。开启自然语言指令时，可对符合触发条件的文本调用意图识别，再按同样的权限与确认规则执行。自然语言分类不明确或不可用时保留原消息进入聊天，不执行含糊操作；实际管理权限和确认规则不变。
3. 私聊使用本人上下文。群根据 @、群模式、引用和冷却决定是否进入模型回复。涉及转发时先恢复结构化内容并标记为外部材料。
4. Guard 合并人格要求并按权限处理记忆与搜索。搜索工具先从候选资料整理答案，再按原问题核对证据，必要时补查。等待较久时，当前 AstrBot 会话可发一条进度提示。核实的资料返回当前会话，最终回复经发送前检查，由 AstrBot 分条交给 OneBot。授权撤销会影响尚未完成的会话。
5. 提醒插件通过 AstrBot 的定时调度独立检查到期任务。

参见 [权限](PERMISSIONS.md) 与 [事件约定](protocols/MESSAGE_EVENT_CONTRACT.md)。

维护入口分别是 [消息网关](../src/fat_fish/gateway)、[Guard 插件](../plugins/fat_fish_guard) 和 [记忆指令](../src/fat_fish/memory/commands.py)。

S1的回复窗口、媒体收集、表情续发及受限附件行为见[S1说明](S1_WINDOWS_SNOWLUMA.md)。仅协议连接ready不代表业务入口可加载；独立实例使用正常受管入口并验证完整发行副本。
