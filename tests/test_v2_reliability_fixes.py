"""Regression tests for review findings at the orchestration boundaries."""
import asyncio
import importlib
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v2.agents.contracts import (
    CompletionResult, Evidence, ExecutionState, PlanResult, ProfileResult,
    ProgramResult, ResearchFact, ResearchResult, RouteDecision, SuccessCriteria,
)
from opportunity_agent.v2.agents.orchestrator import (
    CustomOrchestrator, DeterministicSynthesizer, HeuristicGoalParser, HeuristicRouter, LLMSynthesizer,
)
from opportunity_agent.v2.agents.planning_agent import _verified_research
from opportunity_agent.v2.agents.result_aggregation import ResultAggregator
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import AgentRun, Conversation, Message, Profile
from opportunity_agent.v2.repositories import Repository, VersionConflict
from opportunity_agent.v2.research.quality import program_matches
from opportunity_agent.v2.services.auth import AuthService
from opportunity_agent.v2.services.applications import ApplicationCommandService


def state(message="你好"):
    return ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q", message=message)


async def database(path=None):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(path) if path else "sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def sql_program():
    e = Evidence(source_id="official", url="https://example.edu/admissions", authority="official",
                 program_match="exact", intake="2027 Fall", supports_fields=["gre_policy"],
                 excerpt="GRE is optional", relevance_method="sql_exact", relevance_passed=True,
                 expires_at=date.today() + timedelta(days=30))
    return ProgramResult(program_id="p", university="Example", program="MSCS", intake="2027 Fall",
        gre_policy="optional", evidence=[e], facts=[ResearchFact(field="gre_policy", value="optional",
        verification_status="verified", evidence_ids=[e.evidence_id])])


def test_planning_accepts_sql_exact_without_a_similarity_score_and_rejects_expired():
    p = sql_program()
    assert program_matches(p, SuccessCriteria(gre_policy="not_required", evidence_required=True))
    raw = ResearchResult(programs=[p], status="complete").model_dump(mode="json")
    assert _verified_research(raw).requirements[0].field == "gre"
    p.evidence[0].relevance_score = .95
    p.evidence[0].expires_at = date.today() - timedelta(days=1)
    assert _verified_research(ResearchResult(programs=[p]).model_dump(mode="json")).requirements == []


def test_refresh_token_survives_sqlite_round_trip_and_is_consumed_once():
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as s:
                user = await AuthService(s).register("refresh@example.com", "long-password")
                _, token = await AuthService(s).issue_tokens(user)
            async with factory.begin() as s:
                assert await AuthService(s).rotate_refresh(token) is not None
            async with factory.begin() as s:
                assert await AuthService(s).rotate_refresh(token) is None
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_concurrent_refresh_only_one_request_wins(tmp_path):
    async def scenario():
        engine, factory = await database(tmp_path / "refresh.db")
        try:
            async with factory.begin() as s:
                user = await AuthService(s).register("concurrent@example.com", "long-password")
                _, token = await AuthService(s).issue_tokens(user)
            async def rotate():
                async with factory.begin() as s:
                    return await AuthService(s).rotate_refresh(token)
            results = await asyncio.gather(rotate(), rotate())
            assert sum(result is not None for result in results) == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_request_identity_claim_and_message_conflict(tmp_path):
    async def scenario():
        engine, factory = await database(tmp_path / "runs.db")
        try:
            async with factory.begin() as s:
                s.add(Conversation(id="c", user_id="u", title="review"))
            async def create():
                async with factory.begin() as s:
                    return (await Repository(s).create_run("u", "c", "你好", "same")).id
            ids = await asyncio.gather(create(), create())
            assert ids[0] == ids[1]
            async with factory.begin() as s:
                assert await s.scalar(select(func.count()).select_from(Message)) == 1
                assert await Repository(s).claim_run(ids[0], "first")
            async with factory.begin() as s:
                assert not await Repository(s).claim_run(ids[0], "second")
                with pytest.raises(VersionConflict):
                    await Repository(s).create_run("u", "c", "different", "same")
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_objective_goal_cannot_be_bypassed_by_direct_reply():
    class WrongRouter:
        async def route(self, execution, forced_agents=()):
            return RouteDecision(mode="direct_reply", reason="misclassified")
    class Agents:
        async def execute(self, agent, execution, missing_task=None):
            assert agent == "research"
            return ResearchResult(programs=[sql_program()], status="complete")
    out = asyncio.run(CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=WrongRouter(),
        agent_client=Agents(), synthesizer=DeterministicSynthesizer()).run(state("找1个不要GRE的项目")))
    assert out.route_decision.mode == "delegate" and out.completion.status == "PASS"


def test_partial_or_outdated_plan_cannot_be_proposed():
    s = state()
    s.profile_result = ProfileResult(proposals=[{"type": "profile.change", "payload": {}}])
    s.plan_result = PlanResult(status="complete", roadmap={"timeline": {}}, input_versions={"research_revision": "old"})
    assert ResultAggregator().approval_proposals(s) == s.profile_result.proposals
    s.plan_result.input_versions = {}
    s.completion = CompletionResult(status="PARTIAL")
    assert ResultAggregator().approval_proposals(s) == s.profile_result.proposals


