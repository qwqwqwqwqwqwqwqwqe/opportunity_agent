# Personal Opportunity Awareness Agent：项目结构与实现说明

> 本文以当前仓库代码为准，说明一个本地可运行的留学规划 Agent 如何从用户输入、简历、进展和官网资料中形成可解释的画像、时间轴、岗位建议与规划文章。它同时记录实现中形成的重要边界：**模型负责理解、编排和表达；规则、验证器和用户确认负责决定事实、日期与状态。**

## 1. 项目定位与范围

这个项目不是“把一个通用聊天模型包进网页”。它的核心是把不稳定的自然语言输入转为可追溯的状态，再用该状态驱动规划：

```text
聊天 / 信息表 / 简历 / 任务按钮 / 官网查询
                 │
                 ▼
      CandidateFact、ProgressUpdate、证据
                 │
                 ▼
   Normalizer + ConflictResolver + StateTransitionEngine
                 │
                 ▼
 StudentProfile + UserState + TaskProgress + Timeline
                 │
        ┌────────┴─────────┐
        ▼                  ▼
 确定性任务、岗位推荐    GPT 个性化文章 / 咨询回答
        │                  │
        └────────┬─────────┘
                 ▼
          Web 快照、聊天与引用
```

当前详细规划的专业范围刻意限制在计算机、软件、数据、AI/ML、电子信息/通信、微电子、自动化/控制、机器人、嵌入式和信号处理等相近工科。非该范围的画像仍可保存，但不会强行生成看似专业、实则无根据的课程时间轴。域判断的种子知识在 [`opportunity_agent/domain_knowledge.py`](../opportunity_agent/domain_knowledge.py)，时间轴入口在 [`opportunity_agent/timeline.py:10`](../opportunity_agent/timeline.py:10)。

第一版的“岗位机会感知”仍是本地数据 Demo：岗位来自 [`data/jobs.json`](../data/jobs.json)，不意味着实时招聘，也不执行投递。原计划里讨论过的论坛、爬虫池、PostgreSQL/pgvector、账号系统和跨会话社区 RAG **没有纳入当前版本**；不能将其当作已交付功能。

## 2. 关键设计决策：为什么不是纯 LLM

早期版本曾出现几类典型问题：模型超时使资料丢失、用户提供 GPA 后仍被重复追问、时间一过就被误标为“完成”、规划忽略简历课程、官网资料被模型凭记忆补全。当前设计对应地采用以下原则。

| 问题 | 设计选择 | 实现结果 |
|---|---|---|
| 数值、日期、成绩格式明确 | 快速规则优先 | GPA、TOEFL、GRE、毕业年等即使 LLM 不可用也可保存 |
| 自由表达含指代或不确定语气 | LLM 语义抽取 + Pydantic 校验 | “我可能去加拿大”“这个项目做完了”不被当成无条件事实 |
| 新信息与旧画像冲突 | 冲突解析和确认卡 | 不会静默覆盖用户明确填写的数据 |
| 日期已过去 | 时间位置与执行进度分离 | 历史节点显示“待补录”，而不是自动完成 |
| 规划文章失败或过短 | 确定性时间轴/技能计划兜底 | 页面仍有任务可执行，旧文章不被坏结果覆盖 |
| 简历信息可能有误 | 先形成草稿，后由用户确认 | 未确认经历不会直接变成科研完成证据 |
| 学校要求容易过时/被编造 | 受限官网工具、来源卡与缓存 | 未查到仍是“待核验”，不能推断成“不要求” |

`plan.md` 中的重构思路由此落地为“FastExtractor + Legacy Rule + SemanticExtractor”“事实/状态/文章分层”以及“工具只能读，状态更新须经业务层”的组合，而不是一次性把全部决策交给 GPT。

## 3. 目录与运行入口

```text
examples/opportunity_agent/
├─ opportunity_agent/       # Python 业务核心与 HTTP 服务
├─ web/                     # 无构建步骤的原生 HTML/CSS/JS 界面
├─ data/                    # 本地 JSON 数据与可配置种子库
├─ docs/                    # 架构、简历、状态追踪等说明
├─ tests/                   # 单元、HTTP、存储与浏览器 smoke 测试
├─ plan.md                  # 第二版演进任务清单
├─ README.md                # 安装、运行、环境变量说明
└─ .env.example             # 不含真实密钥的配置样例
```

