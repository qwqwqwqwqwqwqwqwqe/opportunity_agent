# Codex 第二版重构任务清单

目标：在**不推翻现有第一版代码**的前提下，将当前“规则匹配 + 固定问题模板 + 规则规划”的实现逐步升级为：

```text
用户自然语言
    ↓
LLM 结构化语义抽取
    ↓
CandidateFact / StageSignal
    ↓
Profile + UserState
    ↓
动态对话策略
    ↓
RAG / Database / Tools
    ↓
规划 / 推荐
    ↓
自然语言响应
```

总体原则：

* 保留现有规则实现作为 fallback，不一次性删除。
* LLM 不允许直接修改数据库。
* 所有 LLM 结构化输出必须通过 Pydantic 校验。
* 用户显式陈述优先级高于模型推断和行为推断。
* 简单任务关闭 thinking，复杂规划才允许使用 thinking。
* RAG 区分官方事实和社区经验。
* 每个 Task 完成后运行对应单元测试和已有回归测试。

---

# Phase 1：重构用户信息抽取

## Task-001：审计现有 Profile Extraction 代码
目标：
搞清楚现有用户画像抽取链路，并明确如何从纯规则抽取迁移到 FastExtractor + SemanticExtractor 双通道架构。

* [ ] 找出当前所有 `extract()`、regex、关键词匹配逻辑。
* [ ] 列出当前支持的用户字段。
* [ ] 列出当前会导致提取失败的输入类型。
* [ ] 保留现有规则抽取器，不删除。
* [ ] 输出 `docs/profile_extraction_current.md`。
* [ ] 不修改业务行为。

重点记录：

```text
academic_year
major
target_degree
target_countries
target_fields
gpa
class_rank
graduation_year
language_preparation
research_activity
career_goal
```

---

## Task-002：扩展 CandidateFact 数据模型

将现有 `CandidateFact` 扩展为至少：

```python
class CandidateFact(BaseModel):
    field: str

    raw_value: Any
    normalized_value: Any | None = None

    confidence: float

    source: str
    evidence: str | None = None

    operation: Literal["set", "append", "remove"] = "set"

    needs_confirmation: bool = False
```

要求：

* [ ] 保留原始用户表达 `raw_value`。
* [ ] 标准化后的结果存入 `normalized_value`。
* [ ] 保存原始证据 `evidence`。
* [ ] 支持 `set / append / remove`。
* [ ] 支持 `needs_confirmation`。
* [ ] 保持尽量兼容现有调用代码。
* [ ] 添加 migration / compatibility adapter。

测试：

* [ ] `"我是软工的"`。
* [ ] `"我以后可能想做 AI"`。
* [ ] `"我不考虑美国了"`。
* [ ] `"美国加拿大都可以"`。

---

## Task-003：增加 ExtractionResult

新增统一抽取结果：

```python
class ExtractionResult(BaseModel):
    intent: str | None = None

    facts: list[CandidateFact] = []

    stage_signals: list[StageSignal] = []

    information_needs: list[InformationNeed] = []

    should_replan: bool = False
```

要求：

* [ ] 后续 Agent 不再只消费 `list[CandidateFact]`。
* [ ] 同一句话可以同时提取画像、意图和阶段信号。

例如：

```text
我托福考完了，103，接下来准备找美国 AI 暑研。
```

应至少产生：

```text
TOEFL = 103
research interest = AI
country interest = US
LANGUAGE_PREPARATION ↓
RESEARCH / SUMMER_RESEARCH ↑
should_replan = true
```

---

## Task-004：拆分 Fast Rule Extractor

将当前 regex 代码重构为：

```text
RuleExtractor / FastExtractor
```

只负责高确定性结构：

* [ ] 本科年级。
* [ ] GPA。
* [ ] TOEFL。
* [ ] IELTS。
* [ ] GRE。
* [ ] 年份。
* [ ] 排名。
* [ ] 金额/预算。
* [ ] 明确日期。

不要继续扩展：

* 专业词典
* 国家词典
* 职业词典
* 研究方向词典

这些交给 Semantic Extractor。

