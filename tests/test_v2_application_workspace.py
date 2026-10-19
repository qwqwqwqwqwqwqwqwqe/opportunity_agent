import asyncio
from types import SimpleNamespace
import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Response
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from opportunity_agent.v2.db.base import Base, utcnow
from opportunity_agent.v2.db.models import User, Profile, ApplicationTask
from opportunity_agent.v2.workbench.models import Project, Source, Chunk, ImportJob, Reminder, CalendarEvent, Notification
from opportunity_agent.v2.workbench.schemas import Query, EventData, Progress
from opportunity_agent.v2.workbench.service import rebuild_index, recommend, profile_queries


class Embedder:
    def passages(self,texts):return [[1.0]+[0.0]*383 for t in texts]
    def encode(self,texts,kind):return [[1.0]+[0.0]*383 for t in texts]


def test_url_first_draft_extracts_identity_and_blocks_empty_publish():
    from opportunity_agent.v2.workbench.schemas import ImportBatch, ConfirmProject
    from opportunity_agent.v2.workbench import importer, service
    from fastapi import HTTPException
    batch=ImportBatch(request_id='urls-only',projects=[{'data':{},'sources':[{'url':'https://example.edu/ms'}]}])
    assert batch.projects[0].data.university==''
    class Model:
        enabled=True
        def generate_structured(self, schema, **kwargs):
            if 'university' not in kwargs['system'].split('Only these fields: ')[-1].split(','):
                return importer.Extraction()
            return importer.Extraction.model_validate({'fields':[
                {'field':'university','value':'Example University','citations':[{'source_id':'s','quote':'Example University'}]},
                {'field':'program','value':'MS Computer Science','citations':[{'source_id':'s','quote':'MS Computer Science'}]}]})
    async def run():
        engine,factory=await database()
        async with factory.begin() as session:
            p=await service.create_project(session,'a',batch.projects[0],enqueue=True)
            with pytest.raises(HTTPException) as error:
                await service.confirm_project(session,'a',p['id'],ConfirmProject(request_id='publish'))
            assert error.value.status_code==422
        draft=await importer.extract_draft(SimpleNamespace(draft=batch.projects[0].data.model_dump()),
            [SimpleNamespace(id='s',kind='overview',title='MS',url='https://example.edu/ms',content='Example University MS Computer Science')],llm=Model())
        assert draft['university']=='Example University'
        assert draft['program']=='MS Computer Science'
        assert draft['intake']==''  # Unknown cycle must be filled by user, never inferred.
        await engine.dispose()
    asyncio.run(run())


async def database():
    engine=create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as c:await c.run_sync(Base.metadata.create_all)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    async with factory.begin() as s:
        s.add_all([User(id='a',email='a@example.test',password_hash='x'),User(id='b',email='b@example.test',password_hash='x')])
    return engine,factory


