"""V2 profile/planning acceptance flow across domain, aggregation, and approval."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v2.agents.a2a import DomainA2ARequest, execute_domain_request
from opportunity_agent.v2.agents.contracts import ExecutionState, PlanResult
from opportunity_agent.v2.agents.orchestrator import (
    CustomOrchestrator, DeterministicSynthesizer, HeuristicGoalParser, HeuristicRouter,
)
from opportunity_agent.v2.agents.result_aggregation import ResultAggregator
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import ApplicationPlan, ApplicationTask, Profile
from opportunity_agent.v2.services.applications import ApplicationCommandService
from opportunity_agent.v2.services.auth import AuthService


PROFILE = {"major": "Computer Science", "onboarding_completed": True,
           "target_countries": ["US"], "target_degree": "MS", "target_fields": ["AI"],
           "graduation_year": 2027, "planned_enrollment_year": 2028}


def test_profile_conflict_and_plan_have_structured_domain_outputs(monkeypatch):
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))
    profile = execute_domain_request(DomainA2ARequest(
        agent="profile", user_id="u", conversation_id="c", run_id="r", request_id="1",
        message="我的托福是105分", profile_payload=PROFILE,
    ))
    assert profile.projected_profile["toefl_score"] == 105
    assert profile.derived_state["language"] == "completed"
    assert profile.proposals[0]["payload"]["expected_version"] == 1
    assert profile.decisions[0]["status"] == "applied"
    assert profile.conflicts == []

    plan = execute_domain_request(DomainA2ARequest(
        agent="planning", user_id="u", conversation_id="c", run_id="r", request_id="2",
        message="请制定申请规划", profile_payload=profile.projected_profile,
    ))
    assert plan.status == "complete"
    assert len(plan.tasks) >= 5
    assert all(task["stable_key"] for task in plan.tasks)
    assert len(plan.roadmap["article"]) > 600
    assert plan.assumptions  # no unsupported claim about school requirements


def test_planning_advice_returns_article_without_replacing_the_roadmap(monkeypatch):
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))
    plan = execute_domain_request(DomainA2ARequest(
        agent="planning", user_id="u", conversation_id="c", run_id="r", request_id="advice",
        message="我的 SOP 应该如何准备？", profile_payload=PROFILE,
    ))
    assert plan.plan_kind == "advice"
    assert plan.article_markdown.startswith("## 针对本次问题的建议")
    assert plan.roadmap == {} and plan.tasks == [] and plan.timeline == []

    state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="advice",
                           message="我的 SOP 应该如何准备？", plan_result=plan)
    assert ResultAggregator().approval_proposals(state) == []


def test_plan_merge_replaces_removed_tasks_instead_of_resurrecting_them():
    state = ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="merge", message="重规划")
    state.plan_result = PlanResult(
        article_markdown="旧计划", roadmap={"article": "旧计划"},
        tasks=[{"stable_key": "keep"}, {"stable_key": "remove"}], status="complete",
    )
    incoming = PlanResult(
        article_markdown="新计划", roadmap={"article": "新计划"},
        tasks=[{"stable_key": "keep"}, {"stable_key": "new"}], status="complete",
    )
    ResultAggregator().merge_plan(state, incoming)
    assert [item["stable_key"] for item in state.plan_result.tasks] == ["keep", "new"]
    assert state.plan_result.article_markdown == "新计划"


def test_planning_only_promotes_field_level_exact_research_evidence(monkeypatch):
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))
    research = {
        "programs": [{
            "university": "Example University", "program": "MSCS", "intake": "2028-fall",
            "deadline": "2027-12-15",
            "required_fields": ["deadline", "gre_policy"],
            "facts": [
                {"field": "deadline", "value": "2027-12-15", "verification_status": "verified",
                 "evidence_ids": ["deadline-evidence"]},
                {"field": "gre_policy", "value": "not_required", "verification_status": "verified",
                 "evidence_ids": ["generic-gre-evidence"]},
            ],
            "evidence": [
                {"source_id": "deadline-source", "evidence_id": "deadline-evidence",
                 "title": "MSCS admissions", "url": "https://example.edu/mscs/admissions",
                 "intake": "2028-fall",
                 "authority": "official", "program_match": "exact", "supports_fields": ["deadline"],
                 "relevance_score": 0.95, "relevance_passed": True, "excerpt": "Deadline: Dec 15"},
                {"source_id": "generic-source", "evidence_id": "generic-gre-evidence",
                 "title": "Graduate admissions", "url": "https://example.edu/graduate",
                 "authority": "official", "program_match": "generic", "supports_fields": ["gre_policy"],
                 "relevance_score": 0.9, "relevance_passed": True, "excerpt": "General GRE guidance"},
            ],
        }],
        "route": "hybrid", "status": "complete",
    }
    plan = execute_domain_request(DomainA2ARequest(
        agent="planning", user_id="u", conversation_id="c", run_id="r", request_id="evidence",
        message="请制定完整申请规划", profile_payload=PROFILE, research_result=research,
    ))
    requirements = plan.roadmap["verified_requirements"]
    assert [item["field"] for item in requirements] == ["deadline"]
    assert plan.evidence_ids == ["deadline-evidence", "generic-gre-evidence"]
    assert any("gre" in item and "项目级" in item for item in plan.assumptions)


def test_profile_reuses_v1_event_progress_matching(monkeypatch):
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))
    with_exam = {**PROFILE, "exam_plan": {"exam_type": "TOEFL", "next_exam_date": "2027-06-10"}}
    plan = execute_domain_request(DomainA2ARequest(
        agent="planning", user_id="u", conversation_id="c", run_id="r", request_id="plan",
        message="请制定申请规划", profile_payload=with_exam,
    ))
    saved = [{"id": str(index), "stable_key": row["stable_key"], "title": row["title"],
              "category": row["category"], "status": "planned", "evidence": ""}
             for index, row in enumerate(plan.tasks)]
    result = execute_domain_request(DomainA2ARequest(
        agent="profile", user_id="u", conversation_id="c", run_id="r", request_id="progress",
        message="我已经提前考完托福了", profile_payload=with_exam,
        current_plan={"roadmap": plan.roadmap}, current_tasks=saved,
    ))
    assert any(item["type"] == "task.command" and item["payload"]["action"] == "complete"
               for item in result.proposals)
    assert result.derived_state["language_evidence"] == "exam_taken"


def test_replan_fallback_article_reflects_persisted_progress(monkeypatch):
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))
    first = execute_domain_request(DomainA2ARequest(
        agent="planning", user_id="u", conversation_id="c", run_id="r", request_id="first",
        message="请制定完整申请规划", profile_payload=PROFILE,
    ))
    current_tasks = [
        {"id": str(index), "stable_key": task["stable_key"], "title": task["title"],
         "category": task.get("category", "application"),
         "status": "completed" if index == 0 else "planned", "evidence": "已完成" if index == 0 else ""}
        for index, task in enumerate(first.tasks)
    ]
    revised = execute_domain_request(DomainA2ARequest(
        agent="planning", user_id="u", conversation_id="c", run_id="r", request_id="replan",
        message="请重新规划申请时间线", profile_payload=PROFILE,
        current_plan={"roadmap": first.roadmap}, current_plan_version=1, current_tasks=current_tasks,
    ))
    assert "状态：completed" in revised.article_markdown
    assert any(task.get("execution_status") == "completed" for task in revised.tasks)


def test_one_approval_applies_profile_and_plan_then_replan_keeps_progress(monkeypatch):
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                user = await AuthService(session).register("plan-flow@example.com", "long-enough-password")
                session.add(Profile(user_id=user.id, version=1, payload=PROFILE))
            orchestrator = CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
                                              synthesizer=DeterministicSynthesizer())
            result = await orchestrator.ainvoke({
                "user_id": user.id, "conversation_id": "c", "run_id": "r", "request_id": "same-run",
                "message": "我的托福是105分，请为我制定申请规划", "profile_payload": PROFILE,
            })
            assert result["proposals"][0]["type"] == "change_set"
            assert result["plan_result"]["tasks"]
            async with factory.begin() as session:
                profile = await session.scalar(select(Profile).where(Profile.user_id == user.id))
                assert profile.payload.get("toefl_score") is None
                assert await session.scalar(select(ApplicationPlan)) is None
                service = ApplicationCommandService(session)
                proposal = result["proposals"][0]
                rejected = await service.propose(user.id, proposal["type"], proposal["payload"], "declined-run")
                await service.decide(rejected.id, user.id, False)
                assert (await session.scalar(select(Profile).where(Profile.user_id == user.id))).payload.get("toefl_score") is None
                assert await session.scalar(select(ApplicationPlan)) is None
                approval = await service.propose(user.id, proposal["type"], proposal["payload"], "same-run")
                await service.decide(approval.id, user.id, True)
            async with factory.begin() as session:
                profile = await session.scalar(select(Profile).where(Profile.user_id == user.id))
                plan = await session.scalar(select(ApplicationPlan).where(ApplicationPlan.status == "active"))
                task = await session.scalar(select(ApplicationTask).where(ApplicationTask.plan_id == plan.id))
                assert profile.payload["toefl_score"] == 105
                assert plan.version == 1 and task is not None
                service = ApplicationCommandService(session)
                approval = await service.propose(user.id, "task.command", {
                    "task_id": task.id, "action": "complete", "evidence": "已完成", "expected_status": "planned"
                }, "task-complete")
                await service.decide(approval.id, user.id, True)
                task_key = task.stable_key
            async with factory.begin() as session:
                service = ApplicationCommandService(session)
                revised = result["plan_result"]
                approval = await service.propose(user.id, "plan.replace", {
                    "roadmap": revised["roadmap"], "tasks": revised["tasks"],
                    "expected_version": 1, "expected_profile_version": 2,
                }, "replan")
                await service.decide(approval.id, user.id, True)
            async with factory() as session:
                plans = (await session.scalars(select(ApplicationPlan).order_by(ApplicationPlan.version))).all()
                next_task = await session.scalar(select(ApplicationTask).where(
                    ApplicationTask.plan_id == plans[1].id, ApplicationTask.stable_key == task_key))
                assert [item.status for item in plans] == ["superseded", "active"]
                assert next_task.status == "completed"
            async with factory.begin() as session:
                service = ApplicationCommandService(session)
                stale = await service.propose(user.id, "plan.replace", {
                    "roadmap": result["plan_result"]["roadmap"], "tasks": result["plan_result"]["tasks"],
                    "expected_version": 1, "expected_profile_version": 2,
                }, "stale-replan")
                with pytest.raises(ValueError, match="plan version conflict"):
                    await service.decide(stale.id, user.id, True)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
