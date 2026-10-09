"""Preference authority, consolidation safety and HTTP integration regressions."""
import asyncio
import importlib
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v2.agents.a2a import request_from_state
from opportunity_agent.v2.agents.contracts import ExecutionState, ProfileResult, ResearchResult, ProgramResult, Evidence, CompletionResult
from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator, HeuristicGoalParser, HeuristicRouter, DeterministicSynthesizer
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import MemoryItem, MemoryAudit, MemoryOutbox, ApprovalRequest, ChangeProposal, AgentRun
from opportunity_agent.v2.db.session import get_session
from opportunity_agent.v2.repositories import VersionConflict
from opportunity_agent.v2.services.memory import MemoryService, explicit_preferences
from opportunity_agent.v2.services.memory_contracts import ConsolidationInput, PreferenceSnapshot, PreferenceView, UserMemoryMessage, ConsolidationResult
from opportunity_agent.v2.services.memory_consolidator import MemoryConsolidator, process_memory_job, InferredPreferences
from opportunity_agent.v2.services.applications import ApplicationCommandService


async def database(path=None):
    url = "sqlite+aiosqlite:///"+str(path) if path else "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def payload(**overrides):
    return ConsolidationInput(run_id="r", user_id="u", conversation_id="c", completion={"status": "PASS"},
        user_messages=[UserMemoryMessage(message_id="m", content="毕业以后希望尽快找到工作")], **overrides).model_dump(mode="json")


@pytest.mark.parametrize("text", ["如果我不考虑GRE会怎样？", "我可能不想考GRE", "室友不想考GRE", "他说：不考虑GRE",
                                  "我不考虑GRE吗？", "我假设就业优先", "我听说就业优先", "学校官网说GRE optional"])
def test_non_assertions_never_auto_write(text):
    assert explicit_preferences(text) == []


@pytest.mark.parametrize("text,key,value", [("我不考虑需要GRE的项目", "avoid_gre", True),
    ("我愿意考GRE", "avoid_gre", False), ("我的预算是30万元", "budget_preference", "30万元"),
    ("我的备选国家是加拿大", "fallback_country", "加拿大"), ("我更看重就业", "employment_priority", True)])
def test_explicit_candidates_require_positive_rule_proof(text, key, value):
    assert any(p["key"] == key and p["value"] == value for p in explicit_preferences(text))


def test_save_recall_revoke_isolation_expiry_and_idempotence():
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as session:
                service = MemoryService(session)
                first = await service.save_explicit("u", "我不考虑需要GRE的项目", "q", "c", "m")
                assert first[0]["version"] == 1
                assert await service.save_explicit("u", "我不考虑需要GRE的项目", "q", "c", "m") == []
                assert len((await service.retrieve_preferences("u", "推荐项目")).preferences) == 1
                assert not (await service.retrieve_preferences("other", "推荐项目")).preferences
                assert not (await service.retrieve_preferences("u", "你好")).preferences
                with pytest.raises(VersionConflict):
                    await service.save_explicit("u", "我愿意考GRE", "q", "c", "m")
                with pytest.raises(VersionConflict):
                    await service.revoke("u", "avoid_gre", "bad", 99)
                revoked = await service.revoke("u", "avoid_gre", "revoke", 1)
                assert not revoked.active and revoked.version == 2
                assert (await service.revoke("u", "avoid_gre", "revoke", 1)).version == 2
                assert not (await service.retrieve_preferences("u", "推荐项目")).preferences
                await service.save_explicit("u", "我不考虑GRE", "q2", "c2", "m2")
                row = await session.scalar(select(MemoryItem))
                row.expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
                await session.flush()
                assert not (await service.retrieve_preferences("u", "推荐项目")).preferences
                assert await session.scalar(select(func.count()).select_from(MemoryItem)) == 1
                assert await session.scalar(select(func.count()).select_from(MemoryAudit)) == 3
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_concurrent_writes_are_fenced_by_versions(tmp_path):
    async def scenario():
        engine, factory = await database(tmp_path/"concurrent.db")
        try:
            async def write(index):
                try:
                    async with factory.begin() as session:
                        await MemoryService(session).put("u", "avoid_gre", bool(index), source="user_explicit",
                            confidence=1, evidence="explicit", event_key="q"+str(index), expected_version=0)
                    return "ok"
                except VersionConflict:
                    return "conflict"
            assert sorted(await asyncio.gather(write(0), write(1))) == ["conflict", "ok"]
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(MemoryItem)) == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_vector_recall_and_failure_fallback():
    class Embedder:
        model_name = "memory-test"
        def embed(self, text): return [1.]+[0.]*383
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as session:
                service = MemoryService(session, embedder=Embedder())
                await service.save_explicit("u", "我更看重就业", "q", "c", "m")
                out = await service.retrieve_preferences("u", "规划未来")
                assert out.preferences[0].relevance_method == "vector"
                service.embedder.embed = lambda text: (_ for _ in ()).throw(RuntimeError("offline"))
                out = await service.retrieve_preferences("u", "就业规划")
                assert out.preferences[0].relevance_method == "rule"
            broken = MemoryService(None)
            assert (await broken.retrieve_preferences("u", "项目")).retrieval_status == "unavailable"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("message,expected", [("查询学校项目", "not_required"),
    ("查询要求GRE的学校项目", "required"), ("查询学校项目，不筛选GRE", "any")])
