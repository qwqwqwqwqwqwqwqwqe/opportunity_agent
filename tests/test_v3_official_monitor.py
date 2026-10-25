"""Offline safety and transactional acceptance; no real LLM/website claims."""
import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
import httpx
from fastapi import FastAPI
from sqlalchemy import select,func
from opportunity_agent.v2.api.dependencies import current_user
from opportunity_agent.v2.db.session import get_session
from opportunity_agent.v2.db.models import Profile
from opportunity_agent.v3 import models as m,monitor_models as mm
from opportunity_agent.v3.api import router,workflow_mode
from opportunity_agent.v3.monitor_contracts import MonitorConfig,ImpactResult,OfficialEvidence,PersonalEvidence
from opportunity_agent.v3.monitor_service import MonitorService,now,next_daily
from opportunity_agent.v3.monitor_web import parse_html,page_diff,safe_url,PageError,FixtureLoader
from opportunity_agent.v3.monitor_worker import process_one,schedule_due,retry_changed_inputs
from opportunity_agent.v3.impact_agent import PersonalImpactAgent
from opportunity_agent.v3.monitor_evaluation import tools_for_case,evaluate
from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v3.service import WorkflowService,WorkflowError
from test_v3_workflow import database,setup_application,execute

URL='https://example.edu/program'


def html(value='December 10, 2026',noise='2026',link='/apply'):
    return f'<html><body><nav>Navigation {noise}</nav><main><h1>MSCS 2027 Fall Admissions</h1><p>Application Deadline: {value}. All applicants must submit their complete application before the deadline.</p><a href="{link}">Application portal</a></main><footer>{noise}</footer></body></html>'


def config(**kw):
    return MonitorConfig(request_id=uuid4().hex,project_url=URL,confirmed=True,enabled=True,**kw)


class Fetcher:
    def __init__(self,pages):
        self.pages=pages
    async def fetch(self,url,hosts,*args):
        value=self.pages[url]
        if isinstance(value,Exception):
            raise value
        return parse_html(value,url) if value else None


class DecisionAgent:
    def __init__(self,impact='AFFECTED',callback=None):
        self.impact,self.callback=impact,callback
    async def run(self,tools):
        await tools.call('get_application_context',{})
        for identity in tools.configured_ids:
            await tools.call('read_official_page',{'snapshot_id':identity})
        change=tools.changes[0]
        await tools.call('read_official_page',{'snapshot_id':change['after_id']})
        block=tools.pages[change['after_id']]['blocks'][1]
        if self.callback:
            await self.callback()
        result=ImpactResult(check_id=tools.check_id,change_ids=[c['id'] for c in tools.changes],applicability='MATCH',
            impact=self.impact,direction='POSITIVE' if self.impact=='AFFECTED' else 'UNKNOWN',categories=['deadline'],
            affected_items=['申请截止时间变化'] if self.impact=='AFFECTED' else [],
            official_evidence_refs=[OfficialEvidence(snapshot_id=change['after_id'],block_id=block['id'],quote=block['text'])],
            personal_basis_refs=[PersonalEvidence(ref='application',value=tools.context['application'])],
            notification_title='申请截止日期有变化',notification_body='适用当前申请周期，请查看官网证据。')
        return result,True


async def setup(factory,user,**kw):
    async with factory.begin() as session:
        _,offering,app=await setup_application(session,user)
        await MonitorService(session,user).configure(app['id'],config(**kw))
    return app,offering


async def check(factory,user,app):
    async with factory.begin() as session:
        monitor=await MonitorService(session,user).get(app['id'])
        monitor.last_manual_at=None
        return await MonitorService(session,user).enqueue_check(app['id'],uuid4().hex)


def test_noise_ignored_and_links_dates_preserved():
    a=parse_html(html(),URL)
    assert parse_html(html(noise='2040'),URL).content_hash==a.content_hash
    assert parse_html(html(link='/apply-new'),URL).content_hash!=a.content_hash
    b=parse_html(html('December 15, 2026'),URL)
    assert 'December 10' in str(page_diff(a,b)) and 'December 15' in str(page_diff(a,b))
    assert any(d['kind']=='links' for d in page_diff(a,parse_html(html(link='/new'),URL)))


@pytest.mark.parametrize('url',['file:///etc/passwd','http://example.edu','https://localhost/',
    'https://127.0.0.1/','https://169.254.169.254/latest','https://u:p@example.edu/',
    'https://example.edu:444/','https://evil.example/','https://example.edu\\@127.0.0.1/'])
