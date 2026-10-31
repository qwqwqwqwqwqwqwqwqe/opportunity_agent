"""Authenticated V3 endpoints mounted into the existing application."""
from __future__ import annotations

from datetime import date
from fastapi import APIRouter, HTTPException, Query, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from ..v2.api.dependencies import CurrentUser, DBSession
from ..v2.db.models import Application, ApplicationPlan, ApprovalRequest
from ..v2.services.applications import ApplicationCommandService
from . import models as m
from .contracts import Command, CommandBatch, Decision, DraftRequest
from .engine import offering_deadline, review
from .service import ENTITIES, WorkflowError, WorkflowService, row_out
from .workspace import ProjectForm, project_cards, project_form

def workflow_mode():
    from ..v2.core.config import settings
    if settings.app_mode != "applyops":
        raise HTTPException(409, "start the API with APP_MODE=applyops to enable V3 business operations")


router = APIRouter(prefix="/api/v3", tags=["ApplyOps V3"], dependencies=[Depends(workflow_mode)])


async def submit(session, user_id, batch):
    try:
        result = await WorkflowService(session).execute(user_id, batch)
        await session.commit()
        return result
    except WorkflowError as exc:
        await session.rollback()
        raise HTTPException(exc.status, str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "duplicate identity or conflicting update") from exc
    except (ValueError, KeyError) as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from exc


@router.post("/commands")
async def commands(body: CommandBatch, user: CurrentUser, session: DBSession):
    return await submit(session, user.id, body)


@router.post("/proposals", status_code=202)
async def propose(body: CommandBatch, user: CurrentUser, session: DBSession):
    # No command executes here, including validations that would create state.
    approval = await ApplicationCommandService(session).propose(user.id, "workflow.commands", body.model_dump(mode="json"),
                                                                 body.request_id, reason="请确认这些业务变更")
    await session.commit()
    return {"approval_id": approval.id, "status": "WAITING_APPROVAL"}


def register_entity_routes():
    # Concrete resource routes share the same command path. Path identity/type
    # are server-controlled; clients cannot smuggle a different operation.
    for kind, plural in [("program", "programs"), ("offering", "offerings"), ("application", "applications"),
                         ("task", "tasks"), ("calendar_event", "calendar-events")]:
        model = ENTITIES[kind][0]

        def make_list(model):
            async def endpoint(user: CurrentUser, session: DBSession):
                return [row_out(r) for r in (await session.scalars(select(model).where(model.user_id == user.id)
                    .order_by(model.created_at, model.id))).all()]
            return endpoint

        def make_write(kind, create):
            if create:
                async def endpoint(body: CommandBatch, user: CurrentUser, session: DBSession):
                    if len(body.commands) != 1 or body.commands[0].type != f"{kind}.create":
                        raise HTTPException(422, f"expected one {kind}.create command")
                    return await submit(session, user.id, body)
            else:
                async def endpoint(identity: str, body: CommandBatch, user: CurrentUser, session: DBSession):
                    if len(body.commands) != 1 or body.commands[0].type != f"{kind}.update" or body.commands[0].target_id != identity:
                        raise HTTPException(422, f"expected one {kind}.update command for this resource")
                    return await submit(session, user.id, body)
            return endpoint

        def make_get(model):
            async def endpoint(identity: str, user: CurrentUser, session: DBSession):
                return row_out(await owned_or_404(session, model, identity, user.id))
            return endpoint

        router.add_api_route(f"/{plural}", make_list(model), methods=["GET"], name=f"list_{kind}")
        router.add_api_route(f"/{plural}", make_write(kind, True), methods=["POST"], status_code=201, name=f"create_{kind}")
        router.add_api_route(f"/{plural}/{{identity}}", make_get(model), methods=["GET"], name=f"get_{kind}")
        router.add_api_route(f"/{plural}/{{identity}}", make_write(kind, False), methods=["PATCH"], name=f"update_{kind}")


register_entity_routes()


@router.get("/applications/{application_id}/requirements")
async def requirements(application_id: str, user: CurrentUser, session: DBSession):
    await owned_or_404(session, Application, application_id, user.id)
    return [row_out(r) for r in (await session.scalars(select(m.Requirement).where(
        m.Requirement.user_id == user.id, m.Requirement.application_id == application_id))).all()]


