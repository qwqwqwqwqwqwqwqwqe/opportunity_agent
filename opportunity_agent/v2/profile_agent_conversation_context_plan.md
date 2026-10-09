# Profile Agent 与 Conversation Context 改造计划

> 目标：给 Codex 作为实现说明。  
> 本文是在 V2.2 架构基础上，进一步明确 **Profile Agent 的信息抽取链路、冲突处理、评测方法，以及 LLM 多轮上下文恢复机制**。

---

## 1. 本次改造范围

本次只聚焦两个问题：

1. **Profile Agent 如何完整、可靠地抽取用户信息**
2. **LLM 多轮对话为什么会“失忆”，以及如何增加 Conversation State**

总体原则：

- Profile 抽取采用 **规则优先 + LLM 补充**
- 规则已经抽出的事实 **不能从原文中删除后再给 LLM**
- LLM 必须同时看到：
  - 用户完整原文
  - 规则已提取的事实
- LLM 输出必须是 **结构化 JSON**
- 保留 `evidence` 设计，每个事实必须能追溯到用户原文
- LLM 输出不能直接写 Profile，必须经过：
  - Normalizer
  - Validator
  - Conflict Resolver
- Profile 冲突不再通过下一轮聊天输入 `A/B` 解决
- 冲突通过前端弹窗处理：展示新信息和旧信息，用户点击后直接更新
- 完成实现后构造几百条 Profile Extraction Gold Dataset，计算 Precision / Recall / F1
- Conversation State 与 Profile、Preference Memory 严格分离
- 每次调用 LLM 时必须带上：
  - 最近 N 条原始消息
  - 更早历史的 summary
  - Profile summary
  - relevant preferences

---

## 2. Profile Agent 的目标

Profile Agent 的职责不是“让 LLM 总结用户”，而是：

> 从用户输入中提取可验证的用户事实，并通过确定性处理链路生成可靠的 Profile 更新 proposal。

推荐主流程：

```text
User Message
    ↓
FastExtractor（规则 / 正则）
    ↓
High-confidence Facts
    ↓
LLM Structured Extractor
输入 = 用户完整原文 + 已提取 Facts
    ↓
Fact Candidates
    ↓
Candidate Merge / Dedup
    ↓
Normalizer
    ↓
Validator
    ↓
Conflict Resolver
    ↓
Proposal Builder
    ↓
ProfileResult
```

---

## 3. 第一层：FastExtractor（规则 / 正则）

### 3.1 为什么保留规则抽取

对于格式稳定、边界明确的字段，规则通常：

- 更稳定
- 成本更低
- 延迟更低
- 可解释
- 不容易幻觉

因此第一层继续使用高置信规则。

适合优先使用规则的内容包括但不限于：

```text
GPA
TOEFL
IELTS
GRE
排名（如 2/120）
年级
毕业年份
明确日期
明确金额 / 预算
明确数量
```

示例：

```text
用户：
“我 GPA 3.9，托福 107。”
```

规则可以直接得到：

```json
[
  {
    "field": "gpa",
    "value": 3.9,
    "confidence": 0.99,
    "source": "rule",
    "evidence": "GPA 3.9"
  },
  {
    "field": "toefl",
    "value": 107,
    "confidence": 0.99,
    "source": "rule",
    "evidence": "托福 107"
  }
]
```

---

## 4. 第二层：LLM Structured Extractor

### 4.1 不要把规则匹配内容从原文删除

禁止这种实现：

```python
facts = regex_extract(message)
remaining_text = remove_matched_text(message)
llm_extract(remaining_text)
```

原因：被规则匹配的文本可能参与其他语义关系。

例如：

```text
“我托福 107，但这个成绩我暂时不准备用来申请加拿大项目。”
```

如果删掉“托福 107”，LLM 可能无法正确理解后半句针对的对象。

### 4.2 正确输入方式

LLM 应收到：

```text
1. 用户完整原文
2. FastExtractor 已提取的 facts
3. 允许提取的字段 / schema
4. 提取规则
```

伪代码：

```python
fast_facts = fast_extractor.extract(message)

llm_facts = await llm_extractor.extract(
    original_message=message,
    known_facts=fast_facts,
)
```