def test_unsafe_urls_rejected(url):
    with pytest.raises(PageError):
        safe_url(url,{'example.edu','localhost','127.0.0.1','169.254.169.254'})


def test_fixture_loader_cannot_escape_root(tmp_path):
    with pytest.raises(PageError):
        FixtureLoader(tmp_path).load('../outside.html',URL)
    with pytest.raises(PageError):
        parse_html('<html><body>Just a moment. Verify you are human. '+('challenge '*20)+'</body></html>',URL)


def test_configuration_idempotency_isolation_and_conflict():
    async def scenario():
        engine,factory,user,other=await database()
        app,_=await setup(factory,user)
        async with factory.begin() as session:
            service=MonitorService(session,user)
            before=await service.view(app['id'])
            body=config(expected_version=before['version'])
            first=await service.configure(app['id'],body)
            assert await service.configure(app['id'],body)==first
            with pytest.raises(WorkflowError):
                await service.configure(app['id'],config())
            with pytest.raises(WorkflowError):
                await MonitorService(session,other).view(app['id'])
        await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize('impact,expected',[('AFFECTED',1),('UNKNOWN',0),('NO_DIRECT_IMPACT',0)])
def test_baseline_change_agent_and_inbox(impact,expected):
    async def scenario():
        engine,factory,user,_=await database()
        app,_=await setup(factory,user)
        first=await check(factory,user,app)
        await process_one(factory,Fetcher({URL:html()}))
        async with factory() as session:
            assert (await session.get(mm.MonitorCheck,first['id'])).status=='baseline'
            assert await session.scalar(select(func.count()).select_from(m.Notification))==0
        second=await check(factory,user,app)
        await process_one(factory,Fetcher({URL:html('December 15, 2026')}))
        await process_one(factory,agent=DecisionAgent(impact))
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(m.Notification))==expected
            analysis=await session.scalar(select(mm.MonitorAnalysis))
            assert analysis.status=={'AFFECTED':'affected','UNKNOWN':'unknown','NO_DIRECT_IMPACT':'no_direct_impact'}[impact]
            snap=await WorkflowService(session).snapshot(user)
            assert snap['offerings'][0]['deadline_date']=='2027-02-01'
        # Retrying the same completed worker job cannot produce another message.
        assert await process_one(factory,agent=DecisionAgent()) is False
        await engine.dispose()
    asyncio.run(scenario())


def test_faq_only_changes_trigger_and_failed_coverage_never_notifies():
    async def scenario():
        engine,factory,user,_=await database()
        faq='https://example.edu/faq'
        app,_=await setup(factory,user,faq_url=faq)
        await check(factory,user,app)
        await process_one(factory,Fetcher({URL:html(),faq:html()}))
        await check(factory,user,app)
        await process_one(factory,Fetcher({URL:PageError('HTTP_403'),faq:html('December 15, 2026')}))
        for _ in range(3):
            await process_one(factory,agent=DecisionAgent())
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(mm.MonitorChange))==1
            assert await session.scalar(select(func.count()).select_from(m.Notification))==0
            assert (await session.scalar(select(mm.MonitorAnalysis))).error=='INSUFFICIENT_NOTIFICATION_EVIDENCE'
        await engine.dispose()
    asyncio.run(scenario())


def test_304_without_baseline_and_failure_do_not_replace_baseline():
    async def scenario():
        engine,factory,user,_=await database()
        app,_=await setup(factory,user)
        c=await check(factory,user,app)
        await process_one(factory,Fetcher({URL:None}))
        async with factory() as session:
            assert (await session.get(mm.MonitorCheck,c['id'])).coverage[0]['error']=='304_WITHOUT_BASELINE'
        await check(factory,user,app)
        await process_one(factory,Fetcher({URL:html()}))
        await check(factory,user,app)
        await process_one(factory,Fetcher({URL:PageError('HTTP_403')}))
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(mm.MonitorSnapshot))==1
            assert (await session.scalar(select(mm.MonitorSource))).latest_snapshot_id is not None
        await engine.dispose()
    asyncio.run(scenario())


