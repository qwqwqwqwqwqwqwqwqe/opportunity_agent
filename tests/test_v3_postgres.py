"""Opt-in PostgreSQL concurrency checks in an isolated, disposable schema.

APPLYOPS_TEST_DATABASE_URL must point to a dedicated test database. Existing
application tables are never read or dropped.
"""
import asyncio
import os
from uuid import uuid4
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import User
from opportunity_agent.v3 import models as m
from opportunity_agent.v3.contracts import Command, CommandBatch
from opportunity_agent.v3.service import WorkflowError, WorkflowService


@pytest.mark.skipif(not os.getenv("APPLYOPS_TEST_DATABASE_URL"),reason="requires dedicated PostgreSQL test database")
def test_postgres_concurrent_idempotency_and_compare_and_swap():
    async def scenario():
        url=os.environ["APPLYOPS_TEST_DATABASE_URL"]
        if not url.startswith("postgresql+asyncpg://"):
            pytest.fail("APPLYOPS_TEST_DATABASE_URL must use postgresql+asyncpg")
        schema="applyops_test_"+uuid4().hex
        admin=create_async_engine(url)
        engine=create_async_engine(url,connect_args={"server_settings":{"search_path":schema+",public"}})
        try:
            async with admin.begin() as conn:
                await conn.execute(text('CREATE SCHEMA "'+schema+'"'))
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory=async_sessionmaker(engine,expire_on_commit=False)
            async with factory.begin() as session:
                user=User(email=uuid4().hex+"@test.local",password_hash="not-a-password")
                session.add(user)
            batch=CommandBatch(request_id="concurrent",commands=[Command(type="program.create",payload={"university":"A","name":"B"})])
            async def submit(body):
                async with factory.begin() as session:
                    return await WorkflowService(session).execute(user.id,body)
            first,second=await asyncio.gather(submit(batch),submit(batch))
            assert first==second
            identity=first["changed_resources"][0]["id"]
            changes=[CommandBatch(request_id=str(i),commands=[Command(type="program.update",target_id=identity,
                expected_version=1,payload={"name":name})]) for i,name in enumerate(["X","Y"])]
            results=await asyncio.gather(*(submit(body) for body in changes),return_exceptions=True)
            assert sum(isinstance(r,WorkflowError) and r.status==409 for r in results)==1
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(m.Program))==1
                assert (await session.get(m.Program,identity)).version==2
            # The monitor uses the same short user-scoped serialization point.
            # Duplicate requests and competing workers must still emit one message.
            from opportunity_agent.v3.monitor_service import MonitorService
            from opportunity_agent.v3.monitor_contracts import MonitorConfig
            from opportunity_agent.v3.monitor_worker import process_one as monitor_one
            from test_v3_official_monitor import Fetcher,DecisionAgent,html,URL
            async with factory.begin() as session:
                service=WorkflowService(session)
                async def create(kind,payload):
                    response=await service.execute(user.id,CommandBatch(request_id=uuid4().hex,
                        commands=[Command(type=kind,payload=payload)]))
                    return response['changed_resources'][0]
                offering=await create('offering.create',{'program_id':identity,'cycle':'2027 Fall'})
                application=await create('application.create',{'offering_id':offering['id']})
                await MonitorService(session,user.id).configure(application['id'],MonitorConfig(
                    request_id='monitor',project_url=URL,confirmed=True,enabled=True))
            async def enqueue():
                async with factory.begin() as session:
                    return await MonitorService(session,user.id).enqueue_check(application['id'],'same-request')
            checks=await asyncio.gather(enqueue(),enqueue())
            assert checks[0]['id']==checks[1]['id']
            await asyncio.gather(monitor_one(factory,Fetcher({URL:html()})),monitor_one(factory,Fetcher({URL:html()})))
            async with factory.begin() as session:
                monitor=await MonitorService(session,user.id).get(application['id'])
                monitor.last_manual_at=None
                await MonitorService(session,user.id).enqueue_check(application['id'],'changed')
            await monitor_one(factory,Fetcher({URL:html('December 15, 2026')}))
            await asyncio.gather(monitor_one(factory,agent=DecisionAgent()),monitor_one(factory,agent=DecisionAgent()))
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(m.Notification)
                    .where(m.Notification.type=='official_impact'))==1
        finally:
            await engine.dispose()
            # The target is an explicit UUID-named schema created above.
            async with admin.begin() as conn:
                await conn.execute(text('DROP SCHEMA IF EXISTS "'+schema+'" CASCADE'))
            await admin.dispose()
    asyncio.run(scenario())
