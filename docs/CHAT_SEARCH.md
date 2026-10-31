# 聊天官网查询：Tavily MCP 与链接确认

## 使用

打开 `http://127.0.0.1:8001/v3` 的聊天助手，发送：

> 帮我查 CMU MSML 的官网申请材料要求。

助手先展示候选页面标题、完整 URL 和链接，不读取官网正文，也不根据搜索摘要给出材料结论。核对学校、项目和适用周期后，回复“确认第1个”或“确认第一个官网”。多个候选时不能只说“确认官网”，需要选一个序号；不想继续可回复“取消查询”。

候选使用“候选 1：”“候选 2：”等文本编号，避免 Markdown 把分段列表都渲染成编号 1。

确认后读取选中页面，将正文交给 LLM 总结，并展示原文证据、来源链接、读取时间及未找到的信息。不会自动更新申请项目、画像、材料、任务或日历；需要保存业务变更时另走草稿确认。

“查看我已录入的材料”仍是 `query`，读取用户数据库；“帮我查官网/最新要求”是 `search`，使用联网工具。

## 实现

1. 同一次 LLM 调用返回 `ChatUnderstanding.intent=search` 和 `search: SearchRequest`。其中 `search_keywords` 是 LLM 根据当前完整问题生成、可直接交给 Tavily 的完整检索词；`alternative_keywords` 是可选的最多两条完整替代检索词。学校、项目、周期用于身份和域名校验；`query` 只用于记录原查询，不能替代缺失的 `search_keywords`。没有固定话题枚举、话题关键词映射或本地拼词。
2. `ChatSearchService` 复用 V2 的 `v2/research/web.py:TavilyMCP`，通过 MCP 调用 `tavily_search`。不初始化 Research Agent、RAG、向量模型或 A2A 服务。
3. 已知学校从 `data/university_domains.json` 获取官方域名，搜索和候选过滤均保留域名限制。复用旧版 `classify_program_page/program_identity`，仅用标题与 URL 排除明确属于其他项目的结果，并优先展示匹配项目或院系的候选；不把搜索摘要当作事实。未知学校明确标记“域名尚未核实”，由用户检查候选；不把域名后缀当作真实性证明。
4. 每次向 Tavily 请求最多十条，过滤后展示最多五个候选。实际 `query` 原样使用 `search_keywords`；没有合适候选时，按顺序使用 LLM 的替代检索词。仍有预算时可用相同检索词做 advanced 查询，最多三组检索。查询异常时最多额外重试三次，所有检索共享这三次失败重试额度及 55 秒总预算，单次调用最多 12 秒。后端只去重与限制次数，不修改检索内容。同等项目匹配级别保留 Tavily 排序，不按本地主题词重排。项目归属未确认的页面标为补充候选；所有页面均需确认读取后才能核实是否含答案。后续检索失败时保留已有候选并提示。
5. 候选保存在当前对话的持久化 Run 状态中，30 分钟内有效。刷新页面和更换 API 实例仍可继续，其他用户或对话不能复用。
6. 确认后使用 V3 `HttpFetcher` 读取该 URL，沿用 HTTPS、DNS/IP 校验、实际连接目标固定、大小/超时限制和 robots 策略。网页内链接不会自动跟随，登录/验证码/PDF/纯 JS 页面不支持。
7. 地址跳转改变页面时，不能直接让 LLM 总结新地址；同主机新地址重新展示并要求确认，跨主机跳转由读取器拒绝。
8. LLM 返回 `SearchSummary`，每个结论携带 block_id 与原文 quote。后端校验引用属于本次页面且原文真实存在，再添加来源 URL。无正文或引用非法时不发布结论，不用搜索摘要兜底。
9. 正文输入预算为 40,000 字符，超过时明确显示只读取部分内容。结果保留证据块、内容哈希、读取时间和工具轨迹，不保存模型内部推理。

官网确认与业务审批分离。同一对话同时有二者待确认时，泛化的“确认”会追问；“确认第1个官网”只读取页面，“确认保存”才处理业务提案。确认官网不会创建或接受业务审批。

## 配置

V3 依赖增加 `mcp>=1,<2`，Compose 为 API/Worker 传入：

