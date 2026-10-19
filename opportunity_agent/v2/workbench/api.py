from datetime import datetime
import os
from fastapi import APIRouter, HTTPException
from sqlalchemy import select, update
from ..api.dependencies import CurrentUser, DBSession
from ..db.models import Profile, ApplicationTask, ProfileChange
from ..repositories import Repository
from .models import Project, Source, ImportJob, Tracking, TaskDetail, CalendarEvent, Reminder, Notification
from .schemas import (CreateProject, ImportBatch, EditProject, ConfirmProject, Command, Progress,
                      EventCommand, Background, Query, ChecklistEdit, SourceEdit)
from . import service as s

router = APIRouter(prefix='/api/v1', tags=['申请工作台'])


async def mutate(session, user_id, op, body, action):
    return await s.command(session, user_id, op, body.model_dump(mode='json'), action)


@router.get('/project-library')
async def projects(user: CurrentUser, session: DBSession):
    return [s.dump(p) for p in await session.scalars(select(Project).where(Project.user_id == user.id).order_by(Project.created_at.desc()))]


@router.post('/project-library')
async def create(body: CreateProject, user: CurrentUser, session: DBSession):
    return await mutate(session, user.id, 'create_project', body, lambda:s.create_project(session,user.id,body))


@router.get('/project-library/{project_id}')
async def project(project_id: str, user: CurrentUser, session: DBSession):
    p = await s.owned(session,Project,user.id,project_id)
    sources = await session.scalars(select(Source).where(Source.project_id == p.id).order_by(Source.version.desc()))
    return {**s.dump(p), 'sources': [s.dump(row) for row in sources]}


@router.patch('/project-library/{project_id}')
async def edit(project_id: str, body: EditProject, user: CurrentUser, session: DBSession):
    async def action():
        p = await s.owned(session,Project,user.id,project_id,lock=True)
        s.version(p,body.expected_version)
        old = p.draft
        new = body.data.model_dump(mode='json')
        provenance = dict(new.get('provenance',{}))
        for key in list(provenance):
            if old.get(key) != new.get(key):
                provenance.pop(key,None)
        new['provenance'] = provenance
        current = list(await session.scalars(select(Source).where(Source.project_id == p.id, Source.version == p.version)))
        p.draft, p.version = new,p.version+1
        for source in current:
            session.add(Source(project_id=p.id,version=p.version,kind=source.kind,url=source.url,title=source.title,
                content=source.content, content_hash=source.content_hash,status=source.status,method=source.method,error_code=source.error_code))
        await session.flush()
        return s.dump(p)
    return await mutate(session,user.id,'edit:'+project_id,body,action)


@router.post('/project-library/{project_id}/confirm')
async def confirm(project_id: str, body: ConfirmProject, user: CurrentUser, session: DBSession):
    return await mutate(session,user.id,'confirm:'+project_id,body,lambda:s.confirm_project(session,user.id,project_id,body))


@router.put('/project-library/{project_id}/sources/{source_id}')
async def replace_source(project_id:str,source_id:str,body:SourceEdit,user:CurrentUser,session:DBSession):
    async def action():
        from .importer import validate_input_url
        p=await s.owned(session,Project,user.id,project_id,lock=True);s.version(p,body.expected_version)
        sources=list(await session.scalars(select(Source).where(Source.project_id==p.id,Source.version==p.version)))
        if source_id not in {x.id for x in sources}:
            raise HTTPException(404,'来源不属于当前草稿')
        if body.source.url:validate_input_url(body.source.url)
        p.version+=1
        p.draft={**p.draft,'provenance':{},'warnings':[*p.draft.get('warnings',[]),'来源已替换，请重新核对保留的字段值或重新整理。']}
        for source in sources:
            item=body.source if source.id==source_id else None
            session.add(Source(project_id=p.id,version=p.version,kind=item.kind if item else source.kind,
                url=item.url if item else source.url,content=item.text if item else source.content,
                title=source.title,method='pasted' if item else source.method,
                status=('read' if item.text else 'pending') if item else source.status,
                content_hash=__import__('hashlib').sha256(item.text.encode()).hexdigest() if item else source.content_hash,
                error_code='' if item else source.error_code))
        await session.flush()
        return s.dump(p)
    return await mutate(session,user.id,'source:'+source_id,body,action)


@router.post('/project-library/{project_id}/archive')
async def archive(project_id: str, body: Command, user: CurrentUser, session: DBSession):
    async def action():
        p=await s.owned(session,Project,user.id,project_id,lock=True);s.version(p,body.expected_version)
        p.archived=True;p.version+=1
        return s.dump(p)
    return await mutate(session,user.id,'archive:'+project_id,body,action)


