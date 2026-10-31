# ApplyOps

ApplyOps 是一个面向留学申请的个人工作台。当前主版本为 **V3.2**：用户可以手动维护申请项目、材料、待办和日程，也可以通过聊天提出操作；系统在明确确认后才写入业务数据。

V2 的 Research Agent、知识库和 RAG 代码仍保留在仓库中，供旧流程使用。V3.2 的主要工作流不依赖 RAG、向量模型、A2A 服务或学校资料爬虫。

## 当前功能

| 模块 | 功能 |
|---|---|
| 申请总览 | 展示申请项目数量、待确认变更、未完成任务，以及每个项目的材料准备进度 |
| 项目与材料 | 维护学校、项目、申请周期、截止日期、材料要求、份数、考试要求和材料状态 |
| 待办 | 按学校和项目归类；支持全局待办、项目待办、新增、编辑、完成、撤销和删除 |
| 日历 | 汇总申请截止日期、已选择显示的待办和固定日程 |
| 官网监控 | 用户确认项目官网后建立基线，按手动或每日计划检查静态网页变化 |
| 个人影响分析 | 只读 Agent 核对官网证据、适用周期和已记录的个人情况；证据不足或存在冲突时不发送通知 |
| 站内信箱 | 展示有证据支持的官网影响通知，显示未读红点和通知证据 |
| 聊天助手 | 更新画像、查询已有记录、普通聊天、解释通知；通过聊天新增/修改/删除项目与待办、创建日程 |
| 官网查询 | 使用 Tavily MCP 搜索候选页面；用户先确认链接，助手再读取页面并总结 |

聊天中的业务修改会先生成草稿并请求确认。项目与待办的手动删除也会显示确认提示。个人影响 Agent 不直接修改申请数据、日历或材料状态。

## 架构

- **前端**：原生 HTML、CSS 和 JavaScript，入口为 `/v3`。
- **API**：FastAPI，V3 路由位于 `/api/v3`；认证、会话、聊天运行和审批复用现有 V2 基础设施。
- **业务层**：Pydantic 契约、SQLAlchemy Async、统一领域事务服务、乐观版本检查和幂等回执。
- **数据库**：本地开发可用 SQLite；Docker 环境使用 PostgreSQL 16。Alembic 在 API 启动时升级数据库。
- **Worker**：处理持久化官网检查和个人影响分析任务，使用租约和幂等保护恢复执行。
- **模型**：通过环境变量配置 OpenAI 兼容网关或 ModelScope。官网监控的抓取与模型分析分开；没有模型凭据时不把“发现变化”当作可以通知的结论。

项目主要代码位于 `opportunity_agent/v3`，ApplyOps 前端与共享认证页面位于 `opportunity_agent/v2/web`。V3 不读取或迁移 `data/conversations.json`。

## Agent 使用说明

| 组件 | 当前做法 | Agent 边界 |
|---|---|---|
| 个人影响 Agent | Worker 检测到官网有效变化后调用。模型可在限定循环中选择读取变化证据、官方页面与链接、项目周期、画像、材料/待办，并检查 DDL 对未完成任务的影响。 | 这是本版本的工具使用型 Agent；有模型调用、工具次数、页面数和总时限上限。工具只读，结构化结论需通过引用、适用周期、个人依据和输入版本校验后，才可能生成站内信。 |
| 聊天助手 Agent | LLM 识别查询、画像更新、闲聊、通知解释、业务操作或官网搜索意图；按意图调用画像提取、Tavily MCP/官网读取等固定工具流程，也可根据服务端提供的已有记录回答查询。 | 它是有工具能力的受限对话 Agent：只能使用预设的意图和工具流程，没有任意工具调用或无限循环。项目、待办和日程写入先生成类型化草稿，领域服务校验并等待用户确认，不让模型直接写数据库。 |
| 官网查询 | LLM 按本轮问题生成检索词，调用 Tavily MCP 搜索候选链接；用户确认 URL 后，系统读取网页，再让 LLM 总结并引用页面内容。 | 这是固定步骤的联网工具流程，不是可任意浏览网页的自主 Agent；搜索摘要不会直接作为答案或写入申请数据。 |
| 业务编排与规则 | Python 服务处理认证、审批、数据事务、幂等、版本冲突、材料规则和通知资格校验。 | 这些部分是确定性后端逻辑，不属于 LLM Agent。最终写入权限由领域服务和用户确认控制。 |

