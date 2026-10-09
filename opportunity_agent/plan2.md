# 面试级 V2：Long-Horizon Personalized Study-Abroad Agent System

## Summary

把当前 JSON + `ThreadingHTTPServer` + 单体生命周期 Agent，升级为：

```text
Web Frontend
   ↓ HTTP / SSE
FastAPI + Auth
   ↓
LangGraph Agent Orchestrator
   ├─ Profile Agent
   ├─ Research Agent
   ├─ Planning Agent
   └─ HITL / Task Command Gate
   ↓
PostgreSQL + pgvector / Redis / MCP Tools / RAG Worker
   ↓
Grounded Answer + State Update + Evaluation + Trace
```

采用 LangGraph 作为主工作流运行时；现有 openJiuwen ReAct、A2A、`LifecycleTools` 保留为兼容 Tool/A2A Adapter，不再承担主状态编排。保留并迁移已有画像抽取、冲突确认、状态转换、时间轴、简历和官网查询能力，避免重写已验证业务规则。

## 1. 基础设施与持久化

- 用 FastAPI 替换 `ThreadingHTTPServer`，按 `api / services / agents / repositories / workers / rag / mcp / evaluation` 分层；现有原生前端继续使用，但改为调用 `/api/v1/*`。
- 引入 PostgreSQL + pgvector、SQLAlchemy 2 Async、Alembic、Redis、Docker Compose；服务包括 `api`、`worker`、`postgres`、`redis`。
- 完整账号系统：邮箱注册、Argon2id 密码哈希、JWT access token、可撤销 refresh session、HttpOnly/SameSite Cookie、用户隔离和基础角色。
- 将 `conversations.json` 一次性迁移到数据库：保留原 JSON 备份，迁移命令要求指定 legacy owner；迁移可重复执行且不重复写入。
- 核心表：
  - `users`、`auth_sessions`
  - `profiles`、`profile_facts`、`profile_changes`
  - `conversations`、`messages`、`agent_runs`、`agent_events`
  - `application_plans`、`applications`、`application_tasks`、`task_progress`
  - `official_sources`、`official_requirements`、`knowledge_documents`、`knowledge_chunks`
  - `memory_items`、`change_proposals`、`approval_requests`
  - `evaluation_cases`、`evaluation_runs`、`evaluation_metrics`
- Redis 只保存短期会话工作状态、幂等锁、限流、缓存和任务队列，不把用户正式状态仅存于 Redis。

## 2. Agent Orchestrator 与状态写入

- 新建 LangGraph 工作流，图状态包含：`user_id`、`conversation_id`、`run_id`、当前 Profile/申请状态版本、用户消息、路由结果、检索证据、工具轨迹、变更提案、审批结果、最终回复。
- 工作流固定为：

```text
ingest_message
→ classify_intent
→ Profile Agent / Research Agent / Planning Agent
→ evidence_and_policy_check
→ proposal_or_command
→ human_approval_gate
→ persist_state
→ stream_final_answer
```

- Profile Agent 复用现有 `FastExtractor`、`SemanticExtractor`、`ProfileNormalizer`、`ProfileConflictResolver`、`StateTransitionEngine`；输出结构化 `ProfileChangeProposal`，不能直接写数据库。
- Research Agent 复用现有官方域名白名单、Tavily 搜索、安全页面读取和证据模型；负责官网资料同步、RAG 查询、引用选择和事实时效判断。
- Planning Agent 复用 `PlanningTimeline`、`PlanningSkill`、`RoadmapArticleSkill`、稳定任务 ID 与进度继承；只读取已确认 Profile、任务状态和官方证据。
- 新增 Application Task Command 层：创建申请、加入/移出选校表、创建 checklist、完成/延期任务等操作都先生成命令提案；Profile、截止日期、申请清单和任务完成状态必须经用户确认后提交。
- 现有 openJiuwen ReAct/A2A 保留为：
  - 可调用 MCP/业务 Tool 的兼容适配；
  - `Profile Agent` 的旧 A2A 入口；
  - 对比“轻量 ReAct 路由”和“LangGraph 长流程”的面试展示能力。
- 所有状态更新以数据库 `version` 乐观锁提交；迟到 LLM、Worker 或浏览器请求不得覆盖新版本。

## 3. 官方 RAG、MCP 与长期 Memory

- 建立官方知识采集 Worker：
  - 仅采集已审核学校域名、公开招生页/PDF；
  - robots、域名白名单、限速、大小限制、内容哈希、抓取时间、来源 URL 全部持久化；
  - 不绕过登录、验证码、付费墙或申请系统。
- 文档进入 `KnowledgeDocument → Chunk → Embedding` 流程；每个 chunk 保存学校、项目、申请季、资料类型、发布时间、抓取时间、权威等级和 URL。
- 检索链路：

```text
用户问题
→ Query Rewrite（中英检索词与 metadata filter）
→ PostgreSQL Full-text/BM25-like lexical retrieval
 + pgvector cosine retrieval
→ RRF fusion
→ Cross-encoder reranker
→ Citation validator
→ LLM grounded answer
```

