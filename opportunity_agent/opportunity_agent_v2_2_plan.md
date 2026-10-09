# V2.2 重构计划：Custom Orchestrator + Jiuwen Multi-Agent + RAG + 用户偏好长期记忆 + 独立 Synthesizer

## 1. Context

现有 `opportunity_agent/plan2.md` 已规划面试级 V2 系统，并完成了对 openJiuwen Rust Python 能力边界的实测审计。本版本在原计划基础上进一步调整系统职责边界：

- **Orchestrator 改为自定义 Python 控制器**，不再由 Router Agent 同时承担编排与回答职责。
- **Router Agent 只负责高层路由**，决定需要调用 Profile / Research / Planning 哪些领域 Agent。
- **Router 前保留确定性 Guard**，高置信结构化事实优先走规则，不把确定性任务交给 LLM 猜。
- **Research Agent 内新增独立 Query Router**，负责 SQL / RAG / Hybrid / MCP 四类研究路径。
- **三个领域 Agent 全部返回结构化 Result**，统一进入 Result Aggregation & Context Builder。
- **Completion Checker 在最终生成前检查任务是否完成**；未完成则由 Orchestrator 触发下一轮缺失任务。
- **最终回答由独立 Response Synthesizer 生成**，不再由 Router Agent 生成。
- **Memory Service 与 Orchestrator 直接交互**：任务开始前检索相关用户偏好；任务完成后由 Orchestrator 触发 Memory Consolidation。
- **本阶段不实现 Agent Experience / 成功经验记忆**。长期记忆只保存与用户有关的稳定偏好；Profile、Application State 继续使用现有业务表。
- **Orchestrator 不直接写用户长期记忆**。Orchestrator 只决定“什么时候触发记忆整理”；Memory Consolidator / Memory Service 决定“写什么、怎么写、是否需要审批”。

继续遵循原计划的总原则：

> **框架有的用框架，框架没有的才自己写。**

---

## 2. openJiuwen 在新架构中的角色

openJiuwen 不再承担整个系统的控制平面，而主要负责：

- `ReActAgent`
- A2A Client / Server
- `send_streaming`
- `get_task`
- MCP Client（stdio + streamable_http）
- `OutputSchema` 流式分帧
- `Runner` / `ResourceMgr` / `AbilityManager`

以下能力继续由项目自行实现：

- Custom Python Orchestrator
- Session / 跨会话业务状态
- 用户偏好长期记忆
- RAG / embedding / pgvector 检索
- MCP Server
- Completion Checker
- Result Aggregation / Context Builder
- Response Synthesizer
- Evaluation

最终分工：

```text
openJiuwen
├── Agent Runtime
├── ReActAgent
├── A2A
├── MCP Client
└── Streaming primitives

Custom Python
├── Orchestrator
├── Guard
├── Result Aggregation
├── Context Builder
├── Completion Checker
├── Memory Service / Consolidator
├── RAG
├── Synthesizer
└── Evaluation
```

---

## 3. 目标架构

```text
                                  User
                                    │
                                    ▼
                            Frontend / Browser
                                    │
                              HTTP / SSE
                                    ▼
                                FastAPI
                                    │
                                    ▼
                     ┌───────────────────────────┐
                     │ Orchestrator              │
                     │ Custom Python             │
                     │ system-level control loop │
                     └─────────────┬─────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │                             │
                    ▼                             ▼
             Memory Service                    Guard
                    │                             │
                    │                             ▼
                    │                        Router Agent
                    │                    Jiuwen ReActAgent
                    │                             │
                    │                       RouteDecision
                    │                             │
                    │           ┌─────────────────┼─────────────────┐
                    │           ▼                 ▼                 ▼
                    │     Profile Agent     Research Agent     Planning Agent
                    │           │                 │                 │
                    │           │                 ▼                 │
                    │           │            Query Router           │
                    │           │         /      |      |      \    │
                    │           │       SQL     RAG   Hybrid   MCP   │
                    │           │        │       │      │      │     │
                    │           │        ▼       ▼      ▼      ▼     │
                    │           │  PostgreSQL  pgvector  Web / Tools │
                    │           │                  │                 │
                    │           │               Reranker             │
                    │           │                  │                 │
                    │           ▼                  ▼                 ▼
                    │     ProfileResult      ResearchResult      PlanResult
                    │            \                |                /
                    │             \               |               /
                    │              └──────────────┼──────────────┘
                    │                             ▼
                    │                Result Aggregation
                    │                  + Context Builder
                    │                             │
                    │                             ▼
                    │                   Completion Checker
                    │                       /           \
                    │                incomplete        complete
                    │                    │                │
                    │                    ▼                ▼
                    │               MissingTasks    Response Synthesizer
                    │                    │                │
                    │                    └─→ Orchestrator │
                    │                                     ▼
                    │                                 Draft Answer
                    │                                     │
                    │                               Answer Validator
                    │                                  /      \
                    │                                OK     regenerate
                    │                                 │
                    │                                 ▼
                    │                            Final Answer
                    │                                 │
                    │                                 ▼
                    │                     Post-Run Memory Trigger
                    │                                 │
                    │                                 ▼
                    └────────────────────── Memory Consolidator
                                                      │
                                                      ▼
                                               Memory Service
                                                      │
                                               write / proposal
                                                      │
                                                      ▼
                                              PostgreSQL / Redis
```

