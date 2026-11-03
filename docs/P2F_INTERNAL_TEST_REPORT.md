# P2F 内部测试报告

日期：2026-11-03。结果：本轮功能已实现并通过内部合成验收，等待用户QQ实机反馈；不是QQ实机通过声明。

## 验收结果

25组测试/脚本验收全部通过，包含151个unittest用例及8项独立脚本验收。先完成原有22组回归，再对冻结后的P2F源码运行13组合成验收；最后对“发送结果unknown不显示为成功”修正重跑相关两组测试。下表每个套件使用最后一次结果，不重复累加重跑次数。

|套件|用例数或验收方式|结果|local日志|
|---|---|---|---|
|Verify-P2C-Cancellation.py|脚本断言|通过|p2f/acceptance-approved/Verify-P2C-Cancellation.py.log|
|Verify-P2C-Gateway.py|11|通过|p2f/final-logs/Verify-P2C-Gateway.py.log|
|Verify-P2C-GroupQuotes.py|1|通过|p2f/acceptance-approved/Verify-P2C-GroupQuotes.py.log|
|Verify-P2C-NativeBoundary.py|脚本断言|通过|p2f/acceptance-approved/Verify-P2C-NativeBoundary.py.log|
|Verify-P2C-NativeGroups.py|脚本断言|通过|p2f/acceptance-approved/Verify-P2C-NativeGroups.py.log|
|Verify-P2C-Pipeline.py|脚本断言|通过|p2f/acceptance-approved/Verify-P2C-Pipeline.py.log|
|Verify-P2C-Scoped.py|19|通过|p2f/acceptance-approved/Verify-P2C-Scoped.py.log|
|Verify-P2C.py|6|通过|p2f/final-logs/Verify-P2C.py.log|
|test_chat_configuration.py|3|通过|p2f/final-logs/test_chat_configuration.py.log|
|test_decision_adapter.py|4|通过|p2f/final-logs/test_decision_adapter.py.log|
|test_dependencies.py|11|通过|p2f/final-logs/test_dependencies.py.log|
|test_experience.py|脚本断言|通过|p2f/final-logs/test_experience.py.log|
|test_feature_repairs.py|5|通过|p2f/acceptance-approved/test_feature_repairs.py.log|
|test_group_media.py|8|通过|p2f/acceptance-approved/test_group_media.py.log|
|test_lifecycle_recovery.py|10|通过|p2f/final-logs/test_lifecycle_recovery.py.log|
|test_management.py|23|通过|p2f/final-logs/test_management.py.log|
|test_migration_package.py|7|通过|p2f/final-logs/test_migration_package.py.log|
|test_onebot_configuration.py|3|通过|p2f/final-logs/test_onebot_configuration.py.log|
|test_p2f.py|20|通过|p2f/final-delta/test_p2f.py.log|
|test_p2f_deployment.py|脚本断言|通过|p2f/acceptance-approved/test_p2f_deployment.py.log|
|test_p2f_loading.py|脚本断言|通过|p2f/acceptance-approved/test_p2f_loading.py.log|
|test_p2f_native.py|8|通过|p2f/final-delta/test_p2f_native.py.log|
|test_p2f_permissions.py|6|通过|p2f/acceptance-approved/test_p2f_permissions.py.log|
|test_participation.py|6|通过|p2f/final-logs/test_participation.py.log|
|test_upgrade.py|脚本断言|通过|p2f/final-logs/test_upgrade.py.log|

## 功能覆盖

- 权限：主人、全局管理员、原群管理员、好友、非好友、全局/群黑名单；继承、目标保护、跨群拒绝、仅主人变更全局名单，确认绑定/过期/单次消费，原生未授权指令隔离。
- 确认：可逆名单和人格/群模式直接执行；授予群权限、全局身份变化和现有记忆清空仍确认。既有记忆、普通聊天、图片/语音和群共享上下文回归通过。
- 合并转发：合成私聊及群聊经授权入口读取、保留多人归属并产生合成回答；包含NapCat node包装且content为空但message有正文的实际形态、空节点、未知类型、混合媒体、嵌套/上限、重复转发和恶意指令。原生ProviderRequest在当前材料及历史材料场景禁用未授权工具。
- 消息收集：实际continuous_message类的8秒等待策略、群内按发送者分队列及结算后恢复群会话验证；不新建另一套收集队列，普通文本仍为原4秒策略。
- 提醒：一次性、日/周/月/间隔循环、模糊时段及截止范围、修改/删除、暂停/恢复、唯一任务自然语言选择、循环改钟点保留规则、私聊隔离、群目标授权和正文@不改变本人目标。
- 触发：原生APScheduler真实时钟自动调用、私聊Plain文字、群At目标与Plain文字；成员/好友与epoch复验、删除及撤权拒发、并发防重、重启预留恢复、unknown不重发且状态不伪报成功、查询失败最多3次及结果记录。
- 自然语言：同一处理器/同一结果、两条回复顺序、权限错误、缺少对象不移位猜测、异常返回真实失败。黑名单仍在识别及执行之前拒绝。
- 部署：锁定缓存离线构建；真实原生生命周期加载两个插件（无QQ适配器）、启动顺序和独立停用；实际新实例安装、冷SQLite备份恢复、全局身份和提醒任务持久化；原有升级失败回退、显式回滚和损坏包拒绝脚本通过。

