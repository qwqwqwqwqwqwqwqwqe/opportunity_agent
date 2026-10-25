# ApplyOps V3.2 官网监控与个人影响站内信

## 已实现与边界

监控配置 → 定时/手动检查 → 静态 HTML 快照与 diff → 只读个人影响 Agent → 证据/版本/租约校验 → 站内信和未读红点。

复用认证、Profile、项目/材料/待办、业务事务、持久化 Job 和通知表。无需 Research、RAG、向量模型、搜索 API、A2A 服务、浏览器容器或额外微服务。真实邮件、论坛帖子和自动业务修改不在本版范围。

「计划版本」入口已撤下，ApplyOps 模式新建/激活草稿及 planning.request 返回 409。历史只读 API 保留，旧自动重规划任务由 Worker 取消，现有事件改做 Review。旧审批若包含已停用的排期命令会整体拒绝，需重新提案，不能部分提交。

## 启动和首次使用

升级前备份数据库，不执行 `docker compose down -v`。本次新增 `0013_official_monitor`，不修改用户项目、材料、任务和日历。

```powershell
docker compose -f compose.applyops.yml up --build -d
docker compose -f compose.applyops.yml logs --tail 80 api worker
```

Compose 的 API 自动运行 Alembic，Worker 等 API 健康后启动。打开 **http://localhost:8001/v3**，Ctrl+F5 更新静态资源。不是旧文档中的 Docker 8000 端口。

本地运行仍使用 `scripts/run_v3_local.ps1`，默认端口 8000；另一个终端以相同数据库和模型环境运行 `python -m opportunity_agent.v3.worker`。不要把测试模式 AUTO_CREATE_SCHEMA=1 当成生产迁移。

1. 先在项目与材料创建申请，记录真实周期及个人成绩等信息。
2. 官网监控 → 配置官网。项目页必填，学院页/FAQ 可选；确认来源并勾选启用。
3. 默认手动模式，可改每日检查，默认 09:00、Asia/Shanghai。一个 URL 可以承担多个角色。
4. 点击立即检查，首次成功建立基线，不发消息。
5. 后续有有效变化才进入 Agent。只在适用当前申请且有个人影响时发站内信，左上角显示红点。
6. 点击信箱查看证据，显式标记已读。没有通知不代表没有变化：检查历史可能是待核实、无直接影响、不适用或失败。

同一监控执行中返回原 check_id；手动检查每五分钟一次。服务停机后恢复合并补查一次，不追补每一天。电脑关机、休眠或容器停止时无法持续监控。

沿用现有 `LLM_API_KEY/LLM_API_BASE/LLM_MODEL` 或 ModelScope 配置，Worker 必须具有同样配置。缺少凭据时页面抓取和变化仍保留，分析明确报 MODEL_NOT_CONFIGURED，绝不降级成有变化就发信。

## 判断、安全及恢复

- 每个配置最多三种页面角色，按确认的精确主机名限制补查。只读取已配置或从证据页发现的官方链接，不猜 URL、不全网搜索。
- HTTPS、DNS 公网检查、连接固定到验证后的 IP 并保留 TLS 主机校验；重定向重新验证。拒绝凭据、内网、云元数据、非安全端口、file URL。不读取系统代理环境。
- 尊重 robots；robots 不可获取时停止，不绕过验证码、登录或付费限制。
- 单页 15 秒和 2 MiB，正文提取保留章节、表格、日期、否定词及链接；剔除导航、脚本等噪声。动态正文/PDF 不支持。
- 成功快照与分析分别保存；获取失败不覆盖基线，304 无基线报错；提取器升级重新建基线。A→B→A 是两个新变化，不永久按哈希屏蔽。
- 一个 Agent 最多六次模型动作、十二次工具调用、六个不同页面、180 秒。使用三个已配置页面的证据启动核查，后续自主选择个人事实、材料任务和官方补查工具。
- 网页只是数据，无业务写工具。官方引文须确实存在于模型读过的块，个人依据须来自当前用户的工具结果。
- MATCH＋AFFECTED、有正负影响、有官网证据与个人依据、没有未解决冲突/信息缺口、版本仍有效，才有通知资格。模型自报置信度不作为依据。
- 租约 240 秒、30 秒续约、最多三次尝试；结果和通知同事务、唯一键去重。配置/周期/个人输入过期拒绝旧结果。只针对读取过的相关字段变化重试未确定记录。

静态页面可获取不代表语义判断一定正确；证据结构校验也不能证明每个因果判断正确，必须通过真实模型人工审核集评测。

## 接口与数据

所有接口在 `/api/v3` 下，使用已有认证。非本人资源为 404。

