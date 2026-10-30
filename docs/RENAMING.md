# 更名与兼容记录

正式对外名称统一为“肥鱼助手 / Fat-Fish-Assistant”，候选目录及仓库名为 Fat-Fish-Assistant。README、安装向导、管理结果 project 字段、自有命令文案及默认人格名称已更新。没有对外部原始源码、已安装目录或当前运行配置作全局替换。

|旧标识|处理|原因|
|---|---|---|
|feiyu_framework、旧项目显示名称|新候选使用 Fat-Fish-Assistant；旧仓库/入口内部留存|与当前运行和回滚依据分离|
|feiyu_guard.FeiyuGuard|正式类 fat_fish_guard.FatFishGuard；旧路径薄别名|兼容已存在的测试/导入，不安装重复插件|
|FEIYU_RUNTIME_ROOT / SOURCE_ROOT / PERSONA_ROOT|新启动器同时提供 FFA_* 和旧变量|已有核心适配仍可用；无私人盘符|
|FEIYU_TEST_RELEASE|测试入口同时提供 FFA_TEST_RELEASE 和旧变量|既有核心回归可复用|
|feiyu-onebot、feiyu-v1、feiyu-group-*|保留为 v1 持久化兼容命名空间|涉及会话、权限、人格和记忆作用域，不能靠重命名转换|
|feiyu-local-*、feiyu-bge-small-zh|保留 provider/model 协议 ID|与既有模型服务及原生配置兼容|
|_feiyu_*、astrbot.core.feiyu_p2c|保留内部事件键与桥接 API|跨补丁/插件的安全契约，不是产品显示名|
|.feiyu-private-boundary|保护脚本接受旧标记，新建使用 .fat-fish-assistant-private|已有备份与实例可继续受保护|
|p2c/p2d|保留稳定 Python 包名|是阶段模块命名，不是旧产品名|

旧昵称“肥鱼Bot”仅作为群判断的输入别名保留；新显示名加入同一别名表。权限和记忆数据不因品牌变化自动改写。旧快照继续按 envelope/schema/软件提交/原生 db_version 校验；未知版本拒绝，具体跨机兼容留待 VM 测试，不能宣称本轮已迁移。

已安装的第三方目录、当前实例及其主人/群权限没有更名或切换。候选默认人格的调整仅限项目名称，不重新设计人格内容。
