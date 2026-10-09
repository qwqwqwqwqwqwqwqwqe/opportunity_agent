"""Research domain facade. Synchronous A2A workers own an isolated async DB loop."""
from __future__ import annotations

import asyncio
import os

from .contracts import ProgramResult, ResearchResult
from .domain_request import DomainRequest


class ResearchAgent:
    """Return evidence-bearing research results without generating a user-facing answer."""

    def execute(self, request: DomainRequest) -> ResearchResult:
        # Explicit fixture compatibility only; production never treats memory as a knowledge base.
        if os.getenv("RESEARCH_ALLOW_SEED_FIXTURES", "0") == "1":
            programs = [ProgramResult.model_validate(p) for p in request.relevant_memory.get("seed_research_programs", [])]
            return ResearchResult(programs=programs, route="stub", status="complete" if programs else "no_results")
        return asyncio.run(self.aexecute(request))

    async def aexecute(self, request):
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from sqlalchemy.pool import NullPool
        from ..core.config import settings
        from ..research.service import ResearchService
        # Avoid sharing asyncpg connections between the A2A worker threads' event loops.
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return await ResearchService(session).execute(request)
        finally:
            await engine.dispose()