@router.post('/project-library/{project_id}/reindex')
async def reindex(project_id: str, body: Command, user: CurrentUser, session: DBSession):
    async def action():
        p=await s.owned(session,Project,user.id,project_id,lock=True);s.version(p,body.expected_version)
        p.index_status='pending'
        return {'id':p.id,'index_status':p.index_status}
    return await mutate(session,user.id,'index:'+project_id,body,action)


@router.post('/project-library/{project_id}/select')
async def select_project(project_id: str, body: Command, user: CurrentUser, session: DBSession):
    return await mutate(session,user.id,'select:'+project_id,body,lambda:s.select_project(session,user.id,project_id,body.expected_version))


@router.post('/project-library/{project_id}/reextract')
async def reextract(project_id: str, body: Command, user: CurrentUser, session: DBSession):
    async def action():
        p=await s.owned(session,Project,user.id,project_id,lock=True);s.version(p,body.expected_version)
        job=ImportJob(user_id=user.id,project_id=p.id,version=p.version)
        session.add(job);await session.flush()
        return s.dump(job)
    return await mutate(session,user.id,'extract:'+project_id,body,action)


@router.post('/project-imports')
async def imports(body: ImportBatch, user: CurrentUser, session: DBSession):
    async def action():
        rows=[]
        for item in body.projects:
            rows.append(await s.create_project(session,user.id,item,enqueue=True))
        return {'projects':rows}
    return await mutate(session,user.id,'import',body,action)


@router.get('/project-imports')
async def jobs(user: CurrentUser, session: DBSession):
    return [s.dump(j) for j in await session.scalars(select(ImportJob).where(ImportJob.user_id == user.id).order_by(ImportJob.created_at.desc()).limit(100))]


@router.get('/project-imports/{job_id}')
async def job(job_id:str,user:CurrentUser,session:DBSession):
    return s.dump(await s.owned(session,ImportJob,user.id,job_id))


@router.get('/workspace/background')
async def background(user:CurrentUser,session:DBSession):
    p=await Repository(session).ensure_profile(user.id)
    await session.commit()
    return {'version':p.version,'fields':p.payload,'experiences':p.payload.get('experiences',[])}


@router.put('/workspace/background')
async def save_background(body:Background,user:CurrentUser,session:DBSession):
    async def action():
        from ...models import StudentProfile
        p=await Repository(session).ensure_profile(user.id);s.version(p,body.expected_version)
        forbidden={'user_id','facts','change_history','experiences'}
        fields={k:v for k,v in body.fields.items() if k in StudentProfile.model_fields and k not in forbidden}
        try:
            StudentProfile.model_validate({'user_id':user.id,**fields})
        except ValueError:
            raise HTTPException(422,'背景字段类型、成绩分制或日期不合法') from None
        if len({e.id for e in body.experiences}) != len(body.experiences):
            raise HTTPException(422,'经历ID重复')
        before=p.payload
        payload={**before,**fields,'experiences':[e.model_dump() for e in body.experiences]}
        p=await Repository(session).patch_profile(user.id,payload,body.expected_version)
        session.add(ProfileChange(user_id=user.id,field='workspace_background',before=before,
            after=payload,reason='用户在背景表单明确保存'))
        return {'version':p.version,'fields':p.payload,'experiences':body.experiences}
    return await mutate(session,user.id,'background',body,action)


@router.post('/recommendations')
async def recommendations(body:Query,user:CurrentUser,session:DBSession):
    from ..core.config import settings
    if settings.domain_agent_transport=='a2a':
        import uuid
        from ..agents.a2a import OpenJiuwenDomainAgents
        from ..agents.contracts import ExecutionState
        client=OpenJiuwenDomainAgents()
        try:
            state=ExecutionState(user_id=user.id,run_id=uuid.uuid4().hex,conversation_id=uuid.uuid4().hex,
                request_id=uuid.uuid4().hex,message=body.query or '根据我的全部经历推荐项目',
                workspace_mode=True,workspace_query=body.model_dump())
            result=await client.execute('research',state)
            if result.diagnostics.get('workspace'):
                return result.diagnostics['recommendation']
            raise RuntimeError('Research returned a legacy result')
        except Exception as exc:
            report=await s.recommend(session,user.id,body)
            report['transport_error']=type(exc).__name__
            report['warning']+=' Research服务不可用，已降级到本地检索。'
            return report
        finally:
            await client.aclose()
    return await s.recommend(session,user.id,body)


@router.post('/project-library/query')
async def query_library(body:Query,user:CurrentUser,session:DBSession):
    return await recommendations(body,user,session)


@router.get('/workspace/applications')
async def applications(user:CurrentUser,session:DBSession):
    output=[]
    for t in await session.scalars(select(Tracking).where(Tracking.user_id == user.id)):
        p=await s.owned(session,Project,user.id,t.project_id)
        tasks=list(await session.scalars(select(ApplicationTask).where(ApplicationTask.application_id == t.application_id)))
        details={d.task_id:d for d in await session.scalars(select(TaskDetail).where(TaskDetail.task_id.in_([x.id for x in tasks])))}
        output.append({**s.dump(t),'update_available':p.published_version != t.project_version,
            'tasks':[{**s.dump(x),'details':details[x.id].payload,'version':details[x.id].version} for x in tasks if x.id in details]})
    return output