def test_memory_constraints_respect_current_request(message, expected):
    class Agents:
        async def execute(self, agent, state, missing_task=None):
            return ResearchResult(status="no_results")
    async def scenario():
        state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q", message=message,
            preference_memory=PreferenceSnapshot(preferences=[PreferenceView(memory_id="mem", key="avoid_gre", value=True,
                source="user_explicit", confidence=1, version=2)]))
        out = await CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
            synthesizer=DeterministicSynthesizer(), agent_client=Agents(), max_rounds=1).run(state)
        assert out.success_criteria.gre_policy == expected
        if expected == "not_required":
            assert out.success_criteria.memory_constraints[0]["memory_id"] == "mem"
    asyncio.run(scenario())


def test_turn_preference_used_before_research_and_research_privacy():
    class Agents:
        async def execute(self, agent, state, missing_task=None):
            if agent == "profile": return ProfileResult()
            assert state.success_criteria.gre_policy == "not_required"
            request = request_from_state("research", state)
            assert request.conversation_context == {} and request.relevant_memory == {}
            assert request.turn_preferences == [{"key": "avoid_gre", "value": True}]
            return ResearchResult(status="no_results")
    state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q",
        message="我不考虑需要GRE的项目，帮我查询学校项目", conversation_context={"summary": "私人对话"})
    out = asyncio.run(CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
        synthesizer=DeterministicSynthesizer(), agent_client=Agents(), max_rounds=1).run(state))
    assert out.completion.status == "PARTIAL" and out.consolidation_input is None, (out.completion.reasons, out.agent_failures)


def test_inference_proposes_only_and_stale_approval_cannot_overwrite():
    class Model:
        enabled = True
        def generate_structured(self, schema, **kwargs):
            assert "answer" not in kwargs["context"]
            return schema(preferences=[{"key": "employment_priority", "value": True,
                "evidence": "希望尽快找到工作", "message_id": "m", "confidence": .9}])
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as session:
                result = await MemoryConsolidator(Model()).consolidate(session, payload())
                assert result.status == "proposed"
                service = MemoryService(session)
                assert not await service.list_preferences("u")
                approval = await session.get(ApprovalRequest, result.proposal_ids[0])
                await ApplicationCommandService(session).decide(approval.id, "u", True)
                assert (await service.list_preferences("u"))[0].source == "user_confirmed"
                stale = await service.propose_inferred("u", {"key": "employment_priority", "value": False,
                    "evidence": "not priority", "expected_version": 1}, "second")
                await service.save_explicit("u", "我更看重就业", "explicit", "c", "m2")
                with pytest.raises(VersionConflict):
                    await ApplicationCommandService(session).decide(stale.id, "u", True)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("evidence,message_id", [("学校建议就业", "m"), ("希望尽快找到工作", "assistant-id")])
