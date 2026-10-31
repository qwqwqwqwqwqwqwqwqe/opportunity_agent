"""Durable monitor handlers. No network/model calls inside DB transactions."""
from __future__ import annotations
import asyncio
import logging
import socket
from contextlib import suppress
from dataclasses import asdict
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import or_, select, update
from ..llm_client import LLMClient
from . import models as m
from . import monitor_models as mm
from .impact_agent import ImpactTools, PersonalImpactAgent
from .monitor_service import MonitorService, application_scope, aware, fingerprint, next_daily, now, personal_context
from .monitor_web import HttpFetcher, PageError, page_diff
from .service import WorkflowService, row_out


async def schedule_due(factory):
    async with factory() as session:
        ids = list((await session.scalars(select(mm.Monitor.id).where(mm.Monitor.enabled.is_(True),
            mm.Monitor.mode=="daily",mm.Monitor.next_due<=now()).limit(50))).all())
    for identity in ids:
        async with factory.begin() as session:
            monitor = await session.get(mm.Monitor,identity)
            service = MonitorService(session,monitor.user_id)
            await service.workflow.state(monitor.user_id,lock=True)
            await session.refresh(monitor)
            if not monitor.enabled or not monitor.next_due or aware(monitor.next_due)>now():
                continue
            await service.enqueue_check(monitor.application_id,"daily:"+aware(monitor.next_due).isoformat(),"daily")
            monitor.next_due = next_daily(monitor)


async def _guard(session,job_id,token):
    job = await session.scalar(select(m.Job).where(m.Job.id==job_id,m.Job.lease_token==token,
        m.Job.status=="running",m.Job.lease_until>now()).with_for_update())
    if not job:
        raise PageError("STALE_LEASE")
    check = await session.get(mm.MonitorCheck,job.scope["check_id"])
    monitor = await session.get(mm.Monitor,check.monitor_id)
    if not monitor or monitor.user_id!=job.user_id or not monitor.enabled or monitor.version!=check.monitor_version:
        raise PageError("STALE_MONITOR_CONFIG")
    if check.scope_fingerprint != await application_scope(session,job.user_id,monitor.application_id):
        raise PageError("STALE_MONITOR_CONFIG")
    return job,check,monitor


async def _renew(factory,job_id,token):
    while True:
        await asyncio.sleep(30)
        async with factory.begin() as session:
            result = await session.execute(update(m.Job).where(m.Job.id==job_id,m.Job.lease_token==token,
                m.Job.status=="running",m.Job.lease_until>now()).values(lease_until=now()+timedelta(seconds=240)))
            if result.rowcount!=1:
                return


def _snapshot(user_id,monitor_id,source_id,page):
    data = asdict(page)
    data.pop("etag",None);data.pop("modified",None)
    return mm.MonitorSnapshot(id=uuid4().hex,user_id=user_id,monitor_id=monitor_id,source_id=source_id,**data)


