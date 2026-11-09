# 肥鱼助手正式指令说明

## 使用范围

在 QQ 私聊或已授权群使用。群聊自然语言管理沿用现有的 @ 触发。明确的斜杠命令直接进入执行器。程序检查真实身份、目标与权限，明确管理操作缺少必要对象时保留相应校验；自然语言分类不明确或不可用时回退普通聊天，不执行含糊操作。主人、全局管理员、本群管理员、好友的原有层级继续适用。

## 自然语言开关

- /naturalcommand 查看状态，/naturalcommand off 关闭，/naturalcommand on 开启。默认开启。
- 私聊只影响本人私聊，由本人设置。群聊只影响当前群，由本群有权管理员设置。
- 关闭后普通消息继续聊天，斜杠指令仍可用。重新开启须使用斜杠指令。
- 「再来一个／重新发一下」等无明确管理对象的续聊不会作为管理指令执行。
- 例如「关闭自然语言指令」在关闭状态下仍按聊天处理，可直接使用 /naturalcommand on 恢复。

## 联网搜索开关

- `/webresearch` 查看状态，`/webresearch off` 关闭，`/webresearch on` 开启。默认开启。
- 私聊由本人设置，群聊由本群有权管理员设置。每个会话的值分别保存。
- 开启时由回复模型根据本轮问题和资料选择搜索。关闭时不提供搜索工具，省去搜索选择和调用，仍可正常聊天。
- 开启自然语言管理时，可以说「查看联网搜索状态」「关闭这个会话的联网搜索」或「开启联网搜索」。单句「这个问题不要联网」不改变会话设置。
- 自然语言管理已关闭时，用斜杠指令调整联网设置。`/help webresearch` 可查看命令。

## 人格

- /persona 选择本人私聊或当前群。私聊仅本人，群聊仅有权管理员。/globalpersona 面向所有私聊和群聊，仅主人及全局管理员。
- 基础人格之上叠加全局要求，再叠加当前会话要求，冲突时局部优先。修改全局不会覆盖局部。各自的 reset 只清除本作用域的自定义要求。
- 列表编号稳定，删除条目不重排其他编号。整组 set 替换条目。单一作用域的要求合计最多 300 字。
- /persona list 查看编号；/persona set 用简洁的句子 整体替换；/persona add 每句末尾加一个喵 追加；/persona edit 2 避免重复 改编号 2；/persona remove 2 删除该项；/persona reset 清空；/persona undo 恢复本操作者近期上一项仍可恢复的修改。
- 全局使用相同子指令。例如 /globalpersona add 优先说明事实。自然语言「给所有会话追加一条人格要求，先说结论」对应全局 add。「把当前人格第二条改为少用感叹号」对应 persona edit 2。
- 旧版 /privatepersona 和 /grouppersona 仍可使用，旧文本可作为现有条目管理。

## 参数与二次确认

| 表达 | 标准命令或内部参数 | 检查 |
| --- | --- | --- |
| 明天九点叫我起床 | reminder add，时间「明天九点」，事项「起床」 | 使用 Asia/Taipei；04:30 前的「明天」按当天理解，目标时刻已过则不安排在过去 |
| 别忘了明天下午拿快递 | reminder add，时间「明天下午」，事项「拿快递」 | 未给可识别时间或事项时澄清；已支持的时段使用原默认值 |
| 把本群冷却设为两分钟 | /groupsettings cooldown 120 | 当前群管理员，30 至 1800 秒 |
| 把 QQ 12345 设成群 67890 的管理员 | /role add 12345 67890 | 主人或全局管理员；群须获批准，授权须确认 |
| 撤销 12345 在群 67890 的管理权限 | /role remove 12345 67890 | 复核现有权限及目标；若撤销的是一项已执行的权限变更，恢复须再确认 |
| 查询第十二号提醒 | /reminder show 12 | 必须能在当前作用域访问 |
| 取消刚才的申请 | /cancel | 只取消同一账号、同一会话的当前待办 |
| 清空所有记忆 | /memory reset 或 /groupmemory reset | 仅当前作用域，须确认；清空不可撤销 |

查询和示例不产生授权。管理员、记忆重置等待办由原确认机制绑定账号和会话，过期后须重新申请。QQ、群号和编号必须可核实；真实唯一 @ 可绑定目标。普通群管理员不能授予群管理权限或修改全局人格。提醒管理限当前作用域，本人可管理自己的任务，群内有权管理员可管理相应群提醒及代指定成员创建。

## 完整一级和二级命令速查

/help 按当前身份和会话列出一级指令的用法和中文说明。用 /help role 或 /role help 查看当前场景可用的具体格式。每条占一行，需要二次确认的操作会在说明中注明。斜杠命令和子命令使用英文，中文自然语言及参数内容照常支持。

### /help

- /help [command]

### /role

- /role
- /role list [group]
- /role add QQ [group]
- /role remove QQ [group|all]
- /role set QQ 1|2 [group]
- /role global list
- /role global add QQ
- /role global remove QQ
- /role undo

### /whitelist

