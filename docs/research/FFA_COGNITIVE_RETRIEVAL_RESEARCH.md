# FFA Cognitive Retrieval 源码研究

## FFA 当前基线

基于 `1023da5dcb6ee372b8dc0212441d3bb7cf95c3f1` 及 N01 未提交的新库实现。正式实例仍经原 `GatewayMemory`，新库路径由隔离夹具调用。新 `MemoryRepository` 的 `lexical_candidates` 按 scope、term 查 posting，再按当前修订及有效证据取候选。`semantic_candidates` 在同模型空间、按 scope 有界取 float32 向量。两个入口均复用证据 revoke/expiry/grant 的 SQL 过滤。擦除会清除相关修订，级联移除 posting 与 embedding。`CognitiveRetrieval.recall` 对普通查询只走词法；显式回忆最多追加一次已有 BGE。RRF 接收词法命中或余弦 ≥0.60 的向量候选。检索尚未使用实际事件的 actor、phase、occurred_at 和来源作为排序依据。

## 冻结题问题定位

| 题 | 当前证据 |
|---|---|
| 22 降雨 | 正项已进入原始向量候选并排第一，0.518 未过 0.60。不存在词法强锚点 |
| 23 完成的验证 | 记录回顾的干扰项排在薄膜应力测定前。旧实验为每篇语料赋同一种 read，不具备可信状态/动作区分；仅降阈值会选错 |
| 24 夜间过河 | 步行过桥 0.541 比渡轮 0.340 更接近，缺可信时间/动作约束。末班时间若无独立证据，不应强行判定夜间 |
| 25 用户经历 | 用户岩洞经历未进入旧公开语料，旧结果只能证明返回空，不能证明实际主体过滤 |
| 26 私人图谱 | 旧脚本预先排除了私有资料，不能作为 SQL 权限证明 |
| 27 群晨检 | 同理，旧报告不能证明另一群实际授权查询隔离 |
| 28 撤回 | 旧脚本跳过被删素材，未实际写入再撤回。真正回归要先可取、再擦除、然后复查词法/向量 |
| 29 取消 | 旧脚本未插入已取消事件。需要在授权作用域内保留该事件并证明它不能作为完成证据 |

## 七个上游实现与取舍

| 项目 / 许可证 | 已定位的处理路径 | FFA 采用与限制 |
|---|---|---|
| Hindsight / MIT | `retrieve_all_fact_types_parallel` 接收查询向量、bank/tag 和时间窗等，协同 semantic/BM25、图与时间入口，随后融合与重排。PG `retrieve_semantic_bm25_combined_sql` 在候选通道内约束检索。 | 用独立词法、语义与事件信号，再按排名融合。现有 SQLite revisions 与 evidence 足以实现一跳，省去全图、PostgreSQL 和常驻 reranker。[召回编排](https://github.com/vectorize-io/hindsight/blob/04698b6a8250615faa80d4871cb738c3df758057/hindsight-api-slim/hindsight_api/engine/search/retrieval.py#L129) |
| Graphiti / Apache-2.0 | `search → edge_search` 结合全文、embedding 和 group，边分开保存 valid/invalid 与 created/expired。维护器按冲突变化处理边。 | 保留世界发生时间和系统记录时间，候选不因近期写入而替代近期经历。不引图驱动。[搜索和 RRF](https://github.com/getzep/graphiti/blob/4ef979c74e1e68d08217cf0af4022ddbc89c257f/graphiti_core/search/search.py#L259) |
| OpenClaw / MIT | `manager-search-orchestration.ts` 在回复快路径绕过查询 embedding；`hybrid.ts` 拆分关键词、向量、序位和时效，另有 MMR。 | 复用快慢路径及通道分离。短条目的语义歧义需证据，MMR 不能代替。使用 SQLite 已有结构，不引 sqlite-vec。[快路径](https://github.com/openclaw/openclaw/blob/191e118b7c470f7c56bc2f3c8d130d5b94888e15/extensions/memory-core/src/memory/manager-search-orchestration.ts#L437) · [融合](https://github.com/openclaw/openclaw/blob/191e118b7c470f7c56bc2f3c8d130d5b94888e15/extensions/memory-core/src/memory/hybrid.ts) |
| Mem0 / Apache-2.0 | `_search_vector_store → score_and_rank` 有实体 boost、归一化 BM25、semantic 分解；原始 semantic 先过门槛。 | 采用 Explain 及门槛诊断，通道应独立，避免低余弦提前剔除具有可靠结构证据的记忆。[评分](https://github.com/mem0ai/mem0/blob/fff8a6472512d1bc1a4bccab8c645b65a1df7fe5/mem0/utils/scoring.py#L60) |
| Honcho / AGPL-3.0 | RepresentationManager 按 observer/observed、scope 和层级取 semantic/recent；Dialectic 再消费有证据的观察。 | 同 Principal 不自动共有私域与群域，非 Bot 主体不能冒充其经历。不复制原实现与 PG 队列。[表示和过滤](https://github.com/plastic-labs/honcho/blob/19384ce2e1223dc1ec7dbaa194d761517ab5bd7c/src/crud/representation.py#L617) |
| LangBot LongTermMemory / Apache-2.0 | 插件从独立仓迁移，当前对照主仓源码。L1 画像与 L2 事件各有范围键。候选先验 metadata，再按状态有效期、精确匹配和多样性选择。 | FFA 在 SQL 候选阶段完成当前 ACL 并继续复核来源，拒绝依赖远端 metadata 后端的自我声明。[候选选择](https://github.com/langbot-app/LangBot/blob/01f39a75f6b29940353c5be25625e7a99e556b80/plugins/KnowledgeEngine/LongTermMemory/store/recall.py#L96) |
| Hermes Agent / MIT | `session_search` 从状态库查历史，回填受授权消息片段，和 provider 的长期摘要/回合后同步分开。 | 撤回和历史保持原来职责边界；本轮不把历史内容复制进长期记忆或突破原生历史 ACL。[会话入口](https://github.com/NousResearch/hermes-agent/blob/6f929bf85d0e7f224ee23d555b36d705471c77cb/tools/session_search_tool.py#L760) |

版本引用固定到前一轮已逐源码读取的快照。此次原始 raw GitHub 复取遇到连接中断，未将远端更新时间或上游测试状态描述为重新验证。复用机制和约束思想，未复制许可证不兼容代码。

## 路线

先划分三种失败：候选缺失、排序错误与证据不足。第一步用原有 SQL 和索引保持可见性，第二步建确定性的 QueryIntent 并核查来源事件，第三步从候选融合出来的少量记录做证据判断，只有明确支持证据才回答。群、私人、来源撤销仍都覆盖全部通道。不能用普遍加前缀和调低全局阈值代替事件判断。
