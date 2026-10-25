"""Authenticated monitor configuration, history and impact-only inbox."""
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, func
from ..v2.api.dependencies import CurrentUser, DBSession
from . import models as m
from . import monitor_models as mm
from .monitor_contracts import MonitorConfig, Request
from .monitor_service import MonitorService
from .service import WorkflowError, row_out

router = APIRouter()


async def write(session,operation):
    try:
        result = await operation
        await session.commit()
        return result
    except WorkflowError as exc:
        await session.rollback()
        raise HTTPException(exc.status,str(exc)) from exc
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(422,str(exc)) from exc


async def owned(session,user_id,model,identity):
    try:
        return await MonitorService(session,user_id).owned(model,identity)
    except WorkflowError as exc:
        raise HTTPException(exc.status,str(exc)) from exc


@router.get("/applications/{application_id}/monitor")
async def monitor(application_id:str,user:CurrentUser,session:DBSession):
    try:
        return await MonitorService(session,user.id).view(application_id)
    except WorkflowError as exc:
        raise HTTPException(exc.status,str(exc)) from exc


@router.put("/applications/{application_id}/monitor")
async def configure(application_id:str,body:MonitorConfig,user:CurrentUser,session:DBSession):
    return await write(session,MonitorService(session,user.id).configure(application_id,body))


@router.post("/applications/{application_id}/monitor/checks",status_code=202)
async def check(application_id:str,body:Request,user:CurrentUser,session:DBSession):
    return await write(session,MonitorService(session,user.id).enqueue_check(application_id,body.request_id))


@router.get("/monitor-checks/{identity}")
async def check_status(identity:str,user:CurrentUser,session:DBSession):
    check = await owned(session,user.id,mm.MonitorCheck,identity)
    analyses = (await session.scalars(select(mm.MonitorAnalysis).where(mm.MonitorAnalysis.check_id==check.id,
        mm.MonitorAnalysis.user_id==user.id))).all()
    return {**row_out(check),"analyses":[row_out(a) for a in analyses]}


@router.get("/applications/{application_id}/monitor/history")
async def history(application_id:str,user:CurrentUser,session:DBSession,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=100)):
    service = MonitorService(session,user.id)
    try:
        monitor = await service.get(application_id)
    except WorkflowError as exc:
        raise HTTPException(exc.status,str(exc)) from exc
    if not monitor:
        return {"items":[],"total":0}
    condition = (mm.MonitorCheck.monitor_id==monitor.id,mm.MonitorCheck.user_id==user.id)
    checks = (await session.scalars(select(mm.MonitorCheck).where(*condition)
        .order_by(mm.MonitorCheck.created_at.desc(),mm.MonitorCheck.id.desc()).offset(offset).limit(limit))).all()
    items = []
    for check in checks:
        analyses = (await session.scalars(select(mm.MonitorAnalysis).where(mm.MonitorAnalysis.check_id==check.id,
            mm.MonitorAnalysis.user_id==user.id))).all()
        items.append({**row_out(check),"analyses":[row_out(a) for a in analyses]})
    total = await session.scalar(select(func.count()).select_from(mm.MonitorCheck).where(*condition))
    return {"items":items,"total":total}


@router.get("/monitor-analyses/{identity}")
async def analysis(identity:str,user:CurrentUser,session:DBSession):
    row = await owned(session,user.id,mm.MonitorAnalysis,identity)
    changes = (await session.scalars(select(mm.MonitorChange).where(mm.MonitorChange.check_id==row.check_id,
        mm.MonitorChange.user_id==user.id))).all()
    # Raw HTML is never returned or rendered; immutable cited text is enough.
    evidence = []
    for identity in row.snapshot_ids:
        snapshot = await owned(session,user.id,mm.MonitorSnapshot,identity)
        evidence.append({k:v for k,v in row_out(snapshot).items() if k!="html"})
    return {**row_out(row),"changes":[row_out(c) for c in changes],"evidence":evidence}


@router.post("/monitor-analyses/{identity}/retry",status_code=202)
async def retry(identity:str,body:Request,user:CurrentUser,session:DBSession):
    return await write(session,MonitorService(session,user.id).retry(identity,body.request_id))


@router.get("/inbox/unread-count")
async def unread(user:CurrentUser,session:DBSession):
    count = await session.scalar(select(func.count()).select_from(m.Notification).where(m.Notification.user_id==user.id,
        m.Notification.type=="official_impact",m.Notification.read_at.is_(None)))
    return {"count":count}


@router.get("/inbox")
async def inbox(user:CurrentUser,session:DBSession,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=100)):
    conditions = (m.Notification.user_id==user.id,m.Notification.type=="official_impact")
    rows = (await session.scalars(select(m.Notification).where(*conditions)
        .order_by(m.Notification.created_at.desc(),m.Notification.id.desc()).offset(offset).limit(limit))).all()
    items = []
    for row in rows:
        result = await session.get(mm.MonitorAnalysis,row.analysis_id)
        items.append({**row_out(row),"result":result.result if result and result.user_id==user.id else {}})
    return {"items":items,"total":await session.scalar(select(func.count()).select_from(m.Notification).where(*conditions))}


@router.get("/inbox/{identity}")
async def message(identity:str,user:CurrentUser,session:DBSession):
    row = await owned(session,user.id,m.Notification,identity)
    if row.type!="official_impact":
        raise HTTPException(404,"message not found")
    return {**row_out(row),"analysis":await analysis(row.analysis_id,user,session)}


@router.post("/inbox/{identity}/read")
async def mark_read(identity:str,user:CurrentUser,session:DBSession):
    from .monitor_service import now
    row = await owned(session,user.id,m.Notification,identity)
    if row.type!="official_impact":
        raise HTTPException(404,"message not found")
    if row.read_at is None:
        row.read_at = now()
    await session.commit()
    return {"id":row.id,"read_at":row.read_at}