def test_cooldown_running_check_reuse_and_pause_fences_worker():
    async def scenario():
        engine,factory,user,_=await database()
        app,_=await setup(factory,user)
        a=await check(factory,user,app)
        async with factory.begin() as session:
            service=MonitorService(session,user)
            assert (await service.enqueue_check(app['id'],'reuse'))['id']==a['id']
        await process_one(factory,Fetcher({URL:html()}))
        async with factory.begin() as session:
            with pytest.raises(WorkflowError) as error:
                await MonitorService(session,user).enqueue_check(app['id'],'cooldown')
            assert error.value.status==429
        await check(factory,user,app)
        async with factory.begin() as session:
            view=await MonitorService(session,user).view(app['id'])
            body=config(expected_version=view['version']);body.enabled=False
            await MonitorService(session,user).configure(app['id'],body)
        await process_one(factory,Fetcher({URL:html('December 15, 2026')}))
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(mm.MonitorChange))==0
        await engine.dispose()
    asyncio.run(scenario())


def test_personal_edits_during_analysis_reject_stale_result_then_retry():
    async def scenario():
        engine,factory,user,_=await database()
        app,_=await setup(factory,user)
        await check(factory,user,app);await process_one(factory,Fetcher({URL:html()}))
        await check(factory,user,app);await process_one(factory,Fetcher({URL:html('December 15, 2026')}))
        async def edit():
            async with factory.begin() as session:
                session.add(Profile(user_id=user,payload={'toefl_score':102}))
        await process_one(factory,agent=DecisionAgent(callback=edit))
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(m.Notification))==0
            assert (await session.scalar(select(mm.MonitorAnalysis))).error=='STALE_PERSONAL_INPUT'
        await process_one(factory,agent=DecisionAgent())
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(m.Notification))==1
        await engine.dispose()
    asyncio.run(scenario())


def test_daily_scheduler_coalesces_missed_days():
    async def scenario():
        engine,factory,user,_=await database()
        app,_=await setup(factory,user,mode='daily')
        async with factory.begin() as session:
            monitor=await MonitorService(session,user).get(app['id'])
            monitor.next_due=now()-timedelta(days=3)
        await schedule_due(factory);await schedule_due(factory)
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(mm.MonitorCheck))==1
        await engine.dispose()
    asyncio.run(scenario())


def test_applyops_disables_planning_commands(monkeypatch):
    from dataclasses import replace
    from opportunity_agent.v2.core import config as configuration
    monkeypatch.setattr(configuration,'settings',replace(configuration.settings,app_mode='applyops'))
    async def scenario():
        engine,factory,user,_=await database()
        async with factory.begin() as session:
            for command in ('plan.draft','plan.activate','planning.request'):
                with pytest.raises(WorkflowError) as error:
                    await execute(session,user,command)
                assert error.value.status==409
        await engine.dispose()
    asyncio.run(scenario())


def test_http_inbox_read_is_explicit_and_tenant_scoped():
    async def scenario():
        engine,factory,user,other=await database()
        app,_=await setup(factory,user)
        await check(factory,user,app);await process_one(factory,Fetcher({URL:html()}))
        await check(factory,user,app);await process_one(factory,Fetcher({URL:html('December 15, 2026')}))
        await process_one(factory,agent=DecisionAgent())
        api=FastAPI();api.include_router(router)
        api.dependency_overrides[current_user]=lambda:SimpleNamespace(id=user)
        api.dependency_overrides[workflow_mode]=lambda:None
        async def session_dep():
            async with factory() as session:
                yield session
        api.dependency_overrides[get_session]=session_dep
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://test') as client:
            assert (await client.get('/api/v3/inbox/unread-count')).json()['count']==1
            message=(await client.get('/api/v3/inbox')).json()['items'][0]
            assert (await client.get('/api/v3/inbox/'+message['id'])).status_code==200
            assert (await client.get('/api/v3/inbox/unread-count')).json()['count']==1
            assert (await client.post('/api/v3/inbox/'+message['id']+'/read')).status_code==200
            assert (await client.get('/api/v3/inbox/unread-count')).json()['count']==0
            api.dependency_overrides[current_user]=lambda:SimpleNamespace(id=other)
            assert (await client.get('/api/v3/inbox/'+message['id'])).status_code==404
            assert (await client.get('/api/v3/applications/'+app['id']+'/monitor')).status_code==404
        await engine.dispose()
    asyncio.run(scenario())