- 使用 `intfloat/multilingual-e5-small` 作为本地默认 Embedding；使用 `BAAI/bge-reranker-v2-m3` 作为可开关 reranker。模型不可用时退化为关键词检索并明确标注。
- 官方资料与社区/经验资料预留双通道字段；V2 仅上线官方通道。任何社区经验未来都不能覆盖官方政策或用户个人事实。
- 提供真实 MCP Server，至少暴露：
  - `search_official_requirements`
  - `get_official_source`
  - `list_application_tasks`
  - `propose_application_change`
  - `apply_confirmed_change`
- LangGraph 和 openJiuwen 都通过 MCP/统一 Tool Adapter 调用这些工具；Tool 只返回结构化结果，写操作必须带审批令牌。
- 长期 Memory 分层：
  - Profile Memory：用户确认事实；
  - Preference Memory：例如“不考虑 GRE required 项目”；
  - Episodic Memory：某次任务、咨询、决定及证据；
  - Application State：申请学校、材料、截止日期、任务和风险；
  - Redis Working Memory：当前 Agent Run 临时上下文。
- 记忆写入采用提案、去重、来源、置信度和有效期；不从普通问句、假设或网页内容自动写入用户画像。

## 4. API、SSE 与前端迁移

- FastAPI API 保持明确资源边界：
  - `POST /api/v1/auth/register|login|logout|refresh`
  - `GET/PATCH /api/v1/profile`
  - `GET/POST /api/v1/conversations`
  - `POST /api/v1/conversations/{id}/runs`
  - `GET /api/v1/runs/{id}/events`（SSE）
  - `GET/POST/PATCH /api/v1/applications`
  - `POST /api/v1/tasks/{id}/commands`
  - `POST /api/v1/approvals/{id}/accept|reject`
  - `GET /api/v1/research/sources`
  - `POST /api/v1/research/refresh`
- SSE 事件包含：`run_started`、`route_selected`、`retrieval_started`、`retrieval_completed`、`tool_started`、`tool_completed`、`approval_required`、`state_committed`、`final_answer`、`run_failed`。
- 前端继续保留当前聊天、资料表、时间轴、简历审核、来源卡和任务按钮；改为消费服务端 SSE 与版本化 API，不再依赖 localStorage 保存权威状态。
- 右侧保留“事实依据、状态变化、官网引用、任务进度”；新增 Agent Run 详情，展示节点、Tool、耗时、引用和审批结果，作为可观测性展示页面。

## 5. 评测、可观测性与基线实验

- 建立 100–150 条版本化 gold dataset，分为：
  - 画像抽取与冲突处理；
  - 状态转换/延期/取消/历史补录；
  - 官网检索与引用；
  - 申请清单与 deadline 排序；
  - 任务执行与审批；
  - 多轮长期记忆。
- 评测指标：
  - `Profile Fact F1`、`Conflict Safety Rate`
  - `State Transition Accuracy`
  - `Retrieval Recall@5`、`MRR`
  - `Citation Precision / Recall`
  - `Answer Faithfulness`
  - `Task Success Rate`、`Deadline Accuracy`
  - 平均 Tool Calls、P95 延迟、Token 成本、失败率、重试率。
- 构建可比较 RAG baseline：
  1. Vector only；
  2. Hybrid retrieval；
  3. Hybrid + reranker；
  4. Hybrid + reranker + query rewrite。
- 每次评测写入数据库与 JSON/Markdown 报告；CI 至少运行离线 mock 评测和核心回归。
- 接入 OpenTelemetry，提供可选 Langfuse exporter；每个 Agent Run、检索、模型调用、工具调用、审批和数据库提交都有 trace/span、耗时、错误和引用数量。默认脱敏用户文本、简历全文和密钥。

## 实施阶段与验收

1. **Foundation**
   - FastAPI、认证、Docker Compose、PostgreSQL/Redis、Alembic、Repository 层、JSON 迁移、基础 SSE。
   - 验收：注册登录、跨浏览器会话、消息幂等、旧会话迁移、服务重启和版本冲突均可验证。

2. **Stateful Agent Runtime**
   - LangGraph、Profile/Research/Planning Agent、审批流、申请表与任务表、当前前端迁移。
   - 验收：一句用户进展可形成可解释提案；确认后才改变申请/任务状态；重规划不丢进度；Agent Run 可回放。

3. **Grounded RAG + MCP**
   - 官方采集 Worker、pgvector、混合检索、reranker、MCP Server、来源卡和缓存刷新。
   - 验收：CMU/UIUC 等合成官网场景有正确引用；无来源时不编造；检索资料不能直接改变 Profile。

4. **Evaluation + Observability**
   - Gold dataset、评测 Runner、baseline 对比、OTel/Langfuse、性能与失败分析报告。
   - 验收：可输出量化指标、实验表、失败样例和 Agent trace；关键回归在 CI 通过。

## Assumptions

- 新版定位为本地 Docker 可运行、可公开演示的面试级系统；不在本轮做 Kubernetes、Kafka、微服务拆分、支付、论坛或大规模爬虫。
- 使用 PostgreSQL + pgvector、Redis、LangGraph、FastAPI、MCP Python SDK；保留 openJiuwen 作为互操作与兼容展示层。
- 官方资料是唯一可用于项目要求结论的外部知识；用户事实、任务完成和申请状态始终遵循 Human-in-the-loop。
- 现有网页外观和工科规划规则优先复用；重构重点是服务端边界、持久化、工作流、检索、评测和可观测性。
