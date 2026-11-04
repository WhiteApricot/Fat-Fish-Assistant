# OneBot 消息事件

NapCat 通过鉴权的回环反向 WebSocket 连接 AstrBot 的 aiocqhttp 适配器。平台配置由 [OneBot 配置器](../../fat_fish_management/configure_onebot.py) 生成。网关读取 AstrBot 事件的发送者、群号、会话标识和结构化消息组件。文本用于指令或聊天，At 用于识别真实提及，Reply 和 Forward 分别用于引用与合并转发的恢复。

管理权限从事件身份和受信任的群成员资料确定。转发内容、模型输出和网页属于输入资料。健康插件使用 get_status/get_login_info 提供只读连接证据。消息入口和最终发送检查见 [网关](../../src/fat_fish/gateway/gateway.py)。
