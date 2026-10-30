# 安装、维护和恢复

统一安装会在仓库外建立 cache、envs、releases、instance、logs 和安装状态。已完成组件校验后复用；未知现存目录不覆盖。下载先写唯一 partial 文件，核对固定 SHA 后进入缓存。失败材料留存，不激活半成品。重复安装保留实例；软件改变时要求明确 upgrade，不偷偷切换。

安装根、实例、备份和暂存目录用 Windows DACL 限定维护者、SYSTEM、Administrators。不是加密；备份含凭据，私密传输、加密介质和用户登录由维护者负责。不要把日志/备份直接上传到 issue。

## 启停和诊断

start 只管理当前受控安装根，先启动选定模型/QQ，再启动 Chat；stop 反向处理。QQ 官方客户端路径和登录是人工步骤，未配置会明确提示。已有外部会话只读登记，不会自动接管或重启。服务仅回环，端口有占用时拒绝创建重复 Chat。

Chat 通过 STOP 请求触发 AstrBot 原生取消和插件 terminate/索引落盘。强停不等于一致备份，异常留下 DIRTY。OS 维护锁与进程归属检查不能靠删标记绕过。冷启动等待约三分钟，超时先 status/diagnose 和日志，禁止连续重复启动。模型各自 health 验证鉴权与固定身份，Embedding 另验证 512 维；不宣称语义或识别质量已验收。

## 备份、恢复、迁移

```powershell
python -I ffa.py --root ../assistant-local stop
python -I ffa.py --root ../assistant-local backup --destination ../private-snapshots/snapshot-001
python -I ffa.py --root ../new-assistant restore --snapshot ../private-snapshots/snapshot-001
```

恢复前在新机器安装匹配软件、暂不初始化实例（install --no-instance），再恢复到该安装根的新目录。新用户安装不能导入原主机身份/权限/数据库；个人迁移则保留自己的权限映射和长期记忆。禁止同时开启两端同一 QQ 消费者。

快照 envelope=2，校验源软件提交及 LivingMemory 原生 db_version。所有受管理写入者停止后，SQLite backup 生成各自一致副本，再包含已落盘 FAISS；不复制运行 WAL/SHM，不宣称多个数据库原子提交。未受本管理器控制的后台写入须另行停止。恢复先核验全部哈希/版本，再在新暂存目录复制并 integrity_check；失败留存隔离材料，已有目标/新聊天拒绝覆盖。

源码和插件从发布重建；QQ 登录态、环境、可执行文件和模型不作为用户数据快照搬运。缓存按明确数据路径排除，不按所有名为 temp 的文件夹盲删。当前备份/恢复实现沿用已验证机制，本轮没有重跑完整迁移；新品牌/下载布局下的端到端兼容列入 VM 迁移表。

主记忆索引修复入口 `scripts/rebuild_memory.py --instance <实例> --embedding-provider <已配置ID>` 调用原生 MemoryEngine/IndexValidator。先保留原 DB/索引，失败回退且 DIRTY，完整性通过后才清故障标记。graph_memory 开启时拒绝此主索引修复，不冒充已覆盖图索引。

## 升级与回滚

新代码从固定源与必要补丁组装新 release，通过回归后干净停止再 upgrade。切换插件和发布指针；持久化 upgrade-journal 在写入前建立。失败自动回退或用 recover-upgrade 恢复旧代码，不覆盖聊天。只支持已验证的同一 schema 切换，不支持任意上游数据库降级。新聊天出现后不能拿旧数据库快照直接覆盖。

必要补丁的撤销是从固定 pristine 来源重建旧发布/无补丁副本并切回，不是在运行目录逐文件反向改写。版本、前置哈希、补丁文件或后置哈希不符全部停止。现有安装依赖在本轮不升级、不移动；这些命令只在隔离测试实例运行。

## 常见错误

- 缓存 hash mismatch：保留并隔离异常缓存，再按固定来源下载；不要手改 manifest 让它通过。
- 非空目录未受管理：换一个新安装根；不可伪造标记来接管私人目录。
- API key 配置：只走私密文件/stdin 或原生管理页，结果仅显示 changed_fields。
- JSON 查询成功但服务未运行：ok 只说明查询完成，检查 installed/running/ready。
- 模型未授权或缺失：只安装 Chat 仍可进行基本配置；不要因下载器存在而推断权重可分发。
- PowerShell 策略：仅对审阅过的脚本使用进程级启动方式，不修改系统全局策略。