def test_untraceable_inferences_rejected(evidence, message_id):
    class Model:
        enabled = True
        def generate_structured(self, schema, **kwargs):
            return schema(preferences=[{"key": "employment_priority", "value": True,
                "evidence": evidence, "message_id": message_id, "confidence": .99}])
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as session:
                assert (await MemoryConsolidator(Model()).consolidate(session, payload())).status == "no_change"
                assert await session.scalar(select(func.count()).select_from(ChangeProposal)) == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_outbox_commit_idempotence_claim_retry_and_exhaustion(tmp_path):
    class Failure:
        async def consolidate(self, session, body): raise RuntimeError("unavailable")
    class Success:
        calls = 0
        async def consolidate(self, session, body):
            self.calls += 1
            return ConsolidationResult(status="no_change")
    async def scenario():
        engine, factory = await database(tmp_path/"outbox.db")
        try:
            async with factory.begin() as session:
                await MemoryService(session).enqueue(payload())
                await MemoryService(session).enqueue(payload())
            for attempt in range(1, 4):
                assert await process_memory_job(factory, Failure())
                async with factory.begin() as session:
                    job = await session.scalar(select(MemoryOutbox))
                    assert job.attempts == attempt and job.status == ("failed" if attempt == 3 else "queued")
                    assert job.available_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
                    job.available_at = datetime.now(timezone.utc)-timedelta(seconds=1)
            assert not await process_memory_job(factory, Success())
            async with factory.begin() as session:
                await MemoryService(session).enqueue({**payload(), "run_id": "second"})
            success = Success()
            await asyncio.gather(process_memory_job(factory, success), process_memory_job(factory, success))
            assert success.calls == 1
            try:
                async with factory.begin() as session:
                    await MemoryService(session).enqueue({**payload(), "run_id": "rolled-back"})
                    raise ValueError("failed final commit")
            except ValueError:
                pass
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(MemoryOutbox)) == 2
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["RETRY", "PARTIAL", "NEED_USER", "FAIL"])
def test_non_pass_does_not_enqueue(status):
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as session:
                await MemoryService(session).enqueue({**payload(), "completion": {"status": status}})
                assert await session.scalar(select(func.count()).select_from(MemoryOutbox)) == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_http_explicit_cross_conversation_revoke_and_outbox(monkeypatch, tmp_path):
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))
    api = importlib.import_module("opportunity_agent.v2.api.app")
    async def scenario():
        engine, factory = await database(tmp_path/"http.db")
        async def sessions():
            async with factory() as session: yield session
        monkeypatch.setattr(api, "SessionLocal", factory)
        from opportunity_agent.v2.agents.orchestrator import LocalDevelopmentAgents
        monkeypatch.setattr(api, "orchestrator", CustomOrchestrator(goal_parser=HeuristicGoalParser(),
            router=HeuristicRouter(), agent_client=LocalDevelopmentAgents(), synthesizer=DeterministicSynthesizer()))
        api.app.dependency_overrides[get_session] = sessions
        try:
            async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://memory") as client:
                assert (await client.get("/api/v1/memory/preferences")).status_code == 401
                assert (await client.post("/api/v1/auth/register", json={"email": "memory@example.test", "password": "memory-password-123"})).status_code == 201
                for index, text in enumerate(["我不考虑需要GRE的项目", "你好"]):
                    conv = (await client.post("/api/v1/conversations", json={"title": str(index)})).json()
                    created = await client.post(f"/api/v1/conversations/{conv['id']}/runs", json={"message": text, "request_id": "memory-"+str(index)})
                    run_id = created.json()["run_id"]
                    for _ in range(250):
                        run = (await client.get(f"/api/v1/runs/{run_id}")).json()
                        if run["status"] in {"completed", "failed"}: break
                        await asyncio.sleep(.02)
                    assert run["status"] == "completed", run
                    if index == 0:
                        assert run["approval_ids"] == []
                        assert "event: memory_updated" in (await client.get(f"/api/v1/runs/{run_id}/events")).text
                    else:
                        assert run["completion"] is None
                prefs = (await client.get("/api/v1/memory/preferences")).json()
                assert len(prefs) == 1 and prefs[0]["source"] == "user_explicit"
                async with factory() as session:
                    state_debug = (await session.scalar(select(AgentRun).where(AgentRun.request_id == "memory-0"))).graph_state
                    assert await session.scalar(select(func.count()).select_from(MemoryOutbox)) == 1, state_debug.get("profile_result", {}).get("clarifications")
                    state = (await session.scalar(select(AgentRun).where(AgentRun.request_id == "memory-0"))).graph_state
                    assert state["consolidation_input"]["user_messages"][0]["content"] == "我不考虑需要GRE的项目"
                url = "/api/v1/memory/preferences/avoid_gre/revoke"
                assert (await client.post(url, json={"request_id": "revoke", "expected_version": 99})).status_code == 409
                assert (await client.post(url, json={"request_id": "revoke", "expected_version": prefs[0]["version"]})).status_code == 200
                assert (await client.get("/api/v1/memory/preferences")).json() == []
        finally:
            api.app.dependency_overrides.clear()
            if api._run_tasks: await asyncio.gather(*list(api._run_tasks), return_exceptions=True)
            await engine.dispose()
    asyncio.run(scenario())