两个常用入口：

- [`opportunity_agent/main.py`](../opportunity_agent/main.py)：CLI Demo。
- [`opportunity_agent/web_app.py:142`](../opportunity_agent/web_app.py:142)：以 `ThreadingHTTPServer` 启动本机 Web 服务，默认监听 `127.0.0.1:8766`。

Web 端刻意没有引入 React、数据库或后端队列：该项目当前是个人展示型本机 Demo，原生页面和 JSON 持久化降低了运行门槛。正式多用户部署时，这也是最需要替换的部分。

## 4. 核心数据模型：先定义可审计的状态

数据模型集中在 [`opportunity_agent/models.py`](../opportunity_agent/models.py)。Pydantic 在这里不只是类型提示：它是模型输出、表单、工具参数和持久化快照之间的边界验证器。

### 4.1 CandidateFact：事实而不是一句模型结论

[`CandidateFact`](../opportunity_agent/models.py:63) 同时保留：

```python
class CandidateFact(BaseModel):
    field: str
    raw_value: Any
    normalized_value: Any | None
    confidence: float
    source: str
    evidence: str | None
    operation: Literal["set", "append", "remove"]
    needs_confirmation: bool
```

这样做解决了“原文”和“内部标准值”不可逆混在一起的问题。例如用户输入“绩点 3.9，5/120”：`raw_value` 保留原句上下文，`normalized_value` 可保存数值，`evidence` 能回到输入来源；`value` 属性继续映射到规范值以兼容早期代码。`set/append/remove` 允许“改目标国家”“追加课程”“不考虑美国”等不同操作，而不是一律覆盖。

### 4.2 画像、状态、进展与证据

- [`StudentProfile`](../opportunity_agent/models.py:336)：稳定画像，包含院校、专业、年级、目标、成绩、课程、技能、经历、预算和考试计划；`facts` 与 `change_history` 使画像可追溯。
- [`ExtractionResult`](../opportunity_agent/models.py:151)：一次输入的统一结果，可同时有意图、事实、阶段信号、待补信息和 `ProgressUpdate`。
- [`UserState`](../opportunity_agent/models.py:398)：学术、语言、科研、申请、求职等并行状态，避免把用户压扁为单一“阶段”。
- [`TaskProgress`](../opportunity_agent/models.py:184)、[`StateTransition`](../opportunity_agent/models.py:198)、[`StageEvidence`](../opportunity_agent/models.py:210)、[`StageAssessment`](../opportunity_agent/models.py:225)：记录执行状态、前后变化、证据原文和评估结果。
- [`Roadmap`](../opportunity_agent/models.py:542)：将确定性 `PlanningTimeline`、文章、里程碑、生成模式，以及官网 `official_sources` / `verified_requirements` / `unresolved_requirements` 放进同一版本化对象。

这种拆分的直接收益是：文章可以重写，任务进度不丢；时间轴可以重算，用户完成记录可按稳定身份继承；官网查询可以更新证据，不能暗中改画像。

## 5. 输入理解与画像更新

### 5.1 三层抽取链路

入口是 [`HybridFactExtractor.extract_result()`](../opportunity_agent/profile.py:377)：

```text
FastExtractor
  → LegacySemanticRuleExtractor
  → SemanticExtractor（配置 LLM 时）
  → Normalizer / 冲突处理
```

1. [`FastExtractor`](../opportunity_agent/profile.py:18) 只处理高确定性格式：年级、GPA、TOEFL、IELTS、GRE、毕业年份、排名、预算和明确日期。它会做范围校验，不能确定就不猜测。这保证“2027”“3.9，5/120”不必等待模型。
2. [`LegacySemanticRuleExtractor`](../opportunity_agent/profile.py:125) 保存第一版已有的专业、国家、方向、职业等明确关键词能力。保留它是兼容策略，不把旧 MVP 的可用输入因重构突然变成不可识别。
3. [`SemanticExtractor`](../opportunity_agent/semantic_extractor.py:36) 处理自由表述、否定、语气和上下文。它向模型提供当前 Profile、State、最近消息和 Pydantic schema；JSON 先解析再验证，格式错误允许修复一次，失败回退规则路径。关键入口是 [`extract_sync()`](../opportunity_agent/semantic_extractor.py:78) 和 [`_parse_and_validate()`](../opportunity_agent/semantic_extractor.py:173)。

