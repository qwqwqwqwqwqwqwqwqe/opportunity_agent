"""Atomic form, preparation-vs-submission and task/calendar integration."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from uuid import uuid4
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, func
from opportunity_agent.v2.api.dependencies import current_user
from opportunity_agent.v2.db.session import get_session
from opportunity_agent.v2.db.models import Application
from opportunity_agent.v3.api import router, workflow_mode
from opportunity_agent.v3 import models as m
from opportunity_agent.v3.contracts import Command, CommandBatch
from opportunity_agent.v3.engine import review, score_check
from opportunity_agent.v3.workspace import project_cards, project_form
from opportunity_agent.v3.service import WorkflowService, WorkflowError
from opportunity_agent.v3.worker import dispatch, process_one
from test_v3_workflow import database, execute


def form():
    return {"request_id":"project-form", "university":"CMU", "name":"MSAII", "cycle":"2027 Fall",
            "deadline_date":"2027-02-01", "date_precision":"date", "notes":"用户提供的材料要求",
            "requirements":[{"title":"简历 CV", "required_count":1, "materials":[{"slot_key":"CV","status":"ready"}]},
                            {"title":"推荐信", "kind":"recommendation", "required_count":3,
                             "materials":[{"slot_key":str(i),"status":"ready" if i<2 else "missing"} for i in range(3)]}]}


async def save(session,user,data=None,target=None):
    payload=data or form()
    receipt=await WorkflowService(session).execute(user,CommandBatch(request_id=payload["request_id"],commands=[
        Command(type="project.save",target_id=target,payload=payload)]))
    return receipt["changed_resources"][0]


def test_one_form_creates_complete_application_and_replays_once():
    async def scenario():
        engine,factory,user,_=await database()
        async with factory.begin() as session:
            created=await save(session,user)
            assert await save(session,user)==created
            snap=await WorkflowService(session).snapshot(user)
            assert len(snap["applications"])==1 and len(snap["requirements"])==2 and len(snap["materials"])==4
            card=project_cards(snap)[0]
            assert card["progress"]["completed"]==1 and card["progress"]["total"]==2
            assert card["requirements"][1]["prepared"]==2
            # Preparation counts ready, while submission review still rejects it.
            assert review(snap)["completeness"]=="incomplete"
        await engine.dispose()
    asyncio.run(scenario())


def test_project_form_update_retains_ids_archives_removed_groups_and_rejects_stale():
    async def scenario():
        engine,factory,user,_=await database()
        async with factory.begin() as session:
            created=await save(session,user)
            data=await project_form(WorkflowService(session),user,created["id"])
            del data["application_id"]
            old_req=data["requirements"][0]["id"];old_material=data["requirements"][0]["materials"][0]["id"]
            data["request_id"]="edit";data["requirements"]=data["requirements"][:1]
            data["requirements"][0]["title"]="最终简历";data["requirements"][0]["materials"][0]["status"]="submitted"
            await save(session,user,data,created["id"])
            active=await project_form(WorkflowService(session),user,created["id"])
            assert active["requirements"][0]["id"]==old_req and active["requirements"][0]["materials"][0]["id"]==old_material
            assert active["requirements"][0]["title"]=="最终简历"
            assert len(active["requirements"])==1
            archived=await session.scalar(select(m.Requirement).where(m.Requirement.archived.is_(True)))
            assert archived is not None
            data["request_id"]="stale"
            with pytest.raises(WorkflowError,match="已变化"):
                await save(session,user,data,created["id"])
        await engine.dispose()
    asyncio.run(scenario())


def test_foreign_requirement_in_form_rolls_back_all_prior_changes():
    async def scenario():
        engine,factory,user,other=await database()
        async with factory.begin() as session:
            foreign=await save(session,other)
            requirement=await session.scalar(select(m.Requirement).where(m.Requirement.user_id==other))
            body=form();body["requirements"][0]["id"]=requirement.id
            with pytest.raises(WorkflowError):
                await save(session,user,body)
            assert not await session.scalar(select(Application).where(Application.user_id==user))
            assert not await session.scalar(select(m.Program).where(m.Program.user_id==user))
        await engine.dispose()
    asyncio.run(scenario())


def test_language_alternatives_are_or_and_unknown_subscores_never_pass():
    criteria={"any_of":[{"profile_field":"toefl_score","minimum":100},{"profile_field":"ielts_score","minimum":7}]}
    snapshot={"today":"2026-10-20","profile":{"ielts_score":7.5}}
    assert score_check(criteria,snapshot)[0] is True
    criteria["any_of"][1]["subscores"]={"writing":7}
    assert score_check(criteria,snapshot)[0] is None
    snapshot["profile"]["ielts_subscores"]={"writing":7.5}
    assert score_check(criteria,snapshot)[0] is True


def test_calendar_visibility_requires_a_real_task_date():
    from opportunity_agent.v3.contracts import TaskData
    with pytest.raises(ValueError,match="需要填写日期"):
        TaskData(stable_key='no-date',title='Cannot display',show_in_calendar=True)
    assert TaskData(stable_key='start-only',title='Start',start_date='2027-01-01',date_certainty='confirmed',show_in_calendar=True)


def test_unknown_requirements_never_produce_full_preparation():
    async def scenario():
        engine,factory,user,_=await database()
        async with factory.begin() as session:
            data=form();data["requirements"][1]["obligation"]="unknown"
            await save(session,user,data)
            card=project_cards(await WorkflowService(session).snapshot(user))[0]
            assert card["progress"]["unknown"]==1 and not card["progress"]["fully_prepared"]
        await engine.dispose()
    asyncio.run(scenario())


def test_calendar_completion_removes_only_one_item_undo_restores_and_no_material_inference():
    async def scenario():
        engine,factory,user,_=await database()
        app=FastAPI();app.include_router(router)
        from types import SimpleNamespace
        async def db():
            async with factory() as session:yield session
        app.dependency_overrides[get_session]=db
        app.dependency_overrides[current_user]=lambda:SimpleNamespace(id=user)
        app.dependency_overrides[workflow_mode]=lambda:None
        async with factory.begin() as session:
            created=await save(session,user)
            a=await execute(session,user,"task.create",{"application_id":created["id"],"stable_key":"a","title":"SOP 最终版",
                "due_date":"2027-01-10","date_certainty":"confirmed","show_in_calendar":True})
            b=await execute(session,user,"task.create",{"stable_key":"b","title":"Global","due_date":"2027-01-10",
                "date_certainty":"confirmed","show_in_calendar":True})
            await execute(session,user,"task.create",{"stable_key":"hidden","title":"Not on calendar","due_date":"2027-01-10","date_certainty":"confirmed"})
            await execute(session,user,"calendar_event.create",{"title":"Exam","date":"2027-01-10","date_precision":"date"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            before=(await client.get('/api/v3/calendar?from=2027-01-10&to=2027-01-10')).json()
            assert len(before)==3
            body={"request_id":"done","commands":[{"type":"task.update","target_id":a["id"],"expected_version":1,"payload":{"status":"completed"}}]}
            response=await client.post('/api/v3/commands',json=body)
            assert response.status_code==200,response.text
            completed=response.json()["changed_resources"][0]
            after=(await client.get('/api/v3/calendar?from=2027-01-10&to=2027-01-10')).json()
            assert len(after)==2 and all(item["id"]!=a["id"] for item in after)
            async with factory() as session:
                event=await session.scalar(select(m.DomainEvent).where(m.DomainEvent.type=="task.completed"))
                assert event.payload["task_version"]==2 and event.payload["status_before"]=="planned"
                assert (await session.get(m.Task,a["id"])).show_in_calendar
                assert project_cards(await WorkflowService(session).snapshot(user))[0]["progress"]["completed"]==1
            body={"request_id":"undo","commands":[{"type":"task.update","target_id":a["id"],"expected_version":2,"payload":{"status":"planned"}}]}
            assert (await client.post('/api/v3/commands',json=body)).status_code==200
            assert len((await client.get('/api/v3/calendar?from=2027-01-10&to=2027-01-10')).json())==3
        await dispatch(factory)
        while await process_one(factory):pass
        async with factory() as session:
            assert project_cards(await WorkflowService(session).snapshot(user))[0]["progress"]["completed"]==1
        await engine.dispose()
    asyncio.run(scenario())


def test_frontend_directory_projection_is_consistent():
    script=Path(__file__).resolve().parents[1]/"opportunity_agent/v2/web/workspace-state.js"
    code="const u=require(process.argv[1]);const p=[{id:'a',university:'CMU',name:'A'},{id:'b',university:'CMU',name:'B'}];const t=[{id:'1',application_id:'a',status:'planned'},{id:'2',application_id:'b',status:'completed'},{id:'3',application_id:null,status:'planned'}];console.log(JSON.stringify({dir:u.directory(p,t),open:u.visibleTasks(t,'b')}));"
    result=subprocess.run(['node','-e',code,str(script)],capture_output=True,text=True,check=True)
    data=json.loads(result.stdout)
    assert len(data["dir"]["schools"])==1 and len(data["dir"]["schools"][0]["projects"])==2
    assert data["dir"]["global"]==1 and data["dir"]["schools"][0]["count"]==1 and data["open"]==[]


def test_sqlite_command_obeys_outer_rollback_and_concurrent_first_writes(tmp_path):
    async def scenario():
        engine,factory,user,_=await database('sqlite+aiosqlite:///'+(tmp_path/'concurrent.db').as_posix())
        async with factory() as session:
            await execute(session,user,"program.create",{"university":"Rollback","name":"Not committed"})
            await session.rollback()
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(m.Program))==0
        async def write(index):
            async with factory.begin() as session:
                return await execute(session,user,"task.create",{"stable_key":str(index),"title":f'Task {index}'})
        results=await asyncio.gather(*(write(i) for i in range(4)))
        assert len({row["id"] for row in results})==4
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(m.Task))==4
        await engine.dispose()
    asyncio.run(scenario())


def test_legacy_application_can_be_edited_without_replacing_its_id():
    async def scenario():
        engine,factory,user,_=await database()
        async with factory.begin() as session:
            old=Application(user_id=user,university='CMU',program='MSAII',intake='2027 Fall')
            session.add(old);await session.flush();identity=old.id
            data=await project_form(WorkflowService(session),user,identity)
            del data['application_id'];data['request_id']='legacy-edit'
            await save(session,user,data,identity)
            assert (await session.get(Application,identity)).offering_id
            assert await session.scalar(select(func.count()).select_from(Application))==1
        await engine.dispose()
    asyncio.run(scenario())


def test_workspace_migration_backfills_only_existing_open_dated_tasks(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine,text
    root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('experience_migration',root/'alembic/versions/0012_workspace_experience.py')
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    engine=create_engine('sqlite:///'+(tmp_path/'upgrade.db').as_posix())
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE workflow_requirements (id VARCHAR PRIMARY KEY, key VARCHAR)'))
        conn.execute(text("INSERT INTO workflow_requirements VALUES ('r','CV')"))
        conn.execute(text('CREATE TABLE workflow_materials (id VARCHAR PRIMARY KEY)'))
        conn.execute(text('CREATE TABLE workflow_tasks (id VARCHAR PRIMARY KEY,status VARCHAR,due_date DATE,start_date DATE)'))
        conn.execute(text("INSERT INTO workflow_tasks VALUES ('open','planned','2027-01-01',NULL),('done','completed','2027-01-01',NULL),('undated','planned',NULL,NULL)"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade();migration.upgrade()
        assert dict(conn.execute(text('SELECT id,show_in_calendar FROM workflow_tasks')).all())=={'open':1,'done':0,'undated':0}
        assert conn.scalar(text("SELECT title FROM workflow_requirements WHERE id='r'"))=='CV'
    engine.dispose()
