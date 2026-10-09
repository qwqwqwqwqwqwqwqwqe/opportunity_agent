# Research Agent：运行与评测

## 2026-10-08 Router 与诊断入口更新

当前明确学校／项目及字段的请求优先于旧历史；Router 使用请求级 JSON Schema 与返回后校验，提供格式分类、有限重试与窄范围恢复。前端展示 Run/Trace 并持久显示失败。Docs 中可用 `GET /api/v1/conversations/{conversation_id}/runs` 查会话历史任务，再用 `GET /api/v1/runs/{run_id}` 查看 route_decision、routing_diagnostics 和结果。消息 ID、Run ID、Trace ID 是三个不同标识。

Research A2A 默认 90 秒，Research 业务预算仍为 55 秒，整轮预算 180 秒。见 [Router 修复与验收记录](../deliverables/research/router-20261008-report.md)。真实历史上下文外发重放须用户授权；本轮真实模型只验证无个人资料的公开测试句。

## 2026-10-08 多目标查询更新

Research 支持多学校、多项目类型及明确学校／项目配对；GRE 等已识别字段不再因单值学校为空而等待模型。未指定入学年份默认 2027，不默认学期。项目缩写与库内英文全称采用严格学位映射，MSCS、MCS、CSE 不混同。

MCP 按缺失目标轮转并共享页面预算，整体执行预算不变。目录候选命中不等于 GRE 字段已经核验，未知或待审核政策仍不能用于可靠结论。最新功能回归与实际数据库复核见 [多目标修复报告](../deliverables/research/multi-target-20261008-report.md)。

## 2026-10-07 运行状态更新

当前 GPU Compose 已部署路由修复和 OTel 导出。real150 已增量导入业务 PostgreSQL：20 项目、58 来源、510 文档/384 维向量片段、67 条待审核字段候选（GRE 18、日期 39、语言 10）。这些候选和片段的待审核状态被保留，不能把采集成功当作政策核验成功；没有修改人工标注源文件。

导入命令（默认预演并回滚，增加 `--commit` 才保存）：

```powershell
docker compose -f docker-compose.yml -f docker-compose.research.yml exec research python -m opportunity_agent.v2.research.import_corpus --directory /research-artifacts/real150
```

Jaeger UI：`http://localhost:16686`。GET `/api/v1/runs/{run_id}` 返回 trace_id，SSE 有 trace_started；按 trace_id 或 service=opportunity-api 和 run_id 标签查找。Jaeger 2.21 查询 API 使用 `/api/v3/traces/{trace_id}`，旧 `/api/traces` 已不适用。详见 [本轮验证报告](../deliverables/research/routing-20261007-report.md)。

真实 HTTP/MCP/OTel 验证脚本 `scripts/verify_v2_research_deployment.py` 会创建隔离测试账号并运行原始 GRE 假设问句；这只验收路由、偏好不误写与追踪，不要求证据不足的查询强行 PASS。

```powershell
python scripts/verify_v2_research_deployment.py --output deliverables/research/gre-api-otel.json
```

以下历史运行记录中的迁移号和环境状态以最新部署为准；当前运行迁移 head 为 0007，语义阈值仍待人工 dev 标注校准。

Research 接收 Orchestrator 的研究任务，返回项目、字段事实、语义发现和证据。Router 仅选择 Agent，Research 内部的 Query Router 选择 `sql / rag / hybrid / mcp_web`，最终回答由 Synthesizer 生成。

本机已创建 `.venv-research`，安装了 CUDA PyTorch 并下载、检查过两种模型。已有容器仍是原来的镜像；下面的 Compose 重建命令会启用这次实现。当前验证记录见 `deliverables/research/VALIDATION.md`。

## 实现入口