`HybridFactExtractor` 不会因为快速规则抓到一个分数就跳过剩余语义。这是为了解决“我托福考了 105，接下来做什么”既要保存成绩又要回答问题的混合输入。

### 5.2 规范化与冲突解决

[`ProfileNormalizer`](../opportunity_agent/normalizer.py:8) 将可等价的表达转为统一表示，但不把“北美”武断缩成“美国”。之后 [`ProfileConflictResolver.resolve()`](../opportunity_agent/conflict_resolver.py:47) 按来源优先级、置信度、操作类型和当前值决定：应用、保留或创建 `PendingConfirmation`。

实现重点是：LLM 输出永远先是候选事实，而不是数据库写入指令。用户表单、用户确认和明确陈述有较高权重；低置信度、冲突、目标不唯一的项进入确认，不静默覆盖。

### 5.3 生命周期 Agent 如何编排

[`LifecycleAgent.process_user_message()`](../opportunity_agent/lifecycle_agent.py:306) 是主要业务入口。其逻辑可概括为：

```python
event = begin_event(message)          # 先落盘，模型失败也不丢原文
result = extractor.extract_result(...) # 抽取事实与进展
profile = resolver.resolve(profile, result.facts)
apply_progress(result.progress_updates)
collect_stage_evidence(...)
refresh_state()
refresh_plan_if_needed()
reply = advisor.answer(...) or deterministic_summary
finish(event, reply)                  # 保存 assistant 回复与结果快照
```

[`begin_event()`](../opportunity_agent/lifecycle_agent.py:65) 在模型调用前创建带 `request_id` 的 `UserEvent` 和 `ChatMessage`；[`_finish()`](../opportunity_agent/lifecycle_agent.py:83) 再标记处理完成并形成 [`AgentTurnResult`](../opportunity_agent/models.py:243)。这避免了网络超时后“用户消息消失”的问题。

## 6. 状态转换与时间轴：时间不是完成证据

### 6.1 确定性时间轴

[`build_timeline_skeleton()`](../opportunity_agent/timeline.py:10) 根据毕业年月、入学年月、年级和考试日期生成背景提升、寒暑假、材料、网申、Offer/签证、入学等阶段及事件。日期属于确定性引擎：LLM 可以解释任务，不能移动边界。

时间轴项有三个维度：

| 维度 | 例子 | 含义 |
|---|---|---|
| 时间位置 | `history/current/upcoming` | 今天位于哪一段 |
| 执行进度 | `planned/in_progress/completed/cancelled` | 用户实际上做到了什么 |
| 风险 | `needs_backfill/overdue/ahead_of_schedule/schedule_conflict` | 日期与执行信息的关系 |

因此未来项目提前完成会显示“已完成·提前完成”，但紫色当前位置不会离开今天所在的暑期节点；历史节点没有证据会是“历史·待补录”，而不会自动完成。

### 6.2 统一状态转换引擎

[`StateTransitionEngine`](../opportunity_agent/state_transition.py:47) 集中处理进展操作、证据收集与多维状态评估：

- [`decide_progress()`](../opportunity_agent/state_transition.py:52)：任务按钮、明确陈述和确认结果优先；目标不唯一时要求确认。
- [`collect_evidence()`](../opportunity_agent/state_transition.py:92)：把任务操作、明确事实和弱阶段信号保存为来源不同的证据。
- [`assess()`](../opportunity_agent/state_transition.py:129)：根据可用证据而非日历推导状态；弱对话兴趣信号会衰减，用户明确事实不衰减。

[`LifecycleAgent.on_progress()`](../opportunity_agent/lifecycle_agent.py:423) 处理“开始、完成、延期、取消、重置”按钮；[`on_timeline_fact()`](../opportunity_agent/lifecycle_agent.py:439) 处理历史节点补录，并把补录内容变成明确 CandidateFact。普通进展只更新状态并提示“文章待重新生成”，不会每次都调用规划 LLM。