def test_legacy_duplicate_migration_archives_history(tmp_path):
    from sqlalchemy import create_engine, text, inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from pathlib import Path
    import importlib.util
    spec = importlib.util.spec_from_file_location("memory_migration", Path("alembic/versions/0007_preference_memory.py"))
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///"+str(tmp_path/"legacy.db"))
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE memory_items (id VARCHAR PRIMARY KEY, user_id VARCHAR, memory_type VARCHAR, key VARCHAR, value JSON, source VARCHAR, confidence FLOAT, expires_at DATETIME, created_at DATETIME, updated_at DATETIME)"))
            for ident, day, value in [("old", "2026-10-01", "false"), ("latest", "2026-10-05", "true")]:
                connection.execute(text("INSERT INTO memory_items VALUES (:id,'u','preference','avoid_gre',:value,'user_confirmed',1,NULL,:day,:day)"),
                    {"id": ident, "day": day, "value": '{"value":'+value+'}'})
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            assert connection.execute(text("SELECT id FROM memory_items")).scalars().all() == ["latest"]
            assert connection.execute(text("SELECT memory_id FROM memory_audits")).scalars().all() == ["old"]
            assert "uq_memory_preference" in {c["name"] for c in inspect(connection).get_unique_constraints("memory_items")}
    finally:
        engine.dispose()


def test_alembic_fresh_upgrade_to_memory_head(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect
    url = "sqlite+aiosqlite:///"+(tmp_path/"alembic.db").as_posix()
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(url.replace("sqlite+aiosqlite", "sqlite"))
    try:
        with engine.connect() as connection:
            assert {"memory_items", "memory_audits", "memory_outbox"} <= set(inspect(connection).get_table_names())
    finally:
        engine.dispose()


def test_synthesis_failure_never_creates_consolidation_input():
    class Agents:
        async def execute(self, agent, state, missing_task=None): return ProfileResult()
    class Failure:
        async def synthesize(self, state): raise RuntimeError("model down")
    state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q", message="我更看重就业")
    with pytest.raises(RuntimeError):
        asyncio.run(CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
            agent_client=Agents(), synthesizer=Failure()).run(state))
    assert state.completion.status == "PASS" and state.consolidation_input is None


