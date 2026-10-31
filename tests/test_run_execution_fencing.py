"""Lease ownership fences events and the entire final side-effect transaction."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db.models import User, Conversation, AgentRun, AgentEvent
from opportunity_agent.v2.repositories import Repository, VersionConflict


@pytest.mark.parametrize('expired',[False,True])
def test_replaced_or_expired_execution_cannot_append_or_commit(tmp_path,expired):
    async def scenario():
        engine=create_async_engine('sqlite+aiosqlite:///'+(tmp_path/'fencing.db').as_posix())
        factory=async_sessionmaker(engine,expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory.begin() as session:
            u=User(email=uuid4().hex+'@test.local',password_hash='test'); session.add(u); await session.flush()
            c=Conversation(user_id=u.id,title='test'); session.add(c); await session.flush()
            r=AgentRun(user_id=u.id,conversation_id=c.id,status='queued',graph_state={})
            session.add(r); await session.flush(); identity=r.id
        async with factory.begin() as session:
            repo=Repository(session)
            assert await repo.claim_run(identity,'old')
            await repo.append_event(identity,'owned',{},'old')
        async with factory.begin() as session:
            row=await session.get(AgentRun,identity)
            if expired:
                row.lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
            else:
                row.execution_token='new'
        for operation in ('event','commit'):
            with pytest.raises(VersionConflict):
                async with factory.begin() as session:
                    repo=Repository(session)
                    if operation=='event':
                        await repo.append_event(identity,'stale',{},'old')
                    else:
                        await repo.lock_run_execution(identity,'old')
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(AgentEvent))==1
            assert (await session.get(AgentRun,identity)).event_sequence==1
        if not expired:
            async with factory.begin() as session:
                repo=Repository(session)
                row=await repo.lock_run_execution(identity,'new')
                await repo.append_event(identity,'new_owner',{},'new')
                await repo.finalize_run(row,{'answer':'done'},'completed','new')
        await engine.dispose()
    asyncio.run(scenario())