---

## Task-005：实现 LLM SemanticExtractor

新增：

```python
async def extract(
    message: str,
    profile: StudentProfile,
    recent_messages: list[ChatMessage],
) -> ExtractionResult:
    ...
```

LLM 输入至少包含：

* 当前用户消息。
* 当前 UserProfile。
* 当前 UserState。
* 最近少量对话。
* CandidateFact Schema。
* StageSignal Schema。

Prompt 原则：

* [ ] 只提取有明确证据的信息。
* [ ] 不根据常识猜测。
* [ ] “可能 / 考虑 / 也许”等降低 confidence。
* [ ] 用户否定已有信息时使用 `remove` 或覆盖。
* [ ] 不在预定义枚举中的专业/研究方向仍必须保存。
* [ ] 不存在的信息不得输出。
* [ ] 与已有画像冲突时标记确认。
* [ ] 输出必须是结构化 JSON。

---

## Task-006：Structured Output + Pydantic Validation

实现：

```text
LLM JSON
   ↓
Pydantic
   ↓
Business Validation
   ↓
ExtractionResult
```

要求：

* [ ] JSON 无法解析时重试一次。
* [ ] 第二次失败后进入 RuleExtractor fallback。
* [ ] 不允许格式错误导致 Chat 整体失败。
* [ ] 记录 extraction latency。
* [ ] 记录 fallback reason。
* [ ] 记录 malformed output。

---

## Task-007：实现 Profile Normalizer

新增：

```text
app/profile/normalizer.py
```

解决用户表达和内部标准值的区别。

例如：

```text
软件工程
→ raw = 软件工程
→ category = Computer Science / Software Engineering
```

```text
北美
→ target_regions = North America
→ 不自动强行转换成 US
```

```text
AI相关
→ raw = AI相关
→ normalized = Artificial Intelligence
```

要求：

* [ ] raw 信息必须保留。
* [ ] 不确定的 normalization 不覆盖 raw。
* [ ] 支持 region / country 分离。
* [ ] 支持 major raw / canonical 分离。
* [ ] 支持职业方向 raw / canonical 分离。

---

## Task-008：实现 Profile Conflict Resolver

新增：

```text
CandidateFact
+
Existing Profile
    ↓
Conflict Resolver
```

优先级建议：

```text
用户明确陈述
>
用户确认过的信息
>
简历
>
高置信 LLM 抽取
>
行为推断
>
系统推断
```

要求：

* [ ] 检测信息覆盖。
* [ ] 检测否定。
* [ ] 检测冲突。
* [ ] 保存旧值历史。
* [ ] 不静默覆盖高可信信息。

例如：

```text
过去：
target_country = US

现在：
我其实不打算去美国了，更想新加坡。
```

必须正确更新。

---

# Phase 2：优化 LLM 调用和 Thinking 策略

## Task-009：建立统一 LLM Client

不要让不同模块自己直接请求 Qwen。

创建：

```text
app/llm/client.py
```

统一支持：

```python
generate(...)
generate_structured(...)
generate_with_reasoning(...)
```

配置：

```text
timeout
retry
temperature
max_tokens
thinking
model
```

---

## Task-010：关闭简单任务的 Thinking

以下任务默认：

```text
enable_thinking = false
```

包括：

* [ ] CandidateFact extraction。
* [ ] intent classification。
* [ ] stage signal extraction。
* [ ] query rewrite。
* [ ] follow-up question generation。
* [ ] 普通聊天。
* [ ] 简单推荐理由生成。
* [ ] 信息摘要。

不要因为这些任务触发超时后整体 fallback。

---

## Task-011：为复杂规划增加 Reasoning Router

新增：

```python
should_use_deep_reasoning(context) -> bool
```

只有以下情况使用 thinking：

* [ ] 多目标冲突。
* [ ] 大规模 replanning。
* [ ] 国家/项目复杂权衡。
* [ ] 时间严重不足。
* [ ] 多个 deadline 冲突。
* [ ] 用户目标发生重大改变。

普通：

```text
大一 CS → 美国 AI MS
```

