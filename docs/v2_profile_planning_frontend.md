# V2 Profile、Planning 与前端运行说明

V2 页面位于 `http://127.0.0.1:8000/v2`，V1 页面保持原路径。首次进入先注册，填写画像并在“待确认”中确认变更，然后在聊天中输入“请根据我的背景生成申请规划”。规划结果先进入“待确认”，确认后才成为当前计划，任务进度操作也需要确认。

## Docker Compose

在 `examples/opportunity_agent` 目录执行：

```powershell
docker compose up --build
```

Compose 构建默认使用已验证可访问的清华 PyPI 镜像；如需官方源，在项目 `.env` 中设置 `PIP_INDEX_URL=https://pypi.org/simple`。这个设置仅用于镜像构建时安装 Python 依赖，不影响 `LLM_API_KEY` 等运行时配置，也不需要在 Windows 主机上再次安装 V2 依赖。

API 容器启动时先运行 `alembic upgrade head`，再启动 FastAPI。已有 PostgreSQL 数据会原地增加计划版本、计划任务和审批字段；不会迁移 V1 的 `data/conversations.json`。需要调用远程模型时，在启动前配置 `LLM_API_KEY`、`LLM_API_BASE` 和 `LLM_MODEL`。未配置时，规划会使用 V1 的确定性任务和文章；聊天的 Router/Synthesizer 仍需模型，除非在测试中显式替换为离线实现。

健康检查：`http://127.0.0.1:8000/healthz`。三个 A2A Agent 卡分别位于 8771、8772、8773 端口的 `/.well-known/agent-card.json`。若使用 `docker compose up --build`，修改代码后需要重新构建镜像；浏览器刷新 `/v2` 即可看到新页面。

```powershell
docker compose ps
curl.exe http://127.0.0.1:8000/healthz
curl.exe http://127.0.0.1:8771/.well-known/agent-card.json
curl.exe http://127.0.0.1:8773/.well-known/agent-card.json
```

浏览器验收顺序：注册 → 编辑画像 → 确认画像提案 → 发起规划 Run → 查看 SSE 进度和计划预览 → 确认计划 → 完成一个任务并确认 → 再次规划，检查该任务进度仍在且旧版本可查看。拒绝提案时，画像与计划均不应改变。

## 本地启动

```powershell
python -m pip install -e ".[dev,v2]"
$env:JWT_SECRET = "replace-with-a-long-random-local-secret"
powershell -ExecutionPolicy Bypass -File .\scripts\run_v2_local.ps1 -Reload
```

本地脚本使用 SQLite，先运行同一组 Alembic 迁移。要使用三个独立 A2A 服务，还需分别启动 `python -m opportunity_agent.v2.agents.a2a profile`、`research`、`planning`，并设置 `DOMAIN_AGENT_TRANSPORT=a2a`；不设置时使用相同的本地领域逻辑。

## 验证

```powershell
python -m pytest -q -p no:cacheprovider tests/test_v2_profile_planning_flow.py tests/test_v2_web_flow.py tests/test_v2_a2a_domain_agents.py tests/test_v2_orchestration_skeleton.py
```

Research Agent 当前仍以结构化可控结果验证主链路；SQL/RAG/Hybrid/MCP 和记忆服务属于原计划后续阶段。Planning 仅把已有且有可靠来源的 Research 结果纳入计划，缺证据的项目要求会显示待核验。`sentence-transformers` 及其大型 PyTorch/CUDA 依赖已移入可选的 `rag` extra；当前 Docker 镜像不安装它，检索模块在没有本地嵌入模型时会退化为关键词路径。以后启用本地向量检索时，需改为安装 `.[v2,rag]` 并准备模型权重。