async def _check(factory,job_id,token,fetcher):
    async with factory.begin() as session:
        job,check,monitor = await _guard(session,job_id,token)
        check.status,monitor.status = "running","running"
        user_id,monitor_id = job.user_id,monitor.id
        sources = [row_out(s) for s in (await session.scalars(select(mm.MonitorSource).where(
            mm.MonitorSource.monitor_id==monitor.id,mm.MonitorSource.active.is_(True)))).all()]
    hosts = {s["host"] for s in sources}
    results = []
    for source in sources:
        for attempt in range(3):
            try:
                page = await fetcher.fetch(source["url"],hosts,source["etag"],source["modified"])
                results.append((source,page,None))
                break
            except Exception as exc:
                error = str(exc)[:100] if isinstance(exc,PageError) else type(exc).__name__
                temporary = error in {"ROBOTS_UNAVAILABLE","FETCH_TIMEOUT","TimeoutError","OSError","ConnectionError",
                    "HTTP_429","HTTP_500","HTTP_502","HTTP_503","HTTP_504"}
                if not temporary or attempt==2:
                    results.append((source,None,error))
                    break
                await asyncio.sleep(.2*(attempt+1))
    async with factory.begin() as session:
        await WorkflowService(session).state(user_id,lock=True)
        job,check,monitor = await _guard(session,job_id,token)
        coverage,changes,baseline = [],[],False
        for source_data,page,error in results:
            source = await session.get(mm.MonitorSource,source_data["id"])
            source.last_checked_at = now()
            previous = await session.get(mm.MonitorSnapshot,source.latest_snapshot_id) if source.latest_snapshot_id else None
            if not error and page is None and previous is None:
                error = "304_WITHOUT_BASELINE"
            if error:
                source.last_fetch_status = "failed"
                coverage.append({"source_id":source.id,"roles":source.roles,"url":source.url,"status":"failed","error":error})
                continue
            if page is None:
                source.last_fetch_status = "not_modified"
                coverage.append({"source_id":source.id,"roles":source.roles,"url":source.url,"status":"not_modified","snapshot_id":previous.id})
                continue
            if previous and page.parser_version==previous.parser_version and len(page.blocks)<len(previous.blocks)*.2 and sum(len(b["text"]) for b in page.blocks)<150:
                source.last_fetch_status = "failed"
                coverage.append({"source_id":source.id,"roles":source.roles,"url":source.url,"status":"failed","error":"ABNORMAL_CONTENT_LOSS"})
                continue
            if previous and page.content_hash==previous.content_hash and page.parser_version==previous.parser_version:
                current = previous
            else:
                current = _snapshot(user_id,monitor_id,source.id,page)
                session.add(current)
                await session.flush()
                if previous and previous.parser_version==page.parser_version:
                    change = mm.MonitorChange(id=uuid4().hex,user_id=user_id,check_id=check.id,source_id=source.id,
                        before_id=previous.id,after_id=current.id,diff=page_diff(previous,page))
                    session.add(change);changes.append(change)
                else:
                    baseline = True
                source.latest_snapshot_id = current.id
            source.etag,source.modified = page.etag,page.modified
            source.last_fetch_status = "ok"
            coverage.append({"source_id":source.id,"roles":source.roles,"url":source.url,"status":"ok","snapshot_id":current.id})
        check.coverage = coverage
        failed = any(c["status"]=="failed" for c in coverage)
        if not failed:
            monitor.last_success_at = now()
        if changes:
            check.status,monitor.status = "analyzing","analyzing"
            analysis = mm.MonitorAnalysis(id=uuid4().hex,user_id=user_id,monitor_id=monitor_id,check_id=check.id)
            session.add(analysis)
            await session.flush()
            session.add(m.Job(user_id=user_id,dedupe_key="monitor-analysis:"+check.id,type="monitor.analyze",
                input_revision=monitor.version,scope={"check_id":check.id,"analysis_id":analysis.id}))
        else:
            check.status = "fetch_failed" if failed else "baseline" if baseline else "unchanged"
            check.finished_at,monitor.active_check_id,monitor.status = now(),None,check.status
        job.status,job.error,job.lease_token,job.lease_until = "completed",None,None,None
        job.result = {"check_id":check.id,"status":check.status}


