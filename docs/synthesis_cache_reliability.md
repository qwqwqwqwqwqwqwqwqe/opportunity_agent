# 合成回退与核验事实缓存

正常回答继续经过 LLM。合成失败只影响回答方式，不把已有 PASS 改成失败；确定性回退只输出有有效证据的核验事实及来源。`synthesizer_fallback` 标记回退，`synthesizer_diagnostics` 记录尝试次数、耗时、finish_reason 和可用的 usage，不保存 prompt、密钥或原始错误响应。

默认参数：`SYNTHESIZER_TIMEOUT_SECONDS=60`、`SYNTHESIZER_RETRIES=1`、`SYNTHESIZER_BUDGET_SECONDS=90`、`SYNTHESIZER_MAX_TOKENS=1400`。单次读取受剩余预算约束；整个合成阶段给总执行预算预留 5 秒。普通文本和结构化生成各自管理一层重试，不嵌套放大调用数。参数支持本地 `.env` 和 Compose 透传。

`RESEARCH_PERSIST_FACTS=1` 默认启用。在线核验通过后，在独立、最多 5 秒的事务内写入 research_programs、official_sources、research_requirements；PostgreSQL 项目/URL 事务锁等待最多 2 秒。缓存写入失败只记录 persist_errors，仍返回本次核验结果。学校/项目别名及入学季顺序统一匹配，重复内容刷新 30 天有效期，同源更新保留 superseded 历史，不同来源冲突保留。

`pending_review` 事实保持原状态。存在网页文档或待审核记录不等于有可用截止日期；核验字段缺失、过期或明确要求最新时仍走 MCP。普通重复查询在事实有效时返回 SQL 结果，diagnostics.cache_hit=true。全文/向量入库保持 RESEARCH_QUEUE_INGEST 原设置，仍由 Redis/worker 异步处理。

运行相关离线测试，并通过 RESEARCH_TEST_POSTGRES_URL 显式启用独立临时 schema 的 PostgreSQL 集成测试。测试显式在临时 schema 创建表，不使用 public 的同名业务表。

部署使用当前 docker-compose.yml + docker-compose.research.yml，构建并更新 api、research、worker，不重建 PostgreSQL/Redis，不删除数据卷。需要撤回时可恢复更新前的镜像；RESEARCH_PERSIST_FACTS=0 可关闭新缓存写入，已有缓存和审核状态不变。

scripts/verify_synthesis_cache_live.py 会创建一个验收账号和两个会话，实际发送两次 CMU MSAII 查询，检查第一次核验事实提交、第二次 SQL 命中，并比较旧 pending_review 行的指纹。脚本要求初始 MSAII 缓存为空；已有有效缓存时不应为了重跑而删除业务事实，应使用隔离环境验证首次缓存路径。
