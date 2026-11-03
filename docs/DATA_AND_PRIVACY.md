# 数据与隐私

安装根与源码分开。聊天 SQLite、插件数据及索引位于 instance/data，权限、身份及密钥位于 instance/private，人格资源位于 instance/persona。管理和服务日志分别写入安装根 logs 和 instance/logs。Dashboard 凭据使用 Windows DPAPI，与 Windows 用户和机器相关。

云端模型收到为完成请求所需的消息、相关上下文、人格指示及检索材料。联网搜索还会将查询发送给已配置服务。管理时应根据所选服务判断传输范围，避免在聊天、日志或截图中提供密钥及个人敏感数据。

换机使用 [加密迁移包](MIGRATION.md)。文件路径、配置和数据兼容规则见 [配置与数据协议](protocols/CONFIG_DATA_SCHEMA.md)。
