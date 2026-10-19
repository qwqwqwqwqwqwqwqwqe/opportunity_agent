"""Deterministic workspace writes. No LLM has direct mutation authority."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from sqlalchemy import select, update, delete
from ..db.models import Application, ApplicationTask, TaskProgress, Profile
from ..db.base import utcnow
from .models import Project, Source, ImportJob, Chunk, Tracking, TaskDetail, CalendarEvent, Reminder, Notification, Receipt
from .schemas import ProjectData, EventData


def dump(row):
    return {c.key: getattr(row, c.key) for c in row.__table__.columns}


async def owned(session, cls, user_id, row_id, *, lock=False):
    stmt = select(cls).where(cls.id == row_id, cls.user_id == user_id)
    row = await session.scalar(stmt.with_for_update() if lock else stmt)
    if row is None:
        raise HTTPException(404, '记录不存在')
    return row


def version(row, expected):
    if row.version != expected:
        raise HTTPException(409, '版本已变化，请刷新后重试')


async def command(session, user_id, operation, body, action):
    from fastapi.encoders import jsonable_encoder
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    receipt = await session.scalar(select(Receipt).where(Receipt.user_id == user_id,
        Receipt.operation == operation, Receipt.request_id == body['request_id']))
    if receipt:
        if receipt.fingerprint != fingerprint:
            raise HTTPException(409, '相同 request_id 的参数已变化')
        return receipt.response
    # Serialize same-user workspace mutations, including simultaneous idempotent requests.
    from ..db.models import User
    await session.scalar(select(User).where(User.id == user_id).with_for_update())
    receipt = await session.scalar(select(Receipt).where(Receipt.user_id == user_id,
        Receipt.operation == operation, Receipt.request_id == body['request_id']))
    if receipt:
        if receipt.fingerprint != fingerprint:
            raise HTTPException(409, '相同 request_id 的参数已变化')
        return receipt.response
    response = jsonable_encoder(await action())
    session.add(Receipt(user_id=user_id, operation=operation, request_id=body['request_id'],
        fingerprint=fingerprint, response=response))
    await session.commit()
    return response


async def create_project(session, user_id, item, *, enqueue=False):
    from .importer import validate_input_url
    # Validate URL shape here; DNS and redirect checks are repeated by the worker.
    for s in item.sources:
        if s.url:
            validate_input_url(s.url)
    p = Project(user_id=user_id, draft=item.data.model_dump(mode='json'))
    session.add(p)
    await session.flush()
    for s in item.sources:
        session.add(Source(project_id=p.id, version=1, kind=s.kind, url=s.url,
            content=s.text, content_hash=hashlib.sha256(s.text.encode()).hexdigest() if s.text else '',
            method='pasted' if s.text else '', status='read' if s.text else 'pending'))
    if enqueue:
        session.add(ImportJob(user_id=user_id, project_id=p.id, version=p.version))
    await session.flush()
    return dump(p)


async def confirm_project(session, user_id, project_id, body):
    p = await owned(session, Project, user_id, project_id, lock=True)
    version(p, body.expected_version)
    ProjectData.model_validate(p.draft)
    if not all(str(p.draft.get(k, '')).strip() for k in ('university', 'program', 'intake')):
        raise HTTPException(422, '发布前请补充学校、项目全称和申请季；网页未注明的信息需手动确认')
    sources = list(await session.scalars(select(Source).where(Source.project_id == p.id, Source.version == p.version)))
    if not set(body.approved_sources) <= {s.id for s in sources if s.content}:
        raise HTTPException(422, '来源不属于当前草稿或没有正文')
    published = json.loads(json.dumps(p.draft))
    published['approved_sources'] = body.approved_sources
    published['field_origins'] = {k: ('user_confirmed_extraction' if k in published.get('provenance', {}) else 'user_manual')
        for k in published if k not in {'provenance', 'approved_sources'}}
    p.published, p.published_version = published, p.version
    p.confirmations = list(set(body.confirmations))
    p.index_status = 'pending'
    p.version += 1
    # A confirmation publishes a snapshot; edits always target a fresh draft version.
    for source in sources:
        session.add(Source(project_id=p.id, version=p.version, kind=source.kind, url=source.url,
            title=source.title, content=source.content, content_hash=source.content_hash,
            status=source.status, method=source.method, error_code=source.error_code))
    await session.flush()
    return dump(p)


async def select_project(session, user_id, project_id, expected):
    p = await owned(session, Project, user_id, project_id, lock=True)
    version(p, expected)
    if p.archived or not p.published_version:
        raise HTTPException(422, '请先确认项目')
    previous = await session.scalar(select(Tracking).where(Tracking.user_id == user_id, Tracking.project_id == p.id))
    if previous:
        return dump(previous)
    d = p.published
    app = Application(user_id=user_id, university=d['university'], program=d['program'], intake=d['intake'],
        status='considering', requirements={'workspace': d, 'confirmations': p.confirmations})
    session.add(app)
    await session.flush()
    tracking = Tracking(user_id=user_id, project_id=p.id, application_id=app.id,
        project_version=p.published_version, snapshot={**d, 'confirmations': p.confirmations})
    session.add(tracking)
    if 'deadlines' in p.confirmations:
        for i, deadline in enumerate(d.get('deadlines', [])):
            if not deadline.get('date'):
                continue
            event = EventData(title=f"{d['university']} {d['program']} · {deadline.get('label', '申请截止')}",
                kind='deadline', date=deadline['date'],
                time=deadline.get('time', '') if 'timezone' in p.confirmations else '',
                timezone=deadline.get('timezone') or 'Asia/Shanghai', notes='来源：用户确认的项目要求；日期精度见项目表')
            session.add(CalendarEvent(user_id=user_id, application_id=app.id,
                origin_key=f'application:{app.id}:deadline:{i}', payload=event.model_dump(mode='json')))
    await session.flush()
    return dump(tracking)


def checklist(snapshot):
    """Planning's deterministic requirements-to-task adapter; unknown stays unknown."""
    confirmed = set(snapshot.get('confirmations', []))
    tasks = []
    for field, title in [('language', '语言成绩'), ('gre', 'GRE 政策'), ('materials', '申请材料'), ('deadlines', '申请截止日期')]:
        if field not in confirmed:
            tasks.append({'stable_key': f'verify:{field}', 'title': f'核对{title}', 'requirement': '要求尚未确认', 'category': field})
    if 'language' in confirmed and snapshot.get('language'):
        tasks.append({'stable_key': 'language', 'title': '准备语言成绩', 'category': 'language',
            'requirement': json.dumps(snapshot['language'], ensure_ascii=False)})
    if 'gre' in confirmed and snapshot.get('gre') in {'required', 'optional'}:
        tasks.append({'stable_key': 'gre', 'title': '准备 GRE 成绩' if snapshot['gre'] == 'required' else '决定是否提供 GRE',
            'category': 'gre', 'requirement': snapshot['gre']})
    if 'materials' in confirmed:
        for i, material in enumerate(snapshot.get('materials', [])):
            tasks.append({'stable_key': f'material:{i}', 'title': material['name'], 'category': material.get('category', 'other'),
                'requirement': json.dumps(material, ensure_ascii=False), 'required_count': material.get('count') or 1,
                'invited': 0, 'received': 0})
    tasks.append({'stable_key': 'submit', 'title': '提交申请并核对收件状态', 'category': 'submit',
        'requirement': '准备完成不等于已提交；以申请系统收件状态为准'})
    dates=[d['date'] for d in snapshot.get('deadlines',[]) if d.get('date')] if 'deadlines' in confirmed else []
    if dates:
        from datetime import date,timedelta
        final=date.fromisoformat(min(dates))
        for task in tasks:
            days=0 if task['category']=='submit' else 60 if task['category'] in {'language','gre'} else 30
            suggested=final-timedelta(days=days)
            task['suggested_date']=suggested.isoformat()
            task['date_note']='准备任务建议日期，非官网要求；确认清单后加入日历，可另行调整'
    return tasks


