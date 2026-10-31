"""Offline doubles injected ONLY by the isolated Compose entrypoints."""
import asyncio
import json
import os
import sys
import socket
from pathlib import Path
from datetime import timedelta as real_delta

assert os.environ.get('DATABASE_URL', '').endswith('@postgres:5432/applyops_concurrency'), 'test DB required'
from opportunity_agent.llm_client import LLMClient
LLMClient.enabled = property(lambda self: False)
from opportunity_agent.v3.app import app
from opportunity_agent.v2.db.session import SessionLocal
from opportunity_agent.v3 import monitor_worker as worker
from opportunity_agent.v3.monitor_web import parse_html, PageError
from opportunity_agent.v3.monitor_contracts import ImpactResult, OfficialEvidence, PersonalEvidence

CONTROL = Path('/control')
IDENTITY = sys.argv[1] if __name__ == '__main__' else 'api'


def config():
    try:
        return json.loads((CONTROL / 'settings.json').read_text())
    except FileNotFoundError:
        return {}


async def checkpoint(stage, job_id, token):
    print(json.dumps({'instance':socket.gethostname(),'worker':IDENTITY,
                      'stage':stage,'job_or_check_id':job_id}),flush=True)
    if stage == 'committed':
        from opportunity_agent.v3 import models as m
        async with SessionLocal() as session:
            job = await session.get(m.Job,job_id)
            if job.type != 'monitor.analyze':
                return
    state = config()
    if state.get('barrier_worker') == IDENTITY and state.get('barrier') == stage:
        marker = CONTROL / (IDENTITY + '.barrier')
        marker.write_text(json.dumps({'stage':stage,'job_id':job_id,'token':token}))
        while config().get('barrier') == stage:
            await asyncio.sleep(.1)


class Fetcher:
    async def fetch(self, url, hosts, *args):
        state = config()
        if state.get('fail_key'):
            counter = CONTROL / ('failures-' + state['fail_key'])
            count = int(counter.read_text()) if counter.exists() else 0
            if count < state.get('fail_count',0):
                counter.write_text(str(count+1))
                raise PageError('FETCH_TIMEOUT')
        if state.get('fetch_error'):
            raise PageError('FIXTURE_FAILURE')
        date = state.get('date', 'December 10, 2026')
        return parse_html('<main><h1>MSCS 2027 Fall Admissions</h1><p>Application Deadline: '
                          +date+'. All applicants must submit their complete application before the deadline.</p></main>', url)


class Agent:
    async def run(self, tools):
        if config().get('model_error'):
            raise RuntimeError('offline model failure')
        await tools.call('get_application_context', {})
        for identity in tools.configured_ids:
            await tools.call('read_official_page', {'snapshot_id':identity})
        change = tools.changes[0]
        await tools.call('read_official_page', {'snapshot_id':change['after_id']})
        block = tools.pages[change['after_id']]['blocks'][1]
        await checkpoint('analyzing', tools.check_id, '')
        return ImpactResult(check_id=tools.check_id, change_ids=[c['id'] for c in tools.changes],
            applicability='MATCH', impact='AFFECTED', direction='POSITIVE', categories=['deadline'],
            affected_items=['Deadline extended'], official_evidence_refs=[OfficialEvidence(
                snapshot_id=change['after_id'],block_id=block['id'],quote=block['text'])],
            personal_basis_refs=[PersonalEvidence(ref='application',value=tools.context['application'])],
            notification_title='Deadline changed', notification_body='See official evidence.'), True


async def renew(factory, job_id, token):
    from sqlalchemy import update
    from opportunity_agent.v3 import models as m
    while True:
        await asyncio.sleep(2)
        async with factory.begin() as session:
            result = await session.execute(update(m.Job).where(m.Job.id==job_id,
                m.Job.status=='running', m.Job.lease_token==token, m.Job.lease_until>worker.now())
                .values(lease_until=worker.now()+real_delta(seconds=8)))
            if result.rowcount != 1:
                return


async def main():
    # Short leases only in this test process; production defaults stay unchanged.
    worker.timedelta = lambda **kw: real_delta(seconds=8) if kw=={'seconds':240} else real_delta(**kw)
    worker._renew = renew
    while True:
        if IDENTITY in config().get('enabled', []):
            await worker.schedule_due(SessionLocal)
            await worker.process_one(SessionLocal, Fetcher(), Agent(), checkpoint)
        await asyncio.sleep(.2)


if __name__ == '__main__':
    asyncio.run(main())