def test_repair_pass_enqueues_only_final_snapshot():
    class Agents:
        calls = 0
        async def execute(self, agent, state, missing_task=None):
            self.calls += 1
            evidence = Evidence(source_id="official", url="https://example.edu", authority="official", relevance_score=.95)
            return ResearchResult(status="complete", programs=[ProgramResult(university="U"+str(self.calls), program="MS", evidence=[evidence])])
    state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q", message="找2个项目")
    out = asyncio.run(CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
        agent_client=Agents(), synthesizer=DeterministicSynthesizer()).run(state))
    assert out.completion.status == "PASS" and len(out.research_result.programs) == 2
    assert out.consolidation_input["round_id"] == 1
    assert "answer" not in out.consolidation_input and "research_result" not in out.consolidation_input


def test_old_inference_version_is_not_proposed_after_user_correction():
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as session:
                service = MemoryService(session)
                await service.save_explicit("u", "我愿意考GRE", "new", "c", "m")
                stale = await service.propose_inferred("u", {"key": "avoid_gre", "value": True,
                    "evidence": "旧偏好", "expected_version": 0}, "r")
                assert stale is None
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_current_request_overrides_new_turn_preference():
    class Agents:
        async def execute(self, agent, state, missing_task=None):
            if agent == "profile": return ProfileResult()
            assert state.success_criteria.gre_policy == "required"
            return ResearchResult(status="no_results")
    state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q",
        message="我不考虑GRE的项目，但这次查询要求GRE的学校项目")
    out = asyncio.run(CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
        agent_client=Agents(), synthesizer=DeterministicSynthesizer(), max_rounds=1).run(state))
    assert out.completion.status == "PARTIAL"


def test_live_acceptance_requires_review_and_fixed_distribution():
    from scripts.verify_v2_live_chain import reviewed_cases
    cases = [{"id": f"{category}-{i}", "category": category, "message": "test",
              "expected_agents": [], "annotation_status": "human_reviewed"}
             for category in ["smalltalk", "profile", "research", "planning", "mixed"] for i in range(8)]
    assert len(reviewed_cases({"cases": cases})) == 40
    cases[0]["annotation_status"] = "draft_unreviewed"
    with pytest.raises(ValueError, match="Human review"):
        reviewed_cases({"cases": cases})


def test_duplicate_claim_does_not_duplicate_proposals(tmp_path):
    class Model:
        enabled = True
        def generate_structured(self, schema, **kwargs):
            return schema(preferences=[{"key": "employment_priority", "value": True,
                "evidence": "希望尽快找到工作", "message_id": "m", "confidence": .9}])
    async def scenario():
        engine, factory = await database(tmp_path/"worker-proposal.db")
        try:
            async with factory.begin() as session:
                await MemoryService(session).enqueue(payload())
            worker = MemoryConsolidator(Model())
            await asyncio.gather(process_memory_job(factory, worker), process_memory_job(factory, worker))
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(ChangeProposal)) == 1
                assert await session.scalar(select(func.count()).select_from(ApprovalRequest)) == 1
                assert not await MemoryService(session).list_preferences("u")
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_same_request_id_in_different_conversations_not_deduplicated():
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as session:
                service = MemoryService(session)
                await service.save_explicit("u", "我不考虑GRE", "same", "first", "m1")
                changed = await service.save_explicit("u", "我愿意考GRE", "same", "second", "m2")
                assert changed[0]["value"] is False and changed[0]["version"] == 2
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("completion,fail_commit,expected_memory,expected_jobs", [
    ("PASS", False, 1, 1), ("PARTIAL", False, 1, 0), ("NEED_USER", False, 1, 0),
    ("FAIL", False, 0, 0), ("PASS", True, 0, 0)])