---

## 4. 核心设计原则

### 4.1 LLM 做语义决策，代码做确定性执行

LLM 适合：

- 判断用户意图
- 高层路由
- Research Query Router 的语义分类
- Profile / Research / Planning 领域推理
- 最终答案生成

Python 代码负责：

- 并发
- timeout
- retry
- A2A 执行
- Result 收集
- 状态机
- Completion 判断
- Context 构造
- Memory 写入策略执行
- 最大轮数 / 最大工具调用数 / 最大延迟控制

---

### 4.2 Router 与 Orchestrator 分离

Router：

> 决定“调用谁”。

Orchestrator：

> 决定“怎么执行”。

Router 不负责：

- A2A 连接生命周期
- 并发调度
- timeout / retry
- 收集 Agent Result
- 最终回答
- 长期记忆写入

---

### 4.3 Domain Agent 与最终回答分离

三个领域 Agent 统一返回结构化结果：

```text
Profile Agent   → ProfileResult
Research Agent  → ResearchResult
Planning Agent  → PlanResult
```

最终自然语言回答统一由：

```text
Response Synthesizer
```

生成。

---

### 4.4 任务完成检查发生在 Synthesizer 之前

不采用：

```text
先生成答案
→ 再发现没完成
→ 再重跑
```

采用：

```text
Agent Results
→ Completion Checker
→ 完成后才进入 Synthesizer
```

---

### 4.5 用户偏好记忆采用 retrieve → use → consolidate → update 闭环

```text
任务开始
→ Memory Retrieve
→ Agent 使用相关偏好
→ 完成任务
→ Orchestrator 触发 Memory Consolidation
→ Memory Service 决定写入 / proposal / no-op
```

---

## 5. FastAPI

### 职责

FastAPI 只负责 Web / API 层：

- JWT + Argon2 Auth
- REST API
- SSE
- Request Validation
- Response Serialization
- HTTP Exception Handling
- Conversation API
- Profile API
- Application API
- Task Status API

建议接口：

```text
POST /api/chat
GET  /api/conversations/{conversation_id}
GET  /api/profile
POST /api/applications
GET  /api/applications
GET  /api/tasks/{task_id}
```

FastAPI 不负责：

- Agent 路由
- RAG
- Memory 决策
- Completion
- 最终答案组织

---

## 6. Custom Python Orchestrator

### 定位

Orchestrator 是整个系统的 **Control Plane**。

它不是 ReAct Agent，不负责自由推理。

### 主要职责

1. 创建并维护一次请求的 `ExecutionState`
2. 在路由前从 Memory Service 检索相关用户偏好
3. 执行 Guard
4. 调用 Router Agent 获取 `RouteDecision`
5. 根据 RouteDecision 调用 Profile / Research / Planning Agent
6. 通过 Jiuwen A2A 执行领域 Agent
7. 控制并行 / 串行执行
8. 管理 timeout / retry / failure
9. 收集结构化 Agent Result
10. 调用 Context Builder
11. 调用 Completion Checker
12. 对 MissingTasks 发起下一轮执行
13. 达到完成条件后调用 Response Synthesizer
14. 调用 Answer Validator
15. 在任务成功完成后触发 Memory Consolidation
16. 管理 SSE progress event

