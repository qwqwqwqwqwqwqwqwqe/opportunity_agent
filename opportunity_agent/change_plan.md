可以把你现在的留学 Agent 改造目标浓缩成一句话：

> **从“能聊天的留学助手”升级成“能够长期维护用户申请状态、检索可靠信息、执行申请任务并被量化评测的 Agent 系统”。**

而且你现有前端不用推倒重来，重点改后端和 Agent 核心。

## 一、最终建议增加的技术

我建议分成 **必须做、第二阶段、可选** 三层。

| 技术                         | 必要性 | 在你的项目里干什么                            |
| -------------------------- | --- | ------------------------------------ |
| **FastAPI**                | ⭐⭐⭐ | 替换现在的 `ThreadingHTTPServer`，提供正式 API |
| **Pydantic**               | ⭐⭐⭐ | 请求/响应、Agent Structured Output        |
| **PostgreSQL/MySQL**       | ⭐⭐⭐ | 用户、画像、学校、项目、对话、申请状态持久化               |
| **SQLAlchemy/SQLModel**    | ⭐⭐⭐ | Python ORM                           |
| **Alembic**                | ⭐⭐  | 数据库 migration                        |
| **async/await**            | ⭐⭐⭐ | 并发搜索学校、项目、网页等 IO 操作                  |
| **SSE**                    | ⭐⭐⭐ | Agent streaming 输出                   |
| **Redis**                  | ⭐⭐⭐ | session、缓存、临时 Agent state、限流         |
| **Docker Compose**         | ⭐⭐⭐ | 一键运行 FastAPI + DB + Redis            |
| **LangGraph**              | ⭐⭐⭐ | Agent workflow / state / 多步骤任务       |
| **RAG**                    | ⭐⭐⭐ | 基于学校官方资料、项目要求等做可靠检索                  |
| **Hybrid Search**          | ⭐⭐  | BM25 + Vector，提高召回                   |
| **Reranker**               | ⭐⭐  | 对召回结果重新排序                            |
| **MCP**                    | ⭐⭐  | 把搜索、学校查询、文档处理等能力标准化成 Tools           |
| **Memory**                 | ⭐⭐⭐ | 长期用户偏好、历史决策、申请状态                     |
| **Evaluation**             | ⭐⭐⭐ | 证明你的 Agent 真正有效                      |
| **Langfuse/OpenTelemetry** | ⭐⭐  | trace、token、latency、错误分析             |
| **Pytest**                 | ⭐⭐  | API / Agent 回归测试                     |

### 暂时不要加

Spring Cloud、K8s、Kafka、微服务、Service Mesh。

这些现在会让项目复杂很多，但对你这个项目的第一版收益不大。

---

# 二、你现有项目最重要的变化

你现在大概是：

```text
Frontend
   ↓
ThreadingHTTPServer
   ↓
Opportunity Agent
   ↓
LLM
   ↓
回答
```

目标改成：

```text
Frontend
    ↓ HTTP / SSE
FastAPI
    ↓
Agent Service
    ↓
LangGraph
    ↓
┌──────────────┬───────────────┬──────────────┐
│ Profile      │ Research      │ Planning     │
│ Agent        │ Agent         │ Agent        │
└──────┬───────┴───────┬───────┴──────┬───────┘
       ↓               ↓              ↓
 PostgreSQL          RAG            MCP Tools
       ↓               ↓              ↓
     Redis          Vector DB      Web/Search API
       └───────────────┼──────────────┘
                       ↓
                    Memory
                       ↓
                   Evaluation
```

---

# 三、第一步：先把后端正规化

你现在最应该改的是：

### `ThreadingHTTPServer → FastAPI`

把 API 拆出来，例如：

```text
POST /api/chat
GET  /api/conversations/{id}
POST /api/profile
GET  /api/profile
GET  /api/schools
POST /api/applications
GET  /api/applications
```

项目结构建议变成：

```text
app/
├── main.py
├── api/
│   ├── chat.py
│   ├── profile.py
│   ├── schools.py
│   └── applications.py
├── models/
├── schemas/
├── services/
├── agents/
├── tools/
├── rag/
├── memory/
├── db/
└── core/
```

这样面试官一看就能知道你不是把所有逻辑塞进一个 Python 文件。

---

# 四、第二步：把原来的 JSON / 内存状态变成数据库

你这个项目非常适合设计成：

```text
User
  ↓
UserProfile
  ↓
ApplicationPlan
  ↓
School
  ↓
Program
  ↓
Application
```

再加：

```text
Conversation
   ↓
Message
```

以及：

```text
UserInteraction
```

例如：

```text
users
user_profiles
schools
programs
applications
conversations
messages
tasks
```

### 特别重要：`applications`