不应该使用 deep thinking。

---

## Task-012：细化 LLM Fallback

取消：

```text
LLM timeout
→ 整个 Roadmap 退回规则模板
```

改为组件级 fallback。

例如：

```text
LLM milestone generation timeout
↓
fallback milestones
↓
后续 task personalization 继续执行
```

要求：

* [ ] Extractor fallback。
* [ ] Question generation fallback。
* [ ] Planner fallback。
* [ ] Reranker fallback。

每个 fallback 相互独立。

---

# Phase 3：动态对话策略

## Task-013：移除固定问题“选择逻辑”

当前：

```python
next_profile_question()
next_enrichment_question()
```

不要直接删除文件。

重构成：

```python
collect_information_needs(profile, state) -> list[InformationNeed]
```

例如：

```python
class InformationNeed(BaseModel):
    field: str
    information_gain: float
    planning_importance: float
    stage_importance: float
    uncertainty: float
    user_burden: float
    reason: str
```

---

## Task-014：实现 Question Policy

计算：

```text
QuestionScore =
0.35 * InformationGain
+ 0.30 * PlanningImportance
+ 0.20 * StageImportance
+ 0.15 * Uncertainty
- 0.15 * UserBurden
```

选择当前最值得获取的信息。

要求：

* [ ] 每轮默认只选择 1 个核心问题。
* [ ] 特殊情况下最多 2 个。
* [ ] 不按固定字段顺序机械询问。
* [ ] 已经从行为或简历获得的信息不重复问。

---

## Task-015：实现 LLM Follow-up Generator

Question Policy 决定：

```text
需要询问 academic_year
```

LLM 负责生成自然表达。

输入：

* 当前聊天上下文。
* 已知用户画像。
* selected information need。
* 为什么需要这个信息。

输出：

自然问题。

不要让 LLM 自由决定字段。

---

## Task-016：增加 Answer-First Policy

避免 Agent 变成问卷。

实现：

```text
用户有明确问题？
       ↓
      Yes
       ↓
先回答
       ↓
是否存在会显著影响结果的缺失信息？
       ↓
      Yes
       ↓
在回答结尾自然追问一个问题
```

例如用户问：

```text
CMU MSAII 适合我吗？
```

Agent 应先回答已有信息下的判断，再补充：

```text
如果你告诉我 GPA/排名，我可以进一步估计申请难度。
```

---

## Task-017：增加“停止追问”机制

以下情况不追问：

* [ ] 用户明确说不想补充。
* [ ] 当前问题可以完整回答。
* [ ] 信息增益过低。
* [ ] 用户连续多轮被追问。
* [ ] 用户正在执行具体任务。

避免顾问变审讯员。

---

# Phase 4：用户阶段感知升级

## Task-018：将 Stage Signal 接入 Semantic Extractor

LLM 从用户消息中提取：

```python
class StageSignal(BaseModel):
    stage: str
    direction: Literal["increase", "decrease"]
    strength: float
    evidence: str
```

例如：

```text
我已经开始准备托福了。
```

产生：

```text
LANGUAGE_PREPARATION ↑
```

---

## Task-019：整合 Conversation + Behavior Stage Detection

阶段判断至少结合：

```text
Explicit User Intent
Conversation Signals
Feed Behavior
Search Behavior
Job Behavior
Task Behavior
Timeline
```

不要只依赖关键词。

保留原有公式，但允许扩展：

```text
StageScore =
0.30 ExplicitIntent
+ 0.20 RecentBehavior
+ 0.15 GoalAlignment
+ 0.15 TimelineEvidence
+ 0.10 TaskEvidence
+ 0.10 ContentInterest
```

---

## Task-020：实现 Stage Evidence Log

每次阶段变化记录：

```json
{
  "old_stage": "...",
  "new_stage": "...",
  "confidence": 0.84,
  "evidence": [
    "...",
    "..."
  ]
}
```

支持前端未来展示：

> 为什么 Agent 认为我进入了“申请准备阶段”？

---

# Phase 5：RAG 基础设施

## Task-021：建立统一 Knowledge Document Schema

