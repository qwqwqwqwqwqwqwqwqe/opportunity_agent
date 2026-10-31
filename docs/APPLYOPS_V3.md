# ApplyOps V3.2 运行与验收

## 画像助手入口更新

聊天支持画像与偏好更新、已有数据查询、普通聊天、官网通知解释，以及通过提案确认新增/修改/删除项目和待办；自动排期仍停用。成绩等已有标量变化使用 V2 冲突弹窗；首次录入先提案后确认。聊天视觉统一为 V3 工作台。使用说明与测试见 [画像助手](PROFILE_ASSISTANT.md)。

## 2026-10-23 官网监控与个人影响站内信

本次增量替换「计划版本」为「官网监控」，增加左上角站内信箱；停用 ApplyOps 的自动排期、新建/激活计划草稿，旧计划和快照仅保留历史。本文后续 2026-10-20 的计划相关内容作为历史实现说明，不代表 V3.2 仍提供排期入口。

新增迁移 `0013_official_monitor`。监控默认关闭，用户确认官网地址后启用，支持每日自动检查和手动立即检查。首次检查建立基线，只有证据充分的个人正面/负面影响产生站内信；冲突、资料不足和模型失败均不发信。手动业务闭环不依赖模型，影响分析需要已有 LLM 配置。

详细配置、状态、API、离线夹具、真实模型评测和外部验收限制见 [官网监控运行与验收](OFFICIAL_MONITOR.md)。当前 Compose 端口为 **8001**，本地脚本默认端口为 **8000**。

## 2026-10-20 工作台改版

- **总览**：顶部显示申请项目、待确认变更、待完成任务，下方单列显示项目材料准备卡片；移除新增入口。
- **项目与材料**：仅一个「新增项目」入口，一张完整表单维护学校、周期、DDL、材料和推荐信、语言考试任选条件、GRE、来源与备注。一次事务保存，已有记录保留 ID。
- **待办**：按学校目录分组，每个项目一张卡片，右下角加号新增任务。圆圈勾选后划线、收起并补位；可撤销，并可展开已完成记录。
- **日历**：新待办可选择是否显示，完成后移除自身条目，撤销后恢复；同日其他待办、申请 DDL 与固定事件不受影响。
- **账户与审批**：移入按需打开的侧边抽屉，业务页面不再常驻右栏。聊天会话列表仅在聊天页显示。
- **主动服务**：已预留结构化任务完成事件与 provider 接口；当前 provider 返回空结果，不推断、不自动更新材料。

准备进度将 `ready/submitted` 计为已准备，推荐信全部满足份数后才计一组；提交检查仍按原规则独立计算。未知要求不显示为全部准备完成。

新迁移为 `0012_workspace_experience`。Docker 启动会自动升级，已有未完成且带日期的任务继续显示在日历；新待办默认不显示。

```powershell
docker compose -f compose.applyops.yml up --build -d api worker
```

打开 `http://localhost:8000/v3`，必要时按 Ctrl+F5 强制刷新。升级前仍应保留现有数据库备份，不执行 `down -v`。

浏览器验收脚本为 `tests/browser/workspace.cjs`，使用临时数据库服务和本机 Edge，覆盖完整表单、学校目录、完成与撤销、日历、持久化与手机宽度。截图保存在 `data/workspace-screenshots/`。

本次所选完整回归 **282 passed, 1 skipped**（跳过项为未配置的 PostgreSQL 并发测试）；最后的日期校验与接口补充验证为 **37 passed**。Edge 验收还验证了编辑第三封推荐信后进度从 1/2 变为 2/2，以及编辑时不会重新加入已明确排除的语言要求。

浏览器测试使用的 Playwright 仅安装在忽略的临时目录，不增加应用运行时依赖：`npm install --prefix .tmp-workspace-browser --no-save playwright`。先用独立测试库启动 18032 端口服务，再运行 `node tests/browser/workspace.cjs`；可用 `APPLYOPS_BROWSER_URL` 和 `EDGE_EXECUTABLE` 覆盖地址与浏览器路径。

