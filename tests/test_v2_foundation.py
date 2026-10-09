from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator, DeterministicSynthesizer, HeuristicRouter
from opportunity_agent.v2.core.security import make_access_token, parse_access_token, verify_password
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import OfficialSource, Profile
from opportunity_agent.v2.rag.ingest import OfficialIngestionService
from opportunity_agent.v2.rag.retrieval import EmbeddingProvider, HybridRetriever
from opportunity_agent.v2.services.applications import ApplicationCommandService
from opportunity_agent.v2.services.auth import AuthService


def run(coro):
    return asyncio.run(coro)


async def memory_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


def test_v2_auth_hash_and_jwt_round_trip():
    async def scenario():
        engine, factory = await memory_session()
        async with factory() as session:
            user = await AuthService(session).register("V2@example.com", "long-enough-password")
            assert verify_password("long-enough-password", user.password_hash)
            assert parse_access_token(make_access_token(user.id)) == user.id
        await engine.dispose()
    run(scenario())


def test_orchestrator_produces_proposal_not_direct_write():
    result = run(CustomOrchestrator(router=HeuristicRouter(), synthesizer=DeterministicSynthesizer()).ainvoke({
        "user_id": "user", "conversation_id": "conversation", "run_id": "run", "request_id": "request",
        "message": "我的托福是105分", "profile_payload": {}, "events": [],
    }))
    assert result["approval_required"] is True
    assert result["proposals"][0]["type"] == "profile.change"
    assert any(event["type"] == "approval_required" for event in result["events"])


def test_approval_gate_applies_profile_only_after_acceptance():
    async def scenario():
        engine, factory = await memory_session()
        async with factory.begin() as session:
            user = await AuthService(session).register("gate@example.com", "long-enough-password")
            service = ApplicationCommandService(session)
            approval = await service.propose(user.id, "profile.change", {
                "facts": [{"field": "toefl_score", "raw_value": 105, "normalized_value": 105,
                           "confidence": 1.0, "source": "user", "operation": "set"}],
            }, "r1")
            assert approval.status == "pending"
            assert await session.scalar(select(Profile)) is None
            await service.decide(approval.id, user.id, True)
        async with factory() as session:
            profile = await session.scalar(select(Profile))
            assert profile.payload["toefl_score"] == 105
        await engine.dispose()
    run(scenario())


def test_hybrid_retrieval_has_keyword_fallback_without_model_download():
    async def scenario():
        engine, factory = await memory_session()
        async with factory.begin() as session:
            source = OfficialSource(source_key="cmu-mscs", university="CMU", program="MSCS", url="https://www.cs.cmu.edu/admissions", title="CMU Admissions")
            session.add(source)
            await session.flush()
            await OfficialIngestionService(session).ingest(source, "CMU MSCS application deadline is December 10. GRE policy is optional.",
                                                           {"school": "CMU", "program": "MSCS"})
        async with factory() as session:
            embedder = EmbeddingProvider()
            embedder.embed = lambda _: None  # type: ignore[method-assign]
            hits, trace = await HybridRetriever(session, embedder).search("CMU MSCS deadline", filters={"school": "CMU"})
            assert hits and hits[0].url.startswith("https://")
            assert trace["mode"] == "keyword_fallback"
        await engine.dispose()
    run(scenario())