新增知识文档统一结构：

```python
class KnowledgeDocument(BaseModel):
    id: str

    source_type: str
    source_name: str
    source_url: str | None

    authority_type: Literal[
        "official",
        "community",
        "internal"
    ]

    title: str
    raw_content: str
    clean_content: str

    topics: list[str]
    countries: list[str]
    universities: list[str]
    programs: list[str]
    companies: list[str]
    stages: list[str]

    published_at: datetime | None
    retrieved_at: datetime

    source_quality: float
```

---

## Task-022：区分 Official KB 和 Community KB

逻辑上至少分成：

```text
Official Knowledge
```

用于：

* 学校要求
* deadline
* 学费
* TOEFL/GRE
* Visa
* 官方招聘 JD
* 政策

以及：

```text
Community Knowledge
```

用于：

* 申请经验
* 面经
* 课程体验
* 租房
* 生活
* 找工经验
* 暑研经验

禁止将 community 内容作为官方事实直接回答。

---

## Task-023：实现 Mock RAG 数据

第一版继续人工造数据，但必须使用真实 Schema。

至少生成：

```text
50 official-style documents
100 community-style posts
```

覆盖：

* TOEFL
* GRE
* 选校
* 科研
* 暑研
* SOP
* 推荐信
* 网申
* Visa
* 租房
* AI 求职
* 实习
* 面经

---

## Task-024：实现 Hybrid Retrieval

第一版至少：

```text
metadata filter
+
keyword/BM25
+
vector similarity
```

检索条件可以包含：

```text
country
school
program
stage
topic
career goal
user interests
```

不要只做纯 embedding nearest neighbor。

---

## Task-025：实现 Dual-Channel Retrieval

对于用户问题：

```text
Query
 ↓
Query Router
 ├── Official Retrieval
 └── Community Retrieval
```

返回：

```python
class RetrievalResult(BaseModel):
    official_docs: list[KnowledgeDocument]
    community_docs: list[KnowledgeDocument]
```

---

# Phase 6：Agent Chat 接入 RAG

## Task-026：实现 RAG Trigger

不是每一句话都检索。

需要 RAG 的场景：

* [ ] 学校项目问题。
* [ ] 政策问题。
* [ ] 考试信息。
* [ ] 申请经验。
* [ ] 求职经验。
* [ ] 面经。
* [ ] 租房/生活。
* [ ] 当前外部信息。
* [ ] Roadmap 中需要领域知识的环节。

纯：

```text
“好的”
“我现在大二”
```

不需要 RAG。

---

## Task-027：构建 Context Builder

LLM 输入不要无脑塞数据库。

构造：

```text
Current User Profile
+
Current User State
+
Recent Conversation
+
Current User Goal
+
Relevant Official Knowledge
+
Relevant Community Knowledge
```

限制 Context 长度。

优先级：

```text
用户显式事实
>
官方资料
>
与用户高度匹配的社区经验
>
一般背景知识
```

---

## Task-028：升级 Agent Response Generator

废弃当前“简单问题库式回答”。

新的回答生成器必须：

* [ ] 回答当前用户问题。
* [ ] 使用已有画像个性化。
* [ ] 必要时使用 RAG。
* [ ] 官方事实和社区经验区分表达。
* [ ] 不确定信息明确说明。
* [ ] 如有高价值信息缺口，自然追加一个问题。
* [ ] 不强行每轮追问。
* [ ] 保持像长期顾问，而不是 FAQ Bot。

---

# Phase 7：Feed 与用户画像联动

## Task-029：将帖子行为转化为 Interest Signals

根据：

```text
POST_VIEW
POST_CLICK
POST_LIKE
POST_SAVE
POST_DISMISS
```

更新：

```text
topic_interest
country_interest
school_interest
program_interest
career_interest
stage_interest
```

建议基础权重：

```text
impression = 0.1
view       = 0.5
click      = 1
like       = 2
save       = 3
share      = 4
dismiss    = -3
```

加入时间衰减。

注意：

行为只能作为推断，不应覆盖用户明确声明。

---