Prompt 要明确告诉 LLM：

```text
- 阅读完整用户原文
- known_facts 是规则层已经可靠识别出的事实
- 不要重复 known_facts
- 补充规则层遗漏的事实
- 不得根据常识猜测用户没有明确表达的信息
- 每个事实必须返回 evidence
- evidence 必须是用户原文中的直接文本片段
- 输出必须严格符合 JSON Schema
```

---

## 5. LLM 主要负责哪些信息

LLM 主要处理不适合正则表达式覆盖的自然语言事实，例如：

```text
目标国家
目标项目类型
职业目标
申请动机
科研方向
实习经历描述
研究经历描述
长期规划
申请优先级
复杂否定
纠正
条件表达
```

例如：

```text
“加拿大只是如果美国毕业以后找不到工作时的备选。”
```

不能简单抽成：

```json
{"target_country": "Canada"}
```

而应该保留更准确的语义，例如：

```json
{
  "primary_target_country": "US",
  "fallback_country": "Canada"
}
```

如果 schema 暂时没有对应字段，则不要强行错误映射，可以：

- 放入受支持的自然语言字段
- 或标记为未支持 candidate
- 后续再决定是否扩充 schema

---

## 6. FactCandidate 结构

建议统一抽取层输出：

```python
class FactCandidate(BaseModel):
    field: str

    raw_value: Any
    normalized_value: Any | None = None

    operation: Literal[
        "add",
        "update",
        "remove"
    ]

    statement_kind: Literal[
        "explicit",
        "correction",
        "negation",
        "hypothetical",
        "question",
        "uncertain"
    ]

    evidence: str

    confidence: float

    source: Literal[
        "rule",
        "llm"
    ]
```

---

## 7. evidence 必须保留

`evidence` 是 Profile 防幻觉的重要约束，不删除。

每一个 Profile Fact Candidate 都必须指出：

> 这个事实具体来自用户哪一段原话。

例如：

```text
用户：
“以后想做 AI 工程。”
```

输出：

```json
{
  "field": "career_goal",
  "raw_value": "AI Engineer",
  "operation": "add",
  "statement_kind": "explicit",
  "evidence": "以后想做 AI 工程",
  "confidence": 0.93,
  "source": "llm"
}
```

协议层应校验：

```python
fact.evidence in original_message
```

如果 evidence 不是用户原文子串，则拒绝该 candidate。

这样可以防止：

```text
用户只说：
“我最近考虑申请美国。”

LLM 却自动补：
career_goal = software engineer
```

这种无证据推断。

---

## 8. Candidate Merge / Dedup

FastExtractor 和 LLM Extractor 都可能产生同一个字段。

例如：

```text
FastExtractor:
toefl = 107

LLM:
toefl = 107
```

需要合并去重。

推荐优先级：

```text
高置信规则事实
    >
有明确 evidence 的 LLM explicit fact
    >
低置信推断
```

同一 message 内出现明确纠正时，不能简单按 source 优先级覆盖，必须结合：

```text
statement_kind
operation
evidence order
```

例如：

```text
“不是 105，我托福现在是 110。”
```

最终应该得到：

```text
TOEFL = 110
```

---

## 9. Normalizer 的作用

### 9.1 Normalizer 不是为了把所有自然语言都“统一”

Normalizer 的目标是：

> 对 **能够稳定标准化的字段** 转换成统一的 canonical representation，方便数据库存储、比较、冲突检测、过滤和评测。

并不是所有字段都能或都应该被标准化。

### 9.2 适合标准化的字段

#### 分数

```text
“托福107”
“TOEFL 107”
“toefl:107”
```

统一：

```json
{"toefl": 107}
```

#### GPA

```text
“3.9/4.0”
“GPA 3.90”
```

统一：

```json
{
  "gpa": 3.9,
  "gpa_scale": 4.0
}
```

#### 排名

```text
“专业第二，一共120人”
“2/120”
```

统一：

```json
{
  "rank": 2,
  "cohort_size": 120
}
```

#### 日期

```text
“27年7月24日”
“2027/07/24”
```