## 测试边界与处理过的问题

所有测试数据、实例、数据库、日志、发布构建均在Fat-Fish-Assistant.local/p2f及其子目录。没有真实QQ登录、真实账号消息发送、QQ电话、Computer Use或云模型调用。模型语义部分采用合成provider断言验证输入及回复链路；实际模型理解质量、真实媒体可访问性和平台接口稳定性由用户实机验收。

初次原生加载夹具漏加合成实例导入路径，导致上游自动补装5个可选包。已按日志准确卸载这些新增包恢复环境；修正夹具后禁止动态安装、屏蔽元数据网络请求，重跑原生加载通过。回滚证据留在local/p2f/test-environment-rollback.log。此尝试确有网络下载，不将它描述为离线通过；最终验收不依赖这些包。

最初沙箱阻止测试配置原子替换，相关测试改在local授权的文件操作环境执行；未改外部核心。其他失败已保留旧日志并修复，包括空参数移位、NapCat空content遮蔽message，以及初始化阶段过早启动调度器。最终表格均为修复后结果。

## 明确限制

- QQ电话、自定义音频上传/解码暂缓；只验证专用audio入口明确拒绝并不打开路径，不声称音频文件检查或电话已实现。
- 屏幕只读接口及Work未实现；仅记录权限设计，不执行电脑能力测试。
- 发送前预留到平台回执之间发生崩溃时，OneBot无端到端幂等键，无法同时保证绝不遗漏及绝不重复。状态unknown不自动重发；可核对记录后明确修改时间。
- 提醒使用5秒共享调度tick，重启时循环错过轮次合并补发一次。权限epoch变化会使旧授权任务失效，需有权者明确重新绑定。
- 转发有节点、正文、媒体、深度与超时安全上限；未完整取得必须标注。撤权时间边界之后，平台没有提供可核实时间的节点会拒绝使用。无法识别的媒体会标注，不能声称已理解。
- 自然语言采用保守规则，未知或多目标表达不会猜测执行；可使用对应斜杠命令。原生未列入安全菜单的核心/插件高权限入口仍保留主人边界。

## 同步、回滚与后续

正式修改从local/p2f/source同步到主工程，逐文件SHA-256核对；清单见P2F_DELIVERY_MANIFEST.json，实际同步回执留在local/p2f/sync-receipt.json。经过验收的运行代码与local/p2f/release-approved的源码字节一致。只复制本轮源码、插件元数据、测试及文档，不复制运行数据库、构建包、测试身份配置、日志或凭据。

同步前旧源码保存在local/p2f/rollback/before-P2F.zip，新文件列表也随回滚记录保存。用户实际实例没有切换或启动。没有Git提交或推送。

至此停止P2F开发，等待用户填写P2F_QQ_REAL_DEVICE_TEST_CHECKLIST.md；电话不计普通提醒必测准出，不提前进入P3或其他后续阶段。

## v4 反馈修订与 v5 复测

