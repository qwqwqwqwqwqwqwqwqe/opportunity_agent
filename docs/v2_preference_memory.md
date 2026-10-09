# V2.2 偏好记忆：接口、更新时机与运行

Memory Service (`v2/services/memory.py`) 是偏好唯一读写入口。Profile Agent 提取候选；Conversation Context Service 委托它召回；审批服务委托它应用已确认偏好。Checker、Research、Planning、Synthesizer 不写长期偏好。

## 请求数据流

1. API 构建用户画像、当前会话历史，以及最多 2 条 `PreferenceSnapshot`。版本快照包含四个偏好键的当前版本，缺失键按 0 处理。
2. Orchestrator 保存到 `ExecutionState.preference_memory`，与 `memory` 工作状态分离；正向规则核验的本轮偏好放入 `turn_preferences`。
3. Goal Parser 解析当前请求；Orchestrator 按“当前请求 → 本轮明确偏好 → 已保存偏好”补充条件。`avoid_gre=true` 可补充 GRE 约束；软偏好不自动转为硬过滤。`SuccessCriteria.memory_constraints` 记录服务提供的记忆来源和版本。
4. Router、Planning 和 Synthesizer 接收相关偏好。Research 仅接收最小化键和值，以及派生条件；不接收完整画像、工作状态、会话摘要或原始历史，指代由 Router 的 `resolved_query` 解决。
5. PASS／PARTIAL／NEED_USER 的有效明确偏好在最终结果事务中保存；同时产生 `memory_updated` 事件。FAIL、执行异常和纯闲聊不新增写入。未提交成功时事件、偏好和审计一同回滚。
6. 只有 PASS 且 Synthesizer 成功才构造 `ConsolidationInput`，最终 Run 保存事务同时插入唯一 outbox。Worker 只能在提交后领取。

新增契约位于 `v2/services/memory_contracts.py`。快照包含 `retrieval_status=ok/unavailable` 和偏好 ID、键、值、来源、置信度、版本、原文证据、来源会话／消息、有效期与召回方法。无匹配与召回故障分别表示为空列表和 unavailable。

## 偏好与权限

支持 `avoid_gre`、`employment_priority`、`fallback_country`、`budget_preference`。明确偏好必须通过原文、主体、陈述性质和类型规则；不会仅因模型自报高置信度而直写。规则不能可靠证明的候选仍需确认。

- “我不考虑需要GRE的项目”可直接保存，不被当成取消项目任务。
- “我愿意考GRE”覆盖旧偏好为 false，不强制要求 GRE。
- “这次查询要求GRE的项目”可覆盖本轮检索条件，但不修改已保存的长期偏好。
- “如果我不考虑GRE会怎样？”、“朋友不想考GRE”不会直写。

明确偏好保存是幂等、版本化操作；相同 request_id 在不同会话可分别处理。较慢的旧 Run 不覆盖较新的用户原文更新。推断审批使用 expected_version，过时审批返回 409，不覆盖新明确陈述。

前端新增“偏好”页，显示已保存项并支持撤销；后台归纳提案可能在主回答之后生成，空闲时每 10 秒刷新待确认列表。

## 归纳任务

`ConsolidationInput` 只包含当前 Run 身份、最终 Checker 状态、轮数、最多 6 条本会话用户原文及消息 ID、读到的偏好与版本、偏好候选和 Agent 状态摘要。没有最终答案、学校政策或规划正文。

Worker 每 5 秒检查数据库 outbox；执行租约 90 秒，单次归纳预算 30 秒，最多 3 次，失败后分别等待 30／120 秒。进程退出留下的过期租约可重新领取；最终一次仍过期则标记失败。

模型只能提出带原文证据和消息 ID 的推断。Memory Service 检查版本、相同值和既有审批，然后创建待确认提案。不会直接写入正式偏好；重复领取和现有 Profile/change_set 提案不会生成重复审批。

RETRY 不归纳；PARTIAL、NEED_USER、FAIL 不归纳。归纳异常或模型不可用不会改变已完成的主回答，任务失败原因保存在 outbox。只有明确偏好的用户原文可直接返回 no_change，不重复推断。

## HTTP 接口

接口沿用当前 Cookie/JWT 鉴权与用户隔离：

```text
GET /api/v1/memory/preferences
POST /api/v1/memory/preferences/{key}/revoke
  {"request_id":"unique-request", "expected_version":3}
```

撤销返回 memory_id、更新后的 version 和 active=false；查询不返回撤销或过期项。未知偏好返回 404，版本／幂等键冲突返回 409，无效值返回 422。

## 运行与迁移

升级前备份数据库。API 的 Docker 启动命令执行 Alembic head；新修订为 `0007_preference_memory`。迁移对相同用户／类型／键保留最新记录，重复旧记录先归档到 `memory_audits`，再建立唯一约束。本次开发只迁移隔离测试数据库，没有更新用户业务库或导入 `data/conversations.json`。

```powershell
docker compose up -d --build
docker compose logs --tail 100 api worker
```

非 Docker 环境先执行 `python -m alembic upgrade head`，启动 API 后另开终端运行 `python -m opportunity_agent.v2.worker`。Worker 需要与 API 相同的数据库和模型配置；Compose 已透传 LLM 配置。Redis 摄取循环与数据库记忆循环隔离，Redis 不可用不会停止记忆任务处理。

默认 `MEMORY_VECTOR_ENABLED=0`，使用规则召回。可在安装 `.[rag]` 且缓存 E5 模型的 Python 环境设置为 1；写入用 passage 编码、查询用 query 编码，失败或模型不匹配回退规则。基础 API Docker 镜像未安装 rag 依赖，不能只改一个环境变量就声称启用了真实向量召回；需要提供含依赖与模型缓存的 API 镜像。向量记忆质量未在真实人工数据上验收。

## 验收分层

- 离线回归：`python -m pytest tests/test_v2_memory.py -q`，全量 V2 继续使用原测试集。
- JavaScript：`node --check opportunity_agent/v2/web/app.js`；这不是浏览器渲染验收。
- 真实模型：新增 `scripts/verify_v2_live_chain.py`，只接受 40 个不同 ID、五类各 8 个且 annotation_status=human_reviewed 的用例，每条运行 3 次。用例字段为 id、category、message、expected_agents、annotation_status；category 为 smalltalk/profile/research/planning/mixed。

```powershell
python scripts/verify_v2_live_chain.py --cases path/to/reviewed-40.json --model-label actual-model-version --output deliverables/live-chain.json --confirm-isolated-environment
```

该工具创建隔离测试账户，要求连接独立验收部署；自动报告路由匹配率与契约有效性，并将答案／安全性人工复核标记为 pending。模型标签由操作者填写，不冒充服务端探测的模型身份。门槛为路由匹配率不低于 95%、契约全部有效，人工安全检查另行完成。工具不会把 Checker PASS 当作事实正确性。

当前真实部署、真实模型与浏览器未验收；RAG 人工标注仍由用户继续，未修改标注进度或导出 gold。标注完成后按 `docs/research_agent.md` 独立进行真实检索与答案质量评测。逐 token 输出继续留待第二轮，无在线评估 Agent／Answer Validator。