统一：

```text
2027-07-24
```

#### 可枚举字段

例如：

```text
“计算机”
“CS”
“Computer Science”
```

如果业务上已经明确只有一个 canonical major，可以统一为：

```text
Computer Science
```

### 9.3 不适合强制标准化的字段

大量自然语言字段没有唯一 canonical value，例如：

```text
科研经历描述
实习经历描述
职业目标
长期规划
研究兴趣
申请动机
项目偏好原因
```

这些字段不要为了“统一”而损失语义。

可以保留：

```text
原始自然语言描述
+
少量可选标签
```

例如：

```json
{
  "career_goal_text": "毕业后希望进入 AI 工程或 Agent 开发岗位",
  "career_goal_tags": ["AI Engineering", "Agent Engineering"]
}
```

因此 Normalizer 的原则是：

> **只标准化那些确实具有稳定 canonical representation 的字段。其余自然语言信息保留原义。**

---

## 10. Validator 保留

Normalizer 之后进入 Validator。

Validator 负责检查：

```text
字段是否合法
类型是否合法
取值范围是否合法
evidence 是否有效
confidence 是否有效
operation 是否允许
field 是否在 allowlist 中
```

示例：

```text
TOEFL = 900    → reject
GPA = -2       → reject
rank = "abc"   → reject
confidence = 3 → reject
```

同时检查：

```python
0 <= confidence <= 1
```

以及：

```python
evidence in original_message
```

对于 enum：

```text
statement_kind
operation
source
```

必须强制 schema 校验。

---

## 11. Conflict Resolver

Profile 更新前，需要将新事实与数据库已有事实比较。

例如数据库：

```text
TOEFL = 107
```

新输入：

```text
“我刚考到 110。”
```

得到：

```text
old = 107
new = 110
```

这时产生 conflict。

---

## 12. 冲突处理 UI：不再让用户下一轮输入 A/B

旧方案：

```text
Agent：
A. 更新为 110
B. 保留 107

User：
A
```

问题：

- 依赖下一轮上下文恢复
- 用户输入 `A/B` 本身没有语义
- 如果上下文没有正确恢复就会失败
- 交互体验较差

新方案：

> **冲突直接通过前端 Modal / Dialog 弹窗解决。**

例如：

```text
检测到信息冲突：

TOEFL

旧信息：
107

新信息：
110

[ 使用新信息 110 ]
[ 保留旧信息 107 ]
```

用户点击按钮后，前端直接提交：

```text
POST /api/profile/conflicts/{conflict_id}/resolve
```

请求：

```json
{
  "choice": "new"
}
```

或者：

```json
{
  "choice": "old"
}
```

后端：

```text
Conflict ID
    ↓
验证当前用户权限
    ↓
读取 old/new proposal
    ↓
用户选择
    ↓
Profile Update
    ↓
记录 change history
```

**不需要再调用 LLM，也不需要用户下一轮输入 A/B。**

---

## 13. ProfileResult 设计

建议：

```python
class ProfileResult(BaseModel):
    status: Literal[
        "complete",
        "partial",
        "needs_confirmation",
        "failed"
    ]

    extracted_facts: list[FactCandidate]

    accepted_facts: list[NormalizedFact]

    proposed_changes: list[ProfileChange]

    conflicts: list[ProfileConflict]

    preference_candidates: list[PreferenceCandidate]

    requires_confirmation: bool

    errors: list[str]
```

### 13.1 extracted_facts

本轮所有抽取出的事实，包括规则和 LLM。

### 13.2 accepted_facts

经过：

```text
Merge
→ Normalizer
→ Validator
```

后的合法事实。

### 13.3 proposed_changes

准备对 Profile 执行：

```text
ADD
UPDATE
REMOVE
```

的操作。

### 13.4 conflicts

例如：

```python
class ProfileConflict(BaseModel):
    conflict_id: str

    field: str

    old_value: Any
    new_value: Any

    old_source: str | None
    new_source: str

    new_evidence: str

    status: Literal[
        "pending",
        "resolved_new",
        "resolved_old"
    ]
```

这些 conflicts 由前端弹窗展示。