你原来的 Agent 如果只是：

> “推荐几个学校”

业务感比较弱。

改成：

```text
School
   ↓
Application
   ├── status
   ├── deadline
   ├── requirements
   ├── SOP
   ├── LOR
   ├── transcript
   └── checklist
```

Agent 就开始真正参与“申请流程”。

---

# 五、第三步：加入 RAG，而且不要做成普通 Chat with PDF

这是整个项目升级的关键。

你的 RAG 数据源可以是：

```text
学校官网
项目官网
Admission Requirements
Deadline
Curriculum
Tuition
GRE/TOEFL requirements
就业信息
官方 FAQ
```

然后构建：

```text
Official Web / PDF
       ↓
Crawler / Loader
       ↓
Chunk
       ↓
Embedding
       ↓
Vector DB
```

查询时：

```text
User Query
    ↓
Query Rewrite
    ↓
Hybrid Retrieval
 ┌────────────┐
 │ BM25       │
 │ Vector     │
 └─────┬──────┘
       ↓
    Reranker
       ↓
   Relevant Docs
       ↓
       LLM
```

最终回答必须带：

```text
来源
链接
文档时间
```

这样你就不是：

> “我用 RAG 做了留学问答。”

而是：

> **基于官方项目资料的 evidence-grounded admission research agent。**

这个定位会好很多。

---

# 六、第四步：把 Agent 从“聊天”改成“任务执行”

这是最值得升级的地方。

不要：

```text
User
 ↓
LLM
 ↓
Answer
```

改成：

```text
User Task
    ↓
Planner
    ↓
Research
    ↓
Retrieve
    ↓
Tool Call
    ↓
Validate
    ↓
Update State
    ↓
Generate Result
```

例如用户说：

> “根据我的背景帮我制定美国 CS Master 申请方案。”

Agent 可以自动：

```text
1. 读取 UserProfile
2. 检查 GPA / TOEFL / research
3. 查询目标项目
4. 检索官方要求
5. 判断是否符合
6. 划分冲刺/匹配/保底
7. 生成申请计划
8. 生成 checklist
9. 写入 application state
```

这时 LangGraph 的价值就体现出来了。

---

# 七、第五步：做真正的 Memory

你这个项目非常适合做长期记忆。

不要只保存聊天记录。

分成：

```text
User Profile
   +
Semantic Memory
   +
Preference Memory
   +
Episodic Memory
   +
Application State
```

例如用户第一次说：

> 我不考虑需要 GRE 的项目。

一个月后 Agent 仍然应该记得。

后来用户说：

> 预算提高了。

Agent 应该更新原来的状态，而不是继续按照旧预算推荐。

甚至可以处理：

```text
旧信息
“目标学校 10 所”

新信息
“只想申请 6 所”
```

Agent 必须更新 state。

这就把你的项目从：

**聊天机器人**

变成：

**Long-Horizon Personalized Agent**

---

# 八、第六步：加入 MCP

不要为了“简历上写 MCP”硬加。

让 MCP 真正承担工具。

例如：

```text
MCP Server
├── School Search Tool
├── Program Requirement Tool
├── Deadline Tool
├── Web Search Tool
├── Document Parser Tool
└── Application Tracker Tool
```

Agent：

```text
需要查项目要求
      ↓
调用 Program Requirement Tool
      ↓
返回结构化数据
```

这样你就能在面试中解释：

> 为什么使用 Tool？

> 为什么把这些功能封装成 MCP？

> MCP 和普通 function calling 有什么区别？

---

# 九、第七步：加入 Human-in-the-loop

这个特别适合你的留学场景。

Agent **不能未经确认就修改重要状态**。

例如：

```text
Agent：
“我建议加入 UIUC MCS，是否加入申请列表？”

        ↓

User Confirm
        ↓

写入 Database
```

而不是：

```text
LLM
 ↓
直接修改 application table
```

你原来已经有这种“确认、不擅自修改”思路，这个反而应该保留并工程化。

---

# 十、最重要：加入 Evaluation

这一步决定你的项目最终是不是“简历项目”。

你可以自己构建一个：

```text
100～300 条测试任务
```

例如：

### Profile extraction

输入：

> “我的 GPA 3.9，托福 107，做过 GNN……”

检查：

```text
GPA 是否正确
TOEFL 是否正确
Research 是否正确
```

---

### RAG

问题：

> “UIUC MCS 是否需要 GRE？”

评价：

```text
Retrieval Recall@K
Citation Precision
Citation Recall
Answer Faithfulness
```

---

### Recommendation

准备人工标注结果：

```text
Suitable
Unsuitable
```

然后测试：

```text
Recommendation Precision
Recall
```

---

### Task completion

例如：

> “帮我建立一个 6 所学校的申请清单，并根据 deadline 排序。”

