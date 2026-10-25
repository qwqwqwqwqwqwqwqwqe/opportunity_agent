"""Offline browser-test driver. Refuses to touch any non-test database."""
import asyncio
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from opportunity_agent.v2.db.models import User
from opportunity_agent.v3.monitor_service import MonitorService
from opportunity_agent.v3.monitor_worker import process_one
from test_v3_official_monitor import Fetcher,DecisionAgent,html,URL


async def main():
    url=os.environ.get('DATABASE_URL','')
    if not url.startswith('sqlite+aiosqlite:///') or '.tmp-workspace-browser/monitor-browser.db' not in url.replace('\\','/'):
        raise RuntimeError('Only the isolated monitor browser-test database is allowed')
    email,stage=sys.argv[1:]
    engine=create_async_engine(url)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    async with factory.begin() as session:
        user=await session.scalar(select(User).where(User.email==email))
        from opportunity_agent.v3.monitor_models import Monitor
        monitor=await session.scalar(select(Monitor).where(Monitor.user_id==user.id))
        monitor.last_manual_at=None
        await MonitorService(session,user.id).enqueue_check(monitor.application_id,'browser-'+stage)
    await process_one(factory,Fetcher({URL:html('December 15, 2026') if stage=='after' else html()}))
    if stage=='after':
        await process_one(factory,agent=DecisionAgent())
    await engine.dispose()


asyncio.run(main())
