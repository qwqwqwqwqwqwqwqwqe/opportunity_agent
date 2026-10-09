"""Context transport and regressions for repaired multi-turn extraction defects.

Semantic outputs in offline tests are explicit doubles, not accuracy claims.
Use scripts.audit_profile_context --live for real-model ablations.
"""
import asyncio
import importlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v2.agents.a2a import DomainA2ARequest, execute_domain_request
from opportunity_agent.v2.agents.contracts import ExecutionState, RouteDecision
from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator, LLMGoalParser, LLMRouter, LLMSynthesizer
from opportunity_agent.v2.agents.orchestrator import HeuristicGoalParser, HeuristicRouter, DeterministicSynthesizer
from opportunity_agent.v2.agents.profile_extraction import ProfileExtractionPipeline, FactCandidate, validate_candidate
from opportunity_agent.v2.agents.result_aggregation import ResultAggregator
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import Conversation, MemoryItem, Message, User
from opportunity_agent.v2.db.session import get_session
from opportunity_agent.v2.services.conversation_context import ConversationContextService, extractive_summary
from scripts.audit_profile_context import case_context, evaluate_case, load_cases, oracle_response


CASES = load_cases()
PARAMS = []
for case in CASES:
    if case.get("kind") == "recall":
        continue
    for mode in ("auto", "hybrid"):
        PARAMS.append(pytest.param(case, mode, id=f"{case['id']}-{mode}"))


@pytest.mark.parametrize("case,mode", PARAMS)
def test_profile_multiturn_contract(case, mode):
    record = evaluate_case(case, mode, live=False)
    assert record.get("evidence_valid", True)
    assert record["passed"], record


@pytest.mark.parametrize("text,expected", [
    ("室友托福110。", []), ("我朋友的托福110。", []),
    ("My roommate TOEFL 110.", []), ("我室友托福110，我托福105。", [105]),
])
def test_third_party_score_and_own_score_are_separated(text, expected):
    result = ProfileExtractionPipeline().extract(text, mode="rule_only")
    assert [f.value for f in result.accepted_facts if f.field == "toefl_score"] == expected


def test_semantic_veto_removes_only_matching_rule_candidate():
    def complete(payload):
        body = json.loads(payload["messages"][-1]["content"])["context"]
        fact = next(f for f in body["known_facts"] if f["field"] == "toefl_score")
        return json.dumps({"rejected_known_facts": [{"field": fact["field"], "evidence": fact["evidence"]}]})
    result = ProfileExtractionPipeline(LLMClient(completion_fn=complete)).extract("这个示例写的是托福110。", mode="hybrid")
    assert not result.accepted_facts


def test_gpa_scale_cannot_be_invented_from_assistant_history():
    with pytest.raises(ValueError, match="gpa_scale_requires_current_user_evidence"):
        validate_candidate(FactCandidate(field="gpa_scale", raw_value=4, evidence="3.85", confidence=.99,
                                         source="llm"), "3.85。")
    fact = validate_candidate(FactCandidate(field="gpa_scale", raw_value=4, evidence="3.85/4.0", confidence=.99,
                                           source="llm"), "我的GPA是3.85/4.0。")
    assert fact.value == 4


@pytest.mark.parametrize("value,expected", [("加拿大", "Canada"),
    ({"country": "加拿大", "condition": "美国毕业后找不到工作"},
     {"country": "Canada", "condition": "美国毕业后找不到工作"})])
def test_fallback_country_normalizes_name_and_preserves_condition(value, expected):
    message = "加拿大只是美国毕业后找不到工作时的备选。"
    client = LLMClient(completion_fn=lambda payload: json.dumps({"preferences": [{
        "key": "fallback_country", "value": value, "evidence": message, "confidence": .99}]}))
    result = ProfileExtractionPipeline(client).extract(message, mode="auto")
    assert result.preferences[0].value == expected
    assert not any(f.field == "target_countries" for f in result.accepted_facts)


