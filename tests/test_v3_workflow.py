"""Business state assertions, independent of LLM prose and external datasets."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
import pytest
from sqlalchemy import event, select, func, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import ApplicationPlan, ApprovalRequest, ApplicationTask, Profile, User
from opportunity_agent.v2.services.applications import ApplicationCommandService
from opportunity_agent.v3 import models as m
from opportunity_agent.v3.contracts import Command, CommandBatch, EventData, OfferingData
from opportunity_agent.v3.engine import impact, review, schedule
from opportunity_agent.v3.service import WorkflowError, WorkflowService
from opportunity_agent.v3.worker import dispatch, process_one


def run(coro):
    return asyncio.run(coro)


async def database(url="sqlite+aiosqlite:///:memory:"):
    engine = create_async_engine(url)
    if url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def foreign_keys(dbapi, _):
            cursor = dbapi.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        user = User(email=f"{uuid4().hex}@test.local", password_hash="not-a-login-hash")
        other = User(email=f"{uuid4().hex}@test.local", password_hash="not-a-login-hash")
        session.add_all([user, other])
    return engine, factory, user.id, other.id


async def execute(session, user_id, kind, payload=None, target=None, version=None, request_id=None):
    result = await WorkflowService(session).execute(user_id, CommandBatch(request_id=request_id or uuid4().hex,
        commands=[Command(type=kind, payload=payload or {}, target_id=target, expected_version=version)]))
    return result["changed_resources"][0]


async def setup_application(session, user_id):
    program = await execute(session, user_id, "program.create", {"university": "Example University", "name": "MSCS"})
    offering = await execute(session, user_id, "offering.create", {"program_id": program["id"], "cycle": "2027 Fall",
        "deadline_date": "2027-02-01", "date_precision": "date"})
    app = await execute(session, user_id, "application.create", {"offering_id": offering["id"]})
    return program, offering, app


def test_idempotency_and_content_mismatch():
    async def scenario():
        engine, factory, user, _ = await database()
        async with factory.begin() as session:
            first = await execute(session, user, "program.create", {"university": "A", "name": "B"}, request_id="same")
            again = await execute(session, user, "program.create", {"university": "A", "name": "B"}, request_id="same")
            assert first == again
            with pytest.raises(WorkflowError, match="different content"):
                await execute(session, user, "program.create", {"university": "X", "name": "B"}, request_id="same")
            assert await session.scalar(select(func.count()).select_from(m.Program)) == 1
            assert await session.scalar(select(func.count()).select_from(m.DomainEvent)) == 1
        await engine.dispose()
    run(scenario())


def test_batch_atomicity_and_cross_user_references():
    async def scenario():
        engine, factory, user, other = await database()
        async with factory.begin() as session:
            private = await execute(session, other, "program.create", {"university": "Private", "name": "Secret"})
            with pytest.raises(WorkflowError) as failure:
                await WorkflowService(session).execute(user, CommandBatch(request_id="atomic", commands=[
                    Command(type="program.create", payload={"university": "Must Roll Back", "name": "A"}),
                    Command(type="offering.create", payload={"program_id": private["id"], "cycle": "2027"})]))
            assert failure.value.status == 404
            assert not await session.scalar(select(m.Program).where(m.Program.user_id == user))
            assert not await session.scalar(select(m.CommandReceipt).where(m.CommandReceipt.user_id == user))
        await engine.dispose()
    run(scenario())


def test_versions_and_task_identity_survive_plan_replacement():
    async def scenario():
        engine, factory, user, _ = await database()
        async with factory.begin() as session:
            _, _, app = await setup_application(session, user)
            task = await execute(session, user, "task.create", {"application_id": app["id"], "stable_key": "sop", "title": "SOP"})
            first = await execute(session, user, "plan.draft", {"today": "2027-01-04"})
            assert first["roadmap"]["conflicts"] == []
            await execute(session, user, "plan.activate", target=first["id"], version=first["version"])
            current = await session.get(m.Task, task["id"])
            completed = await execute(session, user, "task.update", {"status": "completed"}, task["id"], current.version)
            with pytest.raises(WorkflowError, match="version conflict"):
                await execute(session, user, "task.update", {"title": "stale"}, task["id"], 1)
            second = await execute(session, user, "plan.draft", {"today": "2027-01-05"})
            await execute(session, user, "plan.activate", target=second["id"], version=second["version"])
            await session.refresh(current)
            assert current.id == task["id"] and current.status == "completed"
            assert current.due_date.isoformat() == completed["due_date"]
            assert await session.scalar(select(func.count()).select_from(m.Task)) == 1
            assert len((await session.scalars(select(ApplicationPlan).where(ApplicationPlan.status == "active"))).all()) == 1
            assert (await session.get(ApplicationPlan, first["id"])).status == "superseded"
        await engine.dispose()
    run(scenario())


def test_stale_and_rejected_drafts_leave_live_schedule_untouched():
    async def scenario():
        engine, factory, user, _ = await database()
        async with factory.begin() as session:
            _, _, app = await setup_application(session, user)
            task = await execute(session, user, "task.create", {"application_id": app["id"], "stable_key": "sop", "title": "SOP"})
            draft = await execute(session, user, "plan.draft", {"today": "2027-01-04"})
            await execute(session, user, "task.update", {"title": "Changed"}, task["id"], 1)
            with pytest.raises(WorkflowError, match="stale"):
                await execute(session, user, "plan.activate", target=draft["id"], version=draft["version"])
            await execute(session, user, "plan.reject", target=draft["id"], version=draft["version"])
            assert (await session.get(m.Task, task["id"])).due_date is None
            assert (await session.get(ApplicationPlan, draft["id"])).status == "rejected"
        await engine.dispose()
    run(scenario())


def test_chat_approval_rejection_does_not_create_business_data():
    async def scenario():
        engine, factory, user, _ = await database()
        async with factory.begin() as session:
            service = ApplicationCommandService(session)
            body = CommandBatch(request_id="chat", commands=[Command(type="program.create", payload={"university": "A", "name": "B"})])
            approval = await service.propose(user, "workflow.commands", body.model_dump(mode="json"), "chat")
            assert not await session.scalar(select(m.Program))
            await service.decide(approval.id, user, False)
            assert not await session.scalar(select(m.Program))
            assert not await session.scalar(select(m.DomainEvent))
            accepted = await service.propose(user, "workflow.commands", body.model_dump(mode="json"), "other")
            await service.decide(accepted.id, user, True)
            assert await session.scalar(select(func.count()).select_from(m.Program)) == 1
        await engine.dispose()
    run(scenario())


def test_approval_followup_is_created_only_after_confirmation():
    async def scenario():
        engine, factory, user, _ = await database()
        async with factory.begin() as session:
            body = CommandBatch(request_id="dependent", commands=[Command(type="program.create", payload={"university": "A", "name": "B"}), Command(type="planning.request")])
            service = ApplicationCommandService(session)
            approval = await service.propose(user, "workflow.commands", body.model_dump(mode="json"), "dependent")
            assert not await session.scalar(select(m.Job))
            await service.decide(approval.id, user, True)
            job = await session.scalar(select(m.Job))
            state = await WorkflowService(session).state(user)
            assert job.input_revision == state.planning_revision
        await engine.dispose()
    run(scenario())


def test_task_completion_does_not_fulfill_three_recommendation_letters():
    async def scenario():
        engine, factory, user, _ = await database()
        async with factory.begin() as session:
            _, _, app = await setup_application(session, user)
            requirement = await execute(session, user, "requirement.create", {"application_id": app["id"], "key": "推荐信",
                "kind": "recommendation", "obligation": "required", "required_count": 3})
            for key in ["A", "B"]:
                await execute(session, user, "material.create", {"requirement_id": requirement["id"], "slot_key": key, "status": "submitted"})
            await execute(session, user, "task.create", {"application_id": app["id"], "stable_key": "remind", "title": "提醒推荐人", "status": "completed"})
            result = review(await WorkflowService(session).snapshot(user))
            assert result["completeness"] == "incomplete"
            assert result["missing_requirements"][0]["missing"] == 1
            await execute(session, user, "material.create", {"requirement_id": requirement["id"], "slot_key": "C", "status": "ready"})
            assert review(await WorkflowService(session).snapshot(user))["completeness"] == "incomplete"
        await engine.dispose()
    run(scenario())


def test_dependency_cycles_and_cross_application_requirement_rejected():
    async def scenario():
        engine, factory, user, _ = await database()
        async with factory.begin() as session:
            a = await execute(session, user, "task.create", {"stable_key": "a", "title": "A"})
            b = await execute(session, user, "task.create", {"stable_key": "b", "title": "B"})
            await execute(session, user, "dependency.create", {"predecessor_id": a["id"], "successor_id": b["id"]})
            with pytest.raises(WorkflowError, match="cycle"):
                await execute(session, user, "dependency.create", {"predecessor_id": b["id"], "successor_id": a["id"]})
            _, _, app = await setup_application(session, user)
            req = await execute(session, user, "requirement.create", {"application_id": app["id"], "key": "CV"})
            with pytest.raises(WorkflowError, match="same application"):
                await execute(session, user, "task.create", {"requirement_id": req["id"], "stable_key": "wrong", "title": "Wrong"})
        await engine.dispose()
    run(scenario())


def test_worker_delivery_retry_and_no_replanning_loop():
    async def scenario():
        engine, factory, user, _ = await database()
        async with factory.begin() as session:
            _, _, app = await setup_application(session, user)
            await execute(session, user, "task.create", {"application_id": app["id"], "stable_key": "sop", "title": "SOP"})
        await dispatch(factory)
        await dispatch(factory)
        while await process_one(factory):
            pass
        async with factory.begin() as session:
            count = await session.scalar(select(func.count()).select_from(ApplicationPlan))
            assert count > 0
            assert not await session.scalar(select(m.Job).where(m.Job.status != "completed"))
        await dispatch(factory)
        assert await process_one(factory) is False
        async with factory.begin() as session:
            assert await session.scalar(select(func.count()).select_from(ApplicationPlan)) == count
        await engine.dispose()
    run(scenario())


def test_expired_job_lease_is_recovered():
    async def scenario():
        engine, factory, user, _ = await database()
        async with factory.begin() as session:
            await WorkflowService(session).state(user, lock=True)
            session.add(m.Job(user_id=user, dedupe_key="crashed", type="review", input_revision=0, status="running",
                attempt=1, lease_token="old", lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        assert await process_one(factory)
        async with factory() as session:
            job = await session.scalar(select(m.Job))
            assert job.status == "completed" and job.attempt == 2 and job.lease_token is None
        await engine.dispose()
    run(scenario())


def test_legacy_task_migration_is_explicit_idempotent_and_preserves_history():
    async def scenario():
        engine, factory, user, _ = await database()
        async with factory.begin() as session:
            plan = ApplicationPlan(user_id=user, version=1, status="active")
            session.add(plan)
            await session.flush()
            old = ApplicationTask(plan_id=plan.id, stable_key="cv", title="CV", status="completed", evidence="done")
            session.add(old)
            await session.flush()
            first = await WorkflowService(session).migrate_tasks(user)
            second = await WorkflowService(session).migrate_tasks(user)
            assert first["copied"] == [old.id] and second["already_migrated"] == [old.id]
            assert (await session.scalar(select(m.Task))).status == "completed"
            assert await session.get(ApplicationTask, old.id) is not None
        await engine.dispose()
    run(scenario())


def snapshot_fixture():
    return {"revision": 1, "today": "2027-01-04", "availability": {"weekdays": [0,1,2,3,4]},
        "programs": [], "offerings": [{"id": "o", "deadline_date": "2027-02-01"}],
        "applications": [{"id": "a", "offering_id": "o"}], "requirements": [], "materials": [], "profile": {},
        "calendar_events": [], "dependencies": [], "tasks": [
            {"id": "t1", "application_id": "a", "stable_key": "one", "title": "One", "status": "planned",
             "duration_days": 2, "version": 1, "start_date": None, "due_date": None, "locked": False}]}


def test_scheduler_dependency_workdays_and_blocked_dates():
    snap = snapshot_fixture()
    snap["tasks"].append({**snap["tasks"][0], "id": "t2", "title": "Two", "stable_key": "two", "duration_days": 3})
    snap["dependencies"] = [{"predecessor_id": "t1", "successor_id": "t2", "lag_days": 1}]
    snap["calendar_events"] = [{"type": "unavailable", "date": "2027-01-05"}]
    result = schedule(snap)
    assert not result["conflicts"]
    a, b = result["items"]
    assert a["schedule_snapshot"]["due_date"] == "2027-01-06"
    assert b["schedule_snapshot"]["start_date"] == "2027-01-08"
    assert b["schedule_snapshot"]["due_date"] == "2027-01-12"


@pytest.mark.parametrize("mode", ["unknown", "impossible", "cycle", "locked"])
def test_scheduler_reports_unknown_infeasible_cyclic_and_locked_conflicts(mode):
    snap = snapshot_fixture()
    if mode == "unknown": snap["offerings"] = []
    if mode == "impossible": snap["offerings"][0]["deadline_date"] = "2027-01-04"
    if mode == "cycle": snap["dependencies"] = [{"predecessor_id": "t1", "successor_id": "t1", "lag_days": 0}]
    if mode == "locked": snap["tasks"][0].update(locked=True, start_date="2026-12-01", due_date="2026-12-02")
    assert schedule(snap)["conflicts"]


def test_date_precision_never_invents_midnight():
    with pytest.raises(ValueError):
        OfferingData(program_id="a", deadline_at="2027-01-01T12:00:00", date_precision="instant")
    event = EventData(title="Exam", date="2027-01-01", date_precision="date")
    assert event.start_at is None and event.date == date(2027,1,1)


def test_evidence_history_is_superseded_without_fabricating_verification():
    async def scenario():
        engine,factory,user,_=await database()
        async with factory.begin() as session:
            source=await execute(session,user,"source.create",{"excerpt":"A大学MSCS"})
            result=await WorkflowService(session).execute(user,CommandBatch(request_id="evidence",commands=[Command(
                type="program.create",payload={"university":"A大学","name":"MSCS"},evidence_refs=[source["id"]])]))
            program=result["changed_resources"][0]
            await execute(session,user,"program.update",{"name":"MSc CS"},program["id"],1)
            observations=(await session.scalars(select(m.Observation).where(m.Observation.field=="name"))).all()
            assert len(observations)==1 and observations[0].decision_status=="superseded"
            assert observations[0].verification_status=="user_provided"
        await engine.dispose()
    run(scenario())


def test_preference_command_uses_existing_memory_authority():
    async def scenario():
        engine,factory,user,_=await database()
        async with factory.begin() as session:
            await execute(session,user,"preference.change",{"key":"avoid_gre","value":True,
                "evidence":"我不想考GRE","expected_version":0})
            snap=await WorkflowService(session).snapshot(user)
            assert snap["preferences"][0]["value"]=={"value":True}
            assert snap["preference_versions"]["avoid_gre"]==1
            with pytest.raises(WorkflowError,match="version conflict"):
                await execute(session,user,"preference.change",{"key":"avoid_gre","value":False,
                    "evidence":"我愿意考GRE","expected_version":0})
        await engine.dispose()
    run(scenario())


def test_worker_does_not_leave_exhausted_crashed_job_running_forever():
    async def scenario():
        engine,factory,user,_=await database()
        async with factory.begin() as session:
            session.add(m.Job(user_id=user,dedupe_key="exhausted",type="review",input_revision=0,status="running",
                attempt=3,lease_token="dead",lease_until=datetime.now(timezone.utc)-timedelta(seconds=1)))
        assert not await process_one(factory)
        async with factory() as session:
            assert (await session.scalar(select(m.Job))).status=="failed"
        await engine.dispose()
    run(scenario())


@pytest.mark.parametrize("kind,fields,expected", [("plan.activated", [], "no_op"), ("task.updated", ["title"], "no_op"),
    ("task.updated", ["status"], "review"), ("offering.updated", ["deadline_date"], "replan"),
    ("material.updated", ["status"], "review")])
def test_selective_impact(kind, fields, expected):
    assert impact({"type": kind, "payload": {"changed_fields": fields}}) == expected