def test_workspace_manual_end_to_end_and_ownership(monkeypatch):
    from opportunity_agent.v2.rag import models
    from opportunity_agent.v2.core import config
    from dataclasses import replace
    monkeypatch.setattr(config,'settings',replace(config.settings,domain_agent_transport='local'))
    monkeypatch.setattr(models,'shared_embedder',lambda:Embedder())
    async def run():
        import importlib
        api=importlib.import_module('opportunity_agent.v2.api.app')
        from opportunity_agent.v2.api.dependencies import current_user
        from opportunity_agent.v2.db.session import get_session
        engine,factory=await database()
        async def db():
            async with factory() as s:yield s
        async def user(request:Request):
            return SimpleNamespace(id=request.headers.get('x-user','a'),email='a@example.test')
        api.app.dependency_overrides[get_session]=db
        api.app.dependency_overrides[current_user]=user
        data={'university':'Example U','program':'Computational Science','intake':'2027 Fall','summary':'NLP and distributed systems',
            'language':[{'exam':'IELTS','total':'7.5'}], 'gre':'required',
            'materials':[{'name':'SOP','category':'sop','limit':1000,'unit':'words'},
                         {'name':'Letters','category':'recommendation','count':3}],
            'deadlines':[{'label':'Final','date':'2027-01-15','raw_date':'January 15, 2027'}]}
        try:
            async with AsyncClient(transport=ASGITransport(app=api.app),base_url='http://test') as c:
                r=await c.post('/api/v1/project-library',json={'request_id':'create','data':data})
                assert r.status_code==200,r.text
                p=r.json();pid=p['id']
                assert (await c.get('/api/v1/project-library/'+pid,headers={'x-user':'b'})).status_code==404
                assert (await c.get('/api/v1/project-library',headers={'x-user':'b'})).json()==[]
                assert (await c.post('/api/v1/recommendations',json={})).json()['results']==[]
                r=await c.post(f'/api/v1/project-library/{pid}/confirm',json={'request_id':'confirm','expected_version':1,
                    'confirmations':['materials','language','deadlines']})
                assert r.status_code==200,r.text
                confirmed=r.json()
                async with factory.begin() as s:
                    await rebuild_index(s,await s.get(Project,pid),Embedder())
                    s.add(Profile(user_id='a',payload={'experiences':[{'id':'e1','name':'NLP project','description':'NLP and distributed systems'}]}))
                rec=(await c.post('/api/v1/recommendations',json={})).json()
                assert rec['results'][0]['project_id']==pid
                assert 'e1' in rec['experiences_considered']
                assert rec['results'][0]['requirements']['gre']['status']=='待核对'
                r=await c.post(f'/api/v1/project-library/{pid}/select',json={'request_id':'select','expected_version':confirmed['version']})
                assert r.status_code==200,r.text
                appid=r.json()['application_id']
                assert (await c.post(f'/api/v1/project-library/{pid}/select',json={'request_id':'select','expected_version':confirmed['version']})).json()['application_id']==appid
                assert len((await c.get('/api/v1/calendar-events')).json()['events'])==1
                r=await c.post(f'/api/v1/applications/{appid}/checklist/generate',json={'request_id':'generate','expected_version':1})
                assert r.status_code==200,r.text
                r=await c.post(f'/api/v1/applications/{appid}/checklist/confirm',json={'request_id':'check','expected_version':2})
                assert r.status_code==200,r.text
                tasks=(await c.get('/api/v1/workspace/applications')).json()[0]['tasks']
                assert any(t['stable_key']=='verify:gre' for t in tasks)
                task=tasks[0]
                assert (await c.post(f"/api/v1/tasks/{task['id']}/progress",json={'request_id':'done','status':'completed','expected_version':1})).status_code==200
                assert (await c.post(f"/api/v1/tasks/{task['id']}/progress",headers={'x-user':'b'},json={'request_id':'hack','status':'submitted','expected_version':2})).status_code==404
                assert (await c.post(f"/api/v1/tasks/{task['id']}/progress",json={'request_id':'stale','status':'submitted','expected_version':1})).status_code==409
                # A date-only deadline has no invented time and no automatic reminder.
                events=(await c.get('/api/v1/calendar-events')).json()
                assert all(e['payload']['time']=='' for e in events['events']) and not events['reminders']
                r=await c.post('/api/v1/calendar-events',json={'request_id':'event','data':{'title':'IELTS','kind':'exam',
                    'date':'2027-01-01','timezone':'Asia/Shanghai'},'reminder_local':'2026-12-31T09:00'})
                assert r.status_code==200,r.text
                assert len((await c.get('/api/v1/calendar-events',headers={'x-user':'b'})).json()['events'])==0
                assert (await c.post('/api/v1/project-library',json={'request_id':'create','data':{**data,'program':'changed'}})).status_code==409
        finally:
            api.app.dependency_overrides.clear();await engine.dispose()
    asyncio.run(run())


def test_all_experiences_including_long_legacy_are_covered():
    q=profile_queries({'experiences':[{'id':'long','description':'abc '*5000},{'id':'second','description':'vision'}],
                      'internship_experiences':['systems','databases']})
    assert {x[0] for x in q}>={'long','second','legacy:internship:0','legacy:internship:1'}
    assert sum(len(t) for k,t in q if k=='long')>=20000