---

## 14. Profile Extraction Evaluation

完成上面的 Profile Agent 后，必须构造一个人工标注的 Gold Dataset。

建议第一版：

```text
300–500 条
```

至少覆盖：

```text
单一事实
多事实
中英文混合
纠正
否定
假设
不确定表达
Profile + Preference 混合
重复事实
冲突事实
自然语言科研经历
自然语言实习经历
复杂句
口语表达
错别字 / 不规范格式
```

---

## 15. Gold Dataset 格式

例如 JSONL：

```json
{
  "id": "profile_001",
  "input": "我是大三 CS，GPA 3.9，托福 107，不想考 GRE。",
  "expected": [
    {
      "field": "academic_year",
      "value": 3
    },
    {
      "field": "major",
      "value": "Computer Science"
    },
    {
      "field": "gpa",
      "value": 3.9
    },
    {
      "field": "toefl",
      "value": 107
    }
  ],
  "expected_preferences": [
    {
      "key": "avoid_gre",
      "value": true
    }
  ]
}
```

注意：

- expected 是人工标注正确答案
- Profile 和 Preference 分开评测

---

## 16. Precision / Recall / F1

对于 Profile fact extraction：

```text
TP = 正确提取的事实
FP = 模型提取了但不应该存在的事实
FN = Gold 中存在但模型漏掉的事实
```

计算：

```text
Precision = TP / (TP + FP)

Recall = TP / (TP + FN)

F1 = 2 * Precision * Recall / (Precision + Recall)
```

重点：

- `Recall` 反映“是否提取完整”
- `Precision` 反映“是否乱提取 / 幻觉”
- `F1` 综合两者

建议至少分别报告：

```text
Overall Fact Precision
Overall Fact Recall
Overall Fact F1

Rule-only F1
LLM-only F1
Rule + LLM F1
```

最好做 ablation：

```text
Baseline A：只有规则
Baseline B：只有 LLM
Final：规则 + LLM
```

这样能证明组合方案是否真的有效。

---

## 17. 当前 LLM “没有上下文记忆”的问题

当前现象：

```text
上一轮：
Assistant：请选择 A 或 B

下一轮：
User：A

LLM 无法理解 A 指什么
```

最可能的原因是：

> **当前每次 `invoke` 只传入了这一轮用户消息，没有恢复并重新注入最近 conversation context。**

LLM API 本身不会自动记住上一轮独立调用。

如果实际代码类似：

```python
agent.invoke(current_user_message)
```

第二轮模型可能只看到：

```text
User: A
```

自然无法知道之前发生了什么。

需要检查当前 `invoke / stream` 构造 prompt/context 的代码，确认是否真的把历史消息重新传入。

---

## 18. Conversation State 与其他 Memory 必须区分

系统中至少区分三种状态：

### 18.1 Profile

用户稳定事实，例如：

```text
GPA = 3.9
TOEFL = 110
Major = Computer Science
```

存 PostgreSQL 业务表。

### 18.2 Preference Memory

跨会话稳定偏好，例如：

```text
不考虑 GRE-required 项目
更看重就业
预算不超过 80,000 USD
```

存 PostgreSQL `memory_items`。

### 18.3 Conversation State

当前对话上下文，例如：

```text
最近聊了什么
上一轮 Assistant 说了什么
当前 conversation 的局部语境
```

这是解决当前 “指代 / 上下文断裂” 问题的关键。

Conversation State 不等于 Preference Memory。

---

## 19. ConversationContext

Orchestrator 每一轮调用 Router / Agent 前，都应该构造：

```python
class ConversationContext(BaseModel):
    recent_messages: list[Message]

    summary: str | None

    profile_summary: dict

    relevant_preferences: list
```

---

## 20. recent_messages

保存最近 N 条原始消息。

建议第一版：

```text
N = 8–12
```

具体通过 token budget 调整。

例如：

```text
User:
帮我对比 A 和 B。

Assistant:
...

User:
我更关心就业。

Assistant:
...

User:
那 A 呢？
```

LLM 必须能看到最近这些原始 turn。

实现：