```dotenv
TAVILY_API_KEY=your-existing-tavily-key
TAVILY_MCP_URL=https://mcp.tavily.com/mcp/
```

本地已有 Tavily 密钥可复用，不要把密钥发送到聊天。还需配置可用 LLM。容器禁用读取本地 dotenv，必须通过 Compose 环境变量传入。

```powershell
docker compose -f compose.applyops.yml up --build -d api worker
```

本次部署按用户已确认的选择保留正在运行的 `gpt-6-luna` 配置，本地 `.env` 未改。普通 Compose 重建会读取 `.env`，再次部署前应检查模型配置。

## 验证与边界

```powershell
python -m pytest tests/test_v3_chat_search.py tests/test_v3_chat_project_updates.py tests/test_v3_chat_actions.py tests/test_v3_profile_assistant.py tests/test_v3_workflow.py tests/test_v3_http_flow.py tests/test_v3_workspace_experience.py tests/test_run_execution_fencing.py -q
```

2026-10-29 所选回归 126 项通过，联网查询模块 23 项，使用模拟搜索、夹具网页和隔离数据库。覆盖未确认不读正文、不总结搜索摘要、域名和项目身份过滤、短查询降级、过期/取消/序号错误、引用错误、读取失败、跳转重新确认、正文截断、业务审批隔离及真实 HTTP/SSE 流程。

`tests/browser/chat_actions.cjs` 在独立 18035 夹具服务上验证候选链接、刷新后确认、可点击来源、无业务写入、手机布局和原有工作台流程。浏览器测试不访问真实官网或模型。

真实检查脚本（会产生少量模型与 Tavily 费用）：

```powershell
Get-Content -Raw -Encoding UTF8 scripts/smoke_chat_search.py | docker compose -f compose.applyops.yml exec -T api python -
```

该脚本区分两种检查：真实模型识别 + 真实 Tavily MCP 搜索只运行到候选链接；真实模型总结使用独立人工网页夹具和模拟确认，不替用户确认或读取真实候选官网，也不写业务数据库。不能据此声称所有官网可抓取或总结准确率已完成评测。

同日已在 Docker 的 gpt-6-luna 配置下执行：真实 MCP 搜索成功返回 CMU 官网候选；真实模型对人工网页夹具生成三条带有效原文引用的结论。真实官网候选未被代替用户确认或读取，业务数据库无写入。API 和 Worker 已更新，Tavily 密钥与 MCP 客户端在容器中可用。

## 空候选诊断

后续修复增加每次搜索的 `raw_result_count`、`rejected_counts`、`candidate_count`、`search_depth` 与响应结构字段。过滤原因区分不安全 URL、域名不匹配、其他项目、重复链接和当前不支持的 PDF/Office 文档；异常或缺少 results 的上游响应标为服务异常，不伪装成空结果。

学校和项目已识别但上游为空时，回答明确“已识别学校和项目，搜索服务本次没有返回候选”，不再一律要求补全校名。收到结果但全部被过滤时则说明没有通过官网/项目/页面类型检查。Run API 的 `search_request` 和 `search_trace` 可查看结构化诊断，不包含密钥或全文。

此修复的联网模块回归 29 项；包含原有业务及两个旧 MCP 兼容场景的所选回归共 134 项。真实 advanced 查询 `CMU MSML` 返回五条原始结果，过滤一条 PDF 后保留四条官网候选，未读取正文或写业务数据。此前失败记录没有保存原始返回条数，无法事后断言当时是上游为空还是候选被过滤。

## LLM 直接生成检索词（2026-10-29）

实际 Run 记录显示：课程查询首轮返回五条，四条为不支持的文档、一条 URL 不安全；材料查询首轮返回零条。两者随后都被旧代码改成 `CMU MSML`，所以出现相同候选列表。中间加入的 `topic/focus_terms` 与 `TOPIC_TERMS` 映射已移除，当前统一采用 LLM 完整生成检索词。

LLM 输出示例（检索词由模型生成，示例不是模板或话题白名单）：