@pytest.mark.parametrize('url',['http://example.edu','https://127.0.0.1/a','https://localhost/a','https://user:pass@example.edu','https://example.edu:8080'])
def test_private_and_unsafe_urls_rejected(url):
    from opportunity_agent.v2.workbench.importer import validate_input_url
    with pytest.raises(Exception):validate_input_url(url)


def test_tavily_partial_failure_then_advanced(monkeypatch):
    import opportunity_agent.config as config
    import opportunity_agent.v2.workbench.importer as importer
    monkeypatch.setattr(config,'tavily_api_key',lambda:'test-key')
    async def public(url):return 'example.edu'
    monkeypatch.setattr(importer,'validate_public_url',public)
    calls=[]
    def handle(request):
        import json
        calls.append(json.loads(request.content))
        return Response(200,json={'failed_results':[{'url':'https://example.edu','error':'fail'}]} if len(calls)==1 else
            {'results':[{'url':'https://example.edu','raw_content':'# Project\nMaster programme'}]})
    async def run():
        text,_,method=await importer.read_source(SimpleNamespace(url='https://example.edu'),transport=MockTransport(handle))
        assert method=='tavily_advanced' and text.startswith('# Project')
    asyncio.run(run());assert [c['extract_depth'] for c in calls]==['basic','advanced']


def test_failed_import_does_not_block_manual_draft(monkeypatch):
    import opportunity_agent.v2.workbench.importer as importer
    async def fail(source):raise RuntimeError('READ_FAILED')
    monkeypatch.setattr(importer,'read_source',fail)
    async def run():
        engine,factory=await database()
        async with factory.begin() as s:
            p=Project(user_id='a',draft={'university':'U','program':'P','intake':'2027 Fall'});s.add(p);await s.flush()
            source=Source(project_id=p.id,version=1,url='https://example.edu')
            job=ImportJob(user_id='a',project_id=p.id,version=1);s.add_all([source,job]);await s.flush();jid,pid=job.id,p.id
        await importer.process_job(factory,jid)
        async with factory() as s:
            job=await s.get(ImportJob,jid);p=await s.get(Project,pid)
            assert job.status=='awaiting_confirmation' and p.version==2
            assert p.draft['warnings'] and p.published_version==0
        await engine.dispose()
    asyncio.run(run())


def test_reminders_restart_retry_cancel_and_unconfigured_email(monkeypatch):
    from datetime import timedelta
    from opportunity_agent.v2.workbench.worker import tick
    monkeypatch.delenv('SMTP_HOST',raising=False)
    async def run():
        engine,factory=await database()
        async with factory.begin() as s:
            e=CalendarEvent(user_id='a',payload={'title':'Test','date':'2026-01-01'});s.add(e);await s.flush()
            r=Reminder(user_id='a',event_id=e.id,event_version=1,due_at=utcnow()-timedelta(hours=1),email=True);s.add(r)
        await tick(factory);await tick(factory)
        async with factory() as s:
            rows=list(await s.scalars(select(Notification)))
            assert len(rows)==1 and rows[0].email_status=='unavailable' and rows[0].delayed
        await engine.dispose()
    asyncio.run(run())


def test_new_migration_round_trip_on_temporary_sqlite(tmp_path,monkeypatch):
    from alembic.config import Config
    from alembic import command
    import sqlite3
    path=tmp_path/'workspace.db'
    monkeypatch.setenv('DATABASE_URL','sqlite+aiosqlite:///'+path.as_posix())
    config=Config('alembic.ini')
    command.upgrade(config,'head')
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT version_num FROM alembic_version').fetchone()[0]=='0010_application_workspace'
        assert 'model_calls' in {row[1] for row in c.execute('PRAGMA table_info(project_import_jobs)')}
    command.downgrade(config,'0009_research_scope_identity')
    with sqlite3.connect(path) as c:
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='user_projects'").fetchall()
        assert c.execute("SELECT name FROM sqlite_master WHERE name='research_programs'").fetchall()
    # Upgrade an existing 0009 database too; this exercises 0010's explicit DDL,
    # rather than only the old 0001 metadata-created fresh-install path.
    command.upgrade(config,'head')
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE name='user_project_chunks'").fetchall()