def test_contextual_numeric_reply_is_guarded_before_router():
    case = next(c for c in CASES if c["id"] == "score_bare")
    assert CustomOrchestrator._guard(case["message"], case_context(case)) == ["profile"]
    assert CustomOrchestrator._guard(case["message"], {}) == []


def test_summary_fallback_preserves_alias_and_later_correction_within_budget():
    summary = "user: 项目A指星桥计算项目"
    for index in range(20):
        text = "更正：项目A改叫海岚系统项目" if index == 10 else "日常学习情况" * 80
        summary = extractive_summary(summary, [{"role": "user", "content": text}])
        assert len(summary) <= 2400
    assert "星桥计算项目" in summary
    assert "海岚系统项目" in summary
    assert summary.index("星桥计算项目") < summary.index("海岚系统项目")


def test_ainvoke_delivers_history_only_to_relevant_consumers_without_router_duplication():
    case = next(c for c in CASES if c["id"] == "remember_from_summary")
    context = case_context(case)
    captured = []

    def complete(payload):
        captured.append(payload)
        body = json.loads(payload["messages"][-1]["content"])
        title = body.get("output_schema", {}).get("title")
        if title == "SuccessCriteria":
            return "{}"
        if title == "RouteDecision":
            return json.dumps({"mode": "direct_reply", "reason": "fixture recall"})
        return "星桥计算项目"

    client = LLMClient(completion_fn=complete)
    orchestrator = CustomOrchestrator(goal_parser=LLMGoalParser(client), router=LLMRouter(client),
                                      synthesizer=LLMSynthesizer(client))
    result = asyncio.run(orchestrator.ainvoke({"user_id": "audit", "conversation_id": "one", "run_id": "r",
        "request_id": "request", "message": case["message"], "conversation_context": context}))
    assert result["answer"] == "星桥计算项目"
    assert len(captured) == 3
    # Goal parsing only extracts THIS turn's constraints. Router receives history
    # once as typed context for references; Synthesizer retains conversation context.
    assert len(captured[0]["messages"]) == 2
    assert len(captured[1]["messages"]) == 2
    routing_context = json.loads(captured[1]["messages"][-1]["content"])["context"]
    assert routing_context["recent_messages"] == case["history"]
    assert "星桥计算项目" in routing_context["summary"]
    assert captured[2]["messages"][2:4] == case["history"]
    assert "星桥计算项目" in captured[2]["messages"][1]["content"]
    # New user/turn cannot inherit the previous ContextVar after ainvoke returns.
    from opportunity_agent.llm_context import inject_conversation
    assert inject_conversation({"messages": []}) == {"messages": []}


def test_a2a_profile_receives_history_and_keeps_current_evidence(monkeypatch):
    case = next(c for c in CASES if c["id"] == "score_supported_phrase")
    calls = []

    def complete(payload):
        calls.append(payload)
        assert payload["messages"][2:4] == case["history"]
        body = json.loads(payload["messages"][-1]["content"])["context"]
        assert body["original_message"] == case["message"]
        assert body["conversation_context"]["recent_messages"] == case["history"]
        return oracle_response(case)

    monkeypatch.setattr("opportunity_agent.v2.agents.profile_extraction.LLMClient",
                        lambda **kwargs: LLMClient(completion_fn=complete))
    result = execute_domain_request(DomainA2ARequest(agent="profile", user_id="audit", conversation_id="one",
        run_id="r", request_id="request", message=case["message"], profile_payload=case["profile"],
        conversation_context=case_context(case)))
    assert len(calls) == 1
    assert result.conflicts[0]["old_value"] == 107
    assert result.conflicts[0]["new_value"] == 110
    assert result.projected_profile["toefl_score"] == 107
    assert all(f["evidence"] in case["message"] for f in result.accepted_facts)


class OfflineSummaryClient:
    enabled = False