def test_dataset_has_40_pairs_80_cases_and_group_isolated_splits():
    from pathlib import Path
    path=Path('tests/fixtures/official_monitor/dataset.json')
    data=json.loads(path.read_text(encoding='utf-8'))
    assert len(data['cases'])==80
    assert len({r['before'] for r in data['cases']})==40
    groups={}
    for row in data['cases']:
        groups.setdefault(row['group'],set()).add(row['split'])
        tools=tools_for_case(row,FixtureLoader(path.parent))
        assert bool(tools.changes)==(row['expected']['category']!='noise')
    assert len(groups)==8 and all(len(s)==1 for s in groups.values())


def test_real_agent_adapter_uses_tools_and_validates_evidence_without_network():
    async def scenario():
        from pathlib import Path
        path=Path('tests/fixtures/official_monitor/dataset.json')
        row=json.loads(path.read_text(encoding='utf-8'))['cases'][0]
        tools=tools_for_case(row,FixtureLoader(path.parent))
        calls=[]
        def completion(payload):
            context=json.loads(payload['messages'][1]['content'])['context']
            calls.append(context)
            if len(calls)==1:
                return json.dumps({'tool':'get_user_facts','arguments':{'fields':['toefl_score']}})
            page=tools.pages['after'];block=page['blocks'][1]
            return json.dumps({'tool':'finish','result':{'check_id':tools.check_id,'change_ids':['change1'],
                'applicability':'MATCH','impact':'AFFECTED','direction':'POSITIVE','affected_items':['DDL延长'],
                'official_evidence_refs':[{'snapshot_id':'after','block_id':block['id'],'quote':block['text']}],
                'personal_basis_refs':[{'ref':'application','value':row['context']['application']}],
                'notification_title':'DDL延长','notification_body':'当前申请可用时间增加，请核查官方页面。'}})
        client=LLMClient(completion_fn=completion,retries=0)
        result,eligible=await PersonalImpactAgent(client).run(tools)
        assert eligible and result.direction=='POSITIVE' and len(calls)==2
        assert any(t.get('tool')=='get_user_facts' for t in tools.trace)
        result.official_evidence_refs[0].quote='Invented evidence'
        with pytest.raises(ValueError,match='UNGROUNDED'):
            tools.validate(result)
        with pytest.raises(ValueError,match='INVALID_TOOL_ARGUMENTS'):
            await tools.call('get_user_facts',{'fields':[],'user_id':'other-user'})
        with pytest.raises(ValueError,match='URL_NOT_DISCOVERED'):
            await tools.call('read_official_page',{'url':'https://example.edu/guessed-path'})
    asyncio.run(scenario())


def test_unreviewed_dataset_cannot_produce_official_score():
    async def scenario():
        with pytest.raises(ValueError,match='human-reviewed'):
            await evaluate('tests/fixtures/official_monitor/dataset.json',LLMClient(completion_fn=lambda _: '{}'))
    asyncio.run(scenario())


def test_app_cycle_change_supersedes_queued_check():
    async def scenario():
        engine,factory,user,_=await database()
        app,offering=await setup(factory,user)
        await check(factory,user,app)
        async with factory.begin() as session:
            current=await session.get(m.Offering,offering['id'])
            current.cycle='2028 Fall';current.version+=1
        await process_one(factory,Fetcher({URL:html()}))
        async with factory() as session:
            assert (await session.scalar(select(mm.MonitorCheck))).status=='superseded'
            assert await session.scalar(select(func.count()).select_from(mm.MonitorSnapshot))==0
        await engine.dispose()
    asyncio.run(scenario())


def test_return_to_prior_content_is_a_new_occurrence():
    async def scenario():
        engine,factory,user,_=await database()
        app,_=await setup(factory,user)
        for i,content in enumerate([html(),html('December 15, 2026'),html()]):
            await check(factory,user,app);await process_one(factory,Fetcher({URL:content}))
            if i:
                await process_one(factory,agent=DecisionAgent())
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(mm.MonitorChange))==2
            assert await session.scalar(select(func.count()).select_from(m.Notification))==2
        await engine.dispose()
    asyncio.run(scenario())