async def tracking_for(session, user_id, app_id, lock=False):
    stmt = select(Tracking).where(Tracking.user_id == user_id, Tracking.application_id == app_id)
    t = await session.scalar(stmt.with_for_update() if lock else stmt)
    if not t:
        raise HTTPException(404, '申请追踪不存在')
    return t


async def confirm_checklist(session, tracking, expected):
    version(tracking, expected)
    for item in tracking.checklist_draft:
        task = await session.scalar(select(ApplicationTask).where(ApplicationTask.application_id == tracking.application_id,
            ApplicationTask.stable_key == item['stable_key']))
        if task is None:
            task = ApplicationTask(application_id=tracking.application_id, stable_key=item['stable_key'],
                title=item['title'], category=item['category'])
            session.add(task)
            await session.flush()
            session.add(TaskDetail(task_id=task.id, payload={**item,'project_version':tracking.project_version}))
            if item.get('suggested_date'):
                event=EventData(title=item['title'],kind='task',date=item['suggested_date'],
                    notes=item.get('date_note','用户确认的准备任务安排'))
                session.add(CalendarEvent(user_id=tracking.user_id,application_id=tracking.application_id,
                    task_id=task.id,origin_key='task:'+task.id,payload=event.model_dump(mode='json')))
        else:
            detail=await session.scalar(select(TaskDetail).where(TaskDetail.task_id==task.id))
            if detail and detail.payload.get('project_version')!=tracking.project_version:
                if task.status in {'completed','submitted','not_applicable'}:
                    detail.payload={**detail.payload,'requirements_update_available':True}
                else:
                    old=detail.payload
                    detail.payload={**item,'project_version':tracking.project_version,
                        'invited':old.get('invited',0),'received':old.get('received',0),
                        'previous_requirement':old.get('requirement','')}
                    task.title=item['title']
                detail.version+=1
        # Reconfirmation never resets progress or silently replaces existing requirements.
    tracking.version += 1
    await session.flush()
    return {'application_id': tracking.application_id, 'version': tracking.version}