| 功能 | 文件 |
|---|---|
| 任务解析、不可变成功标准、缺失项任务 | `opportunity_agent/v2/research/task.py` |
| 四条执行路径、网页补查、预算 | `opportunity_agent/v2/research/service.py` |
| 参数化项目目录查询、字段级证据 | `opportunity_agent/v2/research/catalog.py` |
| 字段、来源、年份、语义证据门槛 | `opportunity_agent/v2/research/quality.py` |
| 官网正文校验与独立入库 | `opportunity_agent/v2/research/ingestion.py` |
| Tavily MCP、工具发现、受控读取 | `opportunity_agent/v2/research/web.py` |
| 数据库向量查询、全文检索、RRF | `opportunity_agent/v2/rag/retrieval.py` |
| E5 与 GPU Cross-Encoder | `opportunity_agent/v2/rag/models.py` |
| 离线指标与消融 | `opportunity_agent/v2/evaluation/research_benchmark.py` |

生产 PostgreSQL 使用 pgvector 在数据库内计算余弦距离；SQLite 仅用于不超过 2000 个 chunk 的离线功能验证。当前使用精确查询，没有启用 HNSW。全文检索使用 PostgreSQL `simple` 配置和固定 `zh-char-en-word-v1` 中英文词元规则；它不是 BM25。向量和关键词各召回 50 条，以 RRF `k=60` 融合。业务 `hybrid` 则是在 SQL 硬条件过滤后，逐项目检索语义证据。

## Windows 独立 GPU 环境

在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_research_gpu.ps1
```

脚本创建 `.venv-research`，安装固定 `torch==2.8.0` CUDA 12.8 及项目 extras，并实际执行 CUDA 张量和 BGE 推理。模型首次下载和 CUDA wheel 都较大，需要保持代理和网络可用。该环境独立于原有 Python。

只检查已安装环境与缓存模型：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_research_gpu.ps1 -CheckOnly
```