### Orchestrator 不负责

- 判断一条内容是不是长期偏好
- 直接写 `memory_items`
- 直接修改 Profile
- RAG 检索
- 最终自然语言回答

---

## 7. ExecutionState

建议：

```python
class ExecutionState:
    query: str
    user_id: str
    conversation_id: str

    memory_context: MemoryContext

    route_decision: RouteDecision | None

    profile_result: ProfileResult | None
    research_result: ResearchResult | None
    plan_result: PlanResult | None

    success_criteria: SuccessCriteria

    current_round: int
    status: ExecutionStatus

    missing_tasks: list[MissingTask]

    started_at: datetime
    deadline_at: datetime | None
```

---

## 8. Memory Service

Memory Service 与 Orchestrator 直接交互。

### 8.1 读取职责

任务开始前：

```text
Orchestrator
    ↓
MemoryService.retrieve(user_id, query)
    ↓
MemoryContext
```

返回与当前 query 相关的：

- 用户稳定偏好
- 必要的 Profile 摘要
- Application State 摘要
- 可选 Conversation History 摘要

### 8.2 写入职责

Memory Service 负责：

- `put`
- `get`
- `search_semantic`
- `update`
- `remove`
- deduplicate
- conflict detection
- source priority
- confidence threshold
- TTL / expire
- approval routing

### 8.3 不保存 Agent Experience

本阶段不实现：

- successful strategy memory
- tool performance memory
- failure pattern memory
- self-improving agent experience

只保留：

```text
User Preference Memory
```

Profile 与 Application State 仍写现有业务表。

---

## 9. 用户偏好 Memory

### 9.1 保存内容

示例：

```text
不考虑 GRE required 项目
预算不超过 80,000 USD
优先就业导向
更倾向西海岸
更看重课程而非排名
不考虑某类项目
```

存：

```text
memory_items
memory_type = "preference"
```

### 9.2 不保存内容

以下不属于 Preference Memory：

- GPA
- TOEFL
- GRE 分数
- Major
- Research Experience

这些属于 Profile。

以下也不属于 Preference Memory：

- 当前申请状态
- SOP 是否完成
- 推荐信数量

这些属于 Application State。

---

## 10. Memory Consolidator

### 定位

Memory Consolidator 负责：

> 从一次已经完成的任务中识别“是否出现新的稳定用户偏好”。

它不负责系统执行，只负责记忆整理。

### 触发时机

由 Orchestrator 触发：

```text
Task status == COMPLETED
        ↓
Post-Run Memory Consolidation
```

推荐在：

```text
Answer Validator 通过之后
```

触发。

### 输入

```python
class MemoryConsolidationInput:
    query: str
    initial_memory: MemoryContext

    profile_result: ProfileResult | None
    research_result: ResearchResult | None
    plan_result: PlanResult | None

    completion_result: CompletionResult
    final_answer: str
```

### 输出

只生成：

```python
class PreferenceMemoryCandidate:
    key: str
    value: dict

    source: Literal[
        "user_explicit",
        "model_inferred"
    ]

    confidence: float

    operation: Literal[
        "add",
        "update",
        "remove",
        "noop"
    ]

    evidence: str

    requires_approval: bool
```

---

## 11. Preference Memory 写入规则

### 11.1 用户明确陈述

例如：

> 以后不要给我推荐要求 GRE 的项目。

生成：

```text
source = user_explicit
confidence ≈ 0.99
operation = add/update
requires_approval = false
```

允许 Memory Service 直接写入。

### 11.2 用户明确取消偏好

例如：

> 我现在也可以考虑要求 GRE 的项目。

生成：

```text
operation = remove/update
```

由 Memory Service 经过冲突检查后更新。

### 11.3 模型推断

例如：

> 根据历史行为推断用户偏好西海岸。

生成：

```text
source = model_inferred
requires_approval = true
```

进入：

```text
change_proposals
    ↓
approval_requests
    ↓
User Confirm
    ↓
memory_items
```

### 11.4 不允许 Orchestrator 直接写长期偏好

Orchestrator 只：

```text
trigger_consolidation()
```