- /whitelist list
- /whitelist add group
- /whitelist remove group

### /blacklist

- /blacklist list
- /blacklist add QQ
- /blacklist remove QQ

### /groupmode

- /groupmode
- /groupmode mention
- /groupmode smart

### /groupsettings

- /groupsettings
- /groupsettings cooldown seconds

### /personal

- /personal
- /personal short
- /personal normal
- /personal detailed

### /memory

- /memory remember content
- /memory search query
- /memory update id content
- /memory delete id
- /memory reset
- /memory confirm
- /memory cancel

### /groupmemory

- /groupmemory remember content
- /groupmemory search query
- /groupmemory update id content
- /groupmemory delete id
- /groupmemory reset
- /groupmemory cancel

### /reminder

- /reminder add 时间提醒我事项
- /reminder addto QQ 时间提醒事项
- /reminder list
- /reminder show id
- /reminder time id 时间
- /reminder text id 事项
- /reminder pause id
- /reminder resume id
- /reminder delete id
- /reminder select 明天或每周X [新时间]
- /reminder audio

### /privatepersona

- /privatepersona
- /privatepersona set content
- /privatepersona reset
- /privatepersona undo

### /grouppersona

- /grouppersona
- /grouppersona set content
- /grouppersona reset

### /persona

- /persona list
- /persona set content
- /persona add content
- /persona edit id content
- /persona remove id
- /persona reset
- /persona undo

### /globalpersona

- /globalpersona list
- /globalpersona set content
- /globalpersona add content
- /globalpersona edit id content
- /globalpersona remove id
- /globalpersona reset
- /globalpersona undo

### /naturalcommand

- /naturalcommand
- /naturalcommand on
- /naturalcommand off

### /webresearch

- /webresearch
- /webresearch on
- /webresearch off

### /history

- /history
- /history all

### /confirm

- /confirm [code]

### /cancel

- /cancel

### /undo

- /undo [command]

### /new

- /new

### /globaladmin

- /globaladmin list
- /globaladmin add QQ
- /globaladmin remove QQ

## 原有兼容和限制

- 回复模型根据当前问题、前文和本地资料选择 DeepSeek 官方搜索。时效性或需要核实的问题可直接问，也可明确说要联网。查不到可靠结果会说明。查询词和网页资料不提供管理授权。
- 清空记忆、已进入发送流程的提醒不能撤回。近期撤销记录随进程结束清除。恢复操作会拒绝覆盖已变化的状态。
- QQ 电话、自定义提醒音频、电脑控制未在这些 QQ 管理命令中开放。聊天、合并转发、图片和语音继续沿用现有入口。
## 逐类示例

| 指令 | 示例和执行结果 |
| --- | --- |
| 帮助、历史与待办 | /help role 查看授权语法；/history 查最近一条，/history all 查近期回执；/confirm 在原会话确认，/cancel 取消当前待办；/undo whitelist 只撤销匹配的近期名单变更。 |
| 身份及群权限 | /role 查本人；/role list 67890 查指定群显式授权；/role add 12345 67890 申请授予；/role set 12345 2 67890 同义；/role set 12345 1 67890 或 /role remove 12345 67890 撤销显式群权限；/role remove 12345 all 撤销全部群的显式授权；/role undo 引用近期授权。QQ群原生身份仍由 QQ 决定。 |
| 全局管理员 | /globaladmin list 查名单；/globaladmin add 12345 及 /globaladmin remove 12345 分别申请授予和撤销；/role global list、/role global add 12345、/role global remove 12345 是兼容形式。 |
| 群白名单与黑名单 | /whitelist list 查开放群；/whitelist add 67890 开放群，/whitelist remove 67890 关闭群；/blacklist list 查当前作用域名单，/blacklist add 12345 加入，/blacklist remove 12345 移出。受保护的目标不得修改。 |
| 群与个人偏好 | /groupmode 查看，/groupmode mention 仅 @，/groupmode smart 智能回复；/groupsettings 查看冷却，/groupsettings cooldown 120 修改；/personal 查看，/personal short、/personal normal、/personal detailed 分别设置简短、标准、详细。 |
| 记忆 | /memory help、/memory remember 项目代号是测试、/memory search 项目代号、/memory update 17 新内容、/memory delete 17、/memory reset、/memory confirm、/memory cancel。将 memory 换成 groupmemory 可操作当前群，共享重置用 /confirm。归档不等于清空。 |
| 提醒 | /reminder help、/reminder add 明天九点提醒我起床、/reminder addto 12345 明天九点提醒开会、/reminder list、/reminder show 12、/reminder time 12 下午三点、/reminder text 12 领取快递、/reminder pause 12、/reminder resume 12、/reminder delete 12。/reminder select 明天 下午三点 只在该日有唯一匹配时修改，省略新时间表示删除。/reminder audio 返回暂缓说明。 |
| 旧人格与新会话 | /privatepersona set 说短句、/privatepersona reset、/privatepersona undo；/grouppersona set 先说结论、/grouppersona reset；不加子指令查看旧版表达；/new 开启本人新的私聊对话。 |
