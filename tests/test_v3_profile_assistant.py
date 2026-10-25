"""Profile-only chat, saved-data queries, notice ownership and approval integration."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import importlib
from uuid import uuid4
import httpx
import pytest
from sqlalchemy import select,func
from opportunity_agent.v3.profile_assistant import ProfileAssistant,load_notice_context
from opportunity_agent.v3 import models as m
from opportunity_agent.v3.service import WorkflowService
from opportunity_agent.v2.db.models import Profile
from opportunity_agent.v2.db.session import get_session
from opportunity_agent.v2.core import config
from test_v3_workflow import database,setup_application,execute
from test_v3_interfaces import disabled_model
from test_v3_official_monitor import setup,check,process_one,Fetcher,DecisionAgent,html,URL


def initial(message,snapshot,**extra):
    return {'message':message,'workflow_snapshot':snapshot,'request_id':uuid4().hex,'run_id':uuid4().hex,
            'user_id':'u','conversation_id':'c','conversation_context':{},**extra}


async def fixture():
    engine,factory,user,other=await database()
    async with factory.begin() as session:
        _,_,app=await setup_application(session,user)
        await execute(session,user,'task.create',{'title':'Modify SOP','stable_key':'sop','application_id':app['id'],
            'due_date':'2027-01-15','date_certainty':'confirmed','show_in_calendar':True})
        snap=await WorkflowService(session).snapshot(user)
    return engine,factory,user,other,snap


@pytest.mark.parametrize('message',['我的托福是105分','我的专业是计算机科学','我的托福更新为110分','我的托福改为110分'])
def test_profile_changes_are_proposals_only(message):
    async def scenario():
        engine,factory,user,_,snap=await fixture()
        output=await ProfileAssistant(disabled_model()).ainvoke(initial(message,snap))
        assert output['approval_required'] and output['completion']['status']=='WAITING_APPROVAL'
        commands=output['proposals'][0]['payload']['commands']
        assert all(c['type'] in {'profile.change','preference.change'} for c in commands)
        assert '尚未保存' in output['answer'] and not output['plan_draft_requested']
        async with factory() as session:
            assert await session.scalar(select(Profile)) is None
        await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize('message,fragment',[
    ('我目前有哪些申请项目？','Example University'),('我还有哪些未完成的待办？','Modify SOP'),
    ('查看日历','2027-01-15'),('查看我的画像','还没有记录'),('你好','个人画像'),
    ('帮我新增一个待办任务','对应页面修改'),('帮我生成计划','自动排期已停用'),
    ('把申请项目的托福要求改为105分','对应页面修改')])
def test_queries_and_business_mutations_do_not_write(message,fragment):
    async def scenario():
        engine,_,_,_,snap=await fixture()
        original=deepcopy(snap)
        result=await ProfileAssistant(disabled_model()).ainvoke(initial(message,snap))
        assert not result['proposals'] and not result['plan_draft_requested']
        assert fragment in result['answer'],result['answer']
        assert snap==original
        await engine.dispose()
    asyncio.run(scenario())


def test_notice_explanation_is_historical_owned_and_does_not_mark_read():
    async def scenario():
        engine,factory,user,other=await database()
        app,_=await setup(factory,user)
        await check(factory,user,app);await process_one(factory,Fetcher({URL:html()}))
        await check(factory,user,app);await process_one(factory,Fetcher({URL:html('December 15, 2026')}))
        await process_one(factory,agent=DecisionAgent())
        async with factory() as session:
            notice=await session.scalar(select(m.Notification).where(m.Notification.type=='official_impact'))
            message='请解释官网变化通知 '+notice.id
            owned=await load_notice_context(session,user,message)
            private=await load_notice_context(session,other,message)
            snap=await WorkflowService(session).snapshot(user)
            result=await ProfileAssistant(disabled_model()).ainvoke(initial(message,snap,notice_context=owned))
            assert '历史分析' in result['answer'] and URL in result['answer'] and 'December 15' in result['answer']
            assert result['proposals']==[] and notice.read_at is None
            denied=await ProfileAssistant(disabled_model()).ainvoke(initial(message,snap,notice_context=private))
            assert denied['completion']['status']=='NEED_USER' and 'December 15' not in denied['answer']
            assert private['items']==[]
        await engine.dispose()
    asyncio.run(scenario())


def test_multiple_notices_require_selection_unless_latest_requested():
    async def scenario():
        engine,_,_,_,snap=await fixture()
        r={'notification_title':'标题','notification_body':'历史内容','official_evidence_refs':[],'personal_basis_refs':[]}
        notices={'items':[{'id':str(i),'application_id':snap['applications'][0]['id'],'created_at':'2026-10-23',
                          'sources':{},'result':r} for i in range(2)],'total':2}
        agent=ProfileAssistant(disabled_model())
        result=await agent.ainvoke(initial('解释官网变化通知',snap,notice_context=notices))
        assert result['completion']['status']=='NEED_USER'
        latest=await agent.ainvoke(initial('解释最新一封官网变化通知',snap,notice_context=notices))
        assert latest['completion']['status']=='COMPLETED' and '历史分析' in latest['answer']
        await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize('message,found',[('查看1月15日的待办',True),('查看1月16日的待办',False),('查看1月的待办',True),('查看2月的待办',False)])
def test_task_date_filters(message,found):
    async def scenario():
        engine,_,_,_,snap=await fixture()
        snap['today']='2027-01-04'
        response=await ProfileAssistant(disabled_model()).ainvoke(initial(message,snap))
        assert ('Modify SOP' in response['answer'])==found
        assert not response['proposals']
        await engine.dispose()
    asyncio.run(scenario())


def test_school_and_program_filters_intersect():
    from opportunity_agent.v3.profile_assistant import app_scope
    apps=[{'id':'1','university':'CMU','program':'MSAII'},{'id':'2','university':'CMU','program':'MSCS'},
          {'id':'3','university':'Other','program':'MSAII'}]
    assert [a['id'] for a in app_scope('CMU的MSAII还有哪些材料',{'applications':apps})]==['1']


def test_general_chat_model_has_no_write_tools_and_false_save_claim_falls_back():
    class Client:
        enabled=True
        def generate(self,**kwargs):
            assert 'provided_data' in kwargs['user']
            return '已为你保存所有信息'
    async def scenario():
        engine,_,_,_,snap=await fixture()
        result=await ProfileAssistant(Client()).ainvoke(initial('你好',snap))
        assert not result['proposals'] and '已为你保存' not in result['answer']
        await engine.dispose()
    asyncio.run(scenario())


def test_notice_followup_keeps_selected_notice_but_not_after_topic_switch():
    from opportunity_agent.v3.profile_assistant import notice_reference
    history=[{'role':'user','content':'解释官网变化通知 '+'a'*32},{'role':'assistant','content':'历史分析'}]
    assert notice_reference('那这对我意味着什么？',history)==history[0]['content']
    assert notice_reference('查看我的画像',history) is None
    history.append({'role':'user','content':'我的专业是计算机'})
    assert notice_reference('那这对我意味着什么？',history) is None


def test_notification_count_is_a_read_only_summary_not_a_selection_request():
    async def scenario():
        engine,_,_,_,snap=await fixture()
        result=await ProfileAssistant(disabled_model()).ainvoke(initial('我有多少封未读通知',snap,
            notice_context={'items':[],'total':5,'unread_count':2}))
        assert '5 封' in result['answer'] and '2 封未读' in result['answer']
        assert not result['proposals'] and result['completion']['status']=='COMPLETED'
        await engine.dispose()
    asyncio.run(scenario())


def test_actual_chat_api_uses_profile_assistant_and_shared_approval(monkeypatch):
    api=importlib.import_module('opportunity_agent.v2.api.app')
    mode=replace(config.settings,app_mode='applyops')
    monkeypatch.setattr(config,'settings',mode);monkeypatch.setattr(api,'settings',mode)
    monkeypatch.setattr(api,'orchestrator',ProfileAssistant(disabled_model()))
    monkeypatch.setattr(api,'schedule_run',lambda *args:None)
    from opportunity_agent.llm_client import LLMClient
    monkeypatch.setattr(LLMClient,'enabled',property(lambda self:False))
    async def scenario():
        engine,factory,user,_,_=await fixture()
        monkeypatch.setattr(api,'SessionLocal',factory)
        async def session_dep():
            async with factory() as session:
                yield session
        api.app.dependency_overrides[get_session]=session_dep
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test') as client:
                registered=await client.post('/api/v1/auth/register',json={'email':'profile-chat@test.local','password':'long-enough-password'})
                owner=registered.json()['user']['id']
                conversation=(await client.post('/api/v1/conversations',json={'title':'画像'})).json()
                async def run(message):
                    request=uuid4().hex
                    started=await client.post(f"/api/v1/conversations/{conversation['id']}/runs",json={'message':message,'request_id':request})
                    identity=started.json()['run_id']
                    await api.execute_run(identity,owner,conversation['id'],message,request)
                    result=(await client.get('/api/v1/runs/'+identity)).json()
                    assert result['status']=='completed',result
                    result['run_id']=identity
                    return result
                result=await run('我的托福是105分')
                assert len(result['approval_ids'])==1
                assert (await client.get('/api/v1/profile')).json()['payload'].get('toefl_score') is None
                assert (await client.post('/api/v1/approvals/'+result['approval_ids'][0]+'/accept',json={})).status_code==200
                assert (await client.get('/api/v1/profile')).json()['payload']['toefl_score']==105
                queried=await run('查看我的画像')
                assert '105' in queried['answer'] and not queried['approval_ids']
                changed=await run('我的托福是110分')
                assert not changed['approval_ids'] and len(changed['conflict_ids'])==1
                conflicts=(await client.get('/api/v1/profile/conflicts')).json()
                assert len(conflicts)==1 and conflicts[0]['old_value']==105 and conflicts[0]['new_value']==110
                assert (await client.get('/api/v1/profile')).json()['payload']['toefl_score']==105
                assert (await client.post('/api/v1/profile/conflicts/'+conflicts[0]['conflict_id']+'/resolve',json={'choice':'old'})).status_code==200
                explanation=await run('解释最新一封官网变化通知')
                assert '没有找到' in explanation['answer'] and not explanation['approval_ids']
                monitored,_=await setup(factory,owner)
                await check(factory,owner,monitored);await process_one(factory,Fetcher({URL:html()}))
                await check(factory,owner,monitored);await process_one(factory,Fetcher({URL:html('December 15, 2026')}))
                await process_one(factory,agent=DecisionAgent())
                explained=await run('解释最新一封官网变化通知')
                assert 'December 15' in explained['answer']
                # A new notice arrives between questions: pronoun follow-up must
                # stay pinned to the notice actually explained in this session.
                await check(factory,owner,monitored);await process_one(factory,Fetcher({URL:html('December 18, 2026')}))
                await process_one(factory,agent=DecisionAgent())
                followed=await run('那这对我意味着什么？')
                assert 'December 15' in followed['answer'] and 'December 18' not in followed['answer']
                assert (await client.get('/api/v3/inbox/unread-count')).json()['count']==2
                assert 'event: run_completed' in (await client.get('/api/v1/runs/'+result['run_id']+'/events')).text
        finally:
            api.app.dependency_overrides.pop(get_session,None)
            await engine.dispose()
    asyncio.run(scenario())