不：

```text
memory_items.insert(...)
```

---

## 12. Working Memory 与长期偏好分离

### Redis Working Memory

由 Orchestrator 可以直接写：

```text
run:{id}:status
run:{id}:round
run:{id}:missing_tasks
run:{id}:current_agent
run:{id}:temporary_results
```

### PostgreSQL Preference Memory

只能：

```text
Memory Consolidator
→ Memory Service
→ Write Policy
→ DB / Approval
```

因此：

```text
Orchestrator → Redis Working State      ✅
Orchestrator → Long-term User Preference ❌
```

---

## 13. Guard

### 定位

Guard 位于 Router 前：

```text
Orchestrator
    ↓
Guard
    ↓
Router Agent
```

### 职责

对确定性输入优先路由：

- `_requires_semantic_context(msg)`
- `facts_cover_message(...)`
- `FastExtractor`

示例：

```text
“我托福考了 110”
```

可直接：

```text
Profile Agent
```

不用让 Router LLM 再猜。

### Guard 输出

```python
class GuardDecision:
    forced_agents: list[str] | None
    fully_handled: bool
    extracted_facts: list
```

---

## 14. Router Agent

### 实现

```text
Jiuwen ReActAgent
```

### 职责

只负责高层 Agent 路由：

```text
Profile
Research
Planning
```

### 输入

- User Query
- Relevant Preference Memory
- Profile / Application 摘要
- Guard Result

### 输出

```python
class RouteDecision(BaseModel):
    agents: list[
        Literal[
            "profile",
            "research",
            "planning"
        ]
    ]

    parallel: bool

    success_criteria: SuccessCriteria

    reason: str
```

### 不负责

- 最终回答
- Agent 执行
- Memory 写入
- RAG
- Result Fusion

---

## 15. Profile Agent

### 实现

openJiuwen Agent Runtime + A2A Server。

端口：

```text
:8771
```

### 职责

- Profile Fact extraction
- Normalizer
- Conflict Resolver
- State Transition
- Profile proposal
- Preference explicit statement candidate generation
- ProfileResult 输出

### 写权限

三个领域 Agent 中唯一允许产生用户状态修改 proposal。

仍然遵循：

```text
Model → Proposal
      ↓
Approval Gate
      ↓
Write
```

### 输出

```python
class ProfileResult(BaseModel):
    extracted_facts: list
    updated_fields: dict
    conflicts: list
    preference_candidates: list
    requires_confirmation: bool
```

---

## 16. Research Agent

### 实现

openJiuwen Agent Runtime + A2A Server。

端口：

```text
:8772
```

### 定位

负责：

> 事实、结构化数据、官方来源、RAG 证据和实时网页信息。

只读。

### 内部结构

```text
Research Agent
      ↓
 Query Router
 /     |      |       \
SQL   RAG   Hybrid    MCP
```

---

## 17. Research Query Router

### 职责

判断当前 research 子问题应该使用：

- SQL
- RAG
- Hybrid
- MCP / Web

### 输出

```python
class ResearchRoute(BaseModel):
    mode: Literal[
        "sql",
        "rag",
        "hybrid",
        "mcp"
    ]

    sql_filters: dict | None
    rag_query: str | None
    external_tools: list[str]
```

---

## 18. SQL Retrieval

处理：

- deadline
- tuition
- GRE required
- TOEFL minimum
- degree
- country
- program type
- Application State
- 可直接映射数据库字段的条件

底层：

```text
PostgreSQL
```

示例：

```sql
WHERE deadline > :date
AND gre_required = false
```

---

## 19. RAG

### 数据源

- 官方学校网站
- 官方项目页面
- Admission Requirements
- Curriculum
- FAQ
- Program Description
- 官方 PDF

### Ingestion

```text
Official Page
    ↓
HTML / PDF Parser
    ↓
CJK-aware Chunking
    ↓
E5-small Embedding
    ↓
knowledge_chunks
    ↓
pgvector
```

### 检索

```text
Query
 ↓
Embedding
 ↓
pgvector ANN
 ↓
Hybrid Retrieval
 ↓
Reranker
 ↓
Evidence
```

### 必须实现

