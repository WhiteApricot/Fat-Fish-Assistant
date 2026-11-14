# 接入飞书

在已安装的肥鱼助手中使用 AstrBot 官方内置 Lark 适配器。飞书长连接需要访问飞书开放平台。使用长连接时无需公网入站端口或域名。

1. 在飞书开放平台创建企业自建应用，添加机器人能力。在 AstrBot Dashboard 的消息平台中手动添加飞书适配器，填写应用凭证，选择 `socket` 并启用。中国大陆飞书使用 `https://open.feishu.cn`。
2. 在开放平台的「事件与回调」中选择长连接，在「应用订阅」添加接收消息事件 `im.message.receive_v1`。为应用开通单聊接收 `im:message.p2p_msg:readonly`、群内提及 `im:message.group_at_msg:readonly` 和机器人发送 `im:message:send_as_bot` 权限。保存订阅后发布版本，并将可用范围限定为需要使用的成员。[飞书事件配置](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-)
3. 停止肥鱼助手，按 schema 2 在安装实例的 `private/ffa-communication.json` 的 `providers` 数组中追加一条记录。`provider` 为 `astrbot`，`platform` 为 `lark`，`instance` 为 Dashboard 中的平台实例 ID，`bot` 为机器人自身 `open_id`，`enabled` 为 `true`，`config` 为空对象。通过飞书机器人信息接口取得机器人的 `open_id`。App ID、用户的 `open_id` 和群 `chat_id` 各有自己的用途。[配置结构](../examples/config/ffa-communication.json)
4. 在 Dashboard 将聊天模型设为默认模型并启动服务。新身份目录可在飞书私聊用 `/owner` 按提示完成主人验证。使用已有身份目录时，先从已绑定账号发起跨平台绑定，或由主人为不同用户授权。[账号与权限](PERMISSIONS.md)
5. 在群里 @机器人并发送 `/whitelist add`，再在同一群 @机器人发送 `/confirm`。获准群内未被封禁的成员可以按群回复模式与机器人对话。普通群消息和引用邻近历史能否读取，取决于飞书开通的相应权限。[飞书消息权限](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1)

应用密钥只填在 AstrBot 的凭证配置中。沟通故障时可提供脱敏后的日志状态和错误码。