@router.get("/applications/{application_id}/materials")
async def materials(application_id: str, user: CurrentUser, session: DBSession):
    await owned_or_404(session, Application, application_id, user.id)
    return [row_out(r) for r in (await session.scalars(select(m.Material).join(m.Requirement,
        m.Material.requirement_id == m.Requirement.id).where(m.Material.user_id == user.id,
        m.Requirement.user_id == user.id, m.Requirement.application_id == application_id))).all()]


@router.post("/applications/{application_id}/requirements", status_code=201)
async def create_requirement(application_id: str, body: CommandBatch, user: CurrentUser, session: DBSession):
    await owned_or_404(session, Application, application_id, user.id)
    if len(body.commands) != 1 or body.commands[0].type != "requirement.create" or body.commands[0].payload.get("application_id") != application_id:
        raise HTTPException(422, "expected requirement.create for this application")
    return await submit(session, user.id, body)


@router.post("/applications/{application_id}/materials", status_code=201)
async def create_material(application_id: str, body: CommandBatch, user: CurrentUser, session: DBSession):
    await owned_or_404(session, Application, application_id, user.id)
    if len(body.commands) != 1 or body.commands[0].type != "material.create":
        raise HTTPException(422, "expected one material.create")
    requirement = await owned_or_404(session, m.Requirement, body.commands[0].payload.get("requirement_id"), user.id)
    if requirement.application_id != application_id:
        raise HTTPException(404, "resource not found")
    return await submit(session, user.id, body)


@router.post("/tasks/{task_id}/commands")
async def task_command(task_id: str, body: CommandBatch, user: CurrentUser, session: DBSession):
    if len(body.commands) != 1 or body.commands[0].type != "task.update" or body.commands[0].target_id != task_id:
        raise HTTPException(422, "expected task.update for this task")
    return await submit(session, user.id, body)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, user: CurrentUser, session: DBSession,
                      expected_version: int = Query(ge=1), request_id: str = Query(min_length=1, max_length=140)):
    return await submit(session, user.id, CommandBatch(request_id=request_id, commands=[Command(
        type="task.delete", target_id=task_id, expected_version=expected_version)]))


@router.get("/snapshot")
async def snapshot(user: CurrentUser, session: DBSession):
    return await WorkflowService(session).snapshot(user.id)


@router.get("/dashboard")
async def dashboard(user: CurrentUser, session: DBSession):
    snap = await WorkflowService(session).snapshot(user.id)
    pending = len((await session.scalars(select(ApprovalRequest.id).where(ApprovalRequest.user_id == user.id,
                                                   ApprovalRequest.status == "pending"))).all())
    return {"revision": snap["revision"], "applications": len(snap["applications"]),
            "open_tasks": sum(t["status"] not in {"completed", "cancelled"} for t in snap["tasks"]),
            "pending_approvals": pending, "review": review(snap), "projects": project_cards(snap)}


@router.post("/projects", status_code=201)
async def create_project(body: ProjectForm, user: CurrentUser, session: DBSession):
    return await submit(session, user.id, CommandBatch(request_id=body.request_id, commands=[Command(
        type="project.save", payload=body.model_dump(mode="json"))]))


@router.get("/projects/{application_id}")
async def get_project_form(application_id: str, user: CurrentUser, session: DBSession):
    await owned_or_404(session, Application, application_id, user.id)
    return await project_form(WorkflowService(session), user.id, application_id)


@router.put("/projects/{application_id}")
async def update_project(body: ProjectForm, application_id: str, user: CurrentUser, session: DBSession):
    return await submit(session, user.id, CommandBatch(request_id=body.request_id, commands=[Command(
        type="project.save", target_id=application_id, payload=body.model_dump(mode="json"))]))


@router.delete("/projects/{application_id}")
async def delete_project(application_id: str, user: CurrentUser, session: DBSession,
                         expected_version: int = Query(ge=1), expected_revision: int = Query(ge=0),
                         request_id: str = Query(min_length=1, max_length=140)):
    return await submit(session, user.id, CommandBatch(request_id=request_id, commands=[Command(
        type="project.delete", target_id=application_id, expected_version=expected_version,
        payload={"expected_revision": expected_revision})]))


@router.get("/review")
async def review_state(user: CurrentUser, session: DBSession, application_id: str | None = None):
    if application_id:
        await owned_or_404(session, Application, application_id, user.id)
    return review(await WorkflowService(session).snapshot(user.id), application_id)


