"""Durable outbox + leased jobs. Redis is optional for workflow correctness."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import or_, select, update
from . import models as m
from .contracts import Command, CommandBatch
from .engine import impact, review
from .service import WorkflowService, row_out


async def dispatch(factory, limit=50):
    async with factory.begin() as session:
        events = (await session.scalars(select(m.DomainEvent).where(m.DomainEvent.delivery_status == "queued")
                     .order_by(m.DomainEvent.created_at, m.DomainEvent.id).limit(limit).with_for_update(skip_locked=True))).all()
        for event in events:
            # CAS also fences competing workers on databases without row locks.
            won = await session.execute(update(m.DomainEvent).where(m.DomainEvent.id == event.id,
                m.DomainEvent.delivery_status == "queued").values(delivery_status="delivered"))
            if won.rowcount != 1:
                continue
            action = impact(row_out(event))
            from ..v2.core.config import settings
            if settings.app_mode == "applyops":
                from .monitor_worker import retry_changed_inputs
                if event.type.startswith(("profile.", "preference.", "application.", "offering.", "material.", "requirement.", "task.")):
                    await retry_changed_inputs(session, event.user_id)
                if action == "replan":
                    action = "review"
            if action == "no_op":
                continue
            state = await WorkflowService(session).state(event.user_id, lock=True)
            app_id = event.payload.get("application_id")
            key = f"{event.user_id}:{action}:{state.planning_revision}:{app_id or 'all'}"
            scope = {"application_ids": [app_id] if app_id else []}
            if event.type == "task.completed":
                key = f"completion:{event.id}"
                scope["completion_event"] = {"event_id": event.id, "user_id": event.user_id,
                    **{field: event.payload.get(field) for field in ("task_id", "task_version", "application_id", "requirement_id", "title", "completed_at")}}
            if not await session.scalar(select(m.Job.id).where(m.Job.dedupe_key == key)):
                session.add(m.Job(user_id=event.user_id, dedupe_key=key, type=action,
                    input_revision=state.planning_revision, scope=scope))
        return len(events)


async def process_one(factory):
    now, token = datetime.now(timezone.utc), uuid4().hex
    eligible = or_(m.Job.status == "queued", (m.Job.status == "running") & (m.Job.lease_until < now))
    async with factory.begin() as session:
        from ..v2.core.config import settings
        if settings.app_mode == "applyops":
            await session.execute(update(m.Job).where(m.Job.type == "replan", m.Job.status.in_(["queued", "running"]))
                .values(status="cancelled", error="automatic planning retired", lease_token=None, lease_until=None))
        await session.execute(update(m.Job).where(eligible, m.Job.attempt >= 3, m.Job.type.in_(["review", "replan"])).values(
            status="failed", error="retry budget exhausted", lease_token=None, lease_until=None)
            .execution_options(synchronize_session=False))
        job = await session.scalar(select(m.Job).where(eligible, m.Job.attempt < 3, m.Job.type.in_(["review", "replan"]))
                    .order_by(m.Job.created_at, m.Job.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return False
        won = await session.execute(update(m.Job).where(m.Job.id == job.id, eligible).values(
            status="running", lease_token=token, lease_until=now + timedelta(seconds=120), attempt=m.Job.attempt + 1)
            .execution_options(synchronize_session=False))
        if won.rowcount != 1:
            return True
        identity, user_id, kind, scope = job.id, job.user_id, job.type, job.scope
    try:
        async with factory.begin() as session:
            service = WorkflowService(session)
            await service.state(user_id, lock=True)
            snapshot = await service.snapshot(user_id)
        result = review(snapshot)
        # Extension hook only: the default provider returns no suggestions and
        # never writes materials. A future provider must produce approvals.
        if scope.get("completion_event"):
            from .proactive import DisabledMaterialSuggestions, TaskCompletionEvent
            event = TaskCompletionEvent.model_validate(scope["completion_event"])
            current_task = next((task for task in snapshot["tasks"] if task["id"] == event.task_id), None)
            if current_task and current_task["status"] == "completed" and current_task["version"] == event.task_version:
                await DisabledMaterialSuggestions().on_task_completed(event)
        async with factory.begin() as session:
            service = WorkflowService(session)
            state = await service.state(user_id, lock=True)
            job = await session.scalar(select(m.Job).where(m.Job.id == identity, m.Job.status == "running",
                                                           m.Job.lease_token == token).with_for_update())
            if job is None:
                return True
            if state.planning_revision != snapshot["revision"]:
                job.status, job.lease_token, job.lease_until = "superseded", None, None
                key = f"refresh:{identity}:{state.planning_revision}"
                if not await session.scalar(select(m.Job.id).where(m.Job.dedupe_key == key)):
                    session.add(m.Job(user_id=user_id, dedupe_key=key, type=kind,
                        input_revision=state.planning_revision, scope=scope))
                return True
            if kind == "replan":
                import hashlib
                scope_hash = hashlib.sha256(str(sorted(scope.get("application_ids", []))).encode()).hexdigest()[:16]
                receipt = await service.execute(user_id, CommandBatch(request_id=f"worker-draft:{user_id}:{snapshot['revision']}:{scope_hash}",
                    commands=[Command(type="plan.draft", payload={"application_ids": scope.get("application_ids", [])})]), source="worker")
                plan = receipt["changed_resources"][0]
                result["plan_id"] = plan["id"]
                notification_key = f"plan:{plan['id']}:notification"
                if not await session.scalar(select(m.Notification.id).where(m.Notification.dedupe_key == notification_key)):
                    session.add(m.Notification(user_id=user_id, type="plan_draft", target_id=plan["id"],
                        dedupe_key=notification_key, message="申请信息发生变化，已生成计划草稿，请查看差异并确认。"))
            job.result, job.status, job.error = result, "completed", None
            job.lease_token, job.lease_until = None, None
    except Exception as exc:
        async with factory.begin() as session:
            job = await session.scalar(select(m.Job).where(m.Job.id == identity, m.Job.lease_token == token).with_for_update())
            if job:
                job.error = type(exc).__name__
                job.status = "failed" if job.attempt >= 3 else "queued"
                job.lease_token, job.lease_until = None, None
        raise
    return True


async def reminders(factory):
    async with factory.begin() as session:
        users = list((await session.scalars(select(m.WorkflowState.user_id))).all())
    for user_id in users:
        async with factory.begin() as session:
            service = WorkflowService(session)
            await service.state(user_id, lock=True)
            snap = await service.snapshot(user_id)
            today = snap["today"]
            horizon = (datetime.fromisoformat(today).date() + timedelta(days=7)).isoformat()
            for task in snap["tasks"]:
                if task["status"] in {"completed", "cancelled"} or not task["due_date"] or not today <= task["due_date"] <= horizon:
                    continue
                key = f"reminder:{task['id']}:{task['version']}:{today}"
                if not await session.scalar(select(m.Notification.id).where(m.Notification.dedupe_key == key)):
                    session.add(m.Notification(user_id=user_id, type="task_due", target_id=task["id"], dedupe_key=key,
                                               message=f"{task['title']}：计划日期 {task['due_date']}"))


async def run(factory=None):
    if factory is None:
        from ..v2.db.session import SessionLocal
        factory = SessionLocal
    cycles = 0
    while True:
        try:
            from .monitor_worker import process_one as monitor_one, schedule_due
            if cycles % 30 == 0:
                await schedule_due(factory)
            await dispatch(factory)
            await process_one(factory)
            await monitor_one(factory)
            if cycles % 60 == 0:
                await reminders(factory)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("workflow worker iteration failed")
        cycles += 1
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(run())
