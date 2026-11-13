# 长期记忆

启用统一记忆后，授权私聊默认开启。绑定到同一身份的账号共用个人记忆，群记忆按会话隔离。群记忆默认关闭，由获授权的群管理员告知成员后使用 `/memory on` 开启。关闭后暂停当前范围的写入和召回。

- 用 `/help memory` 查看当前账号和会话允许的命令。`/memory remember 键 内容` 保存资料，`/memory search 关键词` 查询。
- 用 `/memory card` 查看活跃卡片、`/memory card-archive` 查档案。授权私聊可通过 `/memory history` 查互动记录。
- 群管理员可以用 `/memory card-sharing on` 允许成员将自己的群发言合并到个人卡片。关闭共享、撤销会话授权或解绑后，受影响的共享内容停止出现在后续请求中。
- 重置需要确认。恢复早期快照前，可用 `export-memory-erasures` 导出删除账本，并通过恢复选项 `--erasure-ledger` 重新应用此前的删除。

可通过实例的 `private/ffa-memory.json` 配置正文捕获、自动提取、反思、向量服务和上下文预算。向量匹配需要已配置的嵌入服务。修改配置前备份实例。[配置示例](../examples/config/ffa-memory.json) · [数据与隐私](DATA_AND_PRIVACY.md)