- pgvector `<=>`
- HNSW
- metadata filtering
- exact program match
- official source priority
- CJK chunking
- E5 query/passsage prefix
- cross-encoder reranker

---

## 20. Hybrid Retrieval

处理同时包含结构化约束和语义约束的问题。

例如：

> 找出不要求 GRE、12 月后截止，而且 AI 课程比较强的项目。

拆分：

```text
SQL:
GRE = false
deadline > Dec 1

RAG:
strong AI curriculum
```

执行后融合。

---

## 21. MCP / Web Tools

Research Agent 内单独保留 MCP 路径。

### 使用场景

- 数据库没有
- RAG 未收录
- 数据可能过期
- 用户明确要求最新官网信息

### Jiuwen 能力

复用：

```text
McpClient
stdio
streamable_http
```

### Tools

```text
Official Web Search
Program Page Reader
Document Reader
Browser Tool
Deadline Verifier
```

### 安全要求

保留：

- HTTPS validation
- domain whitelist
- redirect re-validation
- SSRF protection
- size limit

---

## 22. Reranker

对：

- RAG result
- Hybrid candidates
- MCP / Web evidence

进行：

- deduplicate
- rerank
- official source priority
- exact program match priority
- relevant evidence selection

输出 Top-K evidence。

---

## 23. ResearchResult

统一结构：

```python
class ResearchResult(BaseModel):
    structured_results: list
    evidence: list
    sources: list

    missing_information: list[str]

    confidence: float
```

Research Agent 不返回最终用户回答。

---

## 24. Planning Agent

### 实现

openJiuwen Agent Runtime + A2A Server。

端口：

```text
:8773
```

### 职责

- Application Timeline
- Task Breakdown
- Deadline Scheduling
- School Selection Strategy
- Roadmap
- Essay / Recommendation Planning

### 边界

只读。

不能直接修改：

- Profile
- Application State
- Timeline DB record

修改仍走 Proposal / Approval。

### 输出

```python
class PlanResult(BaseModel):
    timeline: list
    tasks: list
    priorities: list
    dependencies: list
    assumptions: list
```

---

## 25. Jiuwen A2A

三个 Agent 仍独立运行：

```text
Profile Agent   :8771
Research Agent  :8772
Planning Agent  :8773
```

Orchestrator 使用 Jiuwen A2A Client 调用。

继续复用：

- connection pool
- `asyncio.to_thread`
- `send_streaming`
- `get_task`

Planning 长任务继续通过 `get_task` 查询状态。

---

## 26. Result Aggregation & Context Builder

### 实现

普通 Python。

### 输入

```text
ProfileResult
ResearchResult
PlanResult
MemoryContext
User Query
```

### 职责

- merge results
- deduplicate
- evidence ranking
- remove irrelevant content
- resolve duplicate facts
- select relevant preference memory
- context trimming
- build synthesis prompt

### 输出

```python
class SynthesisContext(BaseModel):
    query: str
    profile: ProfileResult | None
    research: ResearchResult | None
    plan: PlanResult | None
    memory: MemoryContext
```

---

## 27. SuccessCriteria

Router 除了路由，还要把用户目标转换成可检查条件。

```python
class SuccessCriteria(BaseModel):
    required_program_count: int | None

    deadline_required: bool
    gre_required: bool
    citation_required: bool
    planning_required: bool

    custom_requirements: list[str]
```

例：

> 找 5 个 12 月以后截止、不要求 GRE、AI 较强的项目并排序。

转成：

```text
program_count = 5
deadline_verified = true
gre_verified = true
ai_evidence_required = true
ranking_required = true
```

---

## 28. Completion Checker

### 实现

优先确定性规则，必要时再用 LLM。

### 输入

- SuccessCriteria
- ProfileResult
- ResearchResult
- PlanResult
- Current ExecutionState

### 输出

```python
class CompletionResult(BaseModel):
    status: ExecutionStatus
    missing_tasks: list[MissingTask]
    needs_user_input: bool
```

### ExecutionStatus

```text
RUNNING
COMPLETED
NEED_MORE_WORK
NEED_USER_INPUT
PARTIAL
FAILED
```

---

## 29. Orchestrator System-Level Control Loop