```python
recent_messages = conversation_repository.get_recent_messages(
    conversation_id=conversation_id,
    limit=N,
)
```

然后按 role 注入：

```text
system
user
assistant
user
assistant
...
current user
```

---

## 21. 更早历史做 summary

不能随着 conversation 增长，把全部消息永久塞进 prompt。

推荐：

```text
Conversation
├── Recent N messages
└── Historical Summary
```

例如：

```text
前 100 条消息
    ↓
Conversation Summary

最后 10 条消息
    ↓
保持原文
```

最终 LLM 上下文：

```text
System Prompt

Conversation Summary:
...

Recent Messages:
...

Current User Message:
...
```

---

## 22. Summary 应保存什么

Summary 只保存当前对话需要的背景，例如：

```text
用户此前正在比较哪些学校
当前讨论的主题
已经做出的决定
尚未解决的问题
重要的对话指代背景
```

不要把 Profile / Preference 全部重复塞进去。

Profile 和 Preference 已经有独立来源。

---

## 23. profile_summary

从 Profile 表构造当前任务真正相关的摘要。

例如：

```json
{
  "major": "Computer Science",
  "gpa": 3.9,
  "toefl": 110,
  "research_summary": "GNN / network science research"
}
```

不要每轮把完整 Profile 全表 dump 给 LLM。

---

## 24. relevant_preferences

通过 Memory Service 根据当前 query 检索相关跨会话偏好。

例如：

```text
Current Query:
“再帮我找几个项目。”

Relevant Preferences:
- 不考虑 GRE-required 项目
- 更看重就业
```

只注入和当前任务相关的 preference。

---

## 25. Orchestrator 每轮 Context 准备流程

建议：

```text
User Query
    ↓
FastAPI
    ↓
Orchestrator
    ↓
Load Recent Messages
    ↓
Load Conversation Summary
    ↓
Load Profile Summary
    ↓
Retrieve Relevant Preferences
    ↓
Build ConversationContext
    ↓
Guard
    ↓
Router
    ↓
Domain Agents
```

即：

```python
conversation_context = ConversationContext(
    recent_messages=await conversation_repo.get_recent(...),
    summary=await conversation_repo.get_summary(...),
    profile_summary=await profile_service.get_summary(...),
    relevant_preferences=await memory_service.retrieve(...),
)
```

然后：

```python
decision = await router.route(
    query=query,
    conversation_context=conversation_context,
)
```

Domain Agent 如有需要，也接收裁剪后的相关 Context。

---

## 26. Summary 更新策略

不要每条消息都重新总结全部历史。

可采用：

```text
recent window 超过阈值
    ↓
把最旧的一部分 recent messages 合并到 summary
    ↓
保留最新 N 条原文
```

例如：

```text
保留 recent 10 条

当消息超过 14 条：
将最旧 4 条压缩进入 summary
仍保留最新 10 条原文
```

目标：

```text
固定 token 成本
+
最近上下文不损失
+
历史仍可恢复
```

---

## 27. 与 V2.2 Memory Service 的关系

注意不要把以下内容混为一谈：

```text
Conversation Summary
≠
Preference Memory
```

以及：

```text
recent_messages
≠
long-term memory
```

推荐数据来源：

```text
ConversationContext
├── recent_messages
│   └── conversations / messages
│
├── summary
│   └── conversation summary
│
├── profile_summary
│   └── profiles / profile_facts
│
└── relevant_preferences
    └── memory_items
```

---

## 28. Profile Agent 最终链路

最终建议实现：

```text
                  User Message
                        ↓
                ConversationContext
                        ↓
                 Profile Agent
                        ↓
              ┌──────────────────┐
              │ FastExtractor    │
              │ Regex / Rules    │
              └────────┬─────────┘
                       ↓
                 Known Facts
                       ↓
              ┌──────────────────┐
              │ LLM Extractor    │
              │ full message     │
              │ + known facts    │
              │ → structured JSON│
              └────────┬─────────┘
                       ↓
              Candidate Merge
                       ↓
                  Normalizer
                       ↓
                   Validator
                       ↓
               Conflict Resolver
                       ↓
             ┌─────────┴─────────┐
             │                   │
         no conflict          conflict
             │                   │
             ↓                   ↓
       Proposal Builder     Conflict Record
             │                   ↓
             │             Frontend Modal
             │             /           \
             │        choose new     choose old
             │             \           /
             └───────────────┬─────────┘
                             ↓
                      Profile Update
                             ↓
                      ProfileResult
```

