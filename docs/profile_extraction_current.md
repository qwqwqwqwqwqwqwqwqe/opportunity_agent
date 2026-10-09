# Profile Extraction Current-State Audit

本文档记录 Task-001 开始时的用户画像抽取实现。Task-001 本身不改变业务行为。

## 调用入口与数据流

当前调用链为：

```text
CLI / Web / LifecycleTools
  -> LifecycleAgent.on_user_message(message)
  -> HybridFactExtractor.extract(message)
     -> ProfileExtractor.extract(message)              # 正则和关键词规则
     -> ModelScopeFactExtractor.extract(message)        # 同步 HTTP，规则未走快速路径时补充
  -> list[CandidateFact]
  -> apply_facts(profile, facts)
  -> derive_state(profile)
  -> next_profile_question / next_enrichment_question
  -> HybridRoadmapPlanner
```

所有画像抽取实现集中在 `opportunity_agent/profile.py`。岗位搜索中的关键词过滤位于
`repository.py`，岗位匹配规则位于 `matcher.py`，二者不属于用户画像抽取器。

## 当前 extractor

### ProfileExtractor

同步规则抽取器，混合了两类职责：

- 高确定性结构：本科年级、GPA、排名、毕业年份。
- 语义关键词：专业、国家、学位、方向、科研、语言准备、技能、职业目标和求职阶段。

### ModelScopeFactExtractor

通过 ModelScope OpenAI-compatible chat completions 接口请求结构化 JSON。模型输出经过
`CandidateFact` 的 Pydantic 校验；HTTP、超时、JSON 和校验错误会记录到 `last_error` 并返回空列表。
它只接收当前消息，不接收当前画像、UserState 或最近对话，因此还不是 Task-005 定义的
SemanticExtractor。

### HybridFactExtractor

规则结果优先，同字段的模型结果被丢弃。规则一次提取至少三个字段，或者命中 GPA、排名、年份、
语言、科研、技能、职业或求职阶段时，走 `rule_fast_path`，不会调用模型。该优化降低延迟，但也可能
遗漏同一句话中规则未识别的语义字段。

## 当前支持字段

| 字段 | 当前来源 | 当前标准化 | 主要消费者 |
|---|---|---|---|
| `academic_year` | `大一`～`大四` 正则、模型 | 1～4 整数 | StudentProfile、UserState |
| `major` | `CS`/`计算机` 关键词、模型 | `Computer Science` | StudentProfile、规划 |
| `target_degree` | `硕士`/`MS` 关键词、模型 | `MS` | StudentProfile、提问、规划 |
| `target_countries` | `美国`/`US` 关键词、模型 | `US` 列表 | StudentProfile、提问、规划 |
| `target_fields` | `AI`/`人工智能` 关键词、模型 | `AI` 列表 | StudentProfile、提问、规划 |
| `gpa` | 带 GPA/绩点标签的正则、模型 | 0～4 浮点数 | StudentProfile、规划 |
| `class_rank` | 排名/Rank 或 `5/120` 正则、模型 | 字符串 | StudentProfile、提问 |
| `graduation_year` | `20xx 毕业` 正则、模型 | 整数年份 | StudentProfile、规划 |
| `language_preparation` | 托福/雅思及否定关键词、模型 | 布尔值事实 | UserState、提问 |
| `research_activity` | 科研/研究/实验室/论文/项目关键词、模型 | 原句或 `none` | UserState、规划、提问 |
| `skills` | 固定技能关键词、模型 | 去重字符串列表 | StudentProfile、岗位推荐 |
| `career_goal` | AI Engineer/后端关键词、模型 | 英文规范值 | StudentProfile、UserState、岗位推荐 |
| `target_locations` | 美国/US 关键词、模型 | `US` 列表 | StudentProfile、岗位推荐 |
| `current_stage` | 找实习/找全职/秋招关键词、模型 | 阶段字符串 | StudentProfile、UserState、岗位推荐 |

`CandidateFact.source` 当前支持 conversation、model、resume、user_confirmed 和 system；事实通过
confidence 与 needs_confirmation 控制是否写入 StudentProfile。所有事实仍保存在 `profile.facts` 供审计、
状态推导、规划 Prompt、Web 快照和前端展示使用。

## 已知失败和误判类型

- 口语和非词典专业：如“我是软工的”不会被规则识别。
- 多目标和地区表达：如“北美”“美国加拿大都可以”不能被规则完整抽取。
- 否定和修正：如“不考虑美国了”会命中“美国”，但当前规则不会生成 remove 操作。
- 不确定表达：只有部分国家规则处理“可能”，其他字段可能被当成确定事实。
- 无标签短回答：如回答“103”时，抽取器不知道上一轮在询问 TOEFL。
- 多轮冲突：没有来源优先级、旧值历史或确认流程，后值可能直接覆盖前值。
- 规则快速路径：命中部分规则后可能跳过模型，遗漏同句中的其他语义。
- 模型单轮上下文：ModelScopeFactExtractor 不知道已有画像和最近问答，可能重复提问或误解短回答。
- 粗粒度状态：language/research 主要依据事实是否存在，尚未消费统一 StageSignal。
- 词内子串：`us`、`ms` 等短英文关键词存在误命中的可能。

## Task-002～Task-004 迁移方案

```text
FastExtractor
  高确定性数字、分数、年份、排名、预算、日期
        +
LegacySemanticRuleExtractor
  原有专业/国家/方向/职业等关键词；只保留，不扩词
        +
ModelScopeFactExtractor
  当前同步模型补充和 fallback
        -> ExtractionResult
        -> CandidateFact / StageSignal / intent / information needs
```

为保持 MVP，`ProfileExtractor` 将保留为旧语义规则的兼容名称，`HybridFactExtractor.extract()` 继续返回
`list[CandidateFact]`；LifecycleAgent 改用新的 `extract_result()`。Task-005 再用带画像和最近消息的异步
SemanticExtractor 替换 legacy 语义职责。

## 本轮边界

Task-001～Task-004 不实现上下文语义抽取、LLM JSON 重试、Profile Normalizer、Conflict Resolver、动态问题策略
或 StageSignal 到 UserState 的融合。上述能力分别属于 Task-005 及后续任务。
