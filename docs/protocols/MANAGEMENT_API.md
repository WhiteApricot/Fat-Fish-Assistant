# 管理接口 v1

维护入口为 `python -I ffa.py --root <安装根> <命令>`。stdout 返回一个 JSON 对象，stderr 输出进度。PowerShell 包装器支持 `-Json`。响应包含 `schema_version`、`project`、`request_id`、`ok`、`code`、`data`、`errors`。`ok` 表示请求成功处理。检测还要读取 `data.ready` 和逐项状态，带人工步骤的生命周期结果返回 `data.complete=false`。

| 命令 | 行为 |
| --- | --- |
| `check [--network]` | 检查平台、解释器、MSVC、磁盘、权限、端口与可选网络连通性 |
| `install --components … --owner …` | 部署 FFA 自有组件。`chat` 包含 Python、AstrBot、插件和 Core，可选 Embedding 与 Speech。SnowLuma 使用独立官方发行包 |
| `install --repair` | 停写后在相同依赖约束下重建，复用发布切换和恢复事务 |
| `status` / `components` | 列出组件状态、程序文件、运行证据、版本、路径、归属和依赖 |
| `component NAME status/version/dependencies/health/integrity` | 查看单项状态或执行受支持的健康、完整性探针 |
| `register-external snowluma --path DIR --version 2.4.2` | 登记独立目录，检查官方 Launcher 和发行元数据。完整 ZIP 的 SHA256 另按发行锁核对 |
| `start` / `stop` / `restart` | 按依赖顺序启停 FFA 受管服务，另返回 SnowLuma 的人工操作步骤 |
| `daily-start` | 启动 FFA，独立报告可选服务故障与当前连接证据 |
| `component NAME start/stop/restart` | 启停指定的受管服务。SnowLuma 返回官方操作指引，Core 随 AstrBot 运行 |
| `health` / `diagnose` | 提供分层健康证据。诊断附带环境检查 |
| `config initialize-chat` | 初始化 DeepSeek 预设和本机 AstrBot 反向 OneBot 监听。初始化及 FFA 启动时会拒绝同时启用其他 QQ 适配器 |
| `config initialize-onebot --bot-id ID` | 只在 FFA 实例停写时校验回环监听、令牌一致性和机器人身份，写入 FFA 私密身份文件。已有不同身份时拒绝覆盖 |
| `config initialize-experience` / `config initialize-local-models` | 使用现有配置器准备分条回复和起步表情，或配置本机模型。保留用户权限和已有凭据 |
| `config show/validate` / `config apply --file FILE` | 查看脱敏配置，或在停写时修改 dashboard_port 与 deepseek_api_key。使用 `--file -` 可从 stdin 传入 JSON |
| `wizard` | 列出本地管理页、QQ 接入和模型凭据的人工步骤 |
| `backup --destination DIR` / `restore --snapshot DIR` | 使用停写状态下的目录快照，恢复目标必须是受管理根中的新目录 |
| `migration-export --destination FILE` / `migration-import --package FILE --confirm-source-offline` | 使用 stdin 传递密码，处理加密的 FFA 数据包，保留源端暂停约定 |
| `resume-source --confirm-target-offline` | 确认目标已停止后归档源迁出暂停记录 |
| `upgrade --release DIR` / `rollback` / `recover-upgrade` | 通过事务切换或恢复代码及插件，保留数据库 |

SnowLuma 官方发行获取、启动、浏览器、剪贴板、连接等待和停止引导使用 snowluma 子命令。命令和状态语义见 [Windows 官方接入](../SNOWLUMA_WINDOWS.md)。

## 组件与健康证据

管理模型分别记录已登记、程序文件存在、运行和就绪。组件状态含 `not_installed`、`installed`、`not_initialized`、`stopped`、`running`、`waiting_for_user`、`unhealthy`。安装和修复阶段由已有进度与事务记录报告。SnowLuma 的 OS 进程在此批次显示未知，反向连接可证明协议对端就绪。未登记时也可从 AstrBot 查看实际连接证据。

健康插件通过已认证连接调用 `get_status` 和 `get_login_info`。读取端核验证据时间、实例部署 ID 和实际写入进程。结果独立报告 AstrBot 反向通道、OneBot 服务、QQ 在线、机器人身份及 Core 插件加载。模型调用与业务聊天标记为 `not_checked`，需单独验证。Windows 服务和官方发行布局由 Windows 适配模块处理。未来 Linux 适配沿用状态与所有权契约。

FFA 操作只写入具备归属证明的安装根。SnowLuma 的登录、启停、设置与数据处理全部使用官方支持渠道。许可约束见 [依赖说明](../DEPENDENCIES.md)。

| 退出码 | 含义 |
| --- | --- |
| 0 | 请求处理完成，读取状态及人工步骤 |
| 20 / 21 / 22 | 参数无效 / 需要输入 / 依赖缺失 |
| 23 / 24 / 25 / 26 | 完整性异常 / 操作忙 / 版本不兼容 / 非受管或只读 |
| 30 | 操作失败，查看本机私密日志 |