---

## 29. 实施顺序

建议 Codex 按下面顺序实现，不要同时改所有模块。

### Phase 1 — Profile extraction pipeline

实现：

```text
FastExtractor
LLM Structured Extractor
Candidate Merge
Normalizer
Validator
ProfileResult schema
```

### Phase 2 — Conflict UI

实现：

```text
ProfileConflict model
Conflict API
Frontend Modal
resolve conflict endpoint
change history
```

不再使用聊天中的 A/B 选择。

### Phase 3 — Conversation Context

实现：

```text
ConversationContext
recent N messages
historical summary
profile summary
relevant preferences
```

并确认每次 Router / Agent invocation 都正确注入 context。

### Phase 4 — Summary compression

实现：

```text
rolling conversation summary
recent window
token budget
```

### Phase 5 — Profile Gold Dataset

人工构造：

```text
300–500 条
```

保存：

```text
input
expected structured JSON
expected preference JSON
```

### Phase 6 — Evaluation

计算：

```text
Precision
Recall
F1
Conflict Accuracy
Invalid Fact Rejection Rate
```

并对比：

```text
Rule Only
LLM Only
Rule + LLM
```

---

## 30. Codex 实现时必须遵守的边界

1. 不删除现有 evidence 机制。
2. 不允许 LLM 抽取结果直接写入数据库。
3. FastExtractor 已识别的 facts 与完整原文必须一起提供给 LLM。
4. LLM 必须输出严格结构化 JSON。
5. Normalizer 只处理真正可 canonicalize 的字段，不强制改写自然语言信息。
6. Validator 必须位于写入之前。
7. Profile conflict 不再通过下一轮聊天中的 A/B 文本处理。
8. 冲突必须通过前端明确选择 old/new。
9. Conversation Context 与 Preference Memory 分离。
10. 每次 LLM invocation 必须显式注入最近对话上下文。
11. 长历史使用 summary，最近 N 条保留原文。
12. Profile / Preference / Conversation State 三类数据必须保持职责边界。
13. 完成 Profile extraction 后必须建立人工 Gold Dataset，并报告 Precision / Recall / F1。
14. 不要一次提交同时重构全部 V2.2；按 Phase 逐步完成并加测试。

---

## 31. 最终验收标准

### Profile extraction

给定：

```text
“我是大三 CS，GPA 3.9，托福 107，做过 GNN 研究，不想考 GRE。”
```

系统能够：

- 规则稳定提取 GPA / TOEFL 等字段
- LLM 补充规则遗漏的自然语言信息
- 不重复 FastExtractor 已识别的 fact
- evidence 均可回指原文
- 输出合法 structured JSON
- Normalizer 正确标准化适合标准化的字段
- Validator 拦截非法值

### Conflict handling

已有：

```text
TOEFL = 107
```

用户输入：

```text
“我刚考到 110。”
```

前端出现：

```text
旧值：107
新值：110

[使用 110]
[保留 107]
```

用户点击后直接更新，不再通过聊天输入 `A/B`。

### Conversation context

上一轮：

```text
User: 帮我比较项目 A 和项目 B。
Assistant: ...
```

本轮：

```text
User: 那 A 的课程怎么样？
```

Router / Research Agent 必须能够理解 A 的指代。

### Long conversation

超过 recent window 后：

- 更早历史进入 summary
- 最近 N 条保持原文
- 不随对话无限增加 prompt token
- 仍可正确理解近期指代关系

### Evaluation

输出至少：

```text
Precision
Recall
F1
```

并比较：

```text
Rule Only
LLM Only
Rule + LLM
```

最终目标不是声称“完整抽取”，而是用测试数据量化：

> 规则 + LLM 的 Profile extraction 是否在保证 Precision 的同时提高 Recall 和 F1。
