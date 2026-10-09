import asyncio
import importlib

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import AgentRun, AgentEvent, Message
from opportunity_agent.v2.db.session import get_session


def test_run_listing_message_mapping_diagnostics_and_ownership(monkeypatch):
    api = importlib.import_module("opportunity_agent.v2.api.app")
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async def session_override():
            async with factory() as session:
                yield session
        api.app.dependency_overrides[get_session] = session_override
        try:
            async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://testserver") as client:
                registered = (await client.post("/api/v1/auth/register", json={
                    "email": "diagnostics@example.com", "password": "long-password"})).json()
                cid = (await client.post("/api/v1/conversations", json={"title": "诊断"})).json()["id"]
                async with factory.begin() as session:
                    session.add(AgentRun(id="r1", user_id=registered["user"]["id"], conversation_id=cid,
                        request_id="q1", status="failed", trace_id="a" * 32, graph_state={"error": "Router failed"}))
                    session.add(Message(id="m1", conversation_id=cid, role="user", content="学校列表",
                        request_id="q1", status="received"))
                    await session.flush()
                    session.add(AgentEvent(run_id="r1", sequence=1, event_type="router_diagnostics",
                        payload={"source": "failed", "attempts": [{"error_code": "empty_content"}]}))
                listed = await client.get(f"/api/v1/conversations/{cid}/runs")
                assert listed.status_code == 200 and listed.json()[0]["run_id"] == "r1"
                assert listed.json()[0]["trace_id"] == "a" * 32
                conversation = (await client.get(f"/api/v1/conversations/{cid}")).json()
                message = conversation["messages"][0]
                assert message["id"] == "m1" and message["run_id"] == "r1"
                assert message["run_status"] == "failed" and message["run_error"] == "Router failed"
                result = (await client.get("/api/v1/runs/r1")).json()
                assert result["routing_diagnostics"]["attempts"][0]["error_code"] == "empty_content"
                assert (await client.get(f"/api/v1/conversations/{cid}/runs?limit=0")).status_code == 422
                await client.post("/api/v1/auth/register", json={"email": "other-diagnostics@example.com", "password": "long-password"})
                assert (await client.get(f"/api/v1/conversations/{cid}/runs")).status_code == 404
                assert (await client.get("/api/v1/runs/r1")).status_code == 404
        finally:
            api.app.dependency_overrides.clear()
            await engine.dispose()
    asyncio.run(run())