async def _analyze(factory,job_id,token,fetcher,agent,checkpoint=None):
    async with factory.begin() as session:
        job,check,monitor = await _guard(session,job_id,token)
        analysis = await session.get(mm.MonitorAnalysis,job.scope["analysis_id"])
        if analysis.user_id!=job.user_id or analysis.check_id!=check.id:
            raise PageError("INVALID_ANALYSIS")
        context = await personal_context(session,job.user_id,monitor.application_id)
        digest = fingerprint(context)
        changes = [row_out(c) for c in (await session.scalars(select(mm.MonitorChange).where(mm.MonitorChange.check_id==check.id))).all()]
        sources = (await session.scalars(select(mm.MonitorSource).where(mm.MonitorSource.monitor_id==monitor.id,mm.MonitorSource.active.is_(True)))).all()
        coverage = [{"source_id":s.id,"roles":s.roles,"url":s.url,"status":s.last_fetch_status,
                     "snapshot_id":s.latest_snapshot_id,"verified_at":s.last_checked_at.isoformat() if s.last_checked_at else None} for s in sources]
        configured_ids = {c["snapshot_id"] for c in coverage if c.get("snapshot_id")}
        ids = configured_ids | {c[k] for c in changes for k in ("before_id","after_id")}
        pages = {s.id:row_out(s) for s in (await session.scalars(select(mm.MonitorSnapshot).where(
            mm.MonitorSnapshot.id.in_(ids),mm.MonitorSnapshot.user_id==job.user_id,mm.MonitorSnapshot.monitor_id==monitor.id))).all()}
        for source in sources:
            expected = next((c.get("snapshot_id") for c in check.coverage if c["source_id"]==source.id),None)
            if expected and expected!=source.latest_snapshot_id:
                raise PageError("STALE_PAGE_EVIDENCE")
        hosts = {s.host for s in sources}
        analysis.status,analysis.input_fingerprint,analysis.input_refs = "running",digest,{"profile_version":context["profile_version"]}
        user_id,monitor_id,check_id,analysis_id = job.user_id,monitor.id,check.id,analysis.id
    async def fetch_extra(url):
        page = await fetcher.fetch(url,hosts)
        if page is None:
            raise PageError("SUPPLEMENTAL_PAGE_EMPTY")
        async with factory.begin() as session:
            await _guard(session,job_id,token)
            saved = _snapshot(user_id,monitor_id,None,page)
            session.add(saved)
            await session.flush()
            return row_out(saved)
    tools = ImpactTools(check_id=check_id,changes=changes,pages=pages,configured_ids=configured_ids,
                        coverage=coverage,context=context,fetch_extra=fetch_extra)
    try:
        result,eligible = await asyncio.wait_for(agent.run(tools),180)
        # Enforce safety again even for a provider/test double returning a result.
        eligible = tools.validate(result)
    except BaseException:
        async with factory.begin() as session:
            job = await session.get(m.Job,job_id)
            if job and job.lease_token==token:
                analysis = await session.get(mm.MonitorAnalysis,analysis_id)
                analysis.trace,analysis.snapshot_ids = tools.trace,list(tools.pages)
                if tools.personal_refs:
                    analysis.input_refs = dict(tools.personal_refs)
        raise
    if checkpoint:
        await checkpoint("before_commit", job_id, token)
    async with factory.begin() as session:
        await WorkflowService(session).state(user_id,lock=True)
        job,check,monitor = await _guard(session,job_id,token)
        analysis = await session.get(mm.MonitorAnalysis,analysis_id)
        current = await personal_context(session,user_id,monitor.application_id)
        if fingerprint(current)!=digest:
            raise PageError("STALE_PERSONAL_INPUT")
        analysis.result = result.model_dump(mode="json")
        analysis.trace,analysis.snapshot_ids = tools.trace,list(tools.pages)
        analysis.input_refs = {k:v for k,v in tools.personal_refs.items()}
        analysis.status = "affected" if eligible else "unknown" if result.impact=="UNKNOWN" or result.applicability=="UNKNOWN" else "not_applicable" if result.applicability=="NOT_MATCH" else "no_direct_impact"
        analysis.error = None
        if eligible:
            key = "official-impact:"+check.id
            if not await session.scalar(select(m.Notification.id).where(m.Notification.dedupe_key==key)):
                session.add(m.Notification(user_id=user_id,type="official_impact",target_id=monitor.application_id,
                    analysis_id=analysis.id,dedupe_key=key,message=result.notification_title))
        check.status,check.finished_at = analysis.status,now()
        monitor.status,monitor.active_check_id = analysis.status,None
        job.status,job.result,job.error,job.lease_token,job.lease_until = "completed",{"analysis_id":analysis.id},None,None,None