```json
{
  "intent": "search",
  "search": {
    "operation": "discover",
    "query": "帮我查 CMU MSML 能否延期入学",
    "search_keywords": "Carnegie Mellon MSML admission deferral policy",
    "alternative_keywords": ["CMU machine learning masters defer enrollment policy"],
    "university": "CMU",
    "program": "MSML"
  }
}
```

代码将第一条原样作为 `tavily_search.query`；需要补查时才使用模型返回的替代词。Run 的 `search_request` 保留这些字段，`search_trace[].query` 记录实际请求，`query_source` 区分 `llm.search_keywords` 与 `llm.alternative_keywords`。缺失或空白检索词会被结构校验拦截，不回退到本地模板或用户原话。旧 Run 已保存的候选仍可确认读取，无需迁移。

本次联网模块 43 项、所选回归共 148 项通过。覆盖延期入学、家属保险、休学实习等无预设话题的检索词原样传递，替代词与三次预算、缺失字段不搜索、保留 Tavily 排序、证据校验及旧候选确认兼容。模拟测试不能代替真实网页匹配质量评测。

真实检索对照脚本：

```powershell
Get-Content -Raw -Encoding UTF8 scripts/smoke_chat_search_topics.py | docker compose -f compose.applyops.yml exec -T api python -
```

脚本调用当前真实模型和 Tavily，检查课程、材料、延期入学/国际生休学实习三种请求：第一条实际检索词必须等于 LLM 返回的 `search_keywords`，其余也只能来自模型的完整检索词，不读取候选正文或写业务数据。候选列表可包含共同的项目主页，列表不同本身不是准确性指标，仍需要用户确认项目、学位分支和周期后读取核实。PDF 支持范围未改变。

已在保留的 Docker `gpt-6-luna` 配置下执行上述三组检查，字段传递断言全部通过。第三组模型生成 `Carnegie Mellon University MSML admission deferral policy international students leave of absence internship regulations`，basic/advanced 实际查询均使用同一字符串，并返回包含延期/休学流程的学校官方候选。此结果证明任意主题的检索词传递链路可用，不代表每个候选均属于目标项目或已证实政策适用；未读取真实候选正文，也未写业务数据。

## 查询异常与重试日志

首次查询异常后，使用相同检索词、域名限制及深度最多再试三次，即连续失败时最多调用四次；不重新调用 LLM。每次打开新的 MCP 会话，退避间隔为 0.5、1、2 秒。替代词和 advanced 查询共享失败重试额度，最多三组正常检索加三次重试，不是每组无限扩大。总预算不足时提前停止；合法空结果不是异常，继续走已有替代词逻辑。用户取消运行不会被当成查询错误重试。

每次调用都写入 Run API 的 `search_trace`。失败项包括 `attempt`、`retries_used`、`elapsed_ms`、`retry_scheduled`、`retry_stop_reason`，以及递归展开的 `error_details`：

```json
{
  "tool": "tavily_search",
  "attempt": 1,
  "status": "failed",
  "error_type": "ExceptionGroup",
  "retry_scheduled": true,
  "error_details": {
    "type": "ExceptionGroup",
    "message": "MCP task group failed (1 sub-exception)",
    "stage": "mcp_connect_initialize",
    "exceptions": [
      {"type": "ReadTimeout", "message": "upstream read timed out"}
    ]
  }
}
```

上例仅演示字段结构。实际按异常保存 `exceptions`、`cause` 或 `context`，HTTP 异常包含 `http_status`；MCP 工具返回错误时保留脱敏后的 `tool_error_message`。不保存原始 traceback、响应体或请求头。消息中的已配置密钥、常见令牌、URL 凭证/查询参数和邮箱被过滤，异常深度和长度受限。

相同安全诊断以 `chat_search_failure` 写入 API 日志，含 request_id/run_id，方便关联持久化 Run：

```powershell
docker compose -f compose.applyops.yml logs --tail 100 -f api
```

历史失败记录只有外层异常类型的，无法通过本次升级补出原始子异常；新请求才记录新字段。本次所选回归共 158 项通过，其中联网模块 53 项，覆盖第三次重试恢复、持续失败、嵌套异常与 HTTP 状态、MCP 工具错误原因、脱敏、总预算停止、取消不重试和已有候选保留。
