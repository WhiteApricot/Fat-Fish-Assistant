# 聊天指令

在当前会话发送 `/help` 查看可用操作。`/help memory` 展示当前范围的记忆命令。`/remind help` 展示提醒参数。

| 操作 | 用法 |
| --- | --- |
| Owner 初始化 | 私聊 `/ffa-owner`，同一账号发送 `/ffa-owner 验证码` |
| 邀请绑定 | `/ffa-invite 平台 实例 账号`，目标账号私聊发送 `/ffa-link 验证码` |
| 解绑 | `/ffa-unlink 平台 实例 账号` |
| 角色 | `/role`，管理者可在私聊申请 `/role grant 平台 实例 账号 member` |
| 群准入 | Owner 私聊使用 `/whitelist list`、`/whitelist add 群号` 或 `/whitelist remove 群号` |
| 群参与 | `/groupmode`，有权管理时可选择 `mention` 或 `smart` |
| 确认 | 当前会话发送 `/confirm 确认码` 或 `/cancel` |
| 卡片和档案 | `/memory card [关键词]`、`/memory card-archive [关键词]`、私聊 `/memory history [关键词]` |
| 编辑 | `/memory update 键 修订号 新内容` 或 `/memory rename 卡号 \| 标题` |
| 管理 | `/memory pin 卡号`、`/memory unpin 卡号`、`/memory merge 卡号 卡号`、`/memory erase-card 卡号`、`/memory reset` |
| 写入与查询 | `/memory remember 键 内容`、`/memory search 关键词`、`/memory delete 键 修订号` |
| 开关 | `/memory status`、`/memory on`、`/memory off` |
| 人格 | `/persona help`，私聊回复长度使用 `/personal 简短`、`/personal 标准` 或 `/personal 详细` |

## 会话与权限设置

| 操作 | 用法 |
| --- | --- |
| 人格条目 | `/persona list`、`/persona add 内容`。全局设置见 `/globalpersona help`。 |
| 新建对话 | 私聊 `/new` |
| 联网与自然语言操作 | `/webresearch on` 或 `/webresearch off`，`/naturalcommand on` 或 `/naturalcommand off` |
| 群参与冷却 | 群管理员可用 `/groupsettings cooldown 秒数` |
| 群限制与成员授权 | `/blacklist list`、`/blacklist add 账号`、`/blacklist remove 账号`。成员授权见 `/role list`、`/role add 账号`、`/role remove 账号`。 |
| 提醒与回执 | `/remind help`、`/history`、`/undo` |
| 群记忆共享 | 获授权的群管理员可用 `/memory card-sharing on` 或 `/memory card-sharing off` |