async def process_one(factory,fetcher=None,agent=None,checkpoint=None):
    token = uuid4().hex
    eligible = or_(m.Job.status=="queued",(m.Job.status=="running") & (m.Job.lease_until<now()))
    async with factory.begin() as session:
        job = await session.scalar(select(m.Job).where(m.Job.type.in_(["monitor.check","monitor.analyze"]),eligible)
            .order_by(m.Job.created_at,m.Job.id).limit(1).with_for_update(skip_locked=True))
        if not job:
            return False
        won = await session.execute(update(m.Job).where(m.Job.id==job.id,eligible).values(status="running",
            lease_token=token,lease_until=now()+timedelta(seconds=240),attempt=m.Job.attempt+1).execution_options(synchronize_session=False))
        if won.rowcount!=1:
            return True
        identity,kind = job.id,job.type
        logging.getLogger(__name__).info("monitor job claimed instance=%s job_id=%s check_id=%s type=%s attempt=%s",
            socket.gethostname(),identity,job.scope.get("check_id"),kind,job.attempt+1)
    renewal = asyncio.create_task(_renew(factory,identity,token))
    try:
        if checkpoint:
            await checkpoint("claimed", identity, token)
        if job.attempt>=3:
            raise PageError("RETRY_BUDGET_EXHAUSTED")
        if kind=="monitor.check":
            await _check(factory,identity,token,fetcher or HttpFetcher())
        else:
            provider = agent or PersonalImpactAgent(LLMClient(timeout_seconds=25,retries=0))
            await _analyze(factory,identity,token,fetcher or HttpFetcher(),provider,checkpoint)
        if checkpoint:
            await checkpoint("committed", identity, token)
    except Exception as exc:
        error = str(exc)[:150] if isinstance(exc,(PageError,ValueError)) else type(exc).__name__
        async with factory.begin() as session:
            job = await session.scalar(select(m.Job).where(m.Job.id==identity,m.Job.lease_token==token))
            if job:
                check = await session.get(mm.MonitorCheck,job.scope["check_id"])
                monitor = await session.get(mm.Monitor,check.monitor_id)
                stale = error in {"STALE_MONITOR_CONFIG","STALE_PAGE_EVIDENCE","STALE_LEASE"}
                terminal = stale or job.attempt>=3 or error=="MODEL_NOT_CONFIGURED"
                job.status,job.error,job.lease_token,job.lease_until = "superseded" if stale else "failed" if terminal else "queued",error,None,None
                if kind=="monitor.analyze":
                    analysis = await session.get(mm.MonitorAnalysis,job.scope["analysis_id"])
                    analysis.status,analysis.error = "superseded" if stale else "failed" if terminal else "queued",error
                if terminal:
                    check.status,check.error,check.finished_at = "superseded" if stale else "analysis_failed" if kind=="monitor.analyze" else "fetch_failed",error,now()
                    if monitor.active_check_id==check.id:
                        monitor.active_check_id,monitor.status = None,check.status
    finally:
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal
    return True


async def retry_changed_inputs(session,user_id):
    """Called only on business events; completed decisions are never replayed."""
    analyses = (await session.scalars(select(mm.MonitorAnalysis).where(mm.MonitorAnalysis.user_id==user_id,
        mm.MonitorAnalysis.status.in_(["unknown","failed"])).limit(100))).all()
    for analysis in analyses:
        monitor = await session.get(mm.Monitor,analysis.monitor_id)
        check = await session.get(mm.MonitorCheck,analysis.check_id)
        if not monitor.enabled or monitor.version!=check.monitor_version or not analysis.input_fingerprint:
            continue
        context = await personal_context(session,user_id,monitor.application_id)
        digest = fingerprint(context)
        if digest==analysis.input_fingerprint:
            continue
        watched = analysis.input_refs or {}
        relevant = False
        for ref,old_value in watched.items():
            if ref.startswith("profile."):
                new_value = context["profile"].get(ref.removeprefix("profile."))
            elif ref in {"application","offering","program","profile_version"}:
                new_value = context.get(ref)
            elif "." in ref:
                kind,identity = ref.split(".",1)
                new_value = next((r for r in context.get(kind,[]) if r["id"]==identity),None)
            else:
                continue
            relevant |= new_value!=old_value
        if not relevant:
            continue
        key = f"monitor-input:{analysis.id}:{digest}"
        if not await session.scalar(select(m.Job.id).where(m.Job.dedupe_key==key)):
            session.add(m.Job(user_id=user_id,type="monitor.analyze",dedupe_key=key,input_revision=monitor.version,
                scope={"check_id":check.id,"analysis_id":analysis.id}))
            analysis.status = "queued"