## 7. 规划系统：稳定骨架 + 可替换文章

### 7.1 Engineering Knowledge 与 Planning Skills

[`domain_knowledge.py`](../opportunity_agent/domain_knowledge.py) 是内部种子知识库，提供各工科域的核心课程、前置课、项目方向、科研/竞赛方向和岗位关键词。它不是学校官网数据库，故不能输出具体校方门槛。

[`planning_skills.py`](../opportunity_agent/planning_skills.py) 定义统一接口：

```python
PlanningSkill.generate(context) -> SkillPlanResult
```

实现包括：

- [`AcademicEnhancementSkill`](../opportunity_agent/planning_skills.py:91)：课程优先级、GPA、前置能力。
- [`LanguageExamSkill`](../opportunity_agent/planning_skills.py:110)：已有成绩和考试日期的安排；不默认建议重考。
- [`ResearchSummerSkill`](../opportunity_agent/planning_skills.py:136)：暑研、导师筛选与套磁产出。
- [`EngineeringInternshipSkill`](../opportunity_agent/planning_skills.py:153)：结合专业域和本地岗位给出实习方向。
- [`ApplicationMaterialsSkill`](../opportunity_agent/planning_skills.py:169)：CV、SOP、推荐信与网申清单。
- [`OfferVisaSkill`](../opportunity_agent/planning_skills.py:188)：Offer、签证、住宿与行前。
- [`TimelineComposerSkill`](../opportunity_agent/planning_skills.py:203)：合并、去重、校验任务依赖与日期边界。

每个 Skill 都有规则 fallback。即使某一组件或最终文章调用超时，`PlanningTimeline` 仍可生成、展示并接受进展更新。

### 7.2 文章生成与失败保护

[`build_roadmap()`](../opportunity_agent/planning.py:64) 总是先生成离线 Roadmap；[`ModelScopeRoadmapPlanner.generate()`](../opportunity_agent/planning.py:123) 在模型可用时再生成详细中文文章。最终 Prompt 位于同文件开头的 `ROADMAP_USER_PROMPT_TEMPLATE`，其中显式放入：已确认画像、事实审计、状态、确定性时间轴、当前文章版本、官网证据与未解决项。

文章不会影响任务和日期。其失败保护包括：

1. 输出少于质量下限时重试一次；见 [`_validate_planning_article()`](../opportunity_agent/planning.py:233)。
2. 两次仍过短、超时或连接失败时保留规则文章或已有文章；错误由 [`planning_error_details()`](../opportunity_agent/planning.py:240) 转为可操作中文提示。
3. 资料/进展变化后，`_refresh_plan()` 仅重建确定性结构并复制旧文章，设置 `replan_required=True`；见 [`LifecycleAgent._refresh_plan()`](../opportunity_agent/lifecycle_agent.py:147)。用户点击“重新规划”才替换文章。

该策略源于实际问题：如果用户每说一句“科研开始了”就重写长文章，网络慢、文章重复且容易覆盖真实进展。

## 8. 简历导入：解析、草稿、确认三段式

简历功能的详细协议见 [`docs/resume_import.md`](resume_import.md)。架构为：

```text
上传 PDF/DOC/DOCX
 → 格式/大小/宏/加密校验
 → 本地文本块读取（必要时云端 OCR 授权）
 → 规则抽取 + LLM 结构化抽取
 → ResumeDraft（可编辑、带证据）
 → 用户确认
 → CandidateFact + ProfileConflictResolver
```

- [`resume_parsers.py`](../opportunity_agent/resume_parsers.py)：校验真实格式、PDF/DOCX 本地读取、脱敏常见联系方式。
- [`resume_worker.py`](../opportunity_agent/resume_worker.py)：隔离解析子进程与超时控制。
- [`resume_mineru.py`](../opportunity_agent/resume_mineru.py)：扫描件/旧 DOC 的可选 MinerU 云端流程；只有用户授权才上传原件。
- [`ResumeExtractionSkill`](../opportunity_agent/resume_extraction.py:129)：按资料与经历分段调用 LLM，Pydantic 校验每行证据定位；LLM 超时时保留规则已提取课程、段落与原文，而不是把草稿清空。
- [`ResumeImportService`](../opportunity_agent/resume_service.py:30)：后台任务、临时文件清理、草稿保存、重试、重新解析和确认。
- [`LifecycleAgent.on_resume_confirmation()`](../opportunity_agent/lifecycle_agent.py:164)：将用户选中的字段统一送进 Normalizer/ConflictResolver；课程做去重合并，科研/项目/实习按完整条目 append，不用逗号切碎。

