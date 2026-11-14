# 开发参考

平台适配层把可信原生事件转换为 `MessageEvent`，其中包含独立的 Provider、发送人、会话和消息标识。Registry 从 schema 2 配置生成 Provider，Runtime 校验来源、授权和去重，Gateway 调用共用的命令、记忆和提醒服务。QQ 和飞书共用业务层。适配器分别承担原生路由、引用读取和发信。[通信配置示例](../examples/config/ffa-communication.json)

AstrBot 持有原生平台连接。FFA 的启动入口在同一事件循环中加载宿主，以便飞书 SDK 的收包任务随宿主运行。更换依赖时校验 [固定来源](../manifests/sources.json) 和现有补丁。飞书字段与 SDK 行为在适配层处理，消息准入与身份策略在通用层处理。

身份目录通过统一身份关联多个平台账号。新人工绑定在另一平台的私聊中兑换邀请码。会话授权以平台实例和会话标识为边界。主动投递与即时回复均在发信前重新核对授权。引用只接受适配器验证的同会话消息，历史中的用户内容作为模型资料。

从项目根目录设置 `PYTHONPATH=src` 后运行 `python -m unittest discover -s tests`。飞书原生契约测试需要锁定的 AstrBot 环境。真实模型、账号、WebSocket 及投递使用独立验收实例验证。依赖构建和软件升级见[运维说明](OPERATIONS.md)。
