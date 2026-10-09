# Opportunity Agent V2 Architecture

V2 turns the existing planning demo into an auditable, long-horizon system.  It
does not delete the V1 implementation: the V1 JSON service remains the legacy
source and its profile/timeline logic is reused by the V2 agents.

```text
Browser -> FastAPI / SSE -> Custom Python Orchestrator -> Router (route only)
                              | Profile / Research / Planning via A2A
                              v
                     Aggregation -> Checker -> Synthesizer
                              |
                  Memory Service -> PostgreSQL preferences / audit / outbox
                              |                   |
                       approval gate       Memory Consolidator worker
```

## Boundaries

| Layer | Code | Responsibility |
|---|---|---|
| HTTP/auth | `opportunity_agent/v2/api/app.py` | Versioned API, cookies/JWT, SSE |
| Persistence | `v2/db/models.py`, `v2/repositories.py` | SQLAlchemy tables, ownership, versions and event trace |
| Orchestration | `v2/agents/orchestrator.py` | Custom control loop, targeted repair, tool-free synthesis |
| Preferences | `v2/services/memory.py` | Sole preference authority: explicit writes, inferred approvals, versioned retrieval |
| Consolidation | `v2/services/memory_consolidator.py` | PASS-only immutable user evidence, durable outbox and retries |
| Approval | `v2/services/applications.py` | Only accepted proposals mutate profile/application/task data |
| Retrieval | `v2/rag/` | Official-only document ingestion and hybrid RRF retrieval |
| Interop | `v2/mcp/`, `v2/agents/openjiuwen_adapter.py` | MCP server and existing openJiuwen capability catalogue |
| Evaluation | `v2/evaluation/` | Persisted cases/runs and offline metrics |

## Local start

```powershell
docker compose up --build
# API: http://127.0.0.1:8000/docs
```

For a no-Docker smoke test, install `.[v2]`, set
`DATABASE_URL=sqlite+aiosqlite:///./data/v2.db` and `AUTO_CREATE_SCHEMA=1`, then
run `uvicorn opportunity_agent.v2.api.app:app --reload`.

`alembic upgrade head` is the PostgreSQL migration path.  V1 JSON is never
deleted. Importing `data/conversations.json` remains deferred.

## Safety properties

* Long-lived profile, task and application state is in PostgreSQL, not Redis or
  browser storage.
* Model/Tool output is a proposal.  Approval is mandatory for profile changes,
  task completion, deadlines and application-list commands.
* RAG V2 indexes reviewed official sources only; absence of a source is never
  converted into a policy conclusion.
* Agent events are persisted per run and stream through SSE, allowing replay and
  interview demos of route selection, retrieval, tool calls and approval.