这是“LLM 仅提出候选，用户确认才进入正式画像”的最典型案例。原件仅临时保存；确认后保存结构化结果和证据片段，不把完整简历放到浏览器 localStorage 或普通日志中。

## 9. 岗位推荐与机会通知

岗位推荐仍保持确定性，以便分数和通知可解释、可测试：

- [`matcher.match_job_to_user()`](../opportunity_agent/matcher.py:27)：对较早 V1 `UserProfile`/`Job` 兼容模型计算技能、目标、地点、公司偏好、阶段等匹配。
- [`career.recommend_jobs()`](../opportunity_agent/career.py:10)：面向当前 `StudentProfile`，只有状态进入实习/全职求职时才读取本地仓库岗位；输出匹配技能、缺失技能、理由、分数和置信度。
- [`recommendation.py`](../opportunity_agent/recommendation.py) 与 [`repository.py`](../opportunity_agent/repository.py)：通知决定和数据读取。

这样 LLM 写得再流畅，也不会改变“是否通知”的确定性结论；模型只可解释推荐或组织对话。

## 10. 官网研究与 GPT Tool Calling

### 10.1 为什么需要受限工具

学校 GRE 政策、申请材料、SOP/小 Essay、字数与截止日期是易变化事实。让模型凭记忆回答会产生看似可信但不可验证的内容，因此官网研究使用只读、白名单工具。

相关数据模型为 [`OfficialSource`](../opportunity_agent/models.py:512)、[`OfficialRequirement`](../opportunity_agent/models.py:527)、[`OfficialResearchResult`](../opportunity_agent/models.py:535)。域名注册表为 [`data/university_domains.json`](../data/university_domains.json)，缓存为 [`data/official_cache.json`](../data/official_cache.json)。

工具定义和执行在 [`official_research.py`](../opportunity_agent/official_research.py)：

| Tool | 作用 | 关键安全约束 |
|---|---|---|
| `resolve_official_domain` | 将 CMU/UIUC 等别名映射为已验证域名 | 未登记学校只能返回 unknown |
| `search_official_program_pages` | Tavily 搜索校方域内项目页面 | `include_domains` 固定为验证域名、最多 5 条 |
| `read_official_program_page` | 读取候选官网页并提炼短证据 | HTTPS、跳转后重验域名、阻止 localhost/私网、限制页面大小 |
| `get_cached_official_requirements` | 读取结构化缓存 | 过期来源标记 stale，不伪造新鲜度 |

`OfficialResearchTools.call()`（[`official_research.py:190`](../opportunity_agent/official_research.py:190)）先用 Pydantic 验证工具参数，模型不可能调用任意 Python 函数。`_assert_public_host()`（[`official_research.py:351`](../opportunity_agent/official_research.py:351)）防止 SSRF。页面内容是“不可信数据”，只被当作证据文本，不执行其中任何指令。

### 10.2 Tool Calling 与规划的不同编排

[`LLMClient.complete_message()`](../opportunity_agent/llm_client.py:57) 支持 OpenAI 兼容 `tool_calls`；[`LLMToolRunner.run()`](../opportunity_agent/tool_runner.py:38) 实现：模型响应 tool call → 本地白名单执行 → 将 `tool` 消息回传模型 → 获得最终回答。若网关以 400/422 拒绝原生工具调用，会退到严格 JSON 协议，仍经过同一参数校验。

自由咨询（例如“CMU SCS 的 GRE 政策是什么？”）由 [`AdviceResponder.answer()`](../opportunity_agent/advisor.py:22) 判断是否为官网问题后走 `LLMToolRunner`。普通聊天、事实更新、任务进展不调用官网工具。

