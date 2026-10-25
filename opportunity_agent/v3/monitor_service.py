"""Short transactional monitoring operations; callers own commit boundaries."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import hashlib
import json
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo
from sqlalchemy import select
from ..v2.db.models import Application
from . import models as m
from . import monitor_models as mm
from .monitor_web import safe_url
from .service import WorkflowService, WorkflowError, row_out


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def next_daily(monitor, clock=None):
    local = (clock or now()).astimezone(ZoneInfo(monitor.timezone))
    hour,minute = map(int,monitor.daily_time.split(":"))
    candidate = local.replace(hour=hour,minute=minute,second=0,microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


async def application_scope(session,user_id,application_id):
    app = await WorkflowService(session).owned(Application,application_id,user_id)
    offering = await session.get(m.Offering,app.offering_id) if app.offering_id else None
    return fingerprint({"application":app.id,"offering":app.offering_id,"cycle":offering.cycle if offering else None,
                        "university":app.university,"program":app.program})


async def personal_context(session, user_id, application_id):
    service = WorkflowService(session)
    snap = await service.snapshot(user_id)
    app = next((r for r in snap["applications"] if r["id"]==application_id),None)
    if not app:
        raise WorkflowError("申请不存在",404)
    offering = next((r for r in snap["offerings"] if r["id"]==app["offering_id"]),None)
    program = next((r for r in snap["programs"] if offering and r["id"]==offering["program_id"]),None)
    requirements = [r for r in snap["requirements"] if r["application_id"]==application_id and not r.get("archived")]
    ids = {r["id"] for r in requirements}
    return {"application":app,"offering":offering,"program":program,
        "profile":snap["profile"],"profile_version":snap["profile_version"],"preferences":snap["preferences"],
        "requirements":requirements,"materials":[r for r in snap["materials"] if r["requirement_id"] in ids and not r.get("archived")],
        "tasks":[r for r in snap["tasks"] if r.get("application_id")==application_id]}


class MonitorService:
    def __init__(self,session,user_id):
        self.session,self.user_id = session,user_id
        self.workflow = WorkflowService(session)

    async def owned(self, model, identity):
        return await self.workflow.owned(model,identity,self.user_id)

    async def get(self,app_id):
        await self.owned(Application,app_id)
        return await self.session.scalar(select(mm.Monitor).where(mm.Monitor.user_id==self.user_id,mm.Monitor.application_id==app_id))

    async def view(self,app_id):
        from ..llm_client import LLMClient
        analysis_available = LLMClient().enabled
        monitor = await self.get(app_id)
        if not monitor:
            return {"application_id":app_id,"version":0,"enabled":False,"status":"not_configured","sources":[],"analysis_available":analysis_available}
        sources = (await self.session.scalars(select(mm.MonitorSource).where(mm.MonitorSource.monitor_id==monitor.id,
            mm.MonitorSource.user_id==self.user_id,mm.MonitorSource.active.is_(True)))).all()
        return {**row_out(monitor),"sources":[row_out(s) for s in sources],"analysis_available":analysis_available}

    async def configure(self,app_id,body):
        await self.workflow.state(self.user_id,lock=True)
        monitor = await self.get(app_id)
        digest = fingerprint({"application_id":app_id,**body.model_dump()})
        receipt = await self.session.scalar(select(m.CommandReceipt).where(m.CommandReceipt.user_id==self.user_id,
            m.CommandReceipt.operation=="monitor.configure",m.CommandReceipt.request_id==body.request_id))
        if receipt:
            if receipt.payload_hash != digest:
                raise WorkflowError("request_id 内容不同",409)
            return receipt.response
        if (monitor.version if monitor else 0) != body.expected_version:
            raise WorkflowError("监控配置已变化，请刷新",409)
        if not monitor:
            monitor = mm.Monitor(user_id=self.user_id,application_id=app_id,version=1)
            self.session.add(monitor)
            await self.session.flush()
        else:
            monitor.version += 1
        for field in ("enabled","mode","timezone","daily_time"):
            setattr(monitor,field,getattr(body,field))
        monitor.next_due = next_daily(monitor) if monitor.enabled and monitor.mode=="daily" else None
        monitor.status = "ready" if monitor.enabled else "paused"
        monitor.active_check_id = None
        urls = {}
        for role in ("project","college","faq"):
            url = getattr(body,role+"_url")
            if url:
                host = urlsplit(url).hostname.encode("idna").decode().lower().rstrip(".")
                url = safe_url(url,{host})
                urls.setdefault(url,[]).append(role)
        old = (await self.session.scalars(select(mm.MonitorSource).where(mm.MonitorSource.monitor_id==monitor.id))).all()
        for source in old:
            if not source.active and source.url in urls:
                source.latest_snapshot_id = source.etag = source.modified = None
            source.active = source.url in urls
            if source.active:
                source.roles = urls.pop(source.url)
        for url,roles in urls.items():
            self.session.add(mm.MonitorSource(user_id=self.user_id,monitor_id=monitor.id,roles=roles,
                                             url=url,host=urlsplit(url).hostname))
        await self.session.flush()
        response = await self.view(app_id)
        self.session.add(m.CommandReceipt(user_id=self.user_id,operation="monitor.configure",request_id=body.request_id,
            payload_hash=digest,response=response))
        return response

    async def enqueue_check(self,app_id,request_id,trigger="manual"):
        await self.workflow.state(self.user_id,lock=True)
        monitor = await self.get(app_id)
        if not monitor or not monitor.enabled:
            raise WorkflowError("请先配置并启用官网监控",409)
        old = await self.session.scalar(select(mm.MonitorCheck).where(mm.MonitorCheck.monitor_id==monitor.id,
            mm.MonitorCheck.request_id==request_id))
        if old:
            return row_out(old)
        if monitor.active_check_id:
            active = await self.session.get(mm.MonitorCheck,monitor.active_check_id)
            if active and active.monitor_version==monitor.version and active.status in {"queued","running","analyzing"}:
                return row_out(active)
        if trigger=="manual" and monitor.last_manual_at:
            seconds = int((aware(monitor.last_manual_at)+timedelta(minutes=5)-now()).total_seconds())
            if seconds>0:
                raise WorkflowError(f"请在 {seconds} 秒后再次检查",429)
        check = mm.MonitorCheck(id=uuid4().hex,user_id=self.user_id,monitor_id=monitor.id,request_id=request_id,
            trigger=trigger,monitor_version=monitor.version,
            scope_fingerprint=await application_scope(self.session,self.user_id,app_id))
        self.session.add(check)
        await self.session.flush()
        self.session.add(m.Job(user_id=self.user_id,dedupe_key="monitor-check:"+check.id,type="monitor.check",
            input_revision=monitor.version,scope={"check_id":check.id}))
        monitor.active_check_id = check.id
        monitor.status = "queued"
        if trigger=="manual":
            monitor.last_manual_at = now()
        return row_out(check)

    async def retry(self,analysis_id,request_id):
        await self.workflow.state(self.user_id,lock=True)
        analysis = await self.owned(mm.MonitorAnalysis,analysis_id)
        monitor = await self.owned(mm.Monitor,analysis.monitor_id)
        check = await self.owned(mm.MonitorCheck,analysis.check_id)
        if not monitor.enabled or monitor.version != check.monitor_version:
            raise WorkflowError("监控配置已变更，请重新检查",409)
        if analysis.status not in {"unknown","failed","queued","running"}:
            raise WorkflowError("仅可重试失败或待核实的分析",409)
        key = f"monitor-retry:{analysis.id}:{request_id}"
        old = await self.session.scalar(select(m.Job).where(m.Job.dedupe_key==key,m.Job.user_id==self.user_id))
        if old:
            return {"analysis_id":analysis.id,"job_id":old.id}
        # One analysis instance cannot have two competing attempts.
        jobs = (await self.session.scalars(select(m.Job).where(m.Job.user_id==self.user_id,
            m.Job.type=="monitor.analyze",m.Job.status.in_(["queued","running"])))).all()
        for job in jobs:
            if job.scope.get("analysis_id")==analysis.id:
                return {"analysis_id":analysis.id,"job_id":job.id}
        job = m.Job(user_id=self.user_id,dedupe_key=key,type="monitor.analyze",input_revision=monitor.version,
                    scope={"check_id":check.id,"analysis_id":analysis.id})
        self.session.add(job)
        analysis.status,analysis.error = "queued",None
        check.status,monitor.active_check_id = "analyzing",check.id
        await self.session.flush()
        return {"analysis_id":analysis.id,"job_id":job.id}