def test_fetcher_rejects_private_dns_and_redirect_without_connecting(monkeypatch):
    from opportunity_agent.v3 import monitor_web as web
    monkeypatch.setattr(web.socket,'getaddrinfo',lambda *a,**kw:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(PageError,match='PRIVATE_ADDRESS'):
        web._request(URL,{'example.edu'}, {})
    monkeypatch.setattr(web.socket,'getaddrinfo',lambda *a,**kw:[(2,1,6,'',('1.1.1.1',443))])
    class Response:
        status=302
        def getheader(self,name,default=''):
            return 'https://127.0.0.1/secrets' if name=='Location' else default
    class Connection:
        def __init__(self,host,address,timeout):
            assert address=='1.1.1.1' and host=='example.edu'
        def request(self,*a,**kw): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(web,'_PinnedHTTPS',Connection)
    with pytest.raises(PageError,match='UNSAFE_URL'):
        web._request(URL,{'example.edu'}, {})


def test_unconfigured_model_is_explicit_failure_not_notification():
    class Disabled:
        enabled=False
    async def scenario():
        engine,factory,user,_=await database()
        app,_=await setup(factory,user)
        await check(factory,user,app);await process_one(factory,Fetcher({URL:html()}))
        await check(factory,user,app);await process_one(factory,Fetcher({URL:html('December 15, 2026')}))
        await process_one(factory,agent=PersonalImpactAgent(Disabled()))
        async with factory() as session:
            analysis=await session.scalar(select(mm.MonitorAnalysis))
            assert analysis.error=='MODEL_NOT_CONFIGURED' and analysis.status=='failed'
            assert await session.scalar(select(func.count()).select_from(m.Notification))==0
        await engine.dispose()
    asyncio.run(scenario())


def test_monitor_reconfiguration_pauses_old_check_but_keeps_history():
    async def scenario():
        engine,factory,user,_=await database()
        app,_=await setup(factory,user,college_url=URL,faq_url=URL)
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(mm.MonitorSource))==1
        await check(factory,user,app);await process_one(factory,Fetcher({URL:html()}))
        async with factory.begin() as session:
            source=await session.scalar(select(mm.MonitorSource))
            saved=source.latest_snapshot_id
        await check(factory,user,app);await process_one(factory,Fetcher({URL:None}))
        async with factory() as session:
            source=await session.scalar(select(mm.MonitorSource))
            assert source.latest_snapshot_id==saved
            assert await session.scalar(select(func.count()).select_from(mm.MonitorSnapshot))==1
        await engine.dispose()
    asyncio.run(scenario())


def test_expired_lease_is_recovered_and_old_owner_cannot_commit():
    from sqlalchemy import update
    async def scenario():
        engine,factory,user,_=await database()
        app,_=await setup(factory,user)
        first=await check(factory,user,app)
        async with factory.begin() as session:
            job=await session.scalar(select(m.Job).where(m.Job.type=='monitor.check'))
            job.status,job.lease_token,job.lease_until,job.attempt='running','dead-worker',now()-timedelta(seconds=1),1
        await process_one(factory,Fetcher({URL:html()}))
        async with factory() as session:
            assert (await session.get(mm.MonitorCheck,first['id'])).status=='baseline'
        second=await check(factory,user,app)
        class TakeoverFetcher:
            async def fetch(self,*args):
                async with factory.begin() as session:
                    await session.execute(update(m.Job).where(m.Job.dedupe_key=='monitor-check:'+second['id'])
                        .values(lease_token='new-owner',lease_until=now()+timedelta(seconds=240)))
                return parse_html(html('December 15, 2026'),URL)
        await process_one(factory,TakeoverFetcher())
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(mm.MonitorChange))==0
            job=await session.scalar(select(m.Job).where(m.Job.dedupe_key=='monitor-check:'+second['id']))
            assert job.lease_token=='new-owner'
        await engine.dispose()
    asyncio.run(scenario())


def test_only_read_personal_fields_requeue_unknown_decision():
    class UnknownScore(DecisionAgent):
        async def run(self,tools):
            await tools.call('get_user_facts',{'fields':['toefl_score']})
            return await super().run(tools)
    async def scenario():
        engine,factory,user,_=await database()
        app,_=await setup(factory,user)
        await check(factory,user,app);await process_one(factory,Fetcher({URL:html()}))
        await check(factory,user,app);await process_one(factory,Fetcher({URL:html('December 15, 2026')}))
        await process_one(factory,agent=UnknownScore('UNKNOWN'))
        async with factory.begin() as session:
            profile=Profile(user_id=user,payload={'major':'CS'})
            session.add(profile);await session.flush()
            await retry_changed_inputs(session,user)
            assert (await session.scalar(select(mm.MonitorAnalysis))).status=='unknown'
            profile.payload={'major':'CS','toefl_score':105};profile.version+=1
            await session.flush()
            await retry_changed_inputs(session,user)
            assert (await session.scalar(select(mm.MonitorAnalysis))).status=='queued'
        await engine.dispose()
    asyncio.run(scenario())