规划前的官网采集则不同。规划任务有固定且必须覆盖所有目标学校的清单：GRE 政策、英语考试政策、截止日期、材料清单、SOP/Essay/字数。若把这件事完全交给单轮工具调用，模型的调用上限可能只查到 CMU 而漏掉 UIUC。故 [`ModelScopeRoadmapPlanner._research()`](../opportunity_agent/planning.py:201) 对每所 `target_schools` 依次调用 [`deterministic_program_research()`](../opportunity_agent/official_research.py:277)。它仍只使用同一组安全工具，但会：

1. 优先排序 `Application Guidelines` / `Application Requirements` / `Graduate Admissions`，降低 FAQ 权重。
2. 先做混合招生查询，再为仍未命中的材料和文书项单独进行域内定向搜索。
3. 未查到时保留 `unresolved_questions`，绝不把“未提及”写成“不要求”。

查询结果先展示在右侧“官网依据”，默认折叠全文证据；用户可点击源链接。点击“重新查询官网”只刷新来源与结构化要求；点击“重新规划”才把它们写入文章。文章 Prompt 强制对每所已有官网来源的目标学校分别覆盖，且使用 `（来源：source_id）`；前端把 source id 渲染成可点击官网链接。这样一次查询不会静默重写文章，也不会因重规划后的搜索波动丢弃已确认的 UIUC 证据。

## 11. LLM 通信、超时和降级

所有 OpenAI-compatible 调用经过 [`LLMClient`](../opportunity_agent/llm_client.py:31)：

- [`generate()`](../opportunity_agent/llm_client.py:87)：普通文本生成。
- [`generate_structured()`](../opportunity_agent/llm_client.py:108)：带 schema 的结构化生成，解析失败可修复/重试。
- [`complete_message()`](../opportunity_agent/llm_client.py:57)：保留 assistant message 与 tool calls。
- [`modelscope_transport.py`](../opportunity_agent/modelscope_transport.py)：兼容部分网络/TLS 环境。

`.env` 由 [`config.py`](../opportunity_agent/config.py) 读取；shell 环境变量优先，且不会把 Key 打到响应、日志或文档里。关键变量：

```dotenv
LLM_API_KEY=...
LLM_API_BASE=https://yibuapi.com/v1
LLM_MODEL=gpt-5.5
TAVILY_API_KEY=...
OFFICIAL_SEARCH_ENABLED=1
MINERU_API_TOKEN=...               # 仅简历增强识别
```

实际问题表明不能把文章、聊天、工具、简历全设成同一个长超时：官网工具单次模型请求使用更短限制，规划文章可使用较长预算，简历分段独立保存。失败原因会通过 Web 快照显示为认证、连接、超时、文章过短或规则降级，而不把底层完整响应泄露给用户。

## 12. HTTP、会话持久化与跨浏览器一致性

### 12.1 服务端是权威来源

[`ConversationStore`](../opportunity_agent/conversation_store.py:22) 用 `data/conversations.json` 持久化会话，并以会话锁、revision 乐观锁、删除墓碑和原子替换写文件维护一致性。浏览器 localStorage 只保留 UI 缓存，不能覆盖服务端正式状态。

[`SessionService.execute()`](../opportunity_agent/session_service.py:76) 是所有状态写接口的统一入口：

```text
验证 path/payload
→ request_id 幂等与 payload fingerprint
→ 先保存 received event
→ 锁外执行 LLM/解析等慢操作
→ 检查 revision 是否仍一致
→ 原子提交快照，或标记失败事件
```

因此两个浏览器同时操作同一会话时，迟到的模型结果不能覆盖较新的资料；删除会话后，旧浏览器缓存也不能重新创建它。

`web_app.py` 提供 `/api/chat`、`/api/onboarding`、`/api/progress`、`/api/timeline-update`、`/api/roadmap/enrich`、`/api/roadmap/replan`、`/api/official/research`、确认接口和简历接口。完整分发逻辑见 [`OpportunityWebHandler.do_POST()`](../opportunity_agent/web_app.py:77)。

### 12.2 前端职责