- 指令统一由原处理器执行。P2F 指令结果加【系统】前缀。自然语言补充独立发送，记忆指令从管线结果改为两次独立发送。新增自然确认词只消费原账号、原会话中有效的待办。
- 群内赋权按 OneBot 的 At 组件读取当前消息的目标，排除唤醒 Bot 的 @。QQ 群主与管理员从同群已验证的 OneBot 事件继承权限，原有黑名单优先。代其他成员安排的提醒在发送前复核群成员名单及创建者的管理权限。
- 已校验本机 NapCat 4.18.41 的运行代码与锁定哈希相符。EraAsh 插件优先读取内联节点，缺少时调用同一 get_forward_msg。NapCat 的公开动作在仅有部分内层 ID 时可能返回「消息已过期或者为内层消息」。完整读取这些节点暂缓，现有可读取节点和缺失提示保留。[EraAsh 插件源码](https://raw.githubusercontent.com/EraAsh/astrbot_plugin_forward_reader/main/main.py)
- 发布测试使用 Local 的合成事件及复用的校验过的外部组件。未连接 QQ 实机或云模型。使用《P2F_QQ_REAL_DEVICE_TEST_CHECKLIST_V5.md》复测。

|v5 验证项|结果|
|---|---|
|作用域网关、自然语言、转发、确认|27 项通过|
|提醒和转发单元回归|20 项通过|
|原生消息、调度和撤权|10 项通过|
|角色、黑名单和待办绑定|6 项通过|
|原生权限边界和管线|2 个脚本通过，模拟发送边界内 QQ 实机消息为零|
|离线封装及发布完整性|通过，复用校验过的现有依赖|
|实例初始化、插件部署、冷备份及恢复|通过|
|AstrBot 原生插件加载与调度启停|通过，无 QQ 适配器|

发布包及日志保存在 Local。正式工程只同步代码、测试和文档，并按文件核对 SHA-256。实际安装实例仍需使用正式包的安装向导更新其发布目录。

## v5 反馈修订与 v6 复测

- 自然语言指令的真实结果和单行人格补充各通过一次发送调用。补充由程序生成，模型不再解释系统回执。到期提醒仍通过原有模型润色并保留原文回退。
- 「确定」按账号和会话绑定确认。「撤销刚刚的管理员申请」取消尚待确认的申请，或引用近期已生效的授权。全局撤销仍需确认。短期上下文只保留当前账号和会话的结构化状态，近期授予记录保留 15 分钟且随进程重启清除。
- Dashboard 更新密码复用隐藏输入，密码经子进程标准输入传递。服务器验证后使用 Windows DPAPI 保存。启用二次验证的账号仍需在浏览器完成验证。
- Local 通过 74 项合成测试，含 28 项网关、20 项提醒与转发、10 项原生消息和提醒、6 项权限、10 项凭据和环境测试。另验证权限边界、取消传播和管线隔离。OneBot 发送夹具收到两次独立的发送调用。真实回环 Dashboard 测试通过密码更改、错误密码保留旧密文、重启复用和日志无明文检查。
- 离线封装和发布完整性核验通过，复用现有依赖。所有验证使用 Local 和合成数据。未使用真实 QQ 或外部模型。使用《P2F_QQ_REAL_DEVICE_TEST_CHECKLIST_V6.md》复测。
- 最终发布包另通过原生插件加载、调度启停、冷备份和恢复。

## v6 反馈修订与 v7 复测

- 恢复鲸鱼娘补充对 AstrBot 当前提供商、所选人格和会话表达设置的调用。生成时使用程序给出的可信状态，不把完整权限回执交给模型重判。补充单行单独发送。模型超时或产出矛盾文案时保留真实系统回执。
- 后续 AstrBot 请求收到同账号同会话的真实指令回执，包含权限变更的账号、范围和动作。授权与撤销各自保留，按顺序提供。回执最多八条、30 分钟，随进程重启清除。记忆查询正文、人格配置和可识别凭据不进入此回执上下文。
- 一键入口共用运行后 NapCat 登录监控，覆盖核心未就绪和探测暂时失败。持续不健康时尝试恢复，重新检查后打开本机 WebUI 并自动带入 token。浏览器启动失败会重试，恢复就绪后重新布防。AstrBot 设置页打开失败时 QQ 监听仍继续。
- Local 通过 75 项合成测试及取消传播、原生管线和 NapCat 脚本测试。测试验证当前人格/提供商调用、矛盾补充抑制、两次独立 OneBot 发送、真实回执进入后续请求、账号及群隔离、连续掉线复现和 URL token 编码。未调用外部模型、真实 QQ 或浏览器操作。实机验证使用 v7 清单。
- 最终发布包通过离线完整性校验、原生插件加载与调度启停、实例部署和冷备份恢复。

### v7 补充分条修订

上一版把模型的多行补充折成单行。现保留模型原换行，从当前 AstrBot 会话读取聊天分段规则及间隔，系统回执保持独立发送，补充的每段单独调用 OneBot。默认按行分段时，模型未换行的中文句末标点也可形成分段。Local 网关夹具检查模型多行输出，原生 OneBot 夹具检查同一行中两句补充产生独立发送。模型继续使用会话人格，矛盾补充仍被抑制。

## v7 实测反馈及 v8 通用引用

- 私聊人格重置现在记录操作和实际回执，并进入同会话的后续 AstrBot 请求。自定义人格与记忆正文保留脱敏边界。历史查询不覆盖被查询的操作。
- AI 选择普通聊天、历史查询、相对撤销或已有操作的引用。常规意图分类成功返回空结果时不执行。原有明确确认协议及模型不可用时的保守回退继续使用。
- 历史引用从当前账号、当前会话的快照解析。提醒引用使用实际任务 ID。权限撤销复核原授权和会话，仍按已有规则确认。不能解析或缺少必要信息时不执行。
- Local 合成回归覆盖实测两段对话、不同说法、历史查询重复、转述和普通聊天、跨账号、提醒修改与删除、独立 OneBot 分段发送及原生权限边界。未调用真实 QQ 或外部模型。
- 最终回归通过 61 项合成测试、原生权限边界和消息管线检查。
- 离线封装和发布完整性校验通过，复用已校验的外部依赖。