- GET/PUT `/applications/{id}/monitor`：配置、模式、时区、启停，PUT 含 request_id、expected_version、project_url、college_url、faq_url、confirmed。
- POST `/applications/{id}/monitor/checks`：202，含 request_id；GET `/monitor-checks/{id}` 查询进度。
- GET `/applications/{id}/monitor/history?offset=0&limit=20`：检查、覆盖范围与分析记录。
- GET `/monitor-analyses/{id}`：结论、前后差异、证据和工具执行轨迹；不返回原始 HTML。
- POST `/monitor-analyses/{id}/retry`：对失败/待核实记录重新分析，含 request_id。
- GET `/inbox`、`/inbox/unread-count`、`/inbox/{id}`；POST `/inbox/{id}/read`。

新增六种持久化实体：监控配置、来源、快照、检查、变化、分析。原 `workflow_jobs` 增加 monitor.check/monitor.analyze，原通知增加 official_impact 和 analysis_id。未设置自动清理，不自动删除审计证据，部署者需关注快照存储增长。

## 离线回归与模型测评

`tests/fixtures/official_monitor` 包含 **40 对人工构造页面 / 80 个人场景 / 8 种模板**，全部标注 `human_review=pending`。模板 1–6 为开发集、7–8 为保留集，同模板全部变体不跨集。它们不是学校真实历史，也不是人工 Gold；还需要加入获准保存的真实官网样本、冲突/澄清等更丰富场景并独立审核。

生产 HttpFetcher 和离线 FixtureLoader 返回同一 PageSnapshot，使用相同提取/diff/Agent/证据资格链路。本地文件加载仅用于测试/评测，不开放 API，不执行脚本或外部资源。

```powershell
$env:DOMAIN_AGENT_TRANSPORT = 'local'
$env:OPPORTUNITY_AGENT_DISABLE_DOTENV = '1'
python -m pytest tests/test_v3_official_monitor.py tests/test_v3_interfaces.py -q
```

正式模型评测默认拒绝未审核标签。人工核对后，将相应用例的 human_review 标为 approved，再运行：

```powershell
python -m opportunity_agent.v3.monitor_evaluation --split heldout --mode agent --output data/monitor-agent.json
python -m opportunity_agent.v3.monitor_evaluation --split heldout --mode single --output data/monitor-single.json
```

仅调试未审核种子时可增加 `--allow-unreviewed`，输出强制标为非 Gold，不可宣称通过。命令会真实调用配置的模型，产生费用；离线是指网页输入固定，不代表模型也是模拟的。评测不连接业务库、不发送站内信。

报告记录通知 Precision/Recall、分类准确率、错误/不确定错误通知、工具和模型调用、时延、供应商返回的 token 用量、数据/夹具/提示词哈希。金额不猜测，根据供应商账单计算。验收目标 P≥95%、R≥85%、不确定误通知为 0，且标签已独立审核。引文存在性自动校验，语义相关性仍需人工审查。不能把本地夹具分数当成真实网站覆盖率。

可选 PostgreSQL 测试使用 `APPLYOPS_TEST_DATABASE_URL` 指向专门测试数据库，在 UUID 隔离 schema 中验证幂等、CAS、监控重复请求、竞争 Worker 和通知唯一性；不配置则跳过。

浏览器脚本 `tests/browser/monitor.cjs` 使用本机 Edge 和临时 Playwright，覆盖配置、基线、变化、真实数据库站内信、红点、证据、已读和手机宽度。`monitor_tick.py` 仅允许 `.tmp-workspace-browser/monitor-browser.db`，离线驱动 Worker，不访问真实网站或模型。

## 当前验证状态与待外部验收

- 所选离线 Python、迁移、API 及原工作流回归 **315 passed, 1 skipped**；跳过的是没有专用数据库的 PostgreSQL 并发测试。新增监控测试 **32 项通过**。V2 回归显式使用 DOMAIN_AGENT_TRANSPORT=local，不依赖未启动的 A2A 服务。
- 本机 Edge 完整闭环通过，截图在 `data/monitor-screenshots/`；模型和官网由离线夹具提供，不是语义效果验收。
- 2026-10-23 实际探测 MSAII 申请页及 robots 均返回 HTTP 503；正确报 ROBOTS_UNAVAILABLE，未绕过限制。不能宣称该站线上监控已验证成功。
- 尚未完成真实模型人工审核保留集、真实 PostgreSQL 多 Worker、目标 Docker 部署和至少七天连续运行验收。当前执行环境没有可用 Docker CLI，不代表用户机器未安装 Docker。
- 不宣称已达到模型指标，也不宣称覆盖学校全部政策。
