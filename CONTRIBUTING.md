# 贡献指南

目录、Git、开发及测试边界以 [WORKFLOW.md](docs/WORKFLOW.md) 为准。

所有工作的基本原则：测试在 `E:\AI_Chat\Fat-Fish-Assistant.local` 进行，所有代码、配置、适配层和测试修改必须同步到正式路径 `E:\AI_Chat\Fat-Fish-Assistant`，从正式路径交付；不得只修复 Local 实例。

新增插件或修改功能涉及安装、启动、转移时，必须同步更新 `Install-Fat-Fish-Assistant.cmd`、`Start-Fat-Fish-Assistant.cmd`、`Migrate-Fat-Fish-Assistant.cmd` 及其共用调用链，并按工作流完成相应验证。

只在隔离版本与合成实例中改造。新增依赖须固定版本和下载哈希，保留来源与许可证。核心/插件覆盖必须记录前后哈希、修改原因及撤销路径，权限、安全 epoch、撤权、黑名单、工具与消息出口回归不可删除或降级。

运行 tests/run_all.py 并保存外部测试结果；不得提交真实配置、聊天、模型、索引、密钥或内部故障材料。提交前扫描当前文件、暂存文件、嵌入归档及候选 Git 历史。任何公开推送都需用户另行授权。

测试须指定实际组装的 `--release` 和仓库外 `--output`，使用安装根的独立 Chat Python。升级回归离线重建发布，先将 `FFA_CACHE` 环境变量设为安装时实际使用的下载缓存：默认是安装根的 cache；若安装传了 `--cache`，使用那个路径。不能隐式依赖维护者旧工作区缓存。默认常规回归不包含原生迁移夹具；`--include-migration` 增加加密包、SQLite/FAISS 和作用域恢复测试，仍不能冒充新 Windows 主机或真实 QQ 人工验收。所有输出、TEMP/TMP 和字节码缓存应指向 local/ux-validation。