[`web/index.html`](../web/index.html) 是布局和渲染主文件：聊天、资料表、横/纵自适应时间轴、右侧画像/事实/路线图、文章放大层、官网来源折叠卡。

[`web/progress_ui.js`](../web/progress_ui.js) 负责状态 mutation、请求加载动画、任务按钮、时间轴补录、确认卡、会话同步和错误恢复。`mutateConversation()` 使用 request_id 发请求，失败时从服务端 reload，而不是把客户端猜测状态当真。

[`web/resume_ui.js`](../web/resume_ui.js) 与 [`web/resume.css`](../web/resume.css) 负责上传、轮询、草稿审核、证据展开和确认。前端不直接调用 LLM，也不保存 API Key、完整简历或官网全文。

## 13. openJiuwen 接入位置

项目核心可以独立运行；openJiuwen 是可选运行时适配，而不是画像和规划业务逻辑的唯一依赖。

[`LifecycleTools`](../opportunity_agent/lifecycle_tools.py:11) 把 `LifecycleAgent` 的安全业务入口封装成 JSON Tool：处理消息、读取状态、生成路线图、岗位推荐、外部事件和官网工具。[`register_rust_lifecycle_tools()`](../opportunity_agent/lifecycle_tools.py:54) 将这些 ToolCard/LocalFunction 注册到 `Runner.resource_mgr`。

这层设计的原因是：无论 ReAct Agent、Web API 还是 CLI 调用，最终都必须经过同一套事实验证、冲突解析、状态转换与时间轴校验；不能允许某个 Agent 绕过它们直接改 Profile 或 Roadmap。

## 14. 测试策略与验证入口

测试按风险层次拆分：

| 范围 | 代表测试 |
|---|---|
| 事实兼容、规范化、冲突 | `test_candidate_fact.py`、`test_profile_normalizer.py`、`test_conflict_resolver.py` |
| 规则/语义抽取 | `test_fast_extractor.py`、`test_semantic_extractor.py`、`test_extraction_result.py` |
| 时间轴和进展 | `test_engineering_timeline.py`、`test_progress_tracking.py`、`test_contextual_state_transitions.py` |
| 文章和 LLM 失败保护 | `test_llm_config_and_async_onboarding.py`、`test_roadmap_llm_limits.py` |
| 简历安全与恢复 | `test_resume_parsing_extraction.py`、`test_resume_service.py`、`test_resume_http.py` |
| 官网工具 | `test_official_research.py` |
| HTTP/会话/浏览器 | `test_web_app.py`、`test_web_conversations_api.py`、`ui_progress_smoke.cjs`、`ui_resume_smoke.cjs` |

常规回归：

```powershell
python -m pytest
```

真实 LLM、MinerU、Tavily 测试必须显式用 `RUN_*_INTEGRATION=1` 开启，并只应使用合成资料；默认单元测试通过 mock 验证协议、超时、缓存、权限和降级逻辑，不消耗用户 Key。

## 15. 当前边界与下一步建议

1. **本地单进程存储**：JSON + `ThreadingHTTPServer` 适合本机展示，不适合多进程、多用户或公网。若进入部署，应先换成数据库、鉴权、服务端 session、任务队列和对象存储。
2. **学校注册表有限**：未知学校不会被任意搜索结果冒充官方，安全但需要维护 [`university_domains.json`](../data/university_domains.json)。可增加审核过的别名/域名，而不是放宽白名单。
3. **官网内容并非全结构化**：有些文书字数在 PDF、申请系统或登录后页面。公开官网没有明确证据时必须保留待核验；不应为“体验完整”而编造。
4. **岗位数据是演示数据**：下一阶段若接外部职位源，需要复用当前的来源、证据、确认和更新边界，不能让网页信息直接改变用户事实。
5. **社区/RAG是后续独立阶段**：若实现论坛，应把社区经验与官方事实分通道。社区帖子只能用于经验建议和相似案例，不能作为用户 GPA、经历完成或官方申请政策的证明。

---

相关专项说明：

- [画像抽取审计](profile_extraction_current.md)
- [工科时间轴架构](engineering_timeline_architecture.md)
- [进展追踪说明](progress_tracking.md)
- [简历导入说明](resume_import.md)