def test_approval_rechecks_incomplete_run():
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as s:
                s.add(Profile(user_id="u", payload={}, version=1))
                s.add(AgentRun(id="r", user_id="u", conversation_id="c",
                    graph_state={"completion": {"status": "PARTIAL"}}))
                await s.flush()
                service = ApplicationCommandService(s)
                approval = await service.propose("u", "plan.replace", {
                    "roadmap": {"timeline": {}}, "tasks": [], "expected_profile_version": 1}, "q", "r")
                with pytest.raises(ValueError, match="evidence is incomplete"):
                    await service.decide(approval.id, "u", True)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_synthesis_rejects_unknown_links_but_keeps_accepted_sources():
    p = sql_program()
    s = state()
    s.research_result = ResearchResult(programs=[p], status="complete")
    client = LLMClient(completion_fn=lambda payload:
        "[官方](https://example.edu/admissions) [伪造](https://invented.example/fact)")
    answer = asyncio.run(LLMSynthesizer(client).synthesize(s))
    assert "https://example.edu/admissions" in answer
    assert "invented.example" not in answer
    assert any(e.type == "citation_validation" for e in s.events)


def test_budget_includes_goal_parser():
    class SlowParser:
        async def parse(self, message):
            await asyncio.sleep(1)
    out = asyncio.run(CustomOrchestrator(goal_parser=SlowParser(), router=HeuristicRouter(),
        synthesizer=DeterministicSynthesizer(), execution_budget_seconds=.02).run(state()))
    assert out.completion.status == "PARTIAL"
    assert out.result_history == [] and out.answer


def test_independent_agents_run_concurrently_before_planning():
    async def scenario():
        ready = set()
        event = asyncio.Event()
        class Agents:
            async def execute(self, agent, execution, missing_task=None):
                if agent in {"profile", "research"}:
                    ready.add(agent)
                    if len(ready) == 2:
                        event.set()
                    await asyncio.wait_for(event.wait(), .5)
                    return ProfileResult() if agent == "profile" else ResearchResult(programs=[sql_program()], status="complete")
                assert execution.profile_result is not None and execution.research_result is not None
                return PlanResult(status="complete")
        s = state()
        s.route_decision = RouteDecision(mode="delegate", agents=["profile", "research", "planning"], parallel=True, reason="independent")
        orchestrator = CustomOrchestrator(agent_client=Agents())
        await orchestrator._execute_routed_agents(s, s.route_decision.agents)
        assert not s.agent_failures and s.plan_result.status == "complete"
    asyncio.run(scenario())


def test_dispatcher_recovers_expired_runs(monkeypatch):
    api = importlib.import_module("opportunity_agent.v2.api.app")
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as s:
                s.add(AgentRun(id="expired", user_id="u", conversation_id="c", request_id="q", status="running",
                    execution_token="old", lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1), graph_state={"request_id": "q"}))
                s.add(Message(conversation_id="c", role="user", request_id="q", content="你好"))
            monkeypatch.setattr(api, "SessionLocal", factory)
            jobs = []
            monkeypatch.setattr(api, "schedule_run", lambda *args: jobs.append(args))
            task = asyncio.create_task(api.dispatch_runs())
            for _ in range(100):
                if jobs:
                    break
                await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert jobs[0][0] == "expired"
            async with factory() as s:
                assert (await s.get(AgentRun, "expired")).status == "queued"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_migration_preserves_historical_duplicates_and_event_cursor(tmp_path):
    from sqlalchemy import create_engine, text, inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    # Load the revision without assuming alembic/ is a Python package.
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("run_migration", Path("alembic/versions/0006_run_reliability.py"))
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///" + str(tmp_path / "migration.db"))
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE agent_runs (id VARCHAR PRIMARY KEY, conversation_id VARCHAR, graph_state JSON, created_at DATETIME)"))
            conn.execute(text("CREATE TABLE agent_events (run_id VARCHAR, sequence INTEGER)"))
            for ident in ("first", "second"):
                conn.execute(text("INSERT INTO agent_runs VALUES (:id, 'c', :state, '2026-10-05')"),
                             {"id": ident, "state": '{"request_id":"same"}'})
            conn.execute(text("INSERT INTO agent_events VALUES ('first', 7)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
            rows = conn.execute(text("SELECT id, request_id, event_sequence FROM agent_runs ORDER BY id")).all()
            assert rows == [("first", "same", 7), ("second", None, 0)]
            assert "uq_run_request" in {c["name"] for c in inspect(conn).get_unique_constraints("agent_runs")}
    finally:
        engine.dispose()


def test_run_migration_can_follow_metadata_based_foundation(tmp_path):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import create_engine
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location("fresh_run_migration", Path("alembic/versions/0006_run_reliability.py"))
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///" + str(tmp_path / "fresh.db"))
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn)
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
    finally:
        engine.dispose()


def test_replaced_execution_cannot_finalize_and_input_identity_survives():
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as session:
                run = AgentRun(id="lease", user_id="u", conversation_id="c", request_id="q",
                    status="running", execution_token="new", graph_state={"message": "你好", "request_id": "q"})
                session.add(run)
                await session.flush()
                repo = Repository(session)
                with pytest.raises(VersionConflict):
                    await repo.finalize_run(run, {"answer": "stale"}, "completed", "old")
                await repo.finalize_run(run, {"answer": "current"}, "completed", "new")
                assert run.graph_state == {"message": "你好", "request_id": "q", "answer": "current"}
                assert run.status == "completed" and run.lease_expires_at is None
        finally:
            await engine.dispose()
    asyncio.run(scenario())