async def set_progress(session, user_id, task_id, body):
    task = await session.get(ApplicationTask, task_id)
    if not task or not task.application_id:
        raise HTTPException(404, '任务不存在')
    await tracking_for(session, user_id, task.application_id, lock=True)
    detail = await session.scalar(select(TaskDetail).where(TaskDetail.task_id == task_id).with_for_update())
    if detail is None:
        raise HTTPException(404, '任务不属于申请工作台')
    version(detail, body.expected_version)
    payload = dict(detail.payload)
    for key in ['invited', 'received']:
        value = getattr(body, key)
        if value is not None:
            if value > payload.get('required_count', 20):
                raise HTTPException(422, '数量超过要求')
            payload[key] = value
    detail.payload, detail.version = payload, detail.version + 1
    task.status, task.evidence = body.status, body.evidence
    session.add(TaskProgress(task_id=task.id, action=body.status, evidence=body.evidence, source='workspace_explicit'))
    if body.status in {'completed', 'submitted', 'not_applicable'}:
        event_ids = select(CalendarEvent.id).where(CalendarEvent.user_id == user_id, CalendarEvent.task_id == task.id)
        await session.execute(update(Reminder).where(Reminder.event_id.in_(event_ids), Reminder.status.in_(['pending', 'sending']))
            .values(status='cancelled'))
    return {'id': task.id, 'status': task.status, 'version': detail.version, 'details': detail.payload}


async def save_event(session, user_id, body, event_id=None):
    if body.reminder_local:
        local = body.reminder_local
        if local.tzinfo is not None:
            raise HTTPException(422, 'reminder_local 必须是无偏移本地时间，或改用 reminder_at')
        zone = ZoneInfo(body.data.timezone)
        aware = local.replace(tzinfo=zone)
        if (aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local
                or aware.utcoffset() != local.replace(tzinfo=zone,fold=1).utcoffset()):
            raise HTTPException(422, '提醒时间处于夏令时跳变或重复区间，请使用明确UTC时间')
        body.data.reminder_at=aware
    e = await owned(session, CalendarEvent, user_id, event_id, lock=True) if event_id else CalendarEvent(user_id=user_id)
    if body.task_id:
        task=await session.get(ApplicationTask,body.task_id)
        if not task or not task.application_id:
            raise HTTPException(404,'任务不存在')
        await tracking_for(session,user_id,task.application_id)
        e.task_id,e.application_id=task.id,task.application_id
        if body.data.time:
            task.due_at=datetime.fromisoformat(str(body.data.date)+'T'+body.data.time).replace(tzinfo=ZoneInfo(body.data.timezone)).astimezone(timezone.utc)
        else:
            task.due_at=None
        detail=await session.scalar(select(TaskDetail).where(TaskDetail.task_id==task.id))
        if detail:
            detail.payload={**detail.payload,'due_date':str(body.data.date),'due_timezone':body.data.timezone}
            detail.version+=1
    if event_id:
        version(e, body.expected_version)
        e.version += 1
        await session.execute(update(Reminder).where(Reminder.event_id == e.id,
            Reminder.status.in_(['pending', 'sending'])).values(status='cancelled'))
    e.payload, e.cancelled = body.data.model_dump(mode='json'), False
    session.add(e)
    await session.flush()
    if body.data.reminder_at:
        session.add(Reminder(user_id=user_id, event_id=e.id, event_version=e.version,
            due_at=body.data.reminder_at.astimezone(timezone.utc), email=body.data.email))
    return dump(e)