```python
MAX_ROUNDS = 3

memory = await memory_service.retrieve(user_id, query)

decision = await router.route(
    query=query,
    memory=memory,
)

for round_id in range(MAX_ROUNDS):

    results = await execute_agents(
        decision,
        state,
    )

    state.update(results)

    context = build_context(state)

    completion = check_completion(
        state,
        decision.success_criteria,
    )

    if completion.status == COMPLETED:
        break

    if completion.status == NEED_USER_INPUT:
        return ask_user(...)

    if completion.status == NEED_MORE_WORK:
        decision = build_followup_tasks(
            completion.missing_tasks
        )

if not completed:
    state.status = PARTIAL
```

### 注意

这是：

```text
System-level Control Loop
```

不是：

```text
ReAct Loop
```

领域 Agent 内部仍可以各自使用 Jiuwen ReAct loop。

---

## 30. 执行预算

第一版：

```text
MAX_ROUNDS = 3
```

同时配置：

```text
MAX_TOOL_CALLS
MAX_SEARCH_CALLS
MAX_LATENCY
MAX_TOKEN_BUDGET
```

达到上限仍未完成：

```text
PARTIAL
```

必须明确告诉用户：

- 已完成什么
- 哪些信息未核实
- 为什么没有继续生成未经证实的结论

---

## 31. Response Synthesizer

### 实现

普通 LLM Node。

不是 ReActAgent。

### 输入

```text
User Query
SynthesisContext
Citations
```

### 职责

- final answer generation
- personalization
- formatting
- citation insertion
- uncertainty expression
- coverage of requested items

### 禁止

- 调工具
- 自己搜索
- 改 Profile
- 改 Memory
- 创造 Context 中不存在的事实

---

## 32. Answer Validator

### 职责

验证最终 Draft Answer：

- 是否覆盖所有 SuccessCriteria
- 是否遗漏 Result
- Citation 是否存在
- 是否出现 Context 中没有的新事实
- 数量要求是否满足
- 是否错误改写 deadline / GRE 等精确事实

### Failure 分类

#### Synthesis Failure

Context 已完整，只是答案漏写。

处理：

```text
regenerate Synthesizer
```

#### Evidence Failure

底层证据缺失。

处理：

```text
return Orchestrator
→ MissingTasks
→ Research Agent
```

---

## 33. Post-Run Memory Trigger

只有：

```text
Completion == COMPLETED
AND
Answer Validator == PASS
```

才触发长期偏好记忆整理。

Orchestrator：

```python
await memory_consolidator.consolidate(...)
```

Orchestrator 只触发。

不直接写长期 Preference Memory。

---

## 34. Preference Memory Consolidation

### 流程

```text
Completed Run
     ↓
Memory Consolidator
     ↓
PreferenceMemoryCandidate
     ↓
Memory Service
     ↓
Conflict / Dedup / Policy
     ↓
 ┌───────────────┬────────────────┐
 │ direct write  │ approval needed│
 ▼               ▼
memory_items   approval_requests
```

### 允许记录

- 明确用户偏好
- 明确用户取消偏好
- 经用户确认的推断偏好

### 不记录

- Agent 执行策略
- 成功经验
- tool performance
- 自动总结出来的系统经验

---

## 35. PostgreSQL + pgvector

### 业务数据

```text
users
profiles
profile_facts
programs
applications
application_tasks
conversations
messages
memory_items
approval_requests
```

### RAG 数据

```text
knowledge_documents
knowledge_chunks
embedding
metadata
```

### pgvector

用于：

- document embedding
- semantic retrieval
- preference semantic recall（如需要）

---

## 36. Redis

Redis 只负责快速、临时状态：

```text
session state
working memory
task queue
task status
temporary results
retrieval cache
rate limit
```

不作为长期用户 Preference Memory 的唯一存储。

---

## 37. Approval Gate

继续保留原有安全不变量：

> **模型只能产出 proposal，不能直接修改重要用户状态。**

### Profile

用户显式事实可按已有高置信规则处理。

### Preference

```text
user_explicit
→ direct write after policy check

model_inferred
→ proposal
→ approval
→ write
```

### Research

永远只读。

### Planning

永远只读。

---

## 38. SSE

Orchestrator 向前端发 progress event。

示例：