## 当前交付范围

本实现复用 V2 登录、会话、运行租约、SSE、Profile 提取与审批，在 `/api/v3` 增加私有项目、申请周期、要求、材料、稳定任务、依赖、日历、计划草稿与后台事件。页面为 `/v3`。

V3 进程内使用 Intake、Planning、Review 和独立 Synthesizer。主路径不导入 RAG、Torch 或 A2A，不需要下载向量模型。没有模型凭据时，表单、材料检查、计划和 Worker 仍可用，聊天提供规则提取及明确降级。

代码完成并不等同于模型效果和部署验收已完成。人工审核、真实模型、真实 PostgreSQL 并发和浏览器操作的状态见文末。

| 阶段 | 已实现与自动化验证 | 保留的验收门槛 |
|---|---|---|
| 0 基线与业务规则 | 固定状态/日期规则、50 条 Intake 种子、80 个参数化业务案例 | 种子需要独立人工审核，实际部署库需要 preflight/备份 |
| 1 领域与数据库 | 固定定义的增量迁移、空库/旧结构升级、所有权、CAS、幂等及原子批次 | PostgreSQL 专用库并发测试 |
| 2 手动闭环 | 表单、项目/要求/材料、稳定任务、日历投影和 Review；本机 Edge 交互与截图验收通过 | 目标部署环境确认 |
| 3 聊天闭环 | 复用 Profile、证据检查、变更提案、批准后续执行、闲聊直答 | 真实模型保留集指标 |
| 4 计划 | DAG、工期/日期约束、草稿/差异/批准/拒绝、稳定任务身份 | 用户真实申请案例验收 |
| 5 后台 | Outbox、租约/令牌、重试/恢复、定向作用域和站内提醒 | PostgreSQL 多 Worker 部署验收 |
| 6 发布 | 本地脚本、独立 Compose、HTTP 集成、规则报告、运行文档与本机 Edge 验收 | Docker、真实模型外部验收 |

## 本地运行

在项目目录使用 Python 3.11+：

```powershell
python -m pip install -e ".[dev,v3]"
$env:JWT_SECRET = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
powershell -ExecutionPolicy Bypass -File .\scripts\run_v3_local.ps1 -Reload
```

打开 `http://127.0.0.1:8000/v3`，接口文档为 `http://127.0.0.1:8000/docs`。

默认创建 `data/applyops.db`，不会导入 `data/conversations.json`。已有 `DATABASE_URL` 环境变量时脚本会使用该数据库；希望使用默认本地库时先运行 `Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue`。