async def rebuild_index(session, project, embedder=None):
    from ..rag.models import shared_embedder
    embedder = embedder or shared_embedder()
    version_at_start = project.published_version
    if not version_at_start:
        return
    # Confirmed structured values are authoritative; raw pages are background only.
    data = {k: v for k, v in project.published.items() if k not in {'provenance', 'approved_sources', 'field_origins'}}
    for key in ['deadlines','language','gre','materials']:
        if key not in project.confirmations:
            data[key]='待确认，不作为已核实要求'
    texts = [(None, '用户确认项目资料\n' + json.dumps(data, ensure_ascii=False))]
    approved = project.published.get('approved_sources', [])
    sources = await session.scalars(select(Source).where(Source.project_id == project.id, Source.id.in_(approved)))
    texts.extend((s.id, '历史来源正文（不得覆盖用户确认字段）\n' + s.content) for s in sources)
    pieces = [(sid, text[i:i+1200]) for sid, text in texts for i in range(0, len(text), 1000)]
    try:
        vectors = await asyncio.wait_for(asyncio.to_thread(embedder.passages, [p[1] for p in pieces]), 60)
    except Exception:
        vectors = [None] * len(pieces)
    current_version=await session.scalar(select(Project.published_version).where(Project.id==project.id))
    if current_version!=version_at_start:
        return  # A newer confirmation owns the next index build.
    await session.execute(delete(Chunk).where(Chunk.project_id == project.id))
    for (sid, text), vector in zip(pieces, vectors):
        session.add(Chunk(user_id=project.user_id, project_id=project.id, version=version_at_start,
            source_id=sid, content=text, embedding=vector))
    project.index_status = 'ready' if vectors and all(v is not None for v in vectors) else 'text_fallback'


def profile_queries(payload):
    """Every saved experience is represented, including long ones and legacy lists."""
    facets = [('preferences', json.dumps({k:v for k,v in payload.items() if k not in {
        'experiences', 'research_experiences', 'project_experiences', 'internship_experiences',
        'competition_experiences', 'paper_experiences', 'facts', 'change_history'}}, ensure_ascii=False))]
    for item in payload.get('experiences', []):
        facets.append((item['id'], json.dumps(item, ensure_ascii=False)))
    for category in ['research', 'project', 'internship', 'competition', 'paper']:
        for i, item in enumerate(payload.get(category + '_experiences', [])):
            facets.append((f'legacy:{category}:{i}', str(item)))
    return [(key, text[i:i+2000]) for key, text in facets for i in range(0, max(1, len(text)), 1800)]


