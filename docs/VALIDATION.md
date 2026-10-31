# P2E-UX 验证（2026-10-31）

工程规则见 WORKFLOW.md。最终状态与 Git 记录见 P2E_UX_REPORT.md。下方旧 P2E 记录只作为历史基线，不代表本轮新增功能或当前人工验收通过。

## 本轮证据分类

|层级|执行内容|当前状态|
|---|---|---|
|回归卡顿诊断|周期栈显示 asyncio/socketpair 本机连接初始化阻塞；允许回环连接后原断言正常运行|原因已确认并保留 hang-diagnostic.log|
|基础原回归|19/20 组首轮通过，升级首轮因缓存路径不符失败；改用真实公共缓存后升级/回滚通过|首轮源码曾变化，不能作为最终冻结通过|
|独立安装|新 CPython、全新 Chat/Speech 环境、固定源码/插件/模型；重复安装不改变身份|实际执行通过；使用已校验缓存，不等于无缓存新 Windows VM|
|实际服务|Chat、Embedding、Speech 鉴权健康、复用、正常停止/重启、突然终止、失效 RUNNING、健康 DIRTY、并发启动|actual-services-20261031-a/results.json 通过|
|实际恢复故障|本地真实 Embedding、原生 FAISS 损坏重建/原件保留、检索/作用域/聊天、SQLite 损坏拒绝、Embedding 缺失/恢复、graph_memory、OS 锁、PID 复用|native-recovery-20261031-c/results.json 全部通过；a/b 失败证据保留|
|同机加密迁移|导出一个包、SHA、暂停源、新独立环境自动准备、迁入新路径、身份/权限/Key/插件资源/用户资产/SQL 记录、恢复后实际服务|actual-services-20261031-a/results.json 通过；不等于跨主机|
|协议健康|真实 Chat 对本机合成 OneBot 对端，仅 get_status/get_login_info；断线不再报连接|protocol-migration-20261031-a/results.json 通过，real_qq_used=false|
|负向迁入|错误密码、标签篡改、依赖版本不符、manifest SHA 不符、已有目标保护|同上通过；失败不创建/覆盖实例|
|桌面传输/CMD|JSON/stderr、Unicode隐藏口令stdin、配置子命令、自动填写及一次登录恢复请求；实际CMD启停，无界面点击|l 实际启动停止通过；m 新增登录恢复前端分支通过；g/h 的旧失败日志保留|
|官方独立 QQ 程序|实际PowerShell前端准备/重复准备、配置保留、真实二维码就绪、正确页面HTTP200且不丢参、复用/停止/完整性|napcat-node-20261031-f/results.json 通过；未代替扫码；QQ原生个人文件夹未保证完全隔离|
|最终冻结回归|原回归与UX、加密原生迁移及基础体验，25组|final-regression-20261031-i：25组退出0，源码冻结true；e 曾有一次Windows底层崩溃，后续多轮通过但未宣称根因已解决|
|真实人工体验|安装、QQ扫码/OneBot/消息，修复后的自动填写、分条及基础图片|用户已确认通过；实际当前健康另见actual-final-health-20261031.json|
|登录卡死恢复|连续确认reconnecting后受控重启本实例QQ，不碰其他服务或代替扫码|actual-login-recovery-20261031.json：真实waiting_qrcode且QR就绪；7项UX测试覆盖扫码中/已登录/外部服务不重启|
|当前API Key日志检查|共享只读读取当前安装及instance/logs，不输出密钥或聊天|41份均可读取，未命中当前真实Key；不扩展为所有第三方秘密都未记录|
|新主机/运行库|干净Windows下载、UAC与重启、真正跨主机导入|未执行，明确待验收|

证据均位于 Fat-Fish-Assistant.local/ux-validation，不进入 Git。最终迁移夹具把上游 Unicode FAISS 临时文件工厂定向到 local，保留真实读写器、索引算法和断言；这不等于实际中文 Windows 用户系统临时目录权限验收。

## 如何复验

维护者使用独立安装的 Chat Python，设置 FFA_CACHE 为实际固定下载缓存，调用 tests/run_all.py，传入仓库外 local 工作区中的 --release 与 --output，并加 --include-migration。执行器自动将 TEMP/TMP 和字节码缓存指向输出目录，逐组输出开始/结束、耗时和周期栈；超时/失败保留原日志，不能标为通过。