RTX 50 系列需要包含 Blackwell 支持的 CUDA PyTorch 构建；这里固定 CUDA 12.8 的版本以便复现，具体支持参见 [PyTorch 官方说明](https://discuss.pytorch.org/t/pytorch-support-for-sm-120-nvidia-geforce-rtx-5060/220941/6)。安装成功后使用 `.\.venv-research\Scripts\python.exe` 执行后续 Python 命令。

## Docker 启动

### CPU 模式（小数据量优先）

基础镜像不安装模型依赖。CPU overlay 为 Research 和 Worker 安装固定
`torch==2.8.0` CPU wheel 和 `.[rag]`，E5 与 BGE Reranker 均使用 CPU，
不申请 GPU。原 GPU overlay 保留；两种 overlay 不要同时使用。

在已配置 JWT_SECRET 和模型服务的项目目录执行：

```powershell
docker compose -f docker-compose.yml -f docker-compose.research.cpu.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.research.cpu.yml logs -f research
```

首次启动会下载两个 Hugging Face 模型并预热，健康检查宽限 30 分钟；
下载需要容器可访问模型站点。共享 `research_models` 卷保留模型缓存，
不要使用 `down -v` 删除缓存或数据库。后续启动也使用相同的两个 `-f` 参数。

验证容器中的实际依赖与模型（预热成功后执行）：

```powershell
docker compose -f docker-compose.yml -f docker-compose.research.cpu.yml exec research python -c "import torch, sentence_transformers; print(torch.__version__); print('CUDA build:', torch.version.cuda); print('CUDA available:', torch.cuda.is_available())"
docker compose -f docker-compose.yml -f docker-compose.research.cpu.yml exec research python -m opportunity_agent.v2.rag.models
```

CPU 构建应输出 `CUDA build: None`、`CUDA available: False`；模型检查报告
`device=cpu` 和 `embedding_dimension=384`。导入真实知识后再测试检索。
Reranker 的语义质量门槛仍需要人工 dev 集校准文件
`deliverables/research/calibration.json`；缺少校准不能当作质量通过。
CPU 延迟可能耗尽现有业务预算，先测真实耗时，不自动扩大请求超时。
这次配置不为 API 安装偏好向量依赖，偏好仍默认使用规则召回。

### GPU 模式（后续提速）

下面命令在同一个 PowerShell 终端执行。密钥使用本地已有值填写，勿提交到仓库：

```powershell
$env:LLM_API_KEY = "你的模型服务密钥"
$env:LLM_API_BASE = "你的模型服务地址"
$env:LLM_MODEL = "你的模型名"
$env:TAVILY_API_KEY = "你的 Tavily 密钥"
$env:RESEARCH_WEB_ENABLED = "1"
$env:RESEARCH_QUEUE_INGEST = "1"

docker compose -f docker-compose.yml -f docker-compose.research.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.research.yml logs -f research
```

GPU overlay 为 Research 申请一块 NVIDIA GPU；Worker 使用同一镜像但在 CPU 上生成 E5 向量。共享模型缓存卷会保留下载的模型。Research 启动前执行模型 warmup，失败时不会报告已就绪；首次下载的健康检查宽限时间为 30 分钟。

本机 Docker 安装在用户目录。如果当前终端找不到 `docker`，先补充当前进程 PATH（不改变系统设置）：

```powershell
$env:PATH = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin;" + $env:PATH
docker version
```

该路径也供 Docker 凭据助手使用。Jaeger 使用官方当前示例中的固定镜像，参见 [Jaeger 2.21 启动说明](https://www.jaegertracing.io/docs/2.21/getting-started/)。

API 启动时执行 Alembic，新增 `0005_research_catalog` 建立项目和字段事实表。Research 调用前应先确认 API 日志中迁移完成。可以显式执行：

```powershell
docker compose -f docker-compose.yml -f docker-compose.research.yml exec api python -m alembic current
docker compose -f docker-compose.yml -f docker-compose.research.yml exec research python -m opportunity_agent.v2.rag.models
docker compose -f docker-compose.yml -f docker-compose.research.yml exec research python -m opportunity_agent.v2.research.cli mcp-check
```

`mcp-check` 只进行 MCP 连接和 `tools/list`，输出实际工具名。调用时使用 Authorization Bearer header，不把密钥写入 URL。实现识别下划线与连字符两种工具命名，实际参数以服务器 schema 为准，参见 [Tavily 官方 MCP](https://github.com/tavily-ai/tavily-mcp)。

`python -m opportunity_agent.v2.research.cli mcp-smoke --output deliverables/research/mcp-smoke.json` 会执行 1 次 CMU 官网搜索、1 次受控页面读取及 1 次正文 Extract，输出工具与正文长度检查结果。Python 客户端支持项目已有 `.env` Tavily 配置。

## 先准备真实知识

项目目录初始为空；没有记录时会返回缺证据或补查需求。旧的 `seed_research_programs` 只在显式测试开关下可用。

采集服务输入一个经核验的页面 JSON。`text` 必须是官网正文，`quote` 必须是其中原文，`value` 必须与原文相符，入学年份和学期必须出现在正文中。示意字段如下，需替换为真实采集内容后才能入库：

```json
{
  "university": "CMU",
  "program": "MSCS",
  "intake": "2027 Fall",
  "url": "https://www.cmu.edu/实际官网路径",
  "title": "实际项目页面标题",
  "text": "实际官网正文",
  "facts": [
    {"field": "deadline", "value": "YYYY-MM-DD", "quote": "支持该日期的原文", "qualifier": "final"},
    {"field": "gre_policy", "value": "optional", "quote": "支持该政策的原文"}
  ]
}
```

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://opportunity:opportunity_dev_only@localhost:5432/opportunity_agent"
$env:RESEARCH_ALLOW_MODEL_DOWNLOAD = "1"
.\.venv-research\Scripts\python.exe -m alembic upgrade head
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.research.cli ingest --input .\你的页面.json
```

域名由 `data/university_domains.json` 或已有的已核验动态域名缓存提供。单靠搜索摘要、校名相似或 `.edu` 后缀不会让新网站获得 `official` 身份；未核验域名记录在 `diagnostics.unverified_domains`。两种镜像都携带种子域名注册表。动态缓存默认本地保存，容器需要显式挂载已有核验缓存。

网页在线搜索每次最多 2 次、读取 5 页。每个重定向都检查官网域名、公网地址及 HTTPS；失败保留已有 SQL/RAG 结果和错误代码。正文清洗保留标题，默认按 tokenizer 约 350 tokens 分块、重叠 50 tokens，E5 查询和正文分别添加 `query:` 和 `passage:`。

如果正文或模型不可用，向量缺失不会被标记为已完成向量检索。模型恢复后再次采集同一正文可补生成缺失向量。新正文版本会使旧文档退出活动检索，但仍保留旧证据供追溯。

## 查询、补查与结果

```powershell
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.research.cli query --message "CMU MSCS 2027 Fall 截止日期和 GRE 要求" --output deliverables/research/sql-query.json
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.research.cli query --message "2027 Fall 找5个不要求GRE、偏AI的项目" --count 5 --gre not_required --deadline-after 2026-12-01 --output deliverables/research/hybrid-query.json
```

`ResearchResult` 包含 `programs / findings / evidence / route_history / missing_items / errors / diagnostics`。每项事实带 `verification_status` 和自己的 `evidence_ids`；同一 URL 的不同片段不会相互覆盖。GRE unknown 不算无需提交；optional、not_required、not_accepted 均保留原政策标签。冲突事实和过期证据不能计数，AI 证据不能证明截止日期或 GRE。

已合格项目由 Orchestrator 保留，新项目补查会排除这些项目；缺字段任务仍可查询原项目。Aggregator 按学校、项目、入学季去重，保存全部证据和各轮结果。来自同一官网的新版本可替换旧事实，跨来源矛盾则保留冲突。Checker 从合并状态重新计算数量；Research 的 `complete` 仅表示当前任务完成。

重排相关性阈值必须来自开发集。未配置校准文件时，系统仍返回候选和诊断，但语义候选不获得“已核实”标签，因此 Hybrid 不会靠任意 `0.7` 阈值通过。SQL 精确字段查询可独立运行。

```powershell
$env:RERANKER_ENABLED = "1"
$env:RERANKER_DEVICE = "cuda"
$env:RESEARCH_CALIBRATION_FILE = (Resolve-Path deliverables/research/calibration.json).Path
$env:RESEARCH_QUERY_REWRITE = "1"
```

校准文件必须匹配模型、`cross_encoder` 方法和 `dev` 划分。GPU OOM 时减小 batch；模型不可用时标记 `reranker_unavailable`。每次 Research 默认预算 55 秒，模型应在启动时 warmup；预算耗尽返回已有结果及缺口。Orchestrator 默认最多 3 轮，随后合成部分回答。

独立进程运行 GPU Research 服务（8782 避开现有 Docker 8772 端口）：

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://opportunity:opportunity_dev_only@localhost:5432/opportunity_agent"
$env:RERANKER_ENABLED = "1"
$env:RESEARCH_WARMUP = "1"
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.agents.a2a research --port 8782 --backend python
```

本地 Orchestrator 设置 `DOMAIN_AGENT_TRANSPORT=a2a` 和 `RESEARCH_A2A_URL=http://127.0.0.1:8782/a2a/jsonrpc/`；完整 Docker 部署直接使用 overlay 中的 Research 服务即可。地区条件由项目目录中的标准国家代码处理；网页发现的项目在地区未知时不能绕过该条件计数。

## 离线评测

无需 GPU/联网的管线自检：

```powershell
python -m pytest tests/test_v2_research.py tests/test_v2_orchestration_skeleton.py tests/test_v2_a2a_domain_agents.py -q
python -m opportunity_agent.v2.evaluation.research_benchmark prepare --output deliverables/research/dataset.json
python -m opportunity_agent.v2.evaluation.research_benchmark run --fixture-models --database sqlite+aiosqlite:///./deliverables/research/fixture-benchmark.db --output deliverables/research/fixture-test.json
```

`prepare` 生成 150 条合成用例，类别配额 SQL 30、RAG 40、Hybrid 40、MCP 20、负例 20；开发 50、测试 100，问题族不跨集合。它们用于测试数据格式和实验执行，**不是人工标注的真实金标准**。`--fixture-models` 使用词元哈希和词重合评分，只验证实验管线；其数值不能证明 E5 或 BGE 的质量。

真实数据按相同 JSON schema 替换：固定官网正文快照、chunk ID、查询日期、字段事实；标注每条证据支持的事实以及 0/1/2 相关性等级。将等级 2 的 ID 放入 `relevant_ids`；保留项目全集 `gold_programs`、必需原子事实 `required_claims`、路由和拒答/澄清目标。将 `synthetic` 设为 false，并把用例 `annotation_status` 设为 `human_reviewed`。一个学校或问题族及中英文改写只能属于同一划分。

对四种方案的候选取并集，补完人工判断后锁定测试集。网络内容不能穷尽时明确记录“Recall 相对于已标注集合”。不能只把某一个方案找到的证据当作全部金标准。

真实模型的 A/B/C/D 实验命令：

```powershell
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.evaluation.research_benchmark run --dataset deliverables/research/gold.json --split dev --output deliverables/research/dev.json
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.evaluation.research_benchmark calibrate --input deliverables/research/dev.json --output deliverables/research/calibration.json
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.evaluation.research_benchmark run --dataset deliverables/research/gold.json --split test --output deliverables/research/test.json
```

真实评测必须使用名为 `opportunity_research_eval_*` 的独立 PostgreSQL 数据库；代码拒绝将真实评测写入业务库。PostgreSQL 路径使用 pgvector 精确向量排序、`simple` 配置的全文检索和 RRF，并建立全文 GIN 索引。SQLite 仅可通过显式传入 URL 用于合成 fixture 测试；real150 `build-pool` 不接受 SQLite。A 为 vector-only，B 为 PostgreSQL 全文检索＋向量 RRF，C 复用 B 同一候选集重排，D 加最多 2 个受约束改写。B/C 的 Candidate Recall@50 必须相同。JSON 保留逐题排名和延迟，Markdown 输出 Precision@5、真实 Recall@5、Hit@5、MRR@50、Candidate Recall@50、p50/p95；逐题配对 bootstrap 报告 Recall 差值和 95% 区间。

当前 ablation runner 只比较文本检索，对 RAG、Hybrid 和无答案文本问题执行。SQL 字段正确性、MCP/新鲜度、最终回答和补查成功率由独立回归及答案标注评测验证，不应从这张检索表推算。

每个答案的独立标注记录结构：

```json
[
  {
    "case_id": "金标准用例ID", "round": 1, "outcome": "answer",
    "programs": ["项目ID"], "claims": ["原子事实ID"],
    "correct_claims": ["人工核验正确的事实ID"],
    "supported_claims": ["能从给Synthesizer的上下文推出的事实ID"],
    "citation_pairs": [["事实ID", "证据ID"]],
    "valid_pairs": [["事实ID", "真正支持它的证据ID"]]
  }
]
```

```powershell
python -m opportunity_agent.v2.evaluation.research_benchmark score-answers --dataset deliverables/research/gold.json --input deliverables/research/answer-labels.json --output deliverables/research/answer-report.json
```

输出项目与事实 Precision/Recall、Citation Precision/Coverage、Answer Faithfulness、首轮与最后一轮 Task Success Rate。成功率取独立金标准，不使用在线 Checker PASS 充当正确标签。无引用的 Citation Precision 为 N/A；无答案用例单独计算拒答表现。至少抽查 20% 的辅助 LLM 标注及全部争议样例。

若标注记录加 `variant: "A"`（或 B/C/D），`score-answers` 会分别输出各方案的答案指标汇总，便于与检索表配对比较。

## OTel

GPU overlay 启用 OTLP HTTP exporter 和 Jaeger：浏览 `http://localhost:16686`，选择 `opportunity-research`。API 的 `agent.run`、A2A `domain.execute` 和 `research.execute` 通过 W3C traceparent/tracestate 关联。Research 子 span 覆盖解析、路由、SQL、向量、全文、RRF、重排、搜索/读取及结果校验。

```powershell
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4318"
$env:OTEL_SERVICE_NAME = "opportunity-research-local"
```

trace 只包含 ID、模型与数量等统计信息；详细证据排名保存在离线报告和 Research diagnostics 中。用户原文、简历和密钥不作为 span 属性。在线执行没有评估 Agent，旧会话 JSON 迁移继续暂缓。

## 十校 150 条真实官网评测集

数据集工具固定覆盖 CMU、UIUC、UCSD、UCSB、UW、Duke、Brown、Northeastern、USC 和 Georgia Tech。查询由可复现生成器产生，只有完成查询、项目、来源、事实、项目金标准和证据人工审核后，才能从 `draft.json` 导出 `gold.json`。这类数据应称为“真实官网语料＋人工金标准的生成查询”，不能称为真实用户日志。

```powershell
# 启动现有 Compose 中的 PostgreSQL，并创建隔离的 real150 评测库（只创建，不清空已有库）
docker compose up -d postgres
.\.venv-research\Scripts\python.exe scripts\verify_research_postgres.py

# 评测池和 A/B/C/D runner 默认都使用这个专用 PostgreSQL 数据库
$env:RESEARCH_EVAL_DATABASE_URL = "postgresql+asyncpg://opportunity:opportunity_dev_only@127.0.0.1:5432/opportunity_research_eval_real150_v2"

# 1. 生成150条查询草稿和人工审核队列
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.evaluation.research_dataset generate-cases

# 2. 使用Tavily MCP发现并冻结官网页面；E5 tokenizer 会按章节切块并保留 section_path。
# 若 tokenizer 不在本地模型缓存，允许这一次下载：$env:RESEARCH_ALLOW_MODEL_DOWNLOAD = "1"
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.evaluation.research_dataset collect --max-pages-per-program 5

# 3. 在独立 PostgreSQL/pgvector/全文检索库建立 A/B/C/D 候选池
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.evaluation.research_dataset build-pool

# 4. 只在本机启动人工标注页面
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.evaluation.research_dataset serve
# 浏览 http://127.0.0.1:8765

# 5. 随时查看缺失审核、双标分歧和Kappa
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.evaluation.research_dataset validate

# 6. 全部审核完成后才允许导出
.\.venv-research\Scripts\python.exe -m opportunity_agent.v2.evaluation.research_dataset export
```

采集器对 Duke 的普通 CS 硕士会拒绝本科生专属的 4+1 招生页，并额外读取 Graduate School 的截止日期、GRE、英语考试政策页。三页作为独立来源保存；截止日期只从 Master's 表中取 Computer Science 行，GRE 只从 Computer Science (MS) 行取，Ph.D. 行不会混入。英语页按适用人群和 waiver 记录为条件要求。Source 审核时需确认共享研究生院政策确实适用于目标项目；Fact 审核仍需逐条核实自动提取的值和原文。

重新采集导致审核项退出当前队列时，旧条目快照会进入 `review-archive.json`，供“修改上一条”查看/修订；归档项不会参与当前进度、候选池或 `gold.json`。这样能保留审核历史，同时避免已判定为错误项目的来源重新进入评测语料。

标注页面的 Evidence 阶段使用快捷选择：1=无关，2=背景相关，3=直接支持。选择 3 时必须勾选它支持的原子事实，且项目必须匹配、来源必须可靠、入学季不得冲突。Program、Query、Source、Fact、Gold 和 Answer 阶段分别处理程序不能安全代替人的项目边界、问题自然度、页面适用性、结构化事实、合格项目集合及最终答案事实/引用审核。

所有中间产物位于 `deliverables/research/real150/`。`annotations.sqlite` 是本地可恢复标注状态，不提交；`review-queue.json`、`candidate-pool.json` 保留审计输入；`gold.json` 是唯一可用于正式指标的导出。20% 的问题—证据对由两个不同标注者复核，分歧须由第三个标注者以 adjudication 记录解决，加权 Cohen's Kappa 低于 0.70 时禁止正式导出。

真实语料分块版本为 `research-real150-v2-e5-sectioned`。从旧草稿继续运行 `collect` 时，会用 E5 fast tokenizer 将已冻结正文离线重切块；无需为了分块变更重新抓取网页。新的 chunk IDs 会使旧的 evidence judgments 不再适用于新候选，旧候选池会在完整 `build-pool` 时保留为带旧版本号的备份；历史标注仍在 `annotations.sqlite` 中，但新 chunk 必须重新标注。`source`、`fact`、`query` 等 item ID 若未改变，其既有审核仍可沿用。

同一网页可能被不同搜索意图或重定向 URL 命中。一个项目下同一来源 ID 只保存一份快照，多个用途保存在 `page_types`，事实候选合并保留。重切前先合并重复来源和 chunk；入库前再次按 chunk ID 去重，遇到同 ID 不同正文或项目元数据则明确报错。旧草稿若含重复记录，可运行 `python -m opportunity_agent.v2.evaluation.research_dataset repair-corpus`：离线修复 `draft.json` 和审核队列，同时保存 `draft.before-dedup.json`、`review-queue.before-dedup.json`；保留现有 chunk/source IDs、查询和人工审核数据库，然后直接运行 `build-pool`，无需重新采集。

## 导出 LLM 证据预标注包

```powershell
python -m opportunity_agent.v2.evaluation.research_dataset export-llm --batch-size 10
# 展开第一批，输出包含 system_prompt、output_schema、input 的独立请求
python -m opportunity_agent.v2.evaluation.research_dataset llm-batch
# 指定批次 ID；ID 在导出文件的 batches 中
python -m opportunity_agent.v2.evaluation.research_dataset llm-batch --batch-id batch-实际ID
```

`export-llm` 只生成一个 `deliverables/research/real150/llm-evidence-review.json`，包含候选池的全部问题—片段对，包括额外困难负例。`--output` 可指定输出文件。它不重新采集、切块或检索，不改变候选池和人工标签，也不调用外部 LLM。人工来源审核和问题修订通过只读 SQLite 连接读取；问题修订与候选池不一致时，要求先重建候选池。

文件内 `cases` 是问题索引，`documents` 是去重正文索引，`batches` 为每批的 `batch_id/case_id/chunk_ids`。共享正文只存一次，同一片段对不同问题仍分别判断。`counts` 保存问题数、标注对数、独立片段数、批次数；`input_hashes` 锁定草稿、候选池和审核上下文版本。提示词保存在 `system_prompt`，输出结构保存在 `output_schema`。排名、重排分数、既有相关性标签和预期拒答结果不会提供给模型。每批默认最多10条片段；单个文件是完整任务包，实际调用时按批展开，避免一次输入全部材料或要求一次输出数千条标签。

发送方式：将 `llm-batch` 输出的 `system_prompt` 作为系统提示词，`input` 作为用户输入，并要求符合 `output_schema` 的 JSON 回复。使用聊天网页时可以复制提示词和一批展开的数据；支持文件读取/脚本的环境可以上传完整任务包，按 `batches` 顺序展开。跨批不得混用片段的证据。

回复结构示例（仅展示格式，ID、claim和引用须替换成当前批次实际内容）：

```json
{
  "batch_id": "batch-原输入ID",
  "case_id": "原问题ID",
  "judgments": [
    {
      "chunk_id": "原片段ID",
      "relevance": 2,
      "supports_claims": ["输入中的claim_id"],
      "supporting_quotes": [{"claim_id": "输入中的claim_id", "quote": "片段中逐字复制的支持原文"}],
      "program_match": "exact",
      "intake_match": "unknown",
      "source_reliable": "yes",
      "needs_human_review": false,
      "reason": "简短说明片段如何支持该事实。",
      "label_origin": "llm_proposed"
    }
  ]
}
```

内部 `relevance=0/1/2` 对应前端按键 `1/2/3`；`null` 代表不确定并且必须请求人工复核。等级2需要有效项目范围、可靠来源和原文引用；等级0/1/null的支持字段为空。`needs_human_review` 是疑点标记，false不表示人工已确认。`validate_llm_response(request, response)` 可检查ID覆盖、重复、claim归属及引用确实存在于原片段；它不判断语义标签是否正确。所有机器输出保持 `llm_proposed`，此次功能不自动导入人工审核表，也不放宽正式金标准的导出规则。提示词全文在导出文件中，可直接复制；调用方另行记录实际使用的模型版本。
# LLM 预标注复核（本地、独立数据层）

原 evidence 与 LLM 复核页都使用本地 Markdown 阅读组件：表格、标题、列表、引用和代码块，
不访问外部 CDN。仅调整展示，不修改 chunk、ID、hash 或标签；逐字引用以折叠面板中的原始文本为准。
不完整表格不猜测表头或缺失字段。正文 HTML 始终转义，链接仅允许 HTTP/HTTPS，图片不自动加载。

启动原标注服务后打开 `http://127.0.0.1:8765/llm`（首页也有入口）。
页面选择完整 `llm-evidence-review-results.json` 并导入；也可先执行：

```powershell
python -m opportunity_agent.v2.evaluation.research_dataset import-llm --input "D:/下载/llm-evidence-review-results.json"
python -m opportunity_agent.v2.evaluation.research_dataset serve
```

导入校验全部批次、问题/片段 ID、输入 hash、统计值、Schema 和逐字引用。
原字节按 SHA256 保存到 `deliverables/research/real150/llm-snapshots/<sha256>.json`；
使用独占创建，重导入不覆盖文件，加载时复核完整性。它不是操作系统级防篡改存储。
原 `judgments` 人工记录不改变。新增 `llm_imports/llm_proposals/llm_human_reviews/llm_review_history`
保存在同一个 `annotations.sqlite`，导入不会产生任何人工审核标签。

筛选顺序：等级 2 → 需复核 → 无机器正例的 RAG/背景匹配问题 A/B/C/D Top5 并集 → 随机 0/1。
“全部重点”按该顺序去重。随机默认 100 对，固定种子 20261007，可调整。
有效既有人工标签和新复核会从所有待办筛选中跳过；既有多人冲突不自动采用机器意见。
接受表示接受原 LLM 标签；修改表示采用表单内容。等级2必须选择事实并填写逐字引用。
快捷键1/2/3只改变等级，随后点击“保存当前人工修订”。支持“修改上一条”，所有提交保留历史。
既有人工意见需要明确确认才允许本次复核作为优先修订，原人工记录仍保留。

三个导出按钮：人工标签、原始机器标签、人工优先混合数据。
混合导出的 `effective_labels` 保留每对 `label_origin`；`machine_proposals` 保存原机器结果，
`human_labels` 保存确认/修订/既有人工记录；`dataset.cases` 给出相关ID和标签来源映射。
没有等级2的查询标记 `needs_answerability_review`，不能自动当作无答案。
旧人工等级2未保存引用时，导出不会伪造引用。

```powershell
python -m opportunity_agent.v2.evaluation.research_dataset export-reviewed
```

默认生成 `llm-reviewed-merged.json`，不会覆盖 `gold.json` 或预标注快照。
混合数据明确为 `llm_assisted_not_human_gold`；不能直接通过现有纯人工 benchmark/gold 校验。
本功能不放宽双人标注、不修改检索指标分母；正式评测前仍需处理查询可回答性及标签来源。
