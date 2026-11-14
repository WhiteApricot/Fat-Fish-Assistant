# 聊天指令

在当前会话发送 `/help` 查看可用操作。`/help memory` 展示当前范围的记忆命令。`/remind help` 展示提醒参数。

| 操作 | 用法 |
| --- | --- |
| 主人初始化 | 私聊 `/owner`，同一账号发送 `/owner 验证码` |
| 邀请绑定 | `/invite`，在另一个平台的机器人私聊发送 `/link 邀请码` |
| 解绑 | `/unlink 平台 实例 账号` |
| 角色 | `/role`，管理者可在私聊申请 `/role grant 平台 实例 账号 member` |
| 群准入 | 主人可在群内 @机器人并发送 `/whitelist add` 或 `/whitelist remove`。私聊也支持 `/whitelist list`、`/whitelist add 群号` 和 `/whitelist remove 群号`。 |
| 群参与 | `/groupmode`，有权管理时可选择 `mention` 或 `smart` |
| 确认 | 在原会话发送 `/confirm` 或 `/cancel`。新请求替换此前的待确认操作 |
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
| 联网与自然语言操作 | 主人或全局管理员用 `/webresearch global on\|off` 和 `/naturalcommand global on\|off` 管理全局开关，省略 `on` 或 `off` 查看状态。全局默认关闭。全局开启后，普通成员可在本人私聊、群管理员可在获准群内用对应的 `/webresearch on\|off` 或 `/naturalcommand on\|off` 管理当前会话。 |
| 群参与冷却 | 群管理员可用 `/groupsettings cooldown 秒数` |
| 群限制 | `/blacklist list`、`/blacklist add 账号`、`/blacklist remove 账号`。可用一条原生 @ 选取目标成员。 |
| 提醒与回执 | `/remind help`、`/history`、`/undo` |
| 群记忆共享 | 获授权的群管理员可用 `/memory card-sharing on` 或 `/memory card-sharing off` |