def test_semantic_extraction_outage_is_not_success(monkeypatch):
    case = next(c for c in CASES if c["id"] == "score_supported_phrase")

    def unavailable(payload):
        raise TimeoutError("synthetic outage")

    monkeypatch.setattr("opportunity_agent.v2.agents.profile_extraction.LLMClient",
                        lambda **kwargs: LLMClient(completion_fn=unavailable, retries=0))
    result = execute_domain_request(DomainA2ARequest(agent="profile", user_id="audit", conversation_id="one",
        run_id="r", request_id="request", message=case["message"], profile_payload=case["profile"],
        conversation_context=case_context(case)))
    assert any("llm_unavailable" in error for error in result.errors)
    state = ExecutionState(user_id="audit", conversation_id="one", run_id="r", request_id="request",
        message=case["message"], profile_result=result,
        route_decision=RouteDecision(mode="delegate", agents=["profile"], reason="update score"))
    assert CustomOrchestrator._check_completion(state).status != "PASS"


def test_partial_extraction_and_aggregation_do_not_hide_failure(monkeypatch):
    def unavailable(payload):
        raise TimeoutError("synthetic outage")
    monkeypatch.setattr("opportunity_agent.v2.agents.profile_extraction.LLMClient",
                        lambda **kwargs: LLMClient(completion_fn=unavailable, retries=0))
    result = execute_domain_request(DomainA2ARequest(agent="profile", user_id="audit", conversation_id="one",
        run_id="r", request_id="request", message="我托福107，毕业后想做AI工程师。"))
    assert result.status == "partial"
    assert result.proposals  # safe rule fact remains a proposal, never persisted
    state = ExecutionState(user_id="audit", conversation_id="one", run_id="r", request_id="request",
        message="update", route_decision=RouteDecision(mode="delegate", agents=["profile"], reason="update"))
    aggregator = ResultAggregator()
    aggregator.merge_profile(state, result)
    aggregator.merge_profile(state, result)
    assert state.profile_result.status == "partial"
    assert CustomOrchestrator._check_completion(state).status == "FAIL"


@asynccontextmanager
async def context_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory.begin() as session:
            session.add(User(id="audit", email="context-audit@example.invalid", password_hash="unused"))
            session.add_all([Conversation(id="one", user_id="audit"), Conversation(id="two", user_id="audit")])
            await session.flush()
            yield session, ConversationContextService(session, OfflineSummaryClient())
    finally:
        await engine.dispose()


def test_db_context_excludes_current_future_and_other_conversation():
    async def scenario():
        async with context_database() as (session, service):
            start = datetime(2026, 10, 4, tzinfo=timezone.utc)
            for index, (conv, role, text, request) in enumerate([
                ("one", "user", "A是星桥计算项目", "old"),
                ("one", "assistant", "我们按A讨论", None),
                ("two", "user", "隔壁会话不应出现", "other"),
                ("one", "user", "那A呢", "current"),
                ("one", "user", "未来消息不应出现", "future"),
            ]):
                session.add(Message(conversation_id=conv, role=role, content=text,
                                    request_id=request, created_at=start + timedelta(seconds=index)))
            await session.flush()
            context = await service.build("audit", "one", "current", "那A呢", {"toefl_score": 107})
            assert [m["content"] for m in context.recent_messages] == ["A是星桥计算项目", "我们按A讨论"]
            assert context.profile_summary == {"toefl_score": 107}
            with pytest.raises(ValueError, match="conversation not found"):
                await service.build("other-user", "one", "current", "那A呢", {})
    asyncio.run(scenario())


@pytest.mark.parametrize("query", ["继续推荐项目", "照刚才条件继续"])
def test_preferences_survive_followup(query):
    async def scenario():
        async with context_database() as (session, service):
            session.add(MemoryItem(user_id="audit", memory_type="preference", key="avoid_gre",
                                   value={"value": True}, confidence=1, source="user_confirmed"))
            session.add(Message(conversation_id="one", role="user", content="推荐不要求GRE的项目"))
            await session.flush()
            context = await service.build("audit", "one", "current", query, {})
            assert any(p["key"] == "avoid_gre" for p in context.relevant_preferences)
    asyncio.run(scenario())