另开 PowerShell，进入同一项目目录启动 Worker：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_v3_local.ps1 -Worker
```

如果 API 使用自定义数据库，Worker 必须设置相同 `DATABASE_URL`。SQLite 用于本地演示；并发部署使用 PostgreSQL。

需要语义提取和自然语言汇总时，沿用已有 `LLM_API_KEY`、`LLM_API_BASE`、`LLM_MODEL` 或 `MODELSCOPE_API_KEY` 配置。没有凭据时不调用外部模型。V3 把会话摘要限制为已有的抽取式摘要，避免持有会话锁期间调用模型。

## Docker

V3 使用独立 Compose 文件，不要求启动 Profile/Research/Planning A2A 服务：

```powershell
$env:JWT_SECRET = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
docker compose -f compose.applyops.yml up --build -d
docker compose -f compose.applyops.yml logs -f api worker
```

仍使用 pgvector PostgreSQL 镜像，以兼容历史迁移和已有表；V3 无向量检索运行依赖。Redis 容器保留为后续缓存接口，当前业务事件持久性由 PostgreSQL 保证。

如旧 Compose 正占用 8000/5432，先停止旧服务。复用现有数据库前先备份：

```powershell
docker compose exec postgres pg_dump -U opportunity -d opportunity_agent -Fc -f /tmp/applyops-before.dump
docker compose cp postgres:/tmp/applyops-before.dump ./applyops-before.dump
```

不要执行 `down -v`。默认 Compose 项目名相同时，两个配置会复用 `postgres_data`。API 启动前自动执行 `alembic upgrade head`，Worker 等待 API 健康检查通过后启动。

## 操作示例

1. 注册并登录，在“项目与材料”新增学校项目。
2. 新增申请周期，填写明确年份及已知截止日期。未知日期保持空白。
3. 新增申请，关联该周期。
4. 添加要求，例如“推荐信”，设为必需、数量 3，并填写来源原文。
5. 为三位推荐人分别添加材料。任务“提醒推荐人”完成，不会改变这些材料。
6. 添加待办及依赖；在“计划版本”生成草稿，查看排期和差异后启用。
7. 完成一个任务，再生成并启用新版本，确认原任务仍然存在且保持完成。
8. 修改截止日期，Worker 生成新草稿和提醒；草稿确认前正式日历保持原排期。

聊天示例：`我的托福是105分，帮我生成计划`。系统先生成画像提案；批准前不规划未确认事实，批准后创建后台规划任务。提案被拒绝则不产生后续任务。闲聊直接调用 Synthesizer，不产生业务变更。

## 接口契约

所有业务写入最终进入 `WorkflowService.execute`。认证身份来自服务器，禁止客户端指定 `user_id`。

```json
{
  "schema_version": 3,
  "request_id": "unique-command-id",
  "commands": [
    {
      "type": "task.update",
      "target_id": "task-id",
      "expected_version": 2,
      "payload": {"status": "completed"},
      "evidence_refs": []
    }
  ]
}
```

明确表单操作：`POST /api/v3/commands`，直接事务提交。聊天提案：`POST /api/v3/proposals`，确认后经原 `/api/v1/approvals/{id}/accept` 执行相同服务。

一组创建可用 `ref` 关联服务器生成的 ID：

```json
{
  "request_id": "linked-intake",
  "commands": [
    {"type":"program.create","ref":"p","payload":{"university":"Example","name":"MSCS"}},
    {"type":"offering.create","ref":"o","payload":{"program_id":"$ref:p","cycle":"2027 Fall"}},
    {"type":"application.create","payload":{"offering_id":"$ref:o"}}
  ]
}
```

引用只能指向本组已执行命令；整组失败即回滚。已有实体修改必须携带 `expected_version`。同一请求相同内容重放返回原回执，内容不同返回 409。

资源 API：

| 路径 | 行为 |
|---|---|
| `/api/v3/programs`, `/offerings`, `/applications`, `/tasks`, `/calendar-events` | GET 列表、POST 创建、GET/PATCH `/{id}` |
| `/applications/{id}/requirements`, `/materials` | GET、POST；修改使用统一命令 |
| `/snapshot`, `/dashboard`, `/review` | 当前状态、汇总、材料检查 |
| `/calendar?from=YYYY-MM-DD&to=YYYY-MM-DD` | 正式日程合并投影 |
| `/plans/drafts` | POST `{request_id,today?,application_ids?}` |
| `/plans`, `/plans/current`, `/plans/{id}`, `/plans/{id}/diff` | 计划与差异 |
| `/plans/{id}/accept`, `/reject` | POST `{request_id,expected_version}` |
| `/jobs`, `/jobs/{id}`, `/notifications` | 后台状态、站内提醒 |

日期仅有日时存 DATE；具体时刻必须带时区。日历中的黄色虚线项为草稿预览。预估工期允许任务并行，不保证总工作量最优。

## 数据库和兼容

基础迁移 `0011_applyops_workflow` 采用固定表定义，保留 0001–0010 与旧 workspace 历史表。本次 `0012_workspace_experience` 补充材料展示名称、归档标记和任务日历开关。升级不删除旧记录。

`workflow_tasks` 是当前任务；`workflow_plan_items` 保存不可变排期快照。计划审批检查输入 revision、原活动计划、每项任务版本；旧草稿不能覆盖更新后的事实。

V3 模式下旧 application/task/plan 写入被禁用，Profile/审批仍可复用。切回旧界面不会让旧任务自动与新任务同步；不要同时运行 V2 与 V3 API 写同一个数据库。

升级前结构检查和显式旧任务迁移：

```powershell
python -m opportunity_agent.v3.admin preflight
python -m opportunity_agent.v3.admin migrate-tasks --user-id YOUR_USER_ID
```

任务迁移保留旧记录，以 `legacy_task_id` 去重，仅处理当前活动计划及申请关联任务。冲突列入 `needs_review`，不按相似标题合并。不会自动迁移旧会话 JSON。

事务包括业务数据、操作回执与事件。Worker 使用租约和执行令牌恢复任务；重复事件通过幂等回执避免重复草稿。计划激活事件不触发下一轮重规划。

模块分工：`contracts.py` 定义接口，`service.py` 执行事务，`engine.py` 是无数据库/无模型的调度与检查内核，`orchestrator.py` 管理业务 Agent 和 Synthesizer，`worker.py` 处理持久化事件。新增代码均在 `opportunity_agent/v3`，不会恢复被撤回的旧 workspace 模块。

## 评测与验收

```powershell
$env:OPPORTUNITY_AGENT_DISABLE_DOTENV = '1'
$env:DOMAIN_AGENT_TRANSPORT = 'local'
python -m pytest tests/test_v3_workflow.py tests/test_v3_interfaces.py tests/test_v3_acceptance.py tests/test_v3_http_flow.py -q
```

验收集含 20 个工期/日期排期组合、20 个材料数量/状态组合、40 个批准/拒绝/材料状态数据库场景，另有跨用户、重复请求、过期草稿、任务身份、Worker 恢复与完整 HTTP 流程。它们是可执行业务规则，并非人工标注的模型 Gold。

Intake 提供 50 条待审核种子，按语义组划分开发/保留集：

```powershell
python -m opportunity_agent.v3.evaluation --export-seed ./data/applyops-intake-seed.json
python -m opportunity_agent.v3.evaluation --mode rules --allow-unreviewed --output ./data/applyops-rules-report.json
```

独立审核人逐条检查 message、snapshot、expected 后，将 `human_review` 改为 `approved`。之后才允许作为真实模型验收数据：

```powershell
python -m opportunity_agent.v3.evaluation --dataset ./data/applyops-intake-seed.json --mode real --split heldout --output ./data/applyops-model-report.json
```

报告包含模型、数据哈希、字段 precision/recall/F1、错误和时延。待审核标签即使产生高分也不会标为验收有效；费用结合提供商账单记录。

PostgreSQL 并发测试使用专用测试数据库，预先安装 vector 扩展：

```powershell
$env:APPLYOPS_TEST_DATABASE_URL = 'postgresql+asyncpg://USER:PASSWORD@localhost:5432/applyops_test'
python -m pytest tests/test_v3_postgres.py -q
```

测试仅创建并清理自身 UUID 命名的 schema。没有配置该变量时明确跳过，不用 SQLite 结果代替 PostgreSQL 结果。

## 尚需外部验收

此前 V3.1 基线回归为 **272 passed, 1 skipped**；本次改版的结果见文首。跳过项是未配置专用测试库的 PostgreSQL 并发用例。JavaScript 通过 Node 语法检查；规则模式开发报告保存于 `data/applyops-rules-report.json`，明确标记为非人工审核、不可作为模型验收。

- 本地自动化已验证业务、迁移、认证、聊天运行、SSE 和审批；当前执行环境找不到 Docker，可选 PostgreSQL 并发测试未执行。
- 未调用真实付费模型，不能宣称 Intake F1 达到 0.90。50 条种子仍需独立人工审核。
- 2026-10-20 已使用本机 Edge 完成工作台浏览器验收并检查截图；真实 PostgreSQL/Docker 运行仍需在目标环境确认。
- 申请要求完整性取决于用户录入；Review 显示的完成情况只覆盖已记录要求。
