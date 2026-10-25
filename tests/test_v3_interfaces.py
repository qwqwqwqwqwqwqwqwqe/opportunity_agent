from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from uuid import uuid4
import pytest
import httpx
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from sqlalchemy import create_engine, inspect, text, select
from opportunity_agent.v2.api.dependencies import current_user
from opportunity_agent.v2.db.session import get_session
from opportunity_agent.v3.api import router, workflow_mode
from opportunity_agent.v3.orchestrator import WorkflowOrchestrator
from opportunity_agent.llm_client import LLMClient
from test_v3_workflow import database, setup_application, execute, snapshot_fixture


ROOT = Path(__file__).resolve().parents[1]


def test_empty_database_upgrades_to_head(tmp_path, monkeypatch):
    target = tmp_path / "empty.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///" + target.as_posix())
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    with create_engine("sqlite:///" + target.as_posix()).connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0013_official_monitor"
        assert "workflow_tasks" in inspect(conn).get_table_names()
        assert "user_projects" in inspect(conn).get_table_names()  # retained legacy history
        assert "uq_workflow_active_plan" in {i["name"] for i in inspect(conn).get_indexes("application_plans")}


def test_frozen_migration_handles_pre_v3_schema_and_keeps_data(tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "legacy.db").as_posix())
    spec = importlib.util.spec_from_file_location("workflow_migration", ROOT / "alembic/versions/0011_applyops_workflow.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id VARCHAR PRIMARY KEY)"))
        conn.execute(text("INSERT INTO users VALUES ('preserved')"))
        conn.execute(text("CREATE TABLE applications (id VARCHAR PRIMARY KEY, user_id VARCHAR)"))
        conn.execute(text("CREATE TABLE application_plans (id VARCHAR PRIMARY KEY, user_id VARCHAR, status VARCHAR)"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()  # re-entry validates rather than replacing tables
        assert conn.scalar(text("SELECT id FROM users")) == "preserved"
        assert {"offering_id", "priority"} <= {c["name"] for c in inspect(conn).get_columns("applications")}
        assert "workflow_command_receipts" in inspect(conn).get_table_names()
    engine.dispose()


def test_authenticated_api_ownership_commands_and_date_projection():
    async def scenario():
        engine, factory, user_id, other = await database()
        app = FastAPI()
        app.include_router(router)
        async def db():
            async with factory() as session:
                yield session
        from types import SimpleNamespace
        app.dependency_overrides[get_session] = db
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=user_id)
        app.dependency_overrides[workflow_mode] = lambda: None
        async with factory.begin() as session:
            _, _, private = await setup_application(session, other)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(f"/api/v3/applications/{private['id']}/materials")).status_code == 404
            body = {"request_id": "api-program", "commands": [{"type": "program.create", "payload": {"university": "A", "name": "B"}}]}
            created = await client.post("/api/v3/programs", json=body)
            assert created.status_code == 201, created.text
            assert (await client.post("/api/v3/programs", json=body)).json() == created.json()
            assert (await client.post("/api/v3/commands", json={**body,"actor_user_id":other})).status_code == 422
            body["commands"][0]["payload"]["name"] = "Conflict"
            assert (await client.post("/api/v3/programs", json=body)).status_code == 409
            event = {"request_id":"event", "commands":[{"type":"calendar_event.create", "payload": {
                "title":"Exam", "date":"2027-01-01", "date_precision":"date"}}]}
            assert (await client.post("/api/v3/calendar-events", json=event)).status_code == 201
            calendar = (await client.get("/api/v3/calendar?from=2027-01-01&to=2027-01-31")).json()
            assert calendar[0]["date"] == "2027-01-01" and calendar[0]["start_at"] is None
        await engine.dispose()
    asyncio.run(scenario())


def disabled_model():
    client = LLMClient(api_key="", retries=0)
    client.api_key = ""
    return client


def initial(message):
    snap = snapshot_fixture()
    snap["profile_version"] = 1
    return {"user_id":"u", "run_id":"r", "request_id":"request", "message":message,
            "workflow_snapshot":snap, "conversation_context":{}}


