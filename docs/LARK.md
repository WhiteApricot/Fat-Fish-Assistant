# 接入飞书

在已安装的肥鱼助手中使用 AstrBot 官方内置 Lark 适配器。飞书长连接需要访问飞书开放平台。使用长连接时无需公网入站端口或域名。

## 安装与授权

1. 在飞书开放平台创建企业自建应用，添加机器人能力。在 AstrBot Dashboard 的消息平台中手动添加飞书适配器，填写应用凭证，选择 `socket` 并启用。中国大陆飞书使用 `https://open.feishu.cn`。
2. 在开放平台的「事件与回调」中选择长连接，在「应用订阅」添加接收消息事件 `im.message.receive_v1`。为应用开通单聊接收 `im:message.p2p_msg:readonly`、群内提及 `im:message.group_at_msg:readonly` 和机器人发送 `im:message:send_as_bot` 权限。保存订阅后发布版本，并将可用范围限定为需要使用的成员。[飞书事件配置](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-)
3. 停止肥鱼助手，按 schema 2 在安装实例的 `private/ffa-communication.json` 的 `providers` 数组中追加一条记录。`provider` 为 `astrbot`，`platform` 为 `lark`，`instance` 为 Dashboard 中的平台实例 ID，`bot` 为机器人自身 `open_id`，`enabled` 为 `true`，`config` 为空对象。通过飞书机器人信息接口取得机器人的 `open_id`。App ID、用户的 `open_id` 和群 `chat_id` 各有自己的用途。[配置结构](../examples/config/ffa-communication.json)
4. 在 Dashboard 将聊天模型设为默认模型并启动服务。新身份目录可在飞书私聊用 `/owner` 按提示完成主人验证。已认证的同组织用户可直接开始私聊。已有 QQ 身份的用户可以通过邀请绑定共享个人记忆和偏好。[账号与权限](PERMISSIONS.md)
5. 在群里 @机器人并发送 `/whitelist add`，再在同一群 @机器人发送 `/confirm`。获准群内未被封禁的成员可以按群回复模式与机器人对话。普通群消息和引用邻近历史能否读取，取决于飞书开通的相应权限。[飞书消息权限](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1)

应用密钥只填在 AstrBot 的凭证配置中。沟通故障时可提供脱敏后的日志状态和错误码。

## 引用、合并转发与语音条

- 在当前会话引用一条消息后可针对其内容提问。作者、上游引用和邻近历史的读取取决于应用权限和当前会话的访问范围。
- 在私聊发送一条合并转发可直接请求摘要。也可在私聊引用该条消息后提问，或在已获准的群内引用该消息并 @机器人。可读取最多四层的转发记录，正文按顺序区分发言者。撤回、权限不足和超出范围的条目会标明缺失。转发副本中的图片、语音、视频和文件资源受飞书读取限制。[飞书获取消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/get) · [飞书消息 FAQ](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/guide/faq)
- 语音通过 DeepSeek ASR 转写。在私聊可直接发送飞书原生语音条。在已获准的群内先发语音条，再在 90 秒内由同一账号 @机器人。应用需要接收普通群消息的权限，才能收到 @ 之前的语音条。识别出错时会提示重试或改用文字。

## 回复与响应时间

- 普通文字收到后立即处理。同一会话内，同一发言者可在回复提交前补充文字，助手会用合并后的内容重新生成。管理写操作开始执行后，补充消息进入下一轮。飞书普通聊天从最近一条消息的接收时间起设定 4–6.5 秒的随机回复期限，模型处理同时进行。单轮最多开放合并 12 秒。显式命令直接执行。模型和平台接口耗时可能延后实际显示。
- 自然语言管理的系统回执提交后，普通人格回应按句末标点与换行分条发送。操作状态以系统回执为准。 [聊天指令](COMMAND_REFERENCE.md)
## 流式回复、提醒与撤回

- 在 AstrBot 启用聊天流式输出并授予飞书应用相应 CardKit 权限后，支持流式的回复可逐步填充卡片。卡片创建失败时可退回普通消息。已尝试提交的卡片若发生错误，可能留在会话中，服务记录发送结果以避免重复回复。
- 从私聊或获准群创建的提醒可在 FFA 重启后投递到原会话，无需先发新消息。若平台适配器尚未就绪，提醒等待恢复。若曾尝试投递但结果不明，使用 `/remind show 编号` 查看执行记录。
- 如需让长连接上的飞书撤回同步到 FFA 统一记忆，请在开放平台订阅 `im.message.recalled_v1`，授予读取消息所需的权限并发布新版。撤回按原消息的会话和作者匹配，适用于启用该版本后已由 FFA 接收并登记的消息。旧消息可以通过当前会话的记忆管理功能处理。[飞书消息事件与权限](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1)
- 如果回调未到达，统一记忆检索会检查当前飞书会话最近最多 100 条消息的明确撤回标记。该检查只处理 FFA 已登记的原消息，需要飞书消息历史读取权限。[飞书历史消息接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list)
