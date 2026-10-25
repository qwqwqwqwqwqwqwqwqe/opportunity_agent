"""Actual auth + conversation/run/SSE + shared approval + workflow HTTP flow."""
import asyncio
from dataclasses import replace
import importlib
from uuid import uuid4
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.session import get_session
from opportunity_agent.v2.core import config
from opportunity_agent.v3.profile_assistant import ProfileAssistant
from test_v3_interfaces import disabled_model


def test_applyops_authenticated_chat_and_manual_workflow(monkeypatch,tmp_path):
    api=importlib.import_module("opportunity_agent.v2.api.app")
    mode=replace(config.settings,app_mode="applyops")
    monkeypatch.setattr(config,"settings",mode)
    monkeypatch.setattr(api,"settings",mode)
    monkeypatch.setattr(api,"orchestrator",ProfileAssistant(client=disabled_model()))
    monkeypatch.setattr(api,"schedule_run",lambda *args:None)
    # Keep summaries/preferences/model calls deterministic in the integration test.
    from opportunity_agent.llm_client import LLMClient
    monkeypatch.setattr(LLMClient,"enabled",property(lambda self:False))

    async def scenario():
        engine=create_async_engine("sqlite+aiosqlite:///"+(tmp_path/"http.db").as_posix())
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory=async_sessionmaker(engine,expire_on_commit=False)
        monkeypatch.setattr(api,"SessionLocal",factory)
        async def db():
            async with factory() as session:
                yield session
        api.app.dependency_overrides[get_session]=db
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url="http://test") as client:
                assert (await client.get("/api/v3/dashboard")).status_code==401
                assert (await client.get("/v3")).status_code==200
                assert (await client.get("/v2/assets/applyops.js")).status_code==200
                registration=await client.post("/api/v1/auth/register",json={"email":"applyops@test.local","password":"long-enough-password"})
                assert registration.status_code==201,registration.text
                conversation=(await client.post("/api/v1/conversations",json={"title":"ApplyOps"})).json()
                for message,request in [("你好","hello"),("我的托福是105分，帮我生成计划","intake")]:
                    started=await client.post(f"/api/v1/conversations/{conversation['id']}/runs",json={"message":message,"request_id":request})
                    assert started.status_code==202,started.text
                    run_id=started.json()["run_id"]
                    await api.execute_run(run_id,registration.json()["user"]["id"],conversation["id"],message,request)
                    run=(await client.get(f"/api/v1/runs/{run_id}")).json()
                    assert run["status"]=="completed",run
                    if request=="hello":
                        assert run["route_decision"]["mode"]=="direct_reply" and run["approval_ids"]==[]
                    else:
                        assert run["completion"]["status"]=="WAITING_APPROVAL"
                        assert (await client.get("/api/v1/profile")).json()["payload"].get("toefl_score") is None
                        approval_id=run["approval_ids"][0]
                        accepted=await client.post(f"/api/v1/approvals/{approval_id}/accept",json={})
                        assert accepted.status_code==200,accepted.text
                        assert (await client.get("/api/v1/profile")).json()["payload"]["toefl_score"]==105
                    events=await client.get(f"/api/v1/runs/{run_id}/events")
                    assert "event: run_completed" in events.text
                # Legacy writes are blocked immediately, before a bad proposal exists.
                legacy=await client.post("/api/v1/applications",json={"university":"A","program":"MSCS","intake":"2027"})
                assert legacy.status_code==409
                async def command(kind,payload):
                    result=await client.post("/api/v3/commands",json={"request_id":uuid4().hex,"commands":[{"type":kind,"payload":payload}]})
                    assert result.status_code==200,result.text
                    return result.json()["changed_resources"][0]
                p=await command("program.create",{"university":"A","name":"MSCS"})
                o=await command("offering.create",{"program_id":p["id"],"cycle":"2027 Fall","deadline_date":"2099-12-01","date_precision":"date"})
                a=await command("application.create",{"offering_id":o["id"]})
                await command("task.create",{"application_id":a["id"],"title":"CV","stable_key":"cv"})
                draft=await client.post("/api/v3/plans/drafts",json={"request_id":"draft"})
                assert draft.status_code==409,draft.text
                assert (await client.get("/api/v3/plans")).json()==[]
                monitor=await client.put(f"/api/v3/applications/{a['id']}/monitor",json={
                    "request_id":"configure","project_url":"https://example.edu/admissions",
                    "confirmed":True,"enabled":True,"mode":"manual"})
                assert monitor.status_code==200,monitor.text
                requested=await client.post(f"/api/v3/applications/{a['id']}/monitor/checks",json={"request_id":"first"})
                assert requested.status_code==202,requested.text
                assert (await client.get("/api/v3/inbox/unread-count")).json()=={"count":0}
        finally:
            api.app.dependency_overrides.pop(get_session,None)
            await engine.dispose()
    asyncio.run(scenario())