@router.get('/applications/{application_id}/checklist')
async def get_checklist(application_id:str,user:CurrentUser,session:DBSession):
    return s.dump(await s.tracking_for(session,user.id,application_id))


@router.post('/applications/{application_id}/checklist/generate')
async def generate_checklist(application_id:str,body:Command,user:CurrentUser,session:DBSession):
    async def action():
        from ..agents.planning_agent import build_workspace_checklist
        t=await s.tracking_for(session,user.id,application_id,lock=True);s.version(t,body.expected_version)
        t.checklist_draft=build_workspace_checklist(t.snapshot);t.version+=1
        return s.dump(t)
    return await mutate(session,user.id,'generate:'+application_id,body,action)


@router.post('/applications/{application_id}/checklist/confirm')
async def confirm_checklist(application_id:str,body:Command,user:CurrentUser,session:DBSession):
    async def action():
        t=await s.tracking_for(session,user.id,application_id,lock=True)
        return await s.confirm_checklist(session,t,body.expected_version)
    return await mutate(session,user.id,'checklist:'+application_id,body,action)


@router.put('/applications/{application_id}/checklist')
async def edit_checklist(application_id:str,body:ChecklistEdit,user:CurrentUser,session:DBSession):
    async def action():
        t=await s.tracking_for(session,user.id,application_id,lock=True);s.version(t,body.expected_version)
        if len({x.stable_key for x in body.items})!=len(body.items):
            raise HTTPException(422,'任务stable_key重复')
        t.checklist_draft=[x.model_dump(mode='json') for x in body.items];t.version+=1
        return s.dump(t)
    return await mutate(session,user.id,'edit-checklist:'+application_id,body,action)


@router.post('/applications/{application_id}/sync-project')
async def sync_project(application_id:str,body:Command,user:CurrentUser,session:DBSession):
    async def action():
        t=await s.tracking_for(session,user.id,application_id,lock=True);s.version(t,body.expected_version)
        p=await s.owned(session,Project,user.id,t.project_id)
        t.snapshot={**p.published,'confirmations':p.confirmations};t.project_version=p.published_version
        t.checklist_draft=[]
        t.version+=1
        # Existing tasks stay untouched; review a new checklist explicitly.
        return s.dump(t)
    return await mutate(session,user.id,'sync:'+application_id,body,action)


@router.post('/tasks/{task_id}/progress')
async def progress(task_id:str,body:Progress,user:CurrentUser,session:DBSession):
    return await mutate(session,user.id,'progress:'+task_id,body,lambda:s.set_progress(session,user.id,task_id,body))


@router.get('/calendar-events')
async def events(user:CurrentUser,session:DBSession):
    return {'events':[s.dump(e) for e in await session.scalars(select(CalendarEvent).where(CalendarEvent.user_id == user.id,
        CalendarEvent.cancelled.is_(False)))], 'email_available':bool(os.getenv('SMTP_HOST') and os.getenv('SMTP_FROM')),
        'reminders':[s.dump(r) for r in await session.scalars(select(Reminder).where(Reminder.user_id == user.id))]}


@router.post('/calendar-events')
async def create_event(body:EventCommand,user:CurrentUser,session:DBSession):
    return await mutate(session,user.id,'event',body,lambda:s.save_event(session,user.id,body))


@router.patch('/calendar-events/{event_id}')
async def edit_event(event_id:str,body:EventCommand,user:CurrentUser,session:DBSession):
    return await mutate(session,user.id,'event:'+event_id,body,lambda:s.save_event(session,user.id,body,event_id))


@router.post('/calendar-events/{event_id}/cancel')
async def cancel_event(event_id:str,body:Command,user:CurrentUser,session:DBSession):
    async def action():
        e=await s.owned(session,CalendarEvent,user.id,event_id,lock=True);s.version(e,body.expected_version)
        e.cancelled=True;e.version+=1
        await session.execute(update(Reminder).where(Reminder.event_id == e.id,Reminder.status.in_(['pending','sending'])).values(status='cancelled'))
        return s.dump(e)
    return await mutate(session,user.id,'cancel:'+event_id,body,action)


@router.get('/notifications')
async def notifications(user:CurrentUser,session:DBSession):
    return [s.dump(n) for n in await session.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.created_at.desc()).limit(100))]


@router.post('/notifications/{notification_id}/read')
async def read_notification(notification_id:str,body:Command,user:CurrentUser,session:DBSession):
    async def action():
        n=await s.owned(session,Notification,user.id,notification_id);n.is_read=True
        return s.dump(n)
    return await mutate(session,user.id,'read:'+notification_id,body,action)