async def recommend(session, user_id, query, *, embedder=None, reranker=None):
    import math, re
    from ..rag.models import shared_embedder
    projects = list(await session.scalars(select(Project).where(Project.user_id == user_id,
        Project.published_version > 0, Project.archived.is_(False))))
    projects = [p for p in projects if (not query.project_ids or p.id in query.project_ids)
        and (not query.country or p.published.get('country', '').casefold() == query.country.casefold())
        and (not query.degree or p.published.get('degree', '').casefold() == query.degree.casefold())]
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    if not projects:
        return {'profile_version': profile.version if profile else 0, 'experiences_considered': [],
            'retrieval_mode':'empty_library','warning':'请先导入并确认本人项目；不会自动联网搜索。','results':[]}
    facets = profile_queries(profile.payload or {}) if profile else []
    if query.query:
        facets.append(('question', query.query))
    if not facets:
        facets = [('question', '项目介绍 培养目标 课程')]
    # Ownership and current version are constrained BEFORE ranking, not after Top-K.
    chunks = list(await session.scalars(select(Chunk).join(Project, Chunk.project_id == Project.id).where(
        Chunk.user_id == user_id, Project.user_id == user_id, Project.archived.is_(False),
        Chunk.version == Project.published_version, Project.id.in_([p.id for p in projects]))))
    fallback = False
    try:
        vectors = await asyncio.wait_for(asyncio.to_thread((embedder or shared_embedder()).encode,
            [t for _,t in facets], 'query'), 45)
    except Exception:
        vectors = [None] * len(facets)
    def terms(t):
        return set(re.findall(r'[\w]+', t.casefold())) | set(re.findall(r'[\u4e00-\u9fff]{2}', t))
    scores, evidence = {}, {}
    for (experience_id, text), vector in zip(facets, vectors):
        per_project = {}
        for chunk in chunks:
            lexical = len(terms(text) & terms(chunk.content)) / max(1, len(terms(text)))
            semantic = sum(a*b for a,b in zip(vector, chunk.embedding)) if vector is not None and chunk.embedding is not None else None
            fallback |= semantic is None
            per_project.setdefault(chunk.project_id, []).append((lexical, semantic, chunk))
        # RRF across lexical/vector project rankings; max hit per project prevents length bias.
        rankings = []
        for axis in [0, 1]:
            best = [(pid, max((x for x in hits if x[axis] is not None), key=lambda x:x[axis], default=None))
                    for pid,hits in per_project.items()]
            rankings.append(sorted([(pid,x) for pid,x in best if x and x[axis] > 0], key=lambda item:-item[1][axis]))
        for ranking in rankings:
            for rank, (pid, hit) in enumerate(ranking):
                scores[pid] = scores.get(pid, 0) + 1/(60+rank+1)/len(facets)
                evidence.setdefault(pid, {})[(experience_id, hit[2].id)] = {
                    'experience_id': experience_id, 'chunk_id': hit[2].id,
                    'source_id': hit[2].source_id, 'excerpt': hit[2].content[:700]}
    ordered = sorted(projects, key=lambda p:(-scores.get(p.id,0),p.id))
    rerank_status = 'disabled'
    if ordered and (reranker is not None or __import__('os').getenv('RERANKER_ENABLED','0') == '1'):
        from ..rag.models import shared_reranker
        ranker=reranker or shared_reranker()
        shortlist=ordered[:20]
        reranked={p.id:0. for p in shortlist}
        try:
            async with asyncio.timeout(45):
                for _,text in facets:
                    passages=[json.dumps({k:p.published.get(k) for k in ['summary','objectives','tracks','courses','background']},ensure_ascii=False)[:4000] for p in shortlist]
                    values=await asyncio.to_thread(ranker.score,text,passages)
                    if len(values)!=len(shortlist) or not all(math.isfinite(v) for v in values):
                        raise ValueError('invalid reranker output')
                    for rank,index in enumerate(sorted(range(len(values)),key=lambda i:-values[i])):
                        reranked[shortlist[index].id]+=1/(61+rank)/len(facets)
            ordered=sorted(shortlist,key=lambda p:-(reranked[p.id]+scores.get(p.id,0)))+ordered[20:]
            rerank_status='cross_encoder'
        except Exception:
            rerank_status='unavailable'
    result = []
    for p in ordered[:query.limit]:
        known = set(p.confirmations)
        checks = requirement_checks(p.published,known,profile.payload if profile else {})
        result.append({'project_id': p.id, 'project_version': p.published_version,
            'university': p.published['university'], 'program': p.published['program'],
            'relative_score': round(scores.get(p.id,0),6), 'evidence': list(evidence.get(p.id,{}).values())[:8],
            'requirements': checks,
            'confirmed_data': p.published, 'confirmations':p.confirmations})
    return {'profile_version': profile.version if profile else 0,
        'experiences_considered': sorted({key for key,_ in facets}),
        'retrieval_mode': 'text_fallback' if fallback or not chunks else 'hybrid_rrf', 'rerank_status':rerank_status,
        'warning': '相对相关性不是录取概率；未确认要求不得视为满足。', 'results': result}


def requirement_checks(data,confirmed,profile):
    checks={k:{'status':'待核对','value':data.get(k) if k in confirmed else None}
        for k in ['language','gre','materials','deadlines']}
    if 'gre' in confirmed and data.get('gre') in {'not_required','optional','not_accepted'}:
        checks['gre']['status']='无需强制提交'
    elif 'gre' in confirmed and data.get('gre')=='required':
        checks['gre']['status']='已具备成绩，仍需核对有效期及最低要求' if profile.get('gre_score') else '有缺口'
    if 'language' in confirmed:
        states=[]
        for requirement in data.get('language',[]):
            exams=[x for x in profile.get('language_exams',[]) if isinstance(x,dict)
                and x.get('exam','').casefold()==requirement['exam'].casefold()
                and (not requirement.get('version') or x.get('version')==requirement['version'])]
            if exams and not requirement.get('conditions') and not requirement.get('sections'):
                try:
                    if any(float(x.get('total',''))>=float(requirement['total']) for x in exams):
                        states.append('已满足');continue
                    states.append('有缺口');continue
                except (ValueError,TypeError):
                    pass
            # Only an unqualified, numeric IELTS total can be compared safely to the legacy score.
            if (requirement['exam'].casefold()=='ielts' and not requirement.get('conditions')
                    and not requirement.get('sections') and profile.get('ielts_score') is not None):
                try:
                    states.append('已满足' if float(profile['ielts_score'])>=float(requirement['total']) else '有缺口')
                except (ValueError,TypeError):states.append('待核对')
            else:states.append('待核对')
        checks['language']['status']='已满足' if '已满足' in states else ('有缺口' if states and all(v=='有缺口' for v in states) else '待核对')
    return checks
