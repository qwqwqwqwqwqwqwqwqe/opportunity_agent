"""Existing-project chat updates: stable IDs, partial merge and approvals."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import importlib
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from opportunity_agent.v3.chat_actions import ChatDraft, build_commands
from opportunity_agent.v3.contracts import CommandBatch
from opportunity_agent.v3.service import WorkflowService, WorkflowError
from opportunity_agent.v3.workspace import project_cards
from opportunity_agent.v3.profile_assistant import ProfileAssistant
from opportunity_agent.v2.core import config
from opportunity_agent.v2.db.session import get_session
from opportunity_agent.v2.db.models import Profile
from test_v3_workflow import database, execute
from test_v3_chat_actions import ScriptedModel


MESSAGE = 'cmu msml项目的材料要求是一个sop,三个推荐信，托福，可选gre，成绩单，简历，其中gre已经提交，其他都准备好了'


async def seed(session, user, name='MSML', cycle='2027 Fall', extra=False):
    requirements = [
        {'title':'SOP','materials':[{'slot_key':'SOP文档','status':'missing'}]},
        {'title':'推荐信','kind':'recommendation','required_count':3,
         'materials':[{'slot_key':f'教授{i}', 'status':'missing'} for i in range(1,4)]},
        {'title':'简历 CV','materials':[{'slot_key':'简历原稿','status':'missing'}]},
    ]
    if extra:
        requirements.append({'title':'作品集','materials':[{'slot_key':'作品集原稿','status':'submitted'}]})
    return await execute(session,user,'project.save',{'request_id':uuid4().hex,'university':'CMU','name':name,'cycle':cycle,
        'deadline_date':'2026-12-15','date_precision':'date','source_url':'https://example.edu/program',
        'notes':'保留的项目备注', 'requirements':requirements})


def update(app_id=None, ref='msml'):
    return {'ref':ref, 'operation':'update','application_id':app_id,'university':'CMU','name':'MSML',
            'changes':{'gre_policy':'optional','requirements':[
                {'title':'SOP','kind':'document','required_count':1,'status':'ready'},
                {'title':'推荐信','kind':'recommendation','required_count':3,'status':'ready'},
                {'title':'托福','kind':'score','required_count':1,'criteria':{'profile_field':'toefl_score'},'status':'ready'},
                {'title':'GRE','kind':'score','required_count':1,'obligation':'optional','criteria':{'profile_field':'gre_score'},'status':'submitted'},
                {'title':'成绩单','kind':'document','required_count':1,'status':'ready'},
                {'title':'简历','kind':'document','required_count':1,'status':'ready'},
            ]}}


def build(projects, snapshot, request=None):
    commands, lines, questions = build_commands(ChatDraft.model_validate({'projects':projects}), snapshot, request or uuid4().hex)
    return commands, lines, questions


def test_user_material_update_reuses_project_requirements_and_slots():
    async def scenario():
        engine,factory,user,_ = await database()
        async with factory.begin() as session:
            app = await seed(session,user)
            snapshot = await WorkflowService(session).snapshot(user)
            before = deepcopy(snapshot)
            commands, lines, questions = build([update(app['id'])], snapshot)
            assert not questions and commands[0]['target_id'] == app['id']
            assert snapshot == before  # Builder never mutates state or writes.
            assert commands[0]['payload']['deadline_date'] == '2026-12-15'
            await WorkflowService(session).execute(user, CommandBatch(request_id=uuid4().hex,commands=commands), source='chat')
            after = await WorkflowService(session).snapshot(user)
            assert len(after['applications']) == 1 and after['applications'][0]['id'] == app['id']
            assert after['offerings'][0]['gre_policy'] == 'optional'
            assert after['offerings'][0]['requirements_json']['notes'] == '保留的项目备注'
            assert {r['id'] for r in before['requirements']} <= {r['id'] for r in after['requirements']}
            assert {r['id'] for r in before['materials']} <= {r['id'] for r in after['materials']}
            assert len(after['requirements']) == 6 and len(after['materials']) == 8
            assert sum(s['status']=='ready' for s in after['materials']) == 7
            assert sum(s['status']=='submitted' for s in after['materials']) == 1
            rec = next(r for r in after['requirements'] if r['kind']=='recommendation')
            assert [s['slot_key'] for s in after['materials'] if s['requirement_id']==rec['id']] == ['教授1','教授2','教授3']
            card = project_cards(after)[0]
            assert card['progress']['completed'] == card['progress']['total'] == 5
            assert after['profile'] == before['profile'] == {} and not after['tasks']
            assert '已准备 3 份' in '\n'.join(lines) and '已提交 1 份' in '\n'.join(lines)
        await engine.dispose()
    asyncio.run(scenario())


def test_partial_update_retains_unmentioned_requirement_submissions_and_criteria():
    async def scenario():
        engine,factory,user,_ = await database()
        async with factory.begin() as session:
            app = await seed(session,user,extra=True)
            snapshot = await WorkflowService(session).snapshot(user)
            commands,_,questions = build([update(app['id'])],snapshot)
            assert not questions
            await WorkflowService(session).execute(user,CommandBatch(request_id=uuid4().hex,commands=commands))
            after = await WorkflowService(session).snapshot(user)
            portfolio = next(r for r in after['requirements'] if r['title']=='作品集')
            assert not portfolio['archived']
            assert next(s for s in after['materials'] if s['requirement_id']==portfolio['id'])['status']=='submitted'
            # A subsequent readiness update preserves earlier GRE submission,
            # unknown score threshold and existing requirement/slot identities.
            gre = next(r for r in after['requirements'] if r['title']=='GRE')
            entry = {'operation':'update','application_id':app['id'],'changes':{'requirements':[{'id':gre['id'],'status':'ready'}]}}
            commands,_,questions = build([entry],after)
            assert not questions
            await WorkflowService(session).execute(user,CommandBatch(request_id=uuid4().hex,commands=commands))
            final = await WorkflowService(session).snapshot(user)
            assert next(s for s in final['materials'] if s['requirement_id']==gre['id'])['status']=='submitted'
            assert next(r for r in final['requirements'] if r['id']==gre['id'])['criteria']['profile_field']=='gre_score'
        await engine.dispose()
    asyncio.run(scenario())


def test_multiple_cycles_require_disambiguation_even_with_guessed_id():
    async def scenario():
        engine,factory,user,_ = await database()
        async with factory.begin() as session:
            first = await seed(session,user)
            await seed(session,user,cycle='2028 Fall')
            snapshot = await WorkflowService(session).snapshot(user)
            commands,_,questions = build([update(first['id'])],snapshot)
            assert not commands and '多个申请周期' in questions[0]
            entry = update(first['id']);entry['cycle']='2027 Fall'
            commands,_,questions = build([entry],snapshot)
            assert not questions and commands[0]['target_id']==first['id']
            entry['cycle']='2028 Fall'
            commands,_,questions = build([entry],snapshot)
            assert not commands and '周期不一致' in questions[0]
        await engine.dispose()
    asyncio.run(scenario())


def test_explicit_replacement_previews_archival_and_keeps_history():
    async def scenario():
        engine,factory,user,_ = await database()
        async with factory.begin() as session:
            app = await seed(session,user,extra=True)
            snapshot = await WorkflowService(session).snapshot(user)
            portfolio = next(r for r in snapshot['requirements'] if r['title']=='作品集')
            entry = update(app['id']);entry['changes']['requirements_mode']='replace'
            commands,lines,questions = build([entry],snapshot)
            assert not questions and '将归档的要求：作品集' in '\n'.join(lines)
            await WorkflowService(session).execute(user,CommandBatch(request_id=uuid4().hex,commands=commands))
            final = await WorkflowService(session).snapshot(user)
            assert next(r for r in final['requirements'] if r['id']==portfolio['id'])['archived']
            assert len([r for r in final['requirements'] if not r['archived']])==6
            assert any(s['requirement_id']==portfolio['id'] for s in final['materials'])
        await engine.dispose()
    asyncio.run(scenario())


def test_individual_recommender_status_and_score_threshold_preview():
    async def scenario():
        engine,factory,user,_ = await database()
        async with factory.begin() as session:
            app = await seed(session,user)
            snapshot = await WorkflowService(session).snapshot(user)
            rec = next(r for r in snapshot['requirements'] if r['kind']=='recommendation')
            slot = next(s for s in snapshot['materials'] if s['requirement_id']==rec['id'])
            entry = {'operation':'update','application_id':app['id'],'changes':{'requirements':[
                {'id':rec['id'],'materials':[{'id':slot['id'],'status':'submitted'}]},
                {'title':'托福','kind':'score','criteria':{'profile_field':'toefl_score','minimum':105}},
            ]}}
            commands,lines,questions = build([entry],snapshot)
            assert not questions and '托福 ≥ 105' in '\n'.join(lines)
            await WorkflowService(session).execute(user,CommandBatch(request_id=uuid4().hex,commands=commands))
            final = await WorkflowService(session).snapshot(user)
            slots = [s for s in final['materials'] if s['requirement_id']==rec['id']]
            assert sum(s['status']=='submitted' for s in slots)==1 and len(slots)==3
            assert next(s for s in slots if s['id']==slot['id'])['status']=='submitted'
        await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize('tamper', ['application','requirement','material'])
def test_foreign_or_cross_project_ids_cannot_be_used(tamper):
    async def scenario():
        engine,factory,user,other = await database()
        async with factory.begin() as session:
            app = await seed(session,user)
            private = await seed(session,other)
            snapshot = await WorkflowService(session).snapshot(user)
            private_snapshot = await WorkflowService(session).snapshot(other)
            entry = update(app['id'])
            if tamper=='application':
                entry['application_id']=private['id']
            elif tamper=='requirement':
                entry['changes']['requirements'][0]['id']=private_snapshot['requirements'][0]['id']
            else:
                entry['changes']['requirements'][0]['materials']=[{'id':private_snapshot['materials'][0]['id'],'status':'submitted'}]
            with pytest.raises(ValueError):
                build([entry],snapshot)
        await engine.dispose()
    asyncio.run(scenario())


def test_two_updates_share_starting_revision_but_stale_batch_rolls_back():
    async def scenario():
        engine,factory,user,_ = await database()
        async with factory.begin() as session:
            first = await seed(session,user)
            second = await seed(session,user,name='MSCS')
            snapshot = await WorkflowService(session).snapshot(user)
            entries = [{'ref':str(i), 'operation':'update','application_id':app['id'],'changes':{'notes':'更新备注'}}
                       for i,app in zip(('first','second'),(first,second))]
            commands,_,questions = build(entries,snapshot)
            assert not questions
            await WorkflowService(session).execute(user,CommandBatch(request_id='two-updates',commands=commands))
            final = await WorkflowService(session).snapshot(user)
            assert all(o['requirements_json']['notes']=='更新备注' for o in final['offerings'])
            # The old snapshot remains stale despite intra-batch revision adjustment.
            with pytest.raises(WorkflowError,match='已变化'):
                await WorkflowService(session).execute(user,CommandBatch(request_id='stale-updates',commands=commands))
            assert len(final['applications']) == 2
        await engine.dispose()
    asyncio.run(scenario())


def test_http_update_preview_confirm_cancel_and_stale_revision(monkeypatch):
    api = importlib.import_module('opportunity_agent.v2.api.app')
    mode = replace(config.settings,app_mode='applyops')
    monkeypatch.setattr(config,'settings',mode);monkeypatch.setattr(api,'settings',mode)
    monkeypatch.setattr(api,'schedule_run',lambda *args:None)
    from opportunity_agent.llm_client import LLMClient
    monkeypatch.setattr(LLMClient,'enabled',property(lambda self:False))
    model = ScriptedModel()
    monkeypatch.setattr(api,'orchestrator',ProfileAssistant(model))
    async def scenario():
        engine,factory,_,_ = await database()
        monkeypatch.setattr(api,'SessionLocal',factory)
        async def dep():
            async with factory() as session:
                yield session
        api.app.dependency_overrides[get_session] = dep
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test') as client:
                user = (await client.post('/api/v1/auth/register',json={'email':'project-update@test.local','password':'long-enough-password'})).json()['user']['id']
                conversation = (await client.post('/api/v1/conversations',json={'title':'修改材料'})).json()['id']
                async with factory.begin() as session:
                    app = await seed(session,user)
                async def run(message):
                    request = uuid4().hex
                    started = await client.post(f'/api/v1/conversations/{conversation}/runs',json={'message':message,'request_id':request})
                    identity = started.json()['run_id']
                    await api.execute_run(identity,user,conversation,message,request)
                    result = (await client.get('/api/v1/runs/'+identity)).json()
                    assert result['status']=='completed',result
                    return result
                model.drafts.append({'intent':'actions','projects':[update(app['id'])]})
                preview = await run(MESSAGE)
                assert preview['completion']['status']=='WAITING_APPROVAL' and '确认保存' in preview['answer']
                assert not preview['profile_result']['accepted_facts']
                before = (await client.get('/api/v3/snapshot')).json()
                assert len(before['requirements'])==3 and all(s['status']=='missing' for s in before['materials'])
                await run('确认修改')
                after = (await client.get('/api/v3/snapshot')).json()
                assert len(after['applications'])==1 and after['applications'][0]['id']==app['id']
                assert len(after['requirements'])==6 and len(after['materials'])==8
                assert all(s['evidence_ref'] for s in after['materials'])
                assert (await client.get('/api/v3/dashboard')).json()['projects'][0]['progress']['completed']==5
                # Cancel changes through chat; neither facts nor materials change.
                patch = {'operation':'update','application_id':app['id'],'changes':{'notes':'不会保存'}}
                model.drafts.append({'projects':[patch]})
                await run('修改项目备注');await run('取消修改')
                assert (await client.get('/api/v3/snapshot')).json()['offerings'][0]['requirements_json']['notes']=='保留的项目备注'
                model.drafts.append({'projects':[patch]})
                await run('修改项目备注')
                async with factory.begin() as session:
                    offering = (await WorkflowService(session).snapshot(user))['offerings'][0]
                    await execute(session,user,'offering.update',{'deadline_date':'2026-12-20'},offering['id'],offering['version'])
                failed = await run('确认保存')
                assert failed['completion']['status']=='NEED_USER' and '没有保存' in failed['answer']
                last = (await client.get('/api/v3/snapshot')).json()
                assert last['offerings'][0]['deadline_date']=='2026-12-20'
                assert last['offerings'][0]['requirements_json']['notes']=='保留的项目备注'
                assert not (await client.get('/api/v1/profile')).json()['payload'].get('toefl_score')
        finally:
            api.app.dependency_overrides.pop(get_session,None)
            await engine.dispose()
    asyncio.run(scenario())