def test_identity_warnings_and_quote_failure_remain_reviewable(monkeypatch):
    from opportunity_agent.v2.workbench.importer import extract_draft,Extraction,Candidate,Citation
    class LLM:
        enabled=True
        def generate_structured(self,*a,**kw):
            assert len(json.dumps(kw['context']['pages']))<22000
            return Extraction(fields=[Candidate(field='summary',value='NLP research',citations=[Citation(source_id='s',quote='invented')]),
                                      Candidate(field='gre',value='required')],observed_identity='PhD in Analytics',warnings=['学位与预期不同'])
    import json
    p=SimpleNamespace(draft={'university':'U','program':'MSCS','intake':'2027 Fall'})
    source=SimpleNamespace(id='s',kind='overview',title='Page',content='Actual source')
    d=asyncio.run(extract_draft(p,[source],llm=LLM()))
    assert d['summary']=='NLP research' and not d['provenance']['summary']['quote_located']
    assert d['observed_identity']=='PhD in Analytics' and d['warnings']


def test_user_edit_while_import_running_is_not_overwritten(monkeypatch):
    import opportunity_agent.v2.workbench.importer as importer
    async def run():
        engine,factory=await database()
        async with factory.begin() as s:
            p=Project(user_id='a',draft={'university':'U','program':'P','intake':'2027 Fall'});s.add(p);await s.flush()
            s.add(Source(project_id=p.id,version=1,content='test',status='read'))
            j=ImportJob(user_id='a',project_id=p.id,version=1);s.add(j);await s.flush();pid,jid=p.id,j.id
        async def extract(project,sources,**kwargs):
            async with factory.begin() as writer:
                from sqlalchemy import update
                await writer.execute(update(Project).where(Project.id==pid).values(version=2,draft={**project.draft,'summary':'user manual'}))
            return {**project.draft,'summary':'model draft'}
        monkeypatch.setattr(importer,'extract_draft',extract)
        await importer.process_job(factory,jid)
        async with factory() as s:
            assert (await s.get(Project,pid)).draft['summary']=='user manual'
            assert (await s.get(ImportJob,jid)).status=='superseded'
        await engine.dispose()
    asyncio.run(run())


def test_library_research_never_calls_old_web_and_does_not_require_gre(monkeypatch):
    import opportunity_agent.v2.workbench.research as research
    from opportunity_agent.v2.agents.contracts import ExecutionState
    from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator
    async def report(*args,**kwargs):return {'results':[{'project_id':'p','project_version':1,'university':'U','program':'P',
        'confirmed_data':{'university':'U','program':'P','gre':'required'},'evidence':[],
        'requirements':{'gre':{'status':'待核对','value':None}},'confirmations':[]}], 'profile_version':1}
    monkeypatch.setattr(research,'recommend',report)
    result=asyncio.run(research.execute_library(None,SimpleNamespace(user_id='a',message='推荐项目',request_id='r')))
    assert result.diagnostics['workspace'] and result.route=='rag' and result.status=='complete'
    assert '待确认' in result.findings[0].statement
    state=ExecutionState(user_id='a',run_id='r',conversation_id='c',request_id='x',message='推荐项目',research_result=result)
    assert CustomOrchestrator._check_completion(state).status=='PASS'


def test_deadline_unknown_year_and_critical_language_checks():
    from opportunity_agent.v2.workbench.schemas import Deadline
    from opportunity_agent.v2.workbench.service import requirement_checks
    assert Deadline(raw_date='February 1').date is None
    with pytest.raises(ValueError):Deadline(date='2027-02-30')
    c=requirement_checks({'gre':'required','language':[{'exam':'IELTS','total':'7.0'}]}, {'gre','language'},{'ielts_score':7.5})
    assert c['language']['status']=='已满足' and c['gre']['status']=='有缺口'
    c=requirement_checks({'gre':'required'}, set(),{'gre_score':330})
    assert c['gre']=={'status':'待核对','value':None}


