"""Executable business oracles; these are not human-labelled LLM Gold."""
from __future__ import annotations
import asyncio
from datetime import date, timedelta
import itertools
import pytest
from sqlalchemy import select, func
from opportunity_agent.v3 import models as m
from opportunity_agent.v3.contracts import Command, CommandBatch
from opportunity_agent.v3.engine import review, schedule
from opportunity_agent.v3.service import WorkflowService
from opportunity_agent.v2.services.applications import ApplicationCommandService
from test_v3_workflow import database, execute, setup_application, snapshot_fixture


@pytest.mark.parametrize("duration,start", list(itertools.product([1,2,3,5,10], ["2027-01-04","2027-01-08","2027-01-09","2027-01-10"])))
def test_planning_working_day_oracle(duration,start):
    snapshot=snapshot_fixture()
    snapshot["today"]=start
    snapshot["tasks"][0]["duration_days"]=duration
    result=schedule(snapshot)
    assert not result["conflicts"]
    item=result["items"][0]["schedule_snapshot"]
    a,b=date.fromisoformat(item["start_date"]),date.fromisoformat(item["due_date"])
    working_days=sum((a+timedelta(days=i)).weekday()<5 for i in range((b-a).days+1))
    assert a>=date.fromisoformat(start)
    assert a.weekday()<5 and b.weekday()<5
    assert working_days==duration
    assert b<=date(2027,2,1)


@pytest.mark.parametrize("count,status", list(itertools.product(range(1,6),["missing","invited","ready","submitted"])))
def test_material_requirement_oracle(count,status):
    snapshot=snapshot_fixture()
    snapshot["requirements"]=[{"id":"r","application_id":"a","key":"Letters","kind":"recommendation",
        "obligation":"required","required_count":count,"criteria":{},"source_id":None}]
    snapshot["materials"]=[{"id":f"m{i}","requirement_id":"r","slot_key":str(i),"status":status} for i in range(count)]
    result=review(snapshot)
    assert result["completeness"]==("complete" if status=="submitted" else "incomplete")
    assert "未验证" in result["coverage_note"]


@pytest.mark.parametrize("count,status,accept,offset", list(itertools.product(range(1,6),["ready","submitted"],[True,False],[0,1])))
def test_approval_database_effect_oracle(count,status,accept,offset):
    async def scenario():
        engine,factory,user,_=await database()
        async with factory.begin() as session:
            _,_,app=await setup_application(session,user)
            req=await execute(session,user,"requirement.create",{"application_id":app["id"],"key":"Letters",
                "kind":"recommendation","obligation":"required","required_count":count})
            existing=max(0,count-1-offset)
            for i in range(existing):
                await execute(session,user,"material.create",{"requirement_id":req["id"],"slot_key":str(i),"status":"submitted"})
            batch=CommandBatch(request_id="candidate",commands=[Command(type="material.create",payload={
                "requirement_id":req["id"],"slot_key":"new","status":status})])
            approval=await ApplicationCommandService(session).propose(user,"workflow.commands",batch.model_dump(mode="json"),"candidate")
            before=(await WorkflowService(session).snapshot(user))["revision"]
            await ApplicationCommandService(session).decide(approval.id,user,accept)
            snap=await WorkflowService(session).snapshot(user)
            assert len(snap["materials"])==existing+int(accept)
            assert snap["revision"]==before+int(accept)
            complete=existing+int(accept and status=="submitted")>=count
            assert review(snap)["completeness"]==("complete" if complete else "incomplete")
        await engine.dispose()
    asyncio.run(scenario())


def test_linked_intake_commands_are_atomic_and_resolve_server_generated_ids():
    async def scenario():
        engine,factory,user,_=await database()
        async with factory.begin() as session:
            result=await WorkflowService(session).execute(user,CommandBatch(request_id="linked",commands=[
                Command(type="program.create",ref="p",payload={"university":"A","name":"MSCS"}),
                Command(type="offering.create",ref="o",payload={"program_id":"$ref:p","cycle":"2027 Fall"}),
                Command(type="application.create",payload={"offering_id":"$ref:o"})]))
            p,o,a=result["changed_resources"]
            assert o["program_id"]==p["id"] and a["offering_id"]==o["id"]
        await engine.dispose()
    asyncio.run(scenario())


def test_evaluation_seed_is_never_mislabeled_as_human_gold():
    from opportunity_agent.v3.evaluation import seed_cases,evaluate
    from test_v3_interfaces import disabled_model
    data=seed_cases()
    assert len(data["cases"])==50
    assert all(row["human_review"]=="pending" for row in data["cases"])
    groups={}
    for row in data["cases"]:
        assert row["group"] not in groups or groups[row["group"]]==row["split"]
        groups[row["group"]]=row["split"]
    with pytest.raises(ValueError,match="independent human approval"):
        evaluate(data,disabled_model())
