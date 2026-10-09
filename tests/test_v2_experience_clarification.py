"""V2 evidence, contextual extraction, modal conflicts and rolling history."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from opportunity_agent.llm_client import LLMClient
from opportunity_agent.llm_context import conversation_scope
from opportunity_agent.v2.agents.a2a import DomainA2ARequest, execute_domain_request, request_from_state
from opportunity_agent.v2.agents.contracts import ExecutionState, RouteDecision
from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator, HeuristicGoalParser, HeuristicRouter, DeterministicSynthesizer
from opportunity_agent.v2.agents.profile_extraction import ProfileExtractionPipeline, FactCandidate, validate_candidate
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import Conversation, Message, Profile, AgentRun, ProfileChange, ConversationSummary, MemoryItem
from opportunity_agent.v2.services.auth import AuthService
from opportunity_agent.v2.services.profile_conflicts import ProfileConflictService
from opportunity_agent.v2.services.conversation_context import ConversationContextService

EXPERIENCES = "我现在有华为实习，一段西湖科研，一段本校科研发了IEEE TNSE，一个agent项目"


def test_rejects_model_misclassification_and_boolean_score():
    with pytest.raises(ValueError, match="experience_is_not_application_target"):
        validate_candidate(FactCandidate(field="target_programs", raw_value=["agent项目"],
                           evidence="agent项目", confidence=.99, source="llm"), EXPERIENCES)
    with pytest.raises(ValueError, match="expected_number_not_boolean"):
        validate_candidate(FactCandidate(field="toefl_score", raw_value=True,
                           evidence="托福", confidence=.99, source="llm"), "托福成绩未知")


def test_experiences_and_project_count(monkeypatch):
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))
    result = asyncio.run(CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
                                           synthesizer=DeterministicSynthesizer()).ainvoke({
        "user_id": "u", "conversation_id": "c", "run_id": "r", "request_id": "one",
        "message": EXPERIENCES, "profile_payload": {"target_programs": ["MSCS", "MCS", "CSE"]},
    }))
    assert result["route_decision"]["agents"] == ["profile"]
    profile = result["profile_result"]
    assert profile["projected_profile"]["project_experiences"] == ["agent项目"]
    assert profile["projected_profile"]["target_programs"] == ["MSCS", "MCS", "CSE"]
    assert len(profile["projected_profile"]["research_experiences"]) == 2
    assert profile["projected_profile"]["paper_experiences"]
    assert all(f["evidence"] in EXPERIENCES for f in profile["accepted_facts"])


def test_full_original_known_facts_and_validator():
    original = "我托福107，但这个成绩暂时不准备用来申请加拿大项目。做过 GNN 研究。"
    calls = []
    def complete(payload):
        calls.append(payload)
        context = json.loads(payload["messages"][-1]["content"])["context"]
        assert context["original_message"] == original
        assert any(f["field"] == "toefl_score" for f in context["known_facts"])
        return json.dumps({"facts": [
            {"field": "toefl_score", "raw_value": 107, "evidence": "托福107", "confidence": .9, "source": "llm"},
            {"field": "gre_score", "raw_value": 900, "evidence": "托福107", "confidence": .9, "source": "llm"},
            {"field": "career_goal", "raw_value": "CEO", "evidence": "我想当CEO", "confidence": .9, "source": "llm"},
        ]})
    result = ProfileExtractionPipeline(LLMClient(completion_fn=complete)).extract(original)
    assert len(calls) == 1
    assert [f.value for f in result.accepted_facts if f.field == "toefl_score"] == [107]
    assert not any(f.field in {"target_countries", "gre_score", "career_goal"} for f in result.accepted_facts)
    assert len(result.errors) == 2


@pytest.mark.parametrize("text,expected", [
    ("我托福105，现在托福110", 110), ("不是105，我托福现在是110", 110),
    ("如果托福110能申请吗", None), ("托福110够吗", None),
])
def test_statement_and_correction(text, expected):
    result = ProfileExtractionPipeline().extract(text, mode="rule_only")
    scores = [f.value for f in result.accepted_facts if f.field == "toefl_score"]
    assert scores == ([] if expected is None else [expected])


def test_context_is_injected_by_role_and_reset():
    captured = []
    client = LLMClient(completion_fn=lambda p: captured.append(p) or "ok")
    context = {"recent_messages": [{"role": "user", "content": "项目A指CMU MSCS"},
                                   {"role": "assistant", "content": "我们在讨论A和B"}],
               "summary": "之前比较A和B", "profile_summary": {"major": "Computer Science"},
               "relevant_preferences": [{"key": "employment_priority", "value": True}]}
    with conversation_scope(context):
        client.generate(system="router", user="那A的课程呢？")
    client.generate(system="other user", user="你好")
    assert [m["role"] for m in captured[0]["messages"]] == ["system", "system", "user", "assistant", "user"]
    assert "CMU" in captured[0]["messages"][2]["content"]
    assert len(captured[1]["messages"]) == 2
    state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="one", message="那A的课程呢？",
                           conversation_context=context, route_decision=RouteDecision(
                               mode="delegate", agents=["research"], reason="reference", resolved_query="CMU MSCS 的课程"))
    request = request_from_state("research", state)
    assert request.message == "CMU MSCS 的课程"
    # Router resolves the referent; Research only receives the resolved query.
    assert request.conversation_context == {}
    assert request.recent_messages == []


def test_contextual_score_conflict(monkeypatch):
    def completion(payload):
        return json.dumps({"facts": [{"field": "toefl_score", "raw_value": 110, "statement_kind": "correction",
                                      "evidence": "刚考到110", "confidence": .99, "source": "llm"}]})
    monkeypatch.setattr("opportunity_agent.v2.agents.profile_extraction.LLMClient", lambda **kw: LLMClient(completion_fn=completion))
    result = execute_domain_request(DomainA2ARequest(agent="profile", user_id="u", conversation_id="c", run_id="r",
        request_id="one", message="我刚考到110。", profile_payload={"toefl_score": 107},
        conversation_context={"recent_messages": [{"role": "assistant", "content": "你的托福最近出分了吗？"}]}))
    assert result.conflicts[0]["old_value"] == 107 and result.conflicts[0]["new_value"] == 110
    assert result.projected_profile["toefl_score"] == 107
    assert result.requires_confirmation and not result.proposals


def test_conflict_ownership_choices_idempotency(monkeypatch):
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                user = await AuthService(session).register("conflict@example.com", "test-password-123")
                conversation = Conversation(user_id=user.id)
                session.add(conversation)
                await session.flush()
                run = AgentRun(user_id=user.id, conversation_id=conversation.id)
                profile = Profile(user_id=user.id, payload={"toefl_score": 107}, version=1)
                session.add_all([run, profile])
                await session.flush()
                result = execute_domain_request(DomainA2ARequest(agent="profile", user_id=user.id, conversation_id=conversation.id,
                    run_id=run.id, request_id="one", message="托福110", profile_payload=profile.payload))
                service = ProfileConflictService(session)
                conflict = await service.record(user.id, run.id, result.conflicts[0])
                with pytest.raises(LookupError):
                    await service.resolve(conflict.id, "other-user", "new")
                await service.resolve(conflict.id, user.id, "new")
                await service.resolve(conflict.id, user.id, "new")
                assert profile.payload["toefl_score"] == 110 and profile.version == 2
                assert len((await session.scalars(select(ProfileChange))).all()) == 1
                with pytest.raises(ValueError):
                    await service.resolve(conflict.id, user.id, "old")
                next_run = AgentRun(user_id=user.id, conversation_id=conversation.id)
                session.add(next_run)
                await session.flush()
                next_result = execute_domain_request(DomainA2ARequest(agent="profile", user_id=user.id,
                    conversation_id=conversation.id, run_id=next_run.id, request_id="two",
                    message="托福111", profile_payload=profile.payload, profile_version=profile.version))
                stale = await service.record(user.id, next_run.id, next_result.conflicts[0])
                profile.payload = {**profile.payload, "toefl_score": 115}
                profile.version += 1
                with pytest.raises(ValueError, match="profile field changed"):
                    await service.resolve(stale.id, user.id, "new")
                await service.resolve(stale.id, user.id, "old")
                assert profile.payload["toefl_score"] == 115 and profile.version == 3
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_context_isolation_across_concurrent_tasks():
    async def scenario():
        async def task(label):
            with conversation_scope({"recent_messages": [{"role": "user", "content": label}]}):
                await asyncio.sleep(0)
                client = LLMClient(completion_fn=lambda p: json.dumps(p))
                result = json.loads(await asyncio.to_thread(client.generate, system="test", user="next"))
                return result["messages"][2]["content"]
        assert await asyncio.gather(task("user-one"), task("user-two")) == ["user-one", "user-two"]
    asyncio.run(scenario())


def test_rolling_summary_is_owned_bounded_and_keeps_originals(monkeypatch):
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                user = await AuthService(session).register("context@example.com", "test-password-123")
                conversation = Conversation(user_id=user.id)
                session.add(conversation)
                await session.flush()
                start = datetime.now(timezone.utc) - timedelta(hours=1)
                for i in range(24):
                    session.add(Message(conversation_id=conversation.id, role="user" if i % 2 == 0 else "assistant",
                                        content=f"原始消息 {i}", created_at=start + timedelta(seconds=i)))
                session.add(MemoryItem(user_id=user.id, memory_type="preference", key="avoid_gre", value={"value": True},
                                       confidence=1.0, source="user_confirmed"))
                await session.flush()
                service = ConversationContextService(session)
                context = await service.build(user.id, conversation.id, "current", "申请项目", {"toefl_score": 107})
                assert len(context.recent_messages) == 10
                assert context.recent_messages[0]["content"] == "原始消息 14"
                assert "原始消息 0" in context.summary
                assert context.relevant_preferences[0]["key"] == "avoid_gre"
                stored = await session.scalar(select(ConversationSummary))
                summary_before = stored.summary
                repeated = await service.build(user.id, conversation.id, "current", "申请项目", {})
                assert repeated.summary == summary_before
                with pytest.raises(ValueError):
                    await service.build("other", conversation.id, "current", "申请项目", {})
        finally:
            await engine.dispose()
    asyncio.run(scenario())
