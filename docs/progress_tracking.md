# 用户进展感知与时间轴状态更新

## 本轮范围与复用

落实 plan.md Task-018 的阶段信号使用、Task-020 的状态证据日志，以及 Task-016/017/019 中咨询回复、停止追问、对话与任务行为感知的相关部分。没有实现完整动态提问评分、浏览/搜索行为追踪、RAG、外部监控或跨对话画像合并。

继续复用 FastExtractor、LegacySemanticRuleExtractor、SemanticExtractor、ProfileNormalizer、ConflictResolver、LLMClient、Planning Skills、时间轴日期引擎和 ConversationStore。新增的是输入日志、进度记录、状态投影和服务端提交协调，不是另一个规划 Runtime；不修改 Rust Core，不增加应用第三方依赖。

## 处理链路

```text
HTTP 输入 → 参数校验 → 会话锁内保存原始消息与 UserEvent(received)
         → 锁外在快照上抽取 / 咨询 / 手动规划
         → Normalizer + ConflictResolver / 确认门槛
         → TaskProgress + derive_state + 时间轴状态投影
         → 会话锁内检查版本并提交 → 返回实际变化与下一步
```

同一会话的写入串行；慢模型调用不持有会话锁。若等待期间出现新输入或删除，旧结果不提交。发生冲突时原输入标记失败并保留，客户端刷新后可重新提交；不擅自重放一项可能已失效的操作。

## 数据与存储

服务端 `data/conversations.json` 为权威来源，格式升级为 schema_version=2。保留原对话字段，同时增加：

| 记录 | 用途 |
| --- | --- |
| ChatMessage | 原始消息、稳定消息 ID、接收时间、处理状态；保留 role/content |
| UserEvent | 请求 ID、输入来源、关联消息、抽取结果、回复、失败原因、请求指纹 |
| TaskProgress | 稳定任务/事件 ID、执行状态、实际日期、延期日期及证据 |
| StateTransition | 字段变化前后值、原因、证据、触发事件与置信度 |
| PendingConfirmation | 待确认事实或进展、候选目标、接受/拒绝状态 |
| state_revision | 服务端提交版本；拒绝迟到快照覆盖 |
| deleted | 已删除对话 ID 标记，阻止旧缓存及迟到请求复活对话 |

每次写入使用临时文件、flush/fsync 和原子替换。旧格式第一次写入前保留 `.v1.bak`；损坏/不可读的文件会明确报错，不会当成空数据库覆盖。

旧消息迁移时补 ID，但 `created_at=null`，不伪造原始发送时间；不调用模型重新分析历史。旧浏览器缓存只允许导入服务端从未存在且未删除的会话，不能覆盖现有服务端进展。

本轮不做多进程锁或登录鉴权。请保持一个 Python 服务进程且仅绑定本机地址；备份文件同样含个人资料。无需删除旧聊天来启用新版。

## 任务身份与日期

任务采用 `phase:skill:purpose:goal_hash`；哈希来自确定性专业域、目标方向、学校/项目和学位等目标信息。生成模型必须沿用种子任务 ID，不能生成执行状态或身份。

旧 ID 显式映射，例如 `research_progress → background_research_map`、`submit_application → application_materials`。不按标题相似度转移完成记录。目标实质变化或语言任务从“备考”转为“核验已有分数”使用新身份；旧进度保留为历史，但不投影到新任务。

时间轴分别显示：

- time_status：history/current/upcoming，完全由日期决定。
- execution_status：planned/in_progress/completed/cancelled，来自明确用户证据。
- is_overdue：日期已到且未记录完成/取消，表示风险，不表示用户实际没做。

时间轴视觉上，历史阶段和节点以灰色展示“已完成（待补录）”：这表示该**日历时间**已经过去，并不把用户的任务执行状态写成完成。点击任一历史节点会打开补录表，可选择科研、实习、项目或已修课程，填写实际日期与证据；内容以 `user_explicit` 的明确事实写入画像，随后询问是否手动重新生成文章。暑假节点的显示日期仍为 7 月 15 日，但有效时间为 7 月 1 日至 8 月 31 日；寒假为 1 月 1 日至 2 月末。因此在 8 月 31 日，紫色圆点优先落在暑假节点，而不是覆盖它的背景提升阶段。紫色圆点只标记一个当前项目：活跃寒暑假优先，否则在重叠阶段中选择开始日期最新者。

延期保持用户日期，越界标记 boundary_conflict，不移动别的阶段。重置恢复原计划日期；原延期仍在 StateTransition 中可追溯。已参加考试、已出分和满足官网要求互不等价；`language_evidence` 对旧 `language` 字段提供更精确的兼容补充。没有官网证据时不会自动宣称 requirements_verified。