def test_smalltalk_has_no_domain_calls_or_proposals():
    result = asyncio.run(WorkflowOrchestrator(client=disabled_model()).ainvoke(initial("你好，今天心情怎么样")))
    assert result["route_decision"]["mode"] == "direct_reply"
    assert result["proposals"] == [] and result["results"] == {}
    assert result["completion"]["status"] == "COMPLETED"


def test_profile_intake_waits_for_approval_and_defers_planning():
    result = asyncio.run(WorkflowOrchestrator(client=disabled_model()).ainvoke(initial("我的托福是105分，帮我生成计划")))
    assert result["completion"]["status"] == "WAITING_APPROVAL"
    assert result["proposals"][0]["type"] == "workflow.commands"
    assert result["proposals"][0]["payload"]["commands"][0]["type"] == "profile.change"
    assert result["deferred_actions"] == ["planning"]
    assert "planning" not in result["results"]


def test_explicit_preferences_are_proposed_without_becoming_smalltalk_writes():
    result=asyncio.run(WorkflowOrchestrator(client=disabled_model()).ainvoke(initial("我不想考GRE")))
    assert result["route_decision"]["agents"]==["intake"]
    assert result["completion"]["status"]=="WAITING_APPROVAL"
    assert result["proposals"][0]["payload"]["commands"][0]["type"]=="preference.change"


def test_missing_agent_retry_retains_success_and_does_not_repeat_it():
    calls = []
    async def flaky(message,snapshot):
        calls.append("review")
        if len(calls) == 1:
            raise ValueError("invalid output")
        return {"completeness":"unknown", "missing_requirements":[]}
    result = asyncio.run(WorkflowOrchestrator(client=disabled_model(),agents={"review":flaky}).ainvoke(initial("检查材料")))
    assert calls == ["review","review"]
    assert result["completion"]["status"] == "COMPLETED"


def test_failed_agent_returns_partial_or_failure_honestly():
    async def broken(message,snapshot):
        raise TimeoutError("unavailable")
    result = asyncio.run(WorkflowOrchestrator(client=disabled_model(),agents={"review":broken}).ainvoke(initial("检查材料")))
    assert result["completion"]["status"] == "FAILED"
    assert result["failures"]


def test_repair_only_repeats_missing_review_and_retains_planning_result():
    calls={"planning":0,"review":0}
    async def planner(message,snapshot):
        calls["planning"]+=1
        return {"items":[{"task_id":"t1"}],"conflicts":[]}
    async def reviewer(message,snapshot):
        calls["review"]+=1
        if calls["review"]==1:
            raise ValueError("bad output")
        return {"completeness":"unknown","missing_requirements":[]}
    result=asyncio.run(WorkflowOrchestrator(client=disabled_model(),agents={"planning":planner,"review":reviewer})
                       .ainvoke(initial("生成计划并检查材料")))
    assert calls=={"planning":1,"review":2}
    assert result["results"]["planning"]["items"]==[{"task_id":"t1"}]
    assert len(result["result_history"])==3
    assert result["completion"]["status"]=="WAITING_APPROVAL"


def test_ambiguous_intake_requests_user_input_without_running_dependent_agents():
    async def ambiguous(message,snapshot):
        return {"ambiguities":["请指定申请周期"],"missing_fields":[],"proposed_commands":[]}
    async def forbidden(message,snapshot):
        raise AssertionError("planning must wait for clarification")
    result=asyncio.run(WorkflowOrchestrator(client=disabled_model(),agents={"intake":ambiguous,"planning":forbidden})
                       .ainvoke(initial("修改项目并生成计划")))
    assert result["completion"]["status"]=="NEED_USER"
    assert not result["proposals"] and "planning" not in result["results"]


def test_ungrounded_semantic_commands_are_not_proposed():
    output={"proposed_commands":[{"type":"program.create","payload":{"university":"A","name":"B"}}],
            "evidence_refs":[{"quote":"invented evidence"}]}
    client=LLMClient(completion_fn=lambda _:json.dumps(output),retries=0)
    result=asyncio.run(WorkflowOrchestrator(client=client).ainvoke(initial("新增一个项目")))
    assert not result["proposals"] and result["completion"]["status"]=="FAILED"
