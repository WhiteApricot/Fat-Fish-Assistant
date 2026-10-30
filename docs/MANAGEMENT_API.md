# 管理接口 v1

收口新增配置入口：`config initialize-chat [--onebot-port 6199] [--model deepseek-chat]` 初始化原生 Chat 与带鉴权的回环 OneBot；`config initialize-onebot --bot-id <自己的Bot账号>` 只在已停止、由本安装根管理的 NapCat 中生成原生反向连接配置。它们不登录、不发送、不变更主人或群权限；不兼容的既有配置拒绝覆盖。`config apply` 另支持 `napcat_webui_port`，强制回环、随机私有令牌并拒绝修改外部只读或运行中的 NapCat。

PowerShell 5 的生命周期输出写入每次独立私密日志，避免后台服务继承捕获管道导致命令挂起。NapCat 可变配置/日志和不可变程序载荷分别校验：配置可由用户保存，代码篡改或未知代码载荷仍拒绝复用。

入口：`python -I ffa.py --root <安装根> <命令>`。Python 接口 stdout 始终为单个 JSON 对象，stderr 为进度；PowerShell 入口默认显示人类可读结果，`-Json` 返回同一 JSON。无 GUI、无对外 HTTP 端口。P6 调用进程并读取 JSON/退出码即可。

响应字段固定为 schema_version=1、project、request_id、ok、code、data、errors。errors 为 type/message/hint 数组。`ok` 表示该操作完成；检测结果须同时读 data.ready 和各项 severity。端口占用是警告，不会按端口杀进程；未知权限/网络错误不能误报端口可用。

|命令|行为|
|---|---|
|check [--network]|Windows、解释器、MSVC、磁盘、权限、端口、可选 DNS/TLS 连通；不下载组件|
|install --components ... --owner ...|固定依赖、新环境、补丁、独立实例；重复执行不覆盖数据|
|components / status|逐组件安装状态、版本、路径、依赖、ownership、运行状态|
|component NAME status/version/dependencies|独立查询；Dagu/Work 如实显示未实现|
|component NAME start/stop/restart|只操作本安装根的受管理服务；Python 和插件不伪装为独立进程|
|component NAME health/integrity|服务鉴权/身份或代码完整性；不把状态查询等同模型质量测试|
|start / stop / restart|依赖顺序启动，反向停止；原生 Chat 生命周期及受验证 PID 归属|
|health / diagnose|实际管理认证、已安装模型/QQ 健康；diagnose 附环境检测|
|config show/validate|脱敏配置、身份/回环检查；不输出凭据|
|config apply --file FILE 或 -|停写后修改允许字段，保留私密历史；禁止通用写 owner/群授权|
|wizard|QQ、模型与本地管理页的人工操作步骤|
|backup --destination DIR|原生停写前提的快照封装；不自动停当前陌生实例|
|restore --snapshot DIR [--destination DIR]|本安装根新目录恢复，拒绝覆盖新聊天|
|upgrade --release DIR / rollback|已验证同 schema 代码切换；数据库不倒灌|
|recover-upgrade|恢复中断事务的代码/插件，不覆盖聊天|
|register-external NAME --path DIR --version TEXT|仅登记只读路径；不会接管、启停或改装该组件|

config apply 允许 dashboard_port、deepseek_api_key、qq_executable。密钥通过文件或 stdin 提供，不出现在命令行或结果中；文件自身也应保存在私密目录。新的 provider 及 QQ 平台配置仍走 AstrBot 原生本地管理页。群/主人权限继续由既有受控命令和实例固定身份机制管理。

|退出码|含义|建议|
|---|---|---|
|0|操作/查询完成|检测还要看 ready，组件看 installed/running|
|20|参数或配置无效|检查允许字段与格式|
|21|需要用户输入/人工配置|QQ 登录、运行库或初始化条件|
|22|依赖/缓存缺失|联网补齐固定源，或准备离线缓存|
|23|哈希/身份/补丁不符|停止，隔离异常材料，不跳过校验|
|24|写入/安装/维护忙|等待或原生停止，不移除锁绕过|
|25|版本/能力不兼容|选择兼容发布；reserved 组件没有实现|
|26|非受管理/只读边界|不接管外部安装，不覆盖未知目录|
|30|运行失败|查看返回的私密日志及失败阶段|

组件更新不原地替换既有外部依赖：AstrBot/插件通过新 release + upgrade/rollback；Python 通过新环境版本；模型与 NapCat 安装到新版本路径并另行审阅许可。本轮不提供任意组件的自动追新或卸载旧版本。插件子集在 install --plugins 指定，始终保留安全适配；没有动态加载未知功能插件的入口。

安装根含 .ffa-managed.json 才能写入。外部登记为 external-readonly；其声明版本不是已验证安装版本，健康条件不足时返回 needs_input。未知管理 schema、安全标记和软件版本均不静默接受。JSON 路径在受管理根内尽量相对；明确登记的外部路径只读。私密数据和日志不提交 Git。