def test_reminder_retries_are_bounded_and_cancel_is_respected(monkeypatch):
    from datetime import timedelta
    from opportunity_agent.v2.workbench.worker import deliver_reminder
    monkeypatch.setenv('SMTP_HOST','smtp.test');monkeypatch.setenv('SMTP_FROM','from@test')
    calls=[]
    def fail(*args):calls.append(1);raise OSError('test')
    async def run():
        engine,factory=await database()
        async with factory.begin() as s:
            e=CalendarEvent(user_id='a',payload={'title':'test','date':'2027-01-01'});s.add(e);await s.flush()
            r=Reminder(user_id='a',event_id=e.id,event_version=1,due_at=utcnow(),email=True,status='sending');s.add(r);await s.flush();rid=r.id
        for _ in range(3):
            async with factory.begin() as s:
                row=await s.get(Reminder,rid);row.status='sending'
            await deliver_reminder(factory,rid,fail)
        async with factory.begin() as s:
            r=await s.get(Reminder,rid);assert r.status=='failed' and r.attempts==3
            r.status='cancelled'
        await deliver_reminder(factory,rid,fail)
        assert len(calls)==3
        await engine.dispose()
    asyncio.run(run())


def test_three_project_import_manual_recovery_match_tracking_calendar(monkeypatch):
    import opportunity_agent.v2.workbench.importer as importer
    from opportunity_agent.v2.workbench import service
    from opportunity_agent.v2.workbench.schemas import ProjectInput,ConfirmProject,Command,EventCommand
    from opportunity_agent.v2.agents.planning_agent import build_workspace_checklist
    async def read(source):
        if 'broken' in source.url:raise OSError('simulated network failure')
        return '# Masters programme\nNLP and distributed systems','Masters programme','fixture'
    async def extract(project,sources,**kwargs):
        return {**project.draft,'summary':'NLP and distributed systems'}
    monkeypatch.setattr(importer,'read_source',read);monkeypatch.setattr(importer,'extract_draft',extract)
    async def run():
        engine,factory=await database()
        ids=[]
        async with factory.begin() as session:
            session.add(Profile(user_id='a',payload={'experiences':[{'id':'nlp','description':'NLP research'}]}))
            for name in ['one','two','broken']:
                p=await service.create_project(session,'a',ProjectInput(data={'university':'Example U','program':name,
                    'intake':'2027 Fall'},sources=[{'url':'https://example.edu/'+name}]),enqueue=True)
                ids.append(p['id'])
        async with factory() as session:
            jobs=list(await session.scalars(select(ImportJob)))
        for job in jobs:await importer.process_job(factory,job.id)
        async with factory.begin() as session:
            for pid in ids:
                p=await session.get(Project,pid)
                if p.draft['program']=='broken':
                    assert p.draft['warnings']
                    p.draft={**p.draft,'summary':'User supplied NLP and distributed systems'}
                await service.confirm_project(session,'a',pid,ConfirmProject(request_id='confirm-'+pid,expected_version=p.version))
                await service.rebuild_index(session,p,Embedder())
        async with factory.begin() as session:
            rec=await service.recommend(session,'a',Query(),embedder=Embedder())
            assert len(rec['results'])==3 and 'nlp' in rec['experiences_considered']
            p=await session.get(Project,ids[0])
            selected=await service.select_project(session,'a',p.id,p.version)
            tracking=await service.tracking_for(session,'a',selected['application_id'])
            tracking.checklist_draft=build_workspace_checklist(tracking.snapshot)
            await service.confirm_checklist(session,tracking,tracking.version)
            tasks=list(await session.scalars(select(ApplicationTask).where(ApplicationTask.application_id==tracking.application_id)))
            task=tasks[0]
            await service.set_progress(session,'a',task.id,Progress(
                request_id='done',expected_version=1,status='completed'))
            event=await service.save_event(session,'a',EventCommand(request_id='exam',data={'title':'IELTS exam',
                'kind':'exam','date':'2027-01-01','timezone':'Asia/Shanghai'},reminder_local='2026-12-30T09:00'))
            assert event['payload']['date']=='2027-01-01'
        await engine.dispose()
    asyncio.run(run())
