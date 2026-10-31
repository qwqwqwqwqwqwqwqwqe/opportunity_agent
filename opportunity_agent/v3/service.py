"""Single transaction boundary for explicit UI commands, approvals and workers.

The caller commits. execute() uses a savepoint so a failed change set cannot
leave successful earlier commands behind if the caller handles the exception.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import delete, func, or_, select, update, text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import ValidationError
from ..v2.db.models import Application, ApplicationPlan, ApplicationTask, TaskProgress, Profile, User, MemoryItem
from . import models as m
from .contracts import (ApplicationData, CommandBatch, DependencyData, EventData, MaterialData,
                        OfferingData, ProgramData, RequirementData, SourceData, TaskData)
from .engine import review, schedule


class WorkflowError(ValueError):
    def __init__(self, detail: str, status: int = 422):
        super().__init__(detail)
        self.status = status


def encode(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def wire(value):
    return json.loads(json.dumps(value, default=encode, ensure_ascii=False))


def row_out(row):
    return wire({col.key: getattr(row, col.key) for col in row.__table__.columns})


ENTITIES = {"program": (m.Program, ProgramData), "offering": (m.Offering, OfferingData),
            "application": (Application, ApplicationData), "requirement": (m.Requirement, RequirementData),
            "material": (m.Material, MaterialData), "task": (m.Task, TaskData),
            "calendar_event": (m.CalendarEvent, EventData)}


async def _delete_task_rows(session, user_id, task_ids):
    """Remove task-owned edges while leaving historical plan snapshots intact."""
    task_ids = list(dict.fromkeys(task_ids))
    if not task_ids:
        return
    legacy_ids = (await session.scalars(select(m.Task.legacy_task_id).where(
        m.Task.user_id == user_id, m.Task.id.in_(task_ids), m.Task.legacy_task_id.is_not(None)))).all()
    await session.execute(delete(m.Dependency).where(m.Dependency.user_id == user_id,
        or_(m.Dependency.predecessor_id.in_(task_ids), m.Dependency.successor_id.in_(task_ids))))
    # Keep old plan items as snapshots; they no longer point at a live task.
    await session.execute(update(m.PlanItem).where(m.PlanItem.user_id == user_id,
        m.PlanItem.task_id.in_(task_ids)).values(task_id=None))
    await session.execute(delete(m.Observation).where(m.Observation.user_id == user_id,
        m.Observation.target_type == "task", m.Observation.target_id.in_(task_ids)))
    await session.execute(delete(m.Notification).where(m.Notification.user_id == user_id,
        m.Notification.type == "task_due", m.Notification.target_id.in_(task_ids)))
    if legacy_ids:
        await session.execute(delete(TaskProgress).where(TaskProgress.task_id.in_(legacy_ids)))
        await session.execute(delete(ApplicationTask).where(ApplicationTask.id.in_(legacy_ids)))
    await session.execute(delete(m.Task).where(m.Task.user_id == user_id, m.Task.id.in_(task_ids)))


async def _delete_monitor_rows(session, user_id, application_id):
    """Remove monitor history tied to a deleted application and cancel its jobs."""
    from . import monitor_models as mm

    monitor = await session.scalar(select(mm.Monitor).where(mm.Monitor.user_id == user_id,
        mm.Monitor.application_id == application_id).with_for_update())
    if not monitor:
        return
    check_ids = (await session.scalars(select(mm.MonitorCheck.id).where(
        mm.MonitorCheck.user_id == user_id, mm.MonitorCheck.monitor_id == monitor.id))).all()
    analysis_ids = (await session.scalars(select(mm.MonitorAnalysis.id).where(
        mm.MonitorAnalysis.user_id == user_id, mm.MonitorAnalysis.monitor_id == monitor.id))).all()
    sources = (await session.scalars(select(mm.MonitorSource).where(
        mm.MonitorSource.user_id == user_id, mm.MonitorSource.monitor_id == monitor.id))).all()
    source_ids = [row.id for row in sources]
    jobs = (await session.scalars(select(m.Job).where(m.Job.user_id == user_id,
        m.Job.type.in_(["monitor.check", "monitor.analyze"]), m.Job.status.in_(["queued", "running"])))).all()
    for job in jobs:
        if job.scope.get("check_id") in check_ids or job.scope.get("analysis_id") in analysis_ids:
            job.status, job.error, job.lease_token, job.lease_until = "cancelled", "project_deleted", None, None
    if analysis_ids:
        await session.execute(delete(m.Notification).where(m.Notification.user_id == user_id,
            m.Notification.analysis_id.in_(analysis_ids)))
        await session.execute(delete(mm.MonitorAnalysis).where(mm.MonitorAnalysis.user_id == user_id,
            mm.MonitorAnalysis.id.in_(analysis_ids)))
    if check_ids:
        await session.execute(delete(mm.MonitorChange).where(mm.MonitorChange.user_id == user_id,
            mm.MonitorChange.check_id.in_(check_ids)))
    await session.execute(delete(mm.MonitorSnapshot).where(mm.MonitorSnapshot.user_id == user_id,
        mm.MonitorSnapshot.monitor_id == monitor.id))
    if source_ids:
        await session.execute(delete(mm.MonitorSource).where(mm.MonitorSource.user_id == user_id,
            mm.MonitorSource.id.in_(source_ids)))
    if check_ids:
        await session.execute(delete(mm.MonitorCheck).where(mm.MonitorCheck.user_id == user_id,
            mm.MonitorCheck.id.in_(check_ids)))
    await session.execute(delete(mm.Monitor).where(mm.Monitor.user_id == user_id, mm.Monitor.id == monitor.id))


class WorkflowService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def owned(self, model, identity, user_id, lock=False):
        statement = select(model).where(model.id == identity, model.user_id == user_id)
        row = await self.session.scalar(statement.with_for_update() if lock else statement)
        if row is None:
            raise WorkflowError("resource not found", 404)
        return row

    async def state(self, user_id, lock=False):
        # Lock the existing user before creating the singleton state. This also
        # serializes distinct commands for the same user on PostgreSQL.
        if lock:
            if self.session.bind.dialect.name == "sqlite":
                # SQLite ignores FOR UPDATE. Acquire its write lock before
                # reading inside the savepoint, avoiding read-to-write upgrade
                # deadlocks with the run dispatcher and other short writers.
                found = (await self.session.execute(text("UPDATE users SET id = id WHERE id = :identity"),
                                                   {"identity": user_id})).rowcount
            else:
                found = await self.session.scalar(select(User.id).where(User.id == user_id).with_for_update())
            if not found:
                raise WorkflowError("user not found", 404)
        row = await self.session.scalar(select(m.WorkflowState).where(m.WorkflowState.user_id == user_id)
                                        .with_for_update() if lock else
                                        select(m.WorkflowState).where(m.WorkflowState.user_id == user_id))
        if row is None and lock:
            row = m.WorkflowState(user_id=user_id)
            self.session.add(row)
            await self.session.flush()
        return row

    async def snapshot(self, user_id, today=None):
        state = await self.state(user_id)
        availability = state.availability if state else {"weekdays": [0, 1, 2, 3, 4], "timezone": "Asia/Shanghai"}
        if today is None:
            today = datetime.now(ZoneInfo(availability.get("timezone", "Asia/Shanghai"))).date()
        result = {"revision": state.planning_revision if state else 0, "today": str(today), "availability": availability}
        for key, model in [("programs", m.Program), ("offerings", m.Offering), ("applications", Application),
                           ("requirements", m.Requirement), ("materials", m.Material), ("tasks", m.Task),
                           ("dependencies", m.Dependency), ("calendar_events", m.CalendarEvent)]:
            rows = (await self.session.scalars(select(model).where(model.user_id == user_id).order_by(model.created_at, model.id))).all()
            result[key] = [row_out(row) for row in rows]
        profile = await self.session.scalar(select(Profile).where(Profile.user_id == user_id))
        result["profile"] = profile.payload if profile else {}
        result["profile_version"] = profile.version if profile else 0
        preferences = (await self.session.scalars(select(MemoryItem).where(MemoryItem.user_id == user_id,
                                      MemoryItem.memory_type == "preference"))).all()
        result["preference_versions"] = {item.key: item.version for item in preferences}
        result["preferences"] = [{"key": item.key, "value": item.value, "evidence": item.evidence}
                                 for item in preferences if item.active]
        current = await self.session.scalar(select(ApplicationPlan).where(ApplicationPlan.user_id == user_id,
                    ApplicationPlan.status == "active").order_by(ApplicationPlan.version.desc()))
        result["current_plan_id"] = current.id if current else None
        return result

    async def execute(self, user_id: str, batch: CommandBatch, source="form", run_id=None):
        fingerprint = hashlib.sha256(json.dumps(batch.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
        # On SQLite the write must begin the outer transaction before SAVEPOINT;
        # otherwise releasing the first savepoint can commit ahead of the API's
        # commit/rollback boundary under the legacy sqlite transaction mode.
        state = await self.state(user_id, lock=True)
        async with self.session.begin_nested():
            old = await self.session.scalar(select(m.CommandReceipt).where(m.CommandReceipt.user_id == user_id,
                    m.CommandReceipt.operation == "batch", m.CommandReceipt.request_id == batch.request_id))
            if old:
                if old.payload_hash != fingerprint:
                    raise WorkflowError("request_id was already used with different content", 409)
                return old.response
            resources, references = [], {}
            base_revision = state.planning_revision
            for command in batch.commands:
                def resolve(value):
                    if isinstance(value, str) and value.startswith("$ref:"):
                        key = value[5:]
                        if key not in references:
                            raise WorkflowError(f"unknown or forward command reference: {key}")
                        return references[key]
                    if isinstance(value, dict):
                        return {key: resolve(item) for key, item in value.items()}
                    if isinstance(value, list):
                        return [resolve(item) for item in value]
                    return value
                command = command.model_copy(update={"payload": resolve(command.payload)})
                if command.type in {'project.save', 'project.delete'} and command.target_id:
                    # Every preview in this batch refers to the same starting
                    # snapshot. Earlier commands may bump the revision, but may
                    # never authorize a draft stale before this transaction.
                    if command.payload.get('expected_revision') != base_revision:
                        raise WorkflowError('项目数据已变化，请刷新后重新确认', 409)
                    command = command.model_copy(update={'payload':{**command.payload, 'expected_revision':state.planning_revision}})
                result = await self._apply(user_id, command, state, source, run_id)
                resources.append(result)
                if command.ref:
                    if command.ref in references:
                        raise WorkflowError("command references must be unique")
                    references[command.ref] = result["id"]
            receipt = {"request_id": batch.request_id, "status": "COMPLETED", "changed_resources": resources,
                       "planning_revision": state.planning_revision}
            self.session.add(m.CommandReceipt(user_id=user_id, operation="batch", request_id=batch.request_id,
                                              payload_hash=fingerprint, response=wire(receipt)))
            await self.session.flush()
            return wire(receipt)

    async def _event(self, user_id, kind, row, fields, state, source, bump=True, application_id=None, extra=None):
        if bump:
            state.planning_revision += 1
        self.session.add(m.DomainEvent(user_id=user_id, aggregate_type=kind.split(".")[0], aggregate_id=row.id,
            aggregate_version=getattr(row, "version", state.planning_revision), type=kind,
            payload={"changed_fields": sorted(fields), "revision": state.planning_revision, "source": source,
                     "application_id": application_id, **(extra or {})}))

    async def _relations(self, user_id, kind, data):
        for field, model in [("program_id", m.Program), ("offering_id", m.Offering), ("application_id", Application),
                             ("requirement_id", m.Requirement), ("source_id", m.Source), ("evidence_ref", m.Source)]:
            if data.get(field):
                await self.owned(model, data[field], user_id)
        if kind == "task" and data.get("requirement_id"):
            requirement = await self.owned(m.Requirement, data["requirement_id"], user_id)
            if data.get("application_id") != requirement.application_id:
                raise WorkflowError("task requirement must belong to the same application")

    async def _apply(self, user_id, command, state, source, run_id):
        from ..v2.core.config import settings
        if settings.app_mode == "applyops" and command.type in {"plan.draft", "plan.activate", "planning.request"}:
            raise WorkflowError("自动排期已停用，请使用官网监控和待办；历史计划仍可查看", 409)
        if command.type == "project.save":
            from .workspace import save_project
            return await save_project(self, user_id, command, state, source, run_id)
        if command.type in {"project.delete", "task.delete"}:
            # Older pending chat proposals may include the chat source ref on
            # every command. It is harmless metadata for deletion and is ignored.
            if command.type == "task.delete" and command.payload:
                raise WorkflowError("task delete commands do not accept a payload")
            if not command.target_id or command.expected_version is None:
                raise WorkflowError("delete command requires a target and expected_version")
            if command.type == "task.delete":
                task = await self.owned(m.Task, command.target_id, user_id, lock=True)
                if task.version != command.expected_version:
                    raise WorkflowError("待办已变化，请刷新后重试删除", 409)
                task_id, title, application_id, version = task.id, task.title, task.application_id, task.version
                await self._event(user_id, "task.deleted", task, ["deleted"], state, source,
                    application_id=application_id, extra={"task_id": task_id, "title": title})
                await _delete_task_rows(self.session, user_id, [task_id])
                return {"resource_type": "task", "type": "task", "id": task_id,
                        "version": version, "title": title, "application_id": application_id, "deleted": True}

            application = await self.owned(Application, command.target_id, user_id, lock=True)
            if application.version != command.expected_version:
                raise WorkflowError("项目已变化，请刷新后重试删除", 409)
            expected_revision = command.payload.get("expected_revision")
            if type(expected_revision) is not int or expected_revision != state.planning_revision:
                raise WorkflowError("项目及关联数据已变化，请刷新后重新确认删除", 409)
            application_id, university, program_name, cycle = application.id, application.university, application.program, application.intake
            offering_id = application.offering_id
            await _delete_monitor_rows(self.session, user_id, application_id)
            requirement_ids = (await self.session.scalars(select(m.Requirement.id).where(
                m.Requirement.user_id == user_id, m.Requirement.application_id == application_id))).all()
            task_ids = (await self.session.scalars(select(m.Task.id).where(
                m.Task.user_id == user_id, m.Task.application_id == application_id))).all()
            await _delete_task_rows(self.session, user_id, task_ids)
            if requirement_ids:
                material_ids = (await self.session.scalars(select(m.Material.id).where(
                    m.Material.user_id == user_id, m.Material.requirement_id.in_(requirement_ids)))).all()
                if material_ids:
                    await self.session.execute(delete(m.Observation).where(m.Observation.user_id == user_id,
                        m.Observation.target_type == "material", m.Observation.target_id.in_(material_ids)))
                    await self.session.execute(delete(m.Material).where(m.Material.user_id == user_id,
                        m.Material.id.in_(material_ids)))
                await self.session.execute(delete(m.Observation).where(m.Observation.user_id == user_id,
                    m.Observation.target_type == "requirement", m.Observation.target_id.in_(requirement_ids)))
                await self.session.execute(delete(m.Requirement).where(m.Requirement.user_id == user_id,
                    m.Requirement.id.in_(requirement_ids)))
            await self.session.execute(delete(m.Observation).where(m.Observation.user_id == user_id,
                m.Observation.target_type == "application", m.Observation.target_id == application_id))
            await self._event(user_id, "project.deleted", application, ["deleted"], state, source,
                application_id=application_id,
                extra={"university": university, "program": program_name, "cycle": cycle,
                       "deleted_tasks": len(task_ids), "deleted_requirements": len(requirement_ids)})
            await self.session.delete(application)
            await self.session.flush()
            if offering_id:
                offering = await self.session.scalar(select(m.Offering).where(m.Offering.id == offering_id,
                    m.Offering.user_id == user_id).with_for_update())
                if offering and not await self.session.scalar(select(Application.id).where(
                        Application.user_id == user_id, Application.offering_id == offering_id).limit(1)):
                    program_id = offering.program_id
                    await self.session.execute(delete(m.Observation).where(m.Observation.user_id == user_id,
                        m.Observation.target_type == "offering", m.Observation.target_id == offering.id))
                    await self.session.delete(offering)
                    await self.session.flush()
                    if not await self.session.scalar(select(m.Offering.id).where(
                            m.Offering.user_id == user_id, m.Offering.program_id == program_id).limit(1)):
                        await self.session.execute(delete(m.Observation).where(m.Observation.user_id == user_id,
                            m.Observation.target_type == "program", m.Observation.target_id == program_id))
                        await self.session.execute(delete(m.Program).where(m.Program.user_id == user_id,
                            m.Program.id == program_id))
            return {"resource_type": "project", "type": "project", "id": application_id,
                    "version": command.expected_version, "university": university,
                    "program": program_name, "cycle": cycle, "deleted": True}
        kind, _, action = command.type.partition(".")
        if kind in ENTITIES and action in {"create", "update"}:
            model, schema = ENTITIES[kind]
            row = None
            prior_status = None
            if action == "update":
                row = await self.owned(model, command.target_id, user_id, lock=True)
                prior_status = getattr(row, "status", None)
                if command.expected_version != row.version:
                    raise WorkflowError("entity version conflict", 409)
                immutable = {"program_id", "offering_id", "application_id", "requirement_id", "key", "stable_key"}
                for field in immutable & command.payload.keys():
                    if kind == "application" and field == "offering_id" and row.offering_id is None:
                        continue  # Link a legacy V2 application once; keep its ID.
                    if hasattr(row, field) and command.payload[field] != getattr(row, field):
                        raise WorkflowError(f"{field} cannot be moved to another identity")
                old_data = {field: getattr(row, field) for field in schema.model_fields}
                body = {**old_data, **command.payload}
            else:
                body = command.payload
            try:
                data = schema.model_validate(body).model_dump()
                if kind == "requirement":
                    data["criteria"] = wire(data["criteria"])
            except (ValidationError, ValueError, KeyError) as exc:
                raise WorkflowError(str(exc)) from exc
            await self._relations(user_id, kind, data)
            if kind == "application":
                offering = await self.owned(m.Offering, data["offering_id"], user_id)
                if not offering.cycle:
                    raise WorkflowError("confirm the offering cycle before creating an application")
                program = await self.owned(m.Program, offering.program_id, user_id)
                duplicate = await self.session.scalar(select(Application).where(Application.user_id == user_id,
                    Application.offering_id == offering.id))
                if duplicate and (row is None or duplicate.id != row.id):
                    raise WorkflowError("application already exists for this offering", 409)
                data.update(university=program.university, program=program.name, intake=offering.cycle)
            if kind == "task":
                data["scope_key"] = data.get("application_id") or "personal"
            if action == "create":
                row = model(user_id=user_id, **data)
                self.session.add(row)
                await self.session.flush()
                changed = list(data)
            else:
                changed = [field for field, value in data.items() if getattr(row, field) != value]
                if changed:
                    expected = row.version
                    outcome = await self.session.execute(update(model).where(model.id == row.id, model.user_id == user_id,
                        model.version == expected).values(**data, version=expected + 1).execution_options(synchronize_session=False))
                    if outcome.rowcount != 1:
                        raise WorkflowError("entity version conflict", 409)
                    await self.session.refresh(row)
            for ref in command.evidence_refs:
                await self.owned(m.Source, ref, user_id)
            for field in changed:
                await self.session.execute(update(m.Observation).where(m.Observation.user_id == user_id,
                    m.Observation.target_type == kind, m.Observation.target_id == row.id,
                    m.Observation.field == field, m.Observation.decision_status == "accepted")
                    .values(decision_status="superseded"))
                for ref in command.evidence_refs:
                    self.session.add(m.Observation(user_id=user_id, target_type=kind, target_id=row.id,
                         field=field, value={"value": wire(data[field])}, source_id=ref))
            if changed:
                app_id = data.get("application_id")
                if kind == "application":
                    app_id = row.id
                if kind == "material":
                    app_id = (await self.owned(m.Requirement, row.requirement_id, user_id)).application_id
                event_type = f"{kind}.{'created' if action == 'create' else 'updated'}"
                extra = None
                if kind == "task" and action == "update" and prior_status != "completed" and row.status == "completed":
                    event_type = "task.completed"
                    extra = {"task_id": row.id, "task_version": row.version, "title": row.title,
                             "requirement_id": row.requirement_id, "status_before": prior_status, "status_after": row.status,
                             "completed_at": datetime.now(timezone.utc).isoformat()}
                await self._event(user_id, event_type, row, changed, state, source, application_id=app_id, extra=extra)
            return {"resource_type": kind, "type": kind, **row_out(row)}
        if command.type == "source.create":
            data = SourceData.model_validate(command.payload).model_dump()
            row = m.Source(user_id=user_id, **data, content_hash=hashlib.sha256(data["excerpt"].encode()).hexdigest())
            self.session.add(row)
            await self.session.flush()
            return {"type": "source", **row_out(row)}
        if command.type == "dependency.create":
            data = DependencyData.model_validate(command.payload).model_dump()
            for field in ("predecessor_id", "successor_id"):
                await self.owned(m.Task, data[field], user_id)
            if data["predecessor_id"] == data["successor_id"]:
                raise WorkflowError("task dependency cycle")
            edges = (await self.session.scalars(select(m.Dependency).where(m.Dependency.user_id == user_id))).all()
            graph = {}
            for edge in edges:
                graph.setdefault(edge.predecessor_id, []).append(edge.successor_id)
            pending, seen = [data["successor_id"]], set()
            while pending:
                current = pending.pop()
                if current == data["predecessor_id"]:
                    raise WorkflowError("task dependency cycle")
                if current not in seen:
                    seen.add(current)
                    pending.extend(graph.get(current, []))
            row = m.Dependency(user_id=user_id, **data)
            self.session.add(row)
            await self.session.flush()
            await self._event(user_id, "dependency.created", row, data, state, source)
            return {"type": "dependency", **row_out(row)}
        if command.type == "profile.change":
            profile = await self.session.scalar(select(Profile).where(Profile.user_id == user_id))
            if command.expected_version != (profile.version if profile else 1):
                raise WorkflowError("profile version conflict", 409)
            from ..v2.services.applications import ApplicationCommandService
            from types import SimpleNamespace
            await ApplicationCommandService(self.session)._apply_one(SimpleNamespace(user_id=user_id, run_id=run_id),
                "profile.change", {**command.payload, "expected_version": command.expected_version})
            profile = await self.session.scalar(select(Profile).where(Profile.user_id == user_id))
            await self._event(user_id, "profile.updated", profile,
                              [fact["field"] for fact in command.payload.get("facts", [])], state, source)
            return {"type": "profile", "id": profile.id, "version": profile.version}
        if command.type == "preference.change":
            from ..v2.services.memory import MemoryService
            body = command.payload
            if set(body) - {"key", "value", "evidence", "expected_version"}:
                raise WorkflowError("unsupported preference fields")
            expected = body.get("expected_version")
            if type(expected) is not int or expected < 0:
                raise WorkflowError("preference expected_version is required")
            from ..v2.repositories import VersionConflict
            try:
                row, changed = await MemoryService(self.session).apply_confirmed(user_id, body,
                    f"workflow:{run_id or 'form'}:{state.planning_revision}:{body['key']}")
            except VersionConflict as exc:
                raise WorkflowError("preference version conflict", 409) from exc
            if changed:
                await self._event(user_id, "preference.updated", row, [body["key"]], state, source)
            return {"type": "preference", "id": row.id, "version": row.version}
        if command.type == "availability.update":
            if command.expected_version != state.planning_revision + 1:
                raise WorkflowError("availability version conflict", 409)
            weekdays = command.payload.get("weekdays")
            if not isinstance(weekdays, list) or not weekdays or any(type(d) is not int or d not in range(7) for d in weekdays):
                raise WorkflowError("weekdays must be a nonempty list of 0..6")
            zone = command.payload.get("timezone", "Asia/Shanghai")
            ZoneInfo(zone)
            state.availability = {"weekdays": sorted(set(weekdays)), "timezone": zone}
            await self._event(user_id, "availability.updated", state, command.payload, state, source)
            return {"type": "availability", "version": state.planning_revision + 1, **state.availability}
        if command.type == "plan.draft":
            options = command.payload
            if set(options) - {"today", "application_ids"}:
                raise WorkflowError("plan drafts are generated from committed state")
            for app_id in options.get("application_ids", []):
                await self.owned(Application, app_id, user_id)
            snapshot = await self.snapshot(user_id, options.get("today"))
            draft = schedule(snapshot, options.get("application_ids"))
            maximum = await self.session.scalar(select(func.max(ApplicationPlan.version)).where(ApplicationPlan.user_id == user_id))
            version = max(state.next_plan_version, (maximum or 0) + 1)
            state.next_plan_version = version + 1
            row = ApplicationPlan(user_id=user_id, version=version, status="draft", schema_version=3,
                base_plan_id=snapshot["current_plan_id"], input_revision=snapshot["revision"], roadmap=wire(draft),
                revision_reason="workflow_schedule", source_run_id=run_id)
            self.session.add(row)
            await self.session.flush()
            for item in draft["items"]:
                self.session.add(m.PlanItem(user_id=user_id, plan_id=row.id, **wire(item)))
            await self._event(user_id, "plan.drafted", row, [], state, source, bump=False)
            return {"type": "plan", **row_out(row)}
        if command.type in {"planning.request", "review.request"}:
            job_type = "replan" if command.type == "planning.request" else "review"
            row = m.Job(user_id=user_id, dedupe_key=f"{job_type}:{user_id}:{state.planning_revision}", type=job_type,
                        input_revision=state.planning_revision, scope={})
            old = await self.session.scalar(select(m.Job).where(m.Job.dedupe_key == row.dedupe_key))
            if not old:
                self.session.add(row)
                await self.session.flush()
            return {"type": "job", "id": (old or row).id, "status": "queued"}
        if command.type in {"plan.activate", "plan.reject"}:
            row = await self.owned(ApplicationPlan, command.target_id, user_id, lock=True)
            if row.schema_version != 3 or row.status != "draft" or command.expected_version != row.version:
                raise WorkflowError("plan is not an available draft", 409)
            if command.type == "plan.reject":
                row.status = "rejected"
                await self._event(user_id, "plan.rejected", row, [], state, source, bump=False)
                return {"type": "plan", "id": row.id, "status": row.status, "version": row.version}
            if row.input_revision != state.planning_revision:
                raise WorkflowError("draft is stale; regenerate from current state", 409)
            current = await self.session.scalar(select(ApplicationPlan).where(ApplicationPlan.user_id == user_id,
                         ApplicationPlan.status == "active").order_by(ApplicationPlan.version.desc()))
            if row.base_plan_id != (current.id if current else None):
                raise WorkflowError("base plan changed; regenerate draft", 409)
            if row.roadmap.get("conflicts") or not row.roadmap.get("items"):
                raise WorkflowError("resolve plan conflicts before activation")
            items = (await self.session.scalars(select(m.PlanItem).where(m.PlanItem.plan_id == row.id))).all()
            for item in items:
                if item.task_id:
                    task = await self.owned(m.Task, item.task_id, user_id, lock=True)
                    if task.version != item.input_task_version:
                        raise WorkflowError("task changed since draft generation", 409)
                    if not task.locked and task.status != "completed":
                        for field, value in item.schedule_snapshot.items():
                            setattr(task, field, date.fromisoformat(value) if field.endswith("date") and value else value)
                        task.version += 1
                else:
                    allowed = set(TaskData.model_fields)
                    data = {k: v for k, v in item.task_spec.items() if k in allowed}
                    data.update(item.schedule_snapshot)
                    data = TaskData.model_validate(data).model_dump()
                    await self._relations(user_id, "task", data)
                    task = m.Task(user_id=user_id, scope_key=data.get("application_id") or "personal", **data)
                    self.session.add(task)
                    await self.session.flush()
                    item.task_id = task.id
            await self.session.execute(update(ApplicationPlan).where(ApplicationPlan.user_id == user_id,
                ApplicationPlan.status == "active").values(status="superseded"))
            await self.session.flush()
            row.status = "active"
            await self._event(user_id, "plan.activated", row, [], state, source)
            return {"type": "plan", "id": row.id, "version": row.version, "status": row.status}
        if command.type == "notification.read":
            row = await self.owned(m.Notification, command.target_id, user_id)
            row.read_at = datetime.now(timezone.utc)
            return {"type": "notification", "id": row.id}
        raise WorkflowError(f"unsupported command: {command.type}")

    async def plan(self, user_id, plan_id):
        row = await self.owned(ApplicationPlan, plan_id, user_id)
        items = (await self.session.scalars(select(m.PlanItem).where(m.PlanItem.plan_id == row.id))).all()
        return {**row_out(row), "items": [row_out(item) for item in items]}

    async def diff(self, user_id, plan_id):
        plan = await self.plan(user_id, plan_id)
        changes = []
        for item in plan["items"]:
            current = await self.owned(m.Task, item["task_id"], user_id) if item["task_id"] else None
            before = {key: row_out(current)[key] for key in item["schedule_snapshot"]} if current else None
            after = item["schedule_snapshot"]
            changes.append({"task_id": item["task_id"], "title": item["task_spec"]["title"],
                            "kind": "added" if before is None else "retained" if before == after else "adjusted",
                            "before": before, "after": after})
        return {"plan_id": plan_id, "changes": changes, "conflicts": plan["roadmap"].get("conflicts", []),
                "input_revision": plan["input_revision"]}

    async def migrate_tasks(self, user_id):
        """Explicit one-time migration; old rows remain readable and untouched."""
        await self.state(user_id, lock=True)
        active = await self.session.scalar(select(ApplicationPlan).where(ApplicationPlan.user_id == user_id,
                      ApplicationPlan.status == "active").order_by(ApplicationPlan.version.desc()))
        app_ids = list((await self.session.scalars(select(Application.id).where(Application.user_id == user_id))).all())
        from sqlalchemy import or_
        query = select(ApplicationTask).where(or_(ApplicationTask.application_id.in_(app_ids),
                    ApplicationTask.plan_id == active.id if active else False))
        report = {"copied": [], "already_migrated": [], "needs_review": []}
        for old in (await self.session.scalars(query)).all():
            if await self.session.scalar(select(m.Task.id).where(m.Task.legacy_task_id == old.id)):
                report["already_migrated"].append(old.id)
                continue
            scope = old.application_id or "personal"
            if await self.session.scalar(select(m.Task.id).where(m.Task.user_id == user_id,
                m.Task.scope_key == scope, m.Task.stable_key == old.stable_key)):
                report["needs_review"].append(old.id)
                continue
            due = old.due_at.date() if old.due_at else None
            row = m.Task(user_id=user_id, application_id=old.application_id, scope_key=scope,
                stable_key=old.stable_key, title=old.title, status=old.status, evidence=old.evidence or "",
                due_date=due, date_certainty="estimated" if due else "unknown", legacy_task_id=old.id,
                show_in_calendar=bool(due and old.status not in {"completed", "cancelled"}))
            self.session.add(row)
            await self.session.flush()
            report["copied"].append(old.id)
        if report["copied"]:
            state = await self.state(user_id, lock=True)
            state.planning_revision += 1
        return report