```text
memory_retrieved
route_decided
profile_started
profile_completed
research_started
research_completed
planning_started
planning_completed
completion_checked
synthesis_started
answer_validated
memory_consolidation_started
done
```

Jiuwen `send_streaming` / `OutputSchema` 继续用于领域 Agent streaming。

---

## 39. Evaluation

Evaluation 独立于在线主链路。

```text
Gold Dataset
    ↓
Agent Pipeline
    ↓
Execution Trace
    ↓
Evaluator
    ↓
Metrics
```

### Router

```text
Routing Accuracy
```

### Profile

```text
Profile Fact F1
Conflict Safety Rate
```

### Research

```text
Retrieval Recall@5
MRR
Citation Precision
Answer Evidence Coverage
```

### Synthesizer

```text
Answer Faithfulness
Answer Relevancy
Coverage Rate
```

### Whole System

新增：

```text
Task Success Rate
Completion Rate
Average Rounds
Tool Calls per Task
Partial Completion Rate
Need-User-Input Rate
Latency
Token Cost
```

### Preference Memory

新增：

```text
Preference Extraction Precision
Preference Update Accuracy
Preference Conflict Safety Rate
Approval Precision
Cross-session Preference Recall
```

---

## 40. 建议目录结构

```text
opportunity_agent/
│
├── v2/
│   │
│   ├── api/
│   │   └── app.py
│   │
│   ├── orchestration/
│   │   ├── orchestrator.py
│   │   ├── execution_state.py
│   │   ├── guard.py
│   │   ├── router.py
│   │   ├── completion.py
│   │   ├── context_builder.py
│   │   ├── synthesizer.py
│   │   └── answer_validator.py
│   │
│   ├── agents/
│   │   ├── profile/
│   │   │   ├── agent.py
│   │   │   └── result.py
│   │   │
│   │   ├── research/
│   │   │   ├── agent.py
│   │   │   ├── query_router.py
│   │   │   ├── sql_search.py
│   │   │   ├── rag_search.py
│   │   │   ├── hybrid_search.py
│   │   │   ├── mcp_search.py
│   │   │   └── result.py
│   │   │
│   │   └── planning/
│   │       ├── agent.py
│   │       └── result.py
│   │
│   ├── memory/
│   │   ├── service.py
│   │   ├── consolidator.py
│   │   ├── policy.py
│   │   ├── repository.py
│   │   └── schemas.py
│   │
│   ├── rag/
│   │   ├── ingest.py
│   │   ├── embedding.py
│   │   ├── retrieval.py
│   │   └── reranker.py
│   │
│   ├── mcp/
│   │   └── server.py
│   │
│   ├── db/
│   │   ├── models.py
│   │   └── repositories.py
│   │
│   ├── services/
│   │   └── applications.py
│   │
│   └── evaluation/
│       ├── datasets/
│       ├── metrics.py
│       ├── runner.py
│       └── reports/
│
├── profile_a2a.py
├── research_a2a.py
└── planning_a2a.py
```

---

## 41. 实施阶段

### Phase 1 — Foundation

- FastAPI
- JWT + Argon2
- PostgreSQL
- Redis
- Docker Compose
- Alembic
- Repository
- SSE
- conversations migration

### Phase 2 — Orchestration Skeleton

先实现：

- `ExecutionState`
- Guard
- Router
- Custom Orchestrator
- 三个 dummy Agent Result
- Context Builder
- Synthesizer

先跑通：

```text
User
→ Guard
→ Router
→ Domain Agents
→ Structured Results
→ Context Builder
→ Synthesizer
```

### Phase 3 — Research Agent

实现：

- Research Query Router
- SQL
- RAG
- Hybrid
- MCP
- Reranker
- ResearchResult

### Phase 4 — Preference Memory

实现：

- Memory Service
- Preference Memory retrieval
- Memory Consolidator
- Preference Candidate
- conflict / dedup
- approval policy
- cross-session recall

明确：

> 本阶段不实现 Agent Experience。

### Phase 5 — Bounded Control Loop

实现：

- SuccessCriteria
- Completion Checker
- MissingTasks
- ExecutionStatus
- MAX_ROUNDS
- PARTIAL
- NEED_USER_INPUT

### Phase 6 — Answer Validation

实现：

- Synthesizer
- Answer Validator
- regenerate
- evidence failure backflow

