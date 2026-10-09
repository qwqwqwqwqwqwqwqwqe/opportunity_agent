"""MCP-neutral structured tools shared by LangGraph and openJiuwen adapters."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Application, ApplicationTask, OfficialSource
from ..rag.retrieval import HybridRetriever
from ..services.applications import ApplicationCommandService


class V2ToolService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search_official_requirements(self, query: str, school: str = "", program: str = "") -> dict[str, Any]:
        hits, trace = await HybridRetriever(self.session).search(query, filters={"school": school, "program": program})
        return {"hits": [{"source_id": item.source_id, "title": item.title, "url": item.url, "excerpt": item.content[:800], "score": item.score} for item in hits], "trace": trace}

    async def get_official_source(self, source_id: str) -> dict[str, Any] | None:
        item = await self.session.get(OfficialSource, source_id)
        if not item:
            return None
        return {"id": item.id, "title": item.title, "url": item.url, "excerpt": item.excerpt, "status": item.status}

    async def list_application_tasks(self, user_id: str) -> list[dict[str, Any]]:
        rows = await self.session.execute(select(ApplicationTask, Application).join(Application).where(Application.user_id == user_id))
        return [{"id": task.id, "application_id": application.id, "university": application.university, "program": application.program,
                 "title": task.title, "stable_key": task.stable_key, "status": task.status, "due_at": task.due_at.isoformat() if task.due_at else None}
                for task, application in rows]

    async def propose_application_change(self, user_id: str, proposal_type: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        approval = await ApplicationCommandService(self.session).propose(user_id, proposal_type, payload, request_id)
        return {"proposal_id": approval.proposal_id, "approval_id": approval.id, "approval_token": approval.token, "status": approval.status}

    async def apply_confirmed_change(self, user_id: str, approval_id: str) -> dict[str, Any]:
        proposal = await ApplicationCommandService(self.session).decide(approval_id, user_id, accept=True)
        return {"proposal_id": proposal.id, "status": proposal.status}