实际安装与服务用 tests/test_ux_installation.py；故障矩阵用 test_native_lifecycle.py；桌面传输/命令入口用 test_desktop_contract.py；只读协议与负向迁入用 test_ux_protocol_migration.py。它们只接受 local/ux-validation 的新目录和合成 owner=100（QQ 测试除外），不会接管正式实例。脚本参数和对应结果以测试代码与本轮报告为准，不要求普通用户运行它们。

语音健康验证模型加载、身份及鉴权，不代表识别准确率；Embedding 健康验证固定模型/512 维和鉴权，不代表所有语义召回质量。合成 OneBot 对端和同机跨路径恢复不能替代真实 QQ 或跨 Windows 主机验收。

## 历史基线：P2E 收口（2026-10-30）

**P2E 工程实现完成，等待必要的人工验收及后续阶段启动。**

## 已执行

- 由统一入口在新安装根获取固定 CPython、160 项 Chat 与 4 项 Speech 依赖、官方源码/补丁/固定 WebUI、NapCat、Embedding 引擎/模型和语音模型，逐项校验。新环境 pyvenv.cfg 指向自己的 bootstrap，未复制旧数据库或隐藏配置。
- 用户明确指定主人与 Bot；只显式复用其授权 API Key 和一个确认的旧批准测试群，不复制旧聊天、记忆、群管理员映射或登录文件。扫码由用户执行。
- Chat、NapCat、Embedding、Speech 的完整鉴权健康通过，Embedding 512 维及错误密钥拒绝通过。用户确认 @ 回复、普通非触发消息策略、测试账号 @ 且无重复生成三项正常。
- 完整停止、重复安装、启动与重复启动实测；559 个实例文件（含聊天/记忆/配置/身份）保持字节不变。完整重启后用户再次扫码，恢复健康；停用旧目录移走后 Chat/模型冷启动成功。
- 19 组最终非迁移回归通过，测试期间运行/测试源码冻结，最终代码逐项核对一致。精确结果在 release-validation.json。
- 166 条 Python/构建包记录对官方 PyPI 当前元数据逐项验证了精确文件名和 SHA，全部匹配；并非只能从旧本地 wheel 获取。详见 DEPENDENCY_AVAILABILITY.json；这不是全量无缓存重新下载或 VM 验收。

## 实测修正与保留失败证据

Windows PowerShell 5 的数组管道筛选导致模型未选中；已修复并通过真实模型启动。后台服务继承捕获管道曾使管理客户端挂起；改为独立日志文件后真实启停返回正常。NapCat 原生 guild1 数据库及三类可变运行目录原被误判为代码漂移；现仅明确运行数据可变，程序篡改仍拒绝。重复安装曾重写模型配置和历史；现只初始化缺失 provider，原配置原字节保留。内部证据读取器混合进度日志的解析错误及先前超时均保留内部失败记录，不充当通过结果。

QQ 平台风控曾导致未重启的会话掉线，用户确认原因并人工重新登录。最终健康恢复，但不能承诺平台登录永不失效或无人值守自动登录。原生 WebUI/控制台提供 NapCat 日志；本轮没有擅自改其原生日志配置。Chat、模型及维护日志均在私密安装根。

## 归档证据与限制

旧源码目录使用内容 SHA 验证。大型历史运行副本采用同盘 OS 目录重命名，前后核对目录文件标识、全部文件元数据清单及关键配置内容 SHA；清单本身有 SHA。**批量历史文件不是逐字节内容复核，不把元数据摘要冒称内容哈希。** 这不是多数据库原子备份或迁移恢复试验。归档只用于内部历史审查，当前运行不依赖 Backup。

## 未执行或未覆盖

全新 Windows VM、跨主机迁移、本轮完整备份恢复迁移、真实第二群、完整图文/顺序/打断/工具私聊合并矩阵未执行。原先普通私聊与两条文本合并沿用用户确认，不扩大为完整专项通过。模型健康不代表语义/语音准确率。Linux、Dagu、Work、P6 GUI 以及 P2F 两个业务插件均未开发。

外部许可证解释、转换模型授权及部分包的许可元数据风险见 THIRD_PARTY_NOTICES 和 license-evidence；不捆绑相应载荷，不声称无条件许可。公开发布另行复核，本轮仅授权私有仓库收口。