测试：

```text
Task Success Rate
Deadline Accuracy
Required-school Recall
```

---

### Agent efficiency

还可以记录：

```text
平均 Tool Calls
Average Latency
Token Cost
Failure Rate
Retry Rate
```

这样你终于有资格写：

> Improved retrieval recall from X to Y.

而不是：

> “效果很好。”

---

# 十一、一定做一个 Baseline

这个是你科研背景可以发挥优势的地方。

比如 RAG：

```text
Baseline
Vector Search

Version 2
Hybrid Search

Version 3
Hybrid + Reranker

Version 4
Hybrid + Reranker + Query Rewrite
```

然后比较：

| 方法              | Recall@5 | Faithfulness | Latency |
| --------------- | -------: | -----------: | ------: |
| Vector          |        X |            X |       X |
| Hybrid          |        X |            X |       X |
| + Reranker      |        X |            X |       X |
| + Query Rewrite |        X |            X |       X |

真实数字以后自己实验出来。

这会让你的项目明显带一点你已有科研项目的风格：

> **提出改进 → 实验 → 对比 → 分析失败案例。**

---

# 十二、加一个简单的 Observability

接入：

```text
Langfuse
```

就已经很好用了。

你可以看到：

```text
User Query
 ↓
Planner
 ↓
Retriever
 ↓
Tool
 ↓
LLM
 ↓
Final Answer
```

然后每一步都有：

```text
Latency
Tokens
Cost
Input
Output
Error
```

这样调 Agent 非常方便。

---

# 十三、最终版本建议长这样

```text
                 ┌───────────────┐
                 │    Frontend   │
                 └───────┬───────┘
                         │
                    HTTP / SSE
                         │
                 ┌───────▼───────┐
                 │    FastAPI    │
                 └───────┬───────┘
                         │
                 ┌───────▼────────┐
                 │ Agent Runtime  │
                 │   LangGraph    │
                 └───────┬────────┘
                         │
        ┌────────────────┼────────────────┐
        │                │                │
        ▼                ▼                ▼
 Profile Agent     Research Agent   Planning Agent
        │                │                │
        │                ▼                │
        │          Hybrid RAG             │
        │          + Reranker             │
        │                │                │
        └────────────────┼────────────────┘
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
         PostgreSQL    Redis       MCP
              │          │          │
              └──────────┼──────────┘
                         ▼
                      Memory
                         ▼
                   Evaluation
                         ▼
                    Langfuse
```

---

# 十四、建议你的改造分 3 个版本

## V1：后端化

先做到：

```text
ThreadingHTTPServer
        ↓
FastAPI

JSON
 ↓
PostgreSQL

in-memory session
 ↓
Redis

普通 response
 ↓
SSE
```

技术上已经是一套正规 Web Agent 后端。

---

## V2：Agent 化

加入：

```text
LangGraph
RAG
Hybrid Search
Reranker
Memory
MCP
HITL
```

然后让 Agent 能真正：

```text
检索 → 判断 → 调工具 → 更新状态 → 继续任务
```

---

## V3：工程化 + 评估

最后加入：

```text
Docker Compose
Pytest
Langfuse
Evaluation Dataset
Baseline
Metrics
Failure Analysis
```

这时候才是你最终拿去找实习的版本。

---

# 十五、你的最终简历项目可以怎么定位

不要叫：

> Study Abroad Chatbot

我建议定位成：

### **Long-Horizon Personalized Study-Abroad Agent**

一句话描述：

> Built a stateful Agent system for personalized graduate application planning, integrating evidence-grounded RAG, long-term memory, external tools, and human-in-the-loop workflows.

然后具体突出：

```text
FastAPI
PostgreSQL
Redis
LangGraph
RAG
Hybrid Retrieval
Reranker
MCP
SSE
Docker
Langfuse
Evaluation
```

最重要的是最后一定有**真实实验数字**。

---

## 最后给你一个优先级

你现在千万不要同时做所有东西。

### 第一优先级

**FastAPI → PostgreSQL/SQLAlchemy → Redis → async → SSE → Docker**

### 第二优先级

**RAG → Hybrid Retrieval → Reranker → LangGraph**

### 第三优先级

**Memory → MCP → HITL**

### 第四优先级

**Evaluation → Observability → Baseline comparison**

---

这样改完以后，你的项目就不是单纯“把一个留学 Agent 加几个热门技术”，而是形成一个完整的 **Agent application engineering project**：

**前端 + 后端 + 数据库 + 缓存 + Agent + RAG + Tool + Memory + Evaluation + 部署。**

这也正好能把你现在缺的“后端工程能力”与已有的 **华为 AI 工程 + 科研 + Agent** 三部分串起来。