刷新、重新聚焦和跨日只重新投影日期状态，不调用模型、不重写文章。普通任务操作也不重建文章；画像更改可更新确定性任务内容，旧文章标记待更新。手动重规划 Prompt 包含当前任务进度、证据及延期日期，返回后再次按稳定身份恢复进度。

## 输入与回复

明确数值、完整日期和按钮操作优先走规则；只有证据覆盖整句话才跳过语义补充，避免数值掩盖后面的实习/科研经历。语义使用已有统一客户端，不增加独立意图分类调用。

咨询、假设和否定不作为实际成绩；混合输入先应用明确变更，再基于新画像回答。咨询回答使用当前画像、选中任务和本地任务知识，未查证的学校要求一律待官网核验。

置信度至少 0.75、明确直接陈述且目标唯一才自动应用。含糊目标、低置信度或冲突生成 PendingConfirmation；每轮只问一个关键确认问题，界面可确认或拒绝，拒绝的同一变更不再重复询问。阶段信号只辅助判断准备状态，不证明任务完成。

纯进展回复由实际变更构造，不额外请求模型润色；咨询失败会返回相关现有任务建议，已接受的进展照常保存。复杂表达仍依赖模型质量；规则不理解时不猜测，可改用任务按钮。

## API 兼容

保留 `/api/chat`、`/api/onboarding`、`/api/roadmap/enrich`、`/api/roadmap/replan` 和原响应字段。`LifecycleAgent.on_user_message() -> str` 保留；内部 `process_user_message() -> AgentTurnResult` 提供结构化结果。

新接口示例（target_id 从返回任务的 progress_key 读取）：

```json
POST /api/progress
{"session_id":"...","request_id":"client-generated-uuid","target_id":"...","target_kind":"task","action":"complete"}
```

action 支持 start/complete/postpone/cancel/reset；延期必须提供 ISO 日期 `postponed_to`，事件使用 `target_kind:"event"`。可提供 evidence 或实际完成日期 actual_date。

```json
POST /api/timeline-update
{"session_id":"...","request_id":"client-generated-uuid","node_title":"暑假：暑研或实习","node_date":"2026-07-15","fact_field":"research_experiences","detail":"完成实验室信号分类复现实验","occurred_on":"2026-08-20"}
```

该接口只保存用户明确填写的事实并将文章标记为待更新，不会自动把关联任务写为已完成。

```json
POST /api/confirmations/{confirmation_id}
{"session_id":"...","request_id":"client-generated-uuid","accept":true,"target_id":"..."}
```

拒绝传 `accept:false`；含糊进展需要选择候选 target_id，延期需要完整日期。响应追加 `progress_updates`、`state_changes`、`pending_confirmations`、`replan_required`、`state_revision`，完整历史在 snapshot 的 user_events/task_progress/state_transitions 中。

同 request_id、同内容的已完成请求返回既有结果，不重复应用；处理中返回 202。相同 ID 对应不同内容、旧版本提交或已失败请求返回 409；删除会话的写入返回 410。收到失败响应后刷新并使用新 request_id 重试；断线留下的 received 请求可用原 ID 续跑。

## 相关文件

- models.py：兼容数据模型。
- progress.py：身份、旧 ID 迁移、时间轴状态投影及重规划进度传递。
- turn_understanding.py / profile.py / semantic_extractor.py：保守断言过滤、规则优先、混合输入与语义进展。
- state.py / lifecycle_agent.py / advisor.py：并行状态、变化日志、确认、手动文章更新与咨询。
- conversation_store.py / session_state.py / session_service.py / web_app.py：持久化、迁移、版本/幂等控制与 HTTP。
- planning.py / planning_skills.py：任务身份约束与带执行进度的文章 Prompt。
- lifecycle_tools.py：保留 openJiuwen Tool 的 JSON 接口并附加结构化结果。
- web/index.html / web/progress_ui.js：状态徽标、任务操作、确认按钮、加载反馈与服务端同步。

## 测试

```powershell
python -B -m pytest -q -p no:cacheprovider
```

重点新增 test_progress_tracking.py 与 test_progress_service.py；旧自动规划测试已改为验证“状态自动、文章手动”，保留原配置、时间轴、岗位、工具和存储测试。

真实浏览器验证使用独立临时目录的测试 HTTP 服务，不连接用户正在运行的服务，不读写真实 conversations.json：

```powershell
# Playwright 需由测试环境提供（可通过 NODE_PATH），不是应用运行依赖。
node tests/ui_progress_smoke.cjs
```

覆盖两个独立浏览器上下文、任务操作、确认/拒绝、刷新恢复、删除同步、小屏不遮挡、滚动和文章放大。模型行为测试使用 completion_fn 注入，不能当作在线验证。真实模型测试依旧仅在 `RUN_MODELSCOPE_INTEGRATION=1` 且配置 Key 时显式运行。
