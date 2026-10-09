"""Authenticated V2 HTTP flow, including page, SSE, plan approval and tasks."""
from __future__ import annotations

import asyncio
import importlib

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v2.agents.orchestrator import (
    CustomOrchestrator, DeterministicSynthesizer, HeuristicGoalParser, HeuristicRouter,
)
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.session import get_session


def test_browser_api_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))
    api = importlib.import_module("opportunity_agent.v2.api.app")

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'web.db').as_posix()}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def session_override():
            async with factory() as session:
                yield session

        monkeypatch.setattr(api, "SessionLocal", factory)
        monkeypatch.setattr(api, "orchestrator", CustomOrchestrator(
            goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
            synthesizer=DeterministicSynthesizer(),
        ))
        api.app.dependency_overrides[get_session] = session_override
        try:
            async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://testserver") as client:
                assert (await client.get("/v2")).status_code == 200
                assert (await client.get("/v2/assets/app.js")).status_code == 200
                registered = await client.post("/api/v1/auth/register", json={
                    "email": "frontend@example.com", "password": "long-enough-password"})
                assert registered.status_code == 201
                profile = await client.get("/api/v1/profile")
                assert profile.status_code == 200
                saved = await client.patch("/api/v1/profile", json={"expected_version": profile.json()["version"],
                    "request_id": "manual-profile-1",
                    "payload": {"major": "Computer Science", "onboarding_completed": True,
                                "target_countries": ["US"], "target_degree": "MS", "target_fields": ["AI"],
                                "graduation_year": 2027, "planned_enrollment_year": 2028}})
                assert saved.status_code == 202
                assert saved.json()["status"] == "pending"
                assert (await client.get("/api/v1/profile")).json()["version"] == profile.json()["version"]
                accepted_profile = await client.post(f"/api/v1/approvals/{saved.json()['approval_id']}/accept", json={})
                assert accepted_profile.status_code == 200, accepted_profile.text
                assert (await client.get("/api/v1/profile")).json()["version"] == profile.json()["version"] + 1
                conversation = (await client.post("/api/v1/conversations", json={"title": "规划"})).json()
                created = await client.post(f"/api/v1/conversations/{conversation['id']}/runs", json={
                    "message": "请为我制定申请规划", "request_id": "http-plan-1"})
                assert created.status_code == 202
                run_id = created.json()["run_id"]
                for _ in range(250):
                    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
                    if run["status"] in {"completed", "failed"}:
                        break
                    await asyncio.sleep(0.02)
                assert run["status"] == "completed", run
                assert run["plan_result"]["tasks"]
                events = await client.get(f"/api/v1/runs/{run_id}/events")
                assert "event: agent_started" in events.text
                assert "event: run_completed" in events.text
                approvals = (await client.get("/api/v1/approvals")).json()
                assert len(approvals) == 1 and approvals[0]["proposal_type"] == "plan.replace"
                before = await client.get("/api/v1/plans/current")
                assert before.status_code == 404
                assert (await client.post(f"/api/v1/approvals/{approvals[0]['id']}/accept", json={})).status_code == 200
                plan = (await client.get("/api/v1/plans/current")).json()
                assert plan["version"] == 1 and plan["tasks"]
                task = plan["tasks"][0]
                requested = await client.post(f"/api/v1/tasks/{task['id']}/commands", json={
                    "action": "complete", "evidence": "项目已完成", "request_id": "http-task-1"})
                assert requested.status_code == 202
                approval_id = requested.json()["approval_id"]
                assert (await client.post(f"/api/v1/approvals/{approval_id}/accept", json={})).status_code == 200
                updated = (await client.get("/api/v1/plans/current")).json()
                assert updated["tasks"][0]["status"] == "completed"
                # Two conversational scores: first is approved normally; the
                # replacement is resolved through the dedicated conflict API.
                for index, score in enumerate((107, 110)):
                    started = await client.post(f"/api/v1/conversations/{conversation['id']}/runs", json={
                        "message": f"我托福{score}", "request_id": f"score-{index}"})
                    score_run = started.json()["run_id"]
                    for _ in range(250):
                        status = (await client.get(f"/api/v1/runs/{score_run}")).json()
                        if status["status"] in {"completed", "failed"}:
                            break
                        await asyncio.sleep(.02)
                    assert status["status"] == "completed", status
                    if index == 0:
                        pending = (await client.get("/api/v1/approvals")).json()
                        assert len(pending) == 1, status
                        assert (await client.post(f"/api/v1/approvals/{pending[0]['id']}/accept", json={})).status_code == 200
                conflicts = (await client.get("/api/v1/profile/conflicts")).json()
                assert len(conflicts) == 1
                assert conflicts[0]["old_value"] == 107 and conflicts[0]["new_value"] == 110
                path = f"/api/v1/profile/conflicts/{conflicts[0]['conflict_id']}/resolve"
                assert (await client.post(path, json={"choice": "new"})).status_code == 200
                assert (await client.post(path, json={"choice": "new"})).status_code == 200
                assert (await client.get("/api/v1/profile")).json()["payload"]["toefl_score"] == 110
                assert (await client.get("/api/v1/profile/conflicts")).json() == []
                async def broken_snapshot(*_args):
                    raise RuntimeError("snapshot unavailable")
                monkeypatch.setattr(api, "_execution_initial", broken_snapshot)
                failed = await client.post(f"/api/v1/conversations/{conversation['id']}/runs", json={
                    "message": "请更新规划", "request_id": "http-failed-snapshot-1"})
                assert failed.status_code == 202
                failed_run_id = failed.json()["run_id"]
                for _ in range(250):
                    failed_run = (await client.get(f"/api/v1/runs/{failed_run_id}")).json()
                    if failed_run["status"] == "failed":
                        break
                    await asyncio.sleep(0.02)
                assert failed_run["status"] == "failed"
                assert "event: run_failed" in (await client.get(f"/api/v1/runs/{failed_run_id}/events")).text
        finally:
            api.app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(scenario())