### Phase 7 — Evaluation & Observability

实现：

- Gold Dataset 100–150 条
- Router / Profile / RAG / Synthesizer / Memory metrics
- OTel Trace
- Failure Analysis
- CI regression

---

## 42. 端到端验收

### 1. Guard

输入：

```text
“我托福考了 110”
```

必须绕过自由 Router 判断，可靠触发 Profile。

### 2. Preference Retrieve

会话 A：

```text
“以后不要推荐要求 GRE 的项目。”
```

会话 B：

```text
“再帮我找几所。”
```

Router / Research Context 中必须包含：

```text
avoid GRE-required programs
```

### 3. Preference Consolidation

任务完成后 Orchestrator 必须触发 Memory Consolidator。

但：

```text
Orchestrator 不得直接 INSERT memory_items
```

### 4. Inferred Preference

模型推断：

```text
用户可能偏好西海岸
```

不得直接写。

必须停在：

```text
approval_requests
```

### 5. Research Routing

```text
deadline query → SQL
curriculum query → RAG
deadline + curriculum → Hybrid
latest official info → MCP
```

### 6. Completion

用户要求：

```text
5 个项目
```

Research 只返回 4 个时：

```text
NEED_MORE_WORK
```

不得进入 Synthesizer。

### 7. Bounded Loop

最多：

```text
MAX_ROUNDS = 3
```

仍不完整：

```text
PARTIAL
```

### 8. Answer Validator

Context 有 5 个项目，但 Synthesizer 只写 4 个：

```text
regenerate
```

不得重新执行全部 Agent。

### 9. RAG 来源

- result 必须含 `source_id`
- source 可回到原网页
- 无来源不得编造
- generic department page 不得冒充 exact program page

### 10. Write Boundary

Research / Planning 不得直接修改：

```text
Profile
Application State
Preference Memory
```

---

## 43. 最终职责表

| 模块 | 职责 | 是否 LLM |
|---|---|---:|
| FastAPI | HTTP / Auth / SSE | 否 |
| Orchestrator | 系统级编排与控制循环 | 否 |
| Memory Service | 用户偏好检索 / 持久化策略 | 否 |
| Memory Consolidator | 提炼偏好 Candidate | 规则 + LLM |
| Guard | 确定性前置路由 | 否 |
| Router Agent | Profile / Research / Planning 路由 | 是 |
| Profile Agent | 用户事实 / 状态 proposal | 是 |
| Research Agent | 事实、证据、官方信息研究 | 是 |
| Research Query Router | SQL / RAG / Hybrid / MCP | 规则 + LLM |
| Planning Agent | 时间线 / 任务规划 | 是 |
| SQL Retriever | 结构化查询 | 否 |
| RAG Retriever | 语义检索 | 否 |
| MCP | 外部实时工具 | Agent 调用 |
| Reranker | Evidence 排序 | 模型 |
| Context Builder | Result 汇总 / 压缩 | 否 |
| Completion Checker | 判断任务目标是否完成 | 规则优先 |
| Synthesizer | 最终自然语言回答 | 是 |
| Answer Validator | Coverage / Faithfulness | 规则 + 可选 LLM |
| PostgreSQL | 长期业务数据 / Preference Memory | 否 |
| pgvector | 向量检索 | 否 |
| Redis | Working State / Cache / Queue | 否 |
| Evaluation | Metrics / Regression | 否 + 可选 LLM Judge |

---

## 44. 最终系统定位

本版本最终不是单纯的：

```text
Multi-Agent Chatbot
```

而是：

> **A stateful long-horizon application agent system combining openJiuwen Agent Runtime, deterministic Python orchestration, hierarchical routing, structured domain-agent contracts, evidence-grounded RAG, cross-session user preference memory, bounded task execution, independent response synthesis, and measurable evaluation.**

核心工程亮点：

```text
Custom Orchestrator
+ Guarded Routing
+ Jiuwen ReAct / A2A / MCP
+ SQL / RAG / Hybrid / MCP Research Routing
+ PostgreSQL + pgvector
+ Redis Working State
+ Cross-session Preference Memory
+ Completion Checker
+ Bounded Control Loop
+ Independent Synthesizer
+ Answer Validation
+ Offline Evaluation
```