## Task-030：完成第二版 End-to-End 回归 Demo

必须覆盖以下场景：

### Scenario A：自由输入画像抽取

用户：

```text
我是西电软工大二，现在主要想去北美读 AI 相关硕士，
美国优先吧，加拿大也可以。我最近刚开始跟老师做 LLM 项目，
托福还没考。
```

验证：

* major 可以识别。
* academic year 可以识别。
* US / Canada preference 可以识别。
* AI target field 可以识别。
* LLM research activity 可以识别。
* language stage 可以识别。
* 不要求用户使用标准术语。

### Scenario B：动态追问

Agent 不按固定：

```text
年级 → 专业 → 学位 → 国家 → 方向
```

顺序机械询问。

根据已有信息选择信息增益最高的问题。

### Scenario C：Profile Conflict

用户后续说：

```text
我现在不太考虑加拿大了，更想新加坡作为第二选择。
```

正确更新画像，并保留历史。

### Scenario D：Feed Perception

用户：

* 点赞暑研帖子。
* 收藏 LLM 科研帖子。
* dismiss 英国本科内容。

验证兴趣变化。

### Scenario E：RAG

用户问：

```text
像我这种背景一般什么时候开始准备暑研？
```

系统：

```text
Profile
+
Stage
+
Community RAG
+
Official knowledge if needed
```

生成针对性回答。

### Scenario F：Stage Change

用户：

```text
托福考完了，103。我接下来准备开始选校了。
```

验证：

```text
language preparation ↓
application preparation ↑
should_replan = true
```

### Scenario G：LLM Timeout

人为触发 Extractor / Planner timeout。

验证：

* 系统不崩。
* 不整条链路回退到简单规则模式。
* 只 fallback 当前组件。

---

# 本轮重构完成后的目标架构

```text
                       User
                        │
                        ▼
                 Chat / Feed Events
                        │
             ┌──────────┴──────────┐
             ▼                     ▼
      Fast Rule Parser      LLM Semantic Extractor
             │                     │
             └──────────┬──────────┘
                        ▼
               ExtractionResult
                        │
       ┌────────────────┼─────────────────┐
       ▼                ▼                 ▼
 CandidateFacts    StageSignals         Intent
       │                │                 │
       ▼                ▼                 │
 User Profile       User State            │
       │                │                 │
       └────────────────┼─────────────────┘
                        ▼
                  Action Planner
                        │
          ┌─────────────┼──────────────┐
          ▼             ▼              ▼
       Tools       Database           RAG
                                    ┌──┴──┐
                                    ▼     ▼
                                Official Community
                                    └──┬──┘
                                       ▼
                              Response Generator
                                       │
                              Information Gap?
                                 │           │
                                Yes          No
                                 │           │
                                 ▼           │
                           Question Policy   │
                                 │           │
                                 └─────┬─────┘
                                       ▼
                                    User
```

# Codex 执行规则

每一个 Task：

* [ ] 先阅读相关现有代码。
* [ ] 尽量局部修改，不重写整个项目。
* [ ] 保留现有稳定 API，确实需要破坏兼容时先创建 adapter。
* [ ] 写对应测试。
* [ ] 运行已有测试。
* [ ] 不因新功能破坏 MVP。
* [ ] 将重要设计决定记录到 `docs/`。
* [ ] 一个 Task 完成后再进行下一个 Task。
* [ ] 不一次实现 Task-001～030。
* [ ] 不擅自引入复杂框架。
* [ ] 不在没有数据反馈的情况下实现 Learning-to-Rank、协同过滤等 ML 推荐算法。

## 推荐实际执行顺序

第一批：

```text
Task-001 → 008
```

先解决**自然语言画像提取**。

第二批：

```text
Task-009 → 012
```

解决 **Qwen thinking / timeout / fallback**。

第三批：

```text
Task-013 → 020
```

解决**动态聊天和阶段感知**。

第四批：

```text
Task-021 → 028
```

接入 **RAG + 顾问式回答**。

最后：

```text
Task-029 → 030
```

打通 **Feed 行为感知 + 完整第二版 Demo**。