def test_long_history_fallback_keeps_early_referent():
    async def scenario():
        async with context_database() as (session, service):
            start = datetime(2026, 10, 4, tzinfo=timezone.utc)
            for index in range(80):
                text = "本次对话项目A指星桥计算项目" if index == 0 else f"第{index}条：" + "今天只是聊一点日常学习情况。" * 15
                session.add(Message(conversation_id="one", role="user" if index % 2 == 0 else "assistant",
                                    content=text, created_at=start + timedelta(seconds=index)))
            await session.flush()
            context = await service.build("audit", "one", "current", "A叫什么", {})
            assert len(context.recent_messages) == 10
            assert await session.scalar(select(func.count()).select_from(Message)) == 80
            assert "星桥计算项目" in context.summary
    asyncio.run(scenario())


def test_http_multiturn_short_score_uses_history_and_conflict_modal(monkeypatch, tmp_path):
    api = importlib.import_module("opportunity_agent.v2.api.app")
    calls = []

    def complete(payload):
        calls.append(payload)
        content = "\n".join(m["content"] for m in payload["messages"][:-1])
        assert "托福107" in content
        assert json.loads(payload["messages"][-1]["content"])["context"]["original_message"] == "110。"
        return json.dumps({"facts": [{"field": "toefl_score", "raw_value": 110,
            "evidence": "110", "statement_kind": "correction", "confidence": .99, "source": "llm"}]})

    monkeypatch.setattr("opportunity_agent.v2.agents.profile_extraction.LLMClient",
                        lambda **kwargs: LLMClient(completion_fn=complete))

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'context-flow.db').as_posix()}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def session_override():
            async with factory() as session:
                yield session

        monkeypatch.setattr(api, "SessionLocal", factory)
        monkeypatch.setattr(api, "orchestrator", CustomOrchestrator(goal_parser=HeuristicGoalParser(),
            router=HeuristicRouter(), synthesizer=DeterministicSynthesizer()))
        api.app.dependency_overrides[get_session] = session_override
        try:
            async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://testserver") as client:
                assert (await client.post("/api/v1/auth/register", json={
                    "email": "multiturn@example.invalid", "password": "long-enough-password"})).status_code == 201
                conversation = (await client.post("/api/v1/conversations", json={"title": "成绩追问"})).json()

                async def turn(message, request):
                    response = await client.post(f"/api/v1/conversations/{conversation['id']}/runs",
                                                 json={"message": message, "request_id": request})
                    assert response.status_code == 202
                    for _ in range(250):
                        run = (await client.get(f"/api/v1/runs/{response.json()['run_id']}")).json()
                        if run["status"] in {"completed", "failed"}:
                            assert run["status"] == "completed", run
                            return run
                        await asyncio.sleep(.02)
                    raise AssertionError("run did not finish")

                await turn("我托福107。", "first")
                approvals = (await client.get("/api/v1/approvals")).json()
                assert len(approvals) == 1
                assert (await client.post(f"/api/v1/approvals/{approvals[0]['id']}/accept", json={})).status_code == 200
                second = await turn("110。", "second")
                assert second["profile_result"]["conflicts"][0]["new_value"] == 110
                assert len(calls) == 1
                assert (await client.get("/api/v1/profile")).json()["payload"]["toefl_score"] == 107
                conflict = (await client.get("/api/v1/profile/conflicts")).json()[0]
                assert (await client.post(f"/api/v1/profile/conflicts/{conflict['conflict_id']}/resolve",
                                          json={"choice": "new"})).status_code == 200
                assert (await client.get("/api/v1/profile")).json()["payload"]["toefl_score"] == 110
        finally:
            api.app.dependency_overrides.pop(get_session, None)
            await engine.dispose()

    asyncio.run(scenario())