聊天助手和个人影响分析都使用 LLM 与工具。个人影响 Agent 会在有界循环中自主选择只读工具核对证据；聊天助手由 LLM 选择预定义意图，再进入对应的工具/业务流程，并在写入前停下来请求用户确认。两者都是受约束的 Agent，只是工具循环和权限边界不同。V2 Research/RAG 与 Jiuwen 相关代码仍在仓库中，V3.2 默认 ApplyOps 主流程不调用这些旧路径，也不依赖 RAG。

## Docker 启动

需要 Docker Desktop、Docker Compose 和可用的模型配置。Windows PowerShell 示例：

```powershell
Copy-Item .env.example .env
```

编辑项目根目录的 `.env`：

- 设置随机且足够长的 `JWT_SECRET`。
- 设置 `POSTGRES_PASSWORD`，不要在公开部署中使用 Compose 默认值。
- 配置聊天和影响分析所需的 `LLM_API_KEY`、`LLM_API_BASE`、`LLM_MODEL`。
- 官网聊天查询可选配置 Tavily MCP；它不是官网监控抓取的依赖。

然后启动：

```powershell
docker compose -f compose.applyops.yml up --build -d
docker compose -f compose.applyops.yml logs -f api worker
```

打开 **http://localhost:8001/v3**。首次启动会自动执行数据库迁移。停止服务但保留数据：

```powershell
docker compose -f compose.applyops.yml down
```

不要使用 `down -v`，它会删除 Compose 数据卷。升级前请备份已有数据库。

## 本地 Python 启动

需要 Python 3.11 或更新版本：

```powershell
python -m pip install -e ".[dev,v3]"
$env:JWT_SECRET = "replace-with-a-long-random-local-secret"
powershell -ExecutionPolicy Bypass -File .\scripts\run_v3_local.ps1 -Reload
```

打开 **http://127.0.0.1:8000/v3**。要运行后台 Worker，在另一个终端进入项目目录并使用同一数据库配置：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_v3_local.ps1 -Worker
```

若不配置模型，表单与确定性业务规则仍可使用；聊天语义解析、自然语言画像抽取和个人影响分析需要可用模型。

## 官网监控边界

- 只监控用户为申请配置并确认的官网页面及允许范围内的官方链接。
- 首次成功检查建立基线，不作为官网变化通知。
- 当前支持静态 HTML；登录页、验证码、依赖 JavaScript 的正文和 PDF 不在支持范围。
- 只有变化适用于当前申请，并且有官方证据与个人依据、没有未解决冲突时才创建站内信。
- 官网变化不会自动修改截止日期、材料、成绩、待办或日历。
- 容器或电脑停止运行时无法持续检查；本功能不发送真实邮件。

## 验证与测评

安装开发依赖后，可运行 V3 聊天和工作流回归：

```powershell
python -m pytest tests/test_v3_chat_actions.py tests/test_v3_chat_project_updates.py tests/test_v3_profile_assistant.py tests/test_v3_workflow.py -q
```

双 API、双 Worker 的 PostgreSQL 故障恢复验证使用隔离的 Compose 项目和专用数据库，不连接业务数据库：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_concurrency.ps1 -Action Start
powershell -ExecutionPolicy Bypass -File .\scripts\run_concurrency.ps1 -Action Test
```

截至 2026-10-27，记录的多进程验收为 **10 轮、110/110 场景通过**；另有 PostgreSQL 并发回归 **3 项通过**。该验证覆盖列出的竞争与故障场景，不代表高 QPS 能力或“恰好执行一次”。

个人影响 Agent 与单次 LLM 的真实模型对照仍需扩大并独立审核评测集。现有开发集结果不能用于宣称 Agent 显著优于单次 LLM。测试报告、实验方法和限制见下方文档。

## 文档

- [ApplyOps V3.2 运行与接口说明](docs/APPLYOPS_V3.md)
- [官网监控与个人影响分析](docs/OFFICIAL_MONITOR.md)
- [聊天助手使用说明](docs/PROFILE_ASSISTANT.md)
- [官网 Tavily 查询与链接确认](docs/CHAT_SEARCH.md)
- [双 API / 双 Worker 并发验证](docs/CONCURRENCY_VALIDATION.md)
- [真实模型对照实验与审核方法](docs/MONITOR_EXPERIMENT.md)
- [2026-10-27 验证记录与结论限制](docs/VALIDATION_20261027.md)
