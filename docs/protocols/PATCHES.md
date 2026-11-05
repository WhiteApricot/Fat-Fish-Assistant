# 固定版本的最小兼容补丁

来源及官方归档 SHA 见 [来源清单](../../manifests/sources.json)。AstrBot 11 个、LivingMemory 3 个、continuous_message 4 个修改文件；meme_manager 无修改。数字是修改文件数，不等于独立功能数。

changes.json 只保存变更行段：path、before/after SHA-256、edits[start,end,old,new]。下标按原文件行号，按逆序应用；前置字节 SHA、原行段、补丁文件 SHA 和最终字节 SHA 均校验。新建的自有桥接文件 before=null。没有完整上游 overlay 或上游 ZIP。

官方 Git 归档采用 LF；旧本地归档曾使用 CRLF。生成本轮补丁时已核对规范化换行后原文完全一致，以官方 LF 字节为新基线，不用旧本地 ZIP 的哈希冒充官方下载哈希。补丁文件也固定完整性校验，版本不符拒绝，不尝试模糊套用。

原因：统一的早期权限/黑名单、撤权 epoch、上下文/工具/引用与唯一发送路径，需要处于扩展 Hook 无法证明同等覆盖的位置。保留这些补丁，不通过关闭检查来减少耦合。判断子集从固定 GPL 上游动态取得，保留 LICENSE，不在自有 src 复制。

恢复/撤销：从清单重建旧 release 并 rollback；仅在停止的隔离目录做 pristine 重建。构建失败的 stage 不激活。上游作者版权及许可继续适用；本项目修改标注 Fat-Fish-Assistant 和日期，不删除原声明。下载脚本不免除 AGPL/GPL 的相应源代码与通知义务。