@router.get("/calendar")
async def calendar(user: CurrentUser, session: DBSession,
                   start: date = Query(alias="from"), end: date = Query(alias="to")):
    if end < start or (end - start).days > 730:
        raise HTTPException(422, "calendar range must be ordered and at most 730 days")
    snap = await WorkflowService(session).snapshot(user.id)
    result = []
    for task in snap["tasks"]:
        if task["status"] in {"completed", "cancelled"} or not task.get("show_in_calendar"):
            continue
        a, b = task["start_date"] or task["due_date"], task["due_date"] or task["start_date"]
        if a and b >= str(start) and a <= str(end):
            result.append({"type": "task", "title": task["title"], "date": task["due_date"] or task["start_date"], **task})
    for event in snap["calendar_events"]:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        a = event["date"]
        if not a and event["start_at"]:
            a = datetime.fromisoformat(event["start_at"]).astimezone(ZoneInfo(event["timezone"])).date().isoformat()
        if a and str(start) <= a <= str(end):
            result.append({**event, "type": "event", "event_type": event["type"], "date": a})
    for application in snap["applications"]:
        offering = next((o for o in snap["offerings"] if o["id"] == application["offering_id"]), None)
        d = offering_deadline(offering or {})
        if d and start <= d <= end:
            result.append({"type": "deadline", "id": application["id"], "title": f"{application['university']} {application['program']} 截止",
                           "date": str(d), "date_precision": offering["date_precision"], "source": "user_provided"})
    return sorted(result, key=lambda r: (r.get("date") or r.get("start_date") or "", r["id"]))


@router.post("/plans/drafts", status_code=201)
async def draft(body: DraftRequest, user: CurrentUser, session: DBSession):
    return await submit(session, user.id, CommandBatch(request_id=body.request_id, commands=[Command(type="plan.draft",
        payload=body.model_dump(mode="json", exclude={"request_id"}, exclude_none=True))]))


@router.get("/plans")
async def plans(user: CurrentUser, session: DBSession):
    return [row_out(r) for r in (await session.scalars(select(ApplicationPlan).where(ApplicationPlan.user_id == user.id,
                  ApplicationPlan.schema_version == 3).order_by(ApplicationPlan.version.desc()))).all()]


@router.get("/plans/current")
async def current_plan(user: CurrentUser, session: DBSession):
    row = await session.scalar(select(ApplicationPlan).where(ApplicationPlan.user_id == user.id,
        ApplicationPlan.status == "active", ApplicationPlan.schema_version == 3))
    if row is None:
        raise HTTPException(404, "no active V3 plan")
    return await WorkflowService(session).plan(user.id, row.id)


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: str, user: CurrentUser, session: DBSession):
    await owned_or_404(session, ApplicationPlan, plan_id, user.id)
    return await WorkflowService(session).plan(user.id, plan_id)


@router.get("/plans/{plan_id}/diff")
async def plan_diff(plan_id: str, user: CurrentUser, session: DBSession):
    await owned_or_404(session, ApplicationPlan, plan_id, user.id)
    return await WorkflowService(session).diff(user.id, plan_id)


@router.post("/plans/{plan_id}/accept")
async def accept_plan(plan_id: str, body: Decision, user: CurrentUser, session: DBSession):
    return await submit(session, user.id, CommandBatch(request_id=body.request_id, commands=[Command(
        type="plan.activate", target_id=plan_id, expected_version=body.expected_version)]))


@router.post("/plans/{plan_id}/reject")
async def reject_plan(plan_id: str, body: Decision, user: CurrentUser, session: DBSession):
    return await submit(session, user.id, CommandBatch(request_id=body.request_id, commands=[Command(
        type="plan.reject", target_id=plan_id, expected_version=body.expected_version)]))


@router.get("/jobs/{job_id}")
async def job(job_id: str, user: CurrentUser, session: DBSession):
    return row_out(await owned_or_404(session, m.Job, job_id, user.id))


@router.get("/jobs")
async def jobs(user: CurrentUser, session: DBSession):
    return [row_out(r) for r in (await session.scalars(select(m.Job).where(m.Job.user_id == user.id)
                .order_by(m.Job.created_at.desc()).limit(50))).all()]


@router.get("/notifications")
async def notifications(user: CurrentUser, session: DBSession):
    return [row_out(r) for r in (await session.scalars(select(m.Notification).where(m.Notification.user_id == user.id, m.Notification.type != "official_impact")
                .order_by(m.Notification.created_at.desc()).limit(100))).all()]


async def owned_or_404(session, model, identity, user_id):
    try:
        return await WorkflowService(session).owned(model, identity, user_id)
    except WorkflowError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


from .monitor_api import router as monitor_router  # noqa: E402
router.include_router(monitor_router)