def test_http_terminal_write_matrix_and_final_transaction_rollback(monkeypatch, tmp_path,
        completion, fail_commit, expected_memory, expected_jobs):
    from opportunity_agent.v2.repositories import Repository
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))
    api = importlib.import_module("opportunity_agent.v2.api.app")
    class Terminal:
        async def ainvoke(self, initial, event_queue=None):
            state = ExecutionState.model_validate(initial)
            state.completion = CompletionResult(status=completion)
            state.answer = "已生成测试回答"
            if completion == "PASS":
                state.consolidation_input = ConsolidationInput(run_id=state.run_id, user_id=state.user_id,
                    conversation_id=state.conversation_id, completion={"status": "PASS"},
                    user_messages=state.user_messages).model_dump(mode="json")
            return state.serialise()
    original_finalize = Repository.finalize_run
    async def failed_finalize(self, run, state, status, execution_token=None):
        if status == "completed": raise RuntimeError("final write unavailable")
        return await original_finalize(self, run, state, status, execution_token)
    if fail_commit: monkeypatch.setattr(Repository, "finalize_run", failed_finalize)
    async def scenario():
        engine, factory = await database(tmp_path/"matrix.db")
        async def sessions():
            async with factory() as session: yield session
        monkeypatch.setattr(api, "SessionLocal", factory)
        monkeypatch.setattr(api, "orchestrator", Terminal())
        api.app.dependency_overrides[get_session] = sessions
        try:
            async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://matrix") as client:
                await client.post("/api/v1/auth/register", json={"email": "matrix@example.test", "password": "matrix-password-123"})
                conv = (await client.post("/api/v1/conversations", json={"title": "matrix"})).json()
                created = await client.post(f"/api/v1/conversations/{conv['id']}/runs", json={
                    "message": "我更看重就业", "request_id": "matrix"})
                run_id = created.json()["run_id"]
                for _ in range(250):
                    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
                    if run["status"] in {"completed", "failed"}: break
                    await asyncio.sleep(.02)
                assert run["status"] == ("failed" if fail_commit else "completed"), run
                async with factory() as session:
                    assert await session.scalar(select(func.count()).select_from(MemoryItem)) == expected_memory
                    assert await session.scalar(select(func.count()).select_from(MemoryAudit)) == expected_memory
                    assert await session.scalar(select(func.count()).select_from(MemoryOutbox)) == expected_jobs
        finally:
            api.app.dependency_overrides.clear()
            if api._run_tasks: await asyncio.gather(*list(api._run_tasks), return_exceptions=True)
            await engine.dispose()
    asyncio.run(scenario())


def test_older_slow_run_cannot_replace_newer_user_preference():
    from opportunity_agent.v2.db.models import Message
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as session:
                session.add_all([Message(id="older", conversation_id="c", role="user", content="我不考虑GRE",
                    created_at=datetime.now(timezone.utc)-timedelta(seconds=10)),
                    Message(id="newer", conversation_id="c", role="user", content="我愿意考GRE",
                    created_at=datetime.now(timezone.utc))])
                await session.flush()
                service = MemoryService(session)
                await service.save_explicit("u", "我愿意考GRE", "new", "c", "newer")
                assert await service.save_explicit("u", "我不考虑GRE", "old", "c", "older") == []
                assert (await service.list_preferences("u"))[0].value == {"value": False}
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("grouped", [False, True])
def test_consolidation_reuses_existing_profile_preference_approval(grouped):
    async def scenario():
        engine, factory = await database()
        try:
            async with factory.begin() as session:
                candidate = {"key": "employment_priority", "value": True, "evidence": "希望工作", "expected_version": 0}
                body = {"changes": [{"type": "preference.change", "payload": candidate}]} if grouped else candidate
                existing = await ApplicationCommandService(session).propose("u", "change_set" if grouped else "preference.change",
                    body, "profile", "r")
                reused = await MemoryService(session).propose_inferred("u", candidate, "r")
                assert reused.id == existing.id
                assert await session.scalar(select(func.count()).select_from(ApprovalRequest)) == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())
