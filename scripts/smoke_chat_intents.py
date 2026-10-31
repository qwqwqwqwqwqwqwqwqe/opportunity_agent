"""Real-model semantic-routing regression, synthetic messages, no DB writes.

Run: python scripts/smoke_chat_intents.py
Makes six billable model calls. Never prints credentials or personal records.
"""
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v3.chat_actions import handle_actions


async def main():
    client = LLMClient(timeout_seconds=25, retries=0)
    if not client.enabled:
        raise SystemExit('Model not configured')
    snapshot = {'today':'2026-10-29', 'availability':{'timezone':'Asia/Shanghai'}, 'applications':[],
                'offerings':[], 'requirements':[], 'materials':[], 'tasks':[], 'calendar_events':[]}
    async def run(message, expected, previous=None):
        started = time.perf_counter()
        result = await handle_actions(client, {'message':message, 'request_id':uuid4().hex,
            'workflow_snapshot':snapshot, 'chat_action_draft':previous})
        assert result and result.get('intent_recognition',{}).get('intent') == expected, result
        print(json.dumps({'intent':expected, 'passed':True, 'seconds':round(time.perf_counter()-started,2)}, ensure_ascii=False), flush=True)
        return result

    first = await run('增加一个cmu msml项目，截止日期11.18', 'actions')
    assert first['completion']['status'] == 'NEED_USER' and not first['proposals']
    draft = first['chat_action_draft']
    assert len(draft['projects']) == 1 and draft['projects'][0]['cycle'] is None
    assert draft['projects'][0]['deadline_date'] is None and draft['projects'][0]['deadline_text']
    second = await run('2027 Fall，截止日期是2026年11月18日', 'actions', draft)
    assert second['completion']['status'] == 'WAITING_APPROVAL', second
    command = second['proposals'][0]['payload']['commands'][0]
    assert command['type'] == 'project.save' and command['payload']['deadline_date'] == '2026-11-18'
    await run('我增加的项目有多少个？', 'query')
    await run('增加申请项目时会修改画像吗？', 'general_chat')
    await run('我做过一个机器学习项目，把经历补到画像', 'profile_update')
    last = await run('我的托福是多少？', 'query', draft)
    assert last['chat_passthrough'] and last['intent_recognition']['scope'] == 'profile'
    print(json.dumps({'cases_passed':6, 'model':client.model, 'database_writes':0}))


if __name__ == '__main__':
    asyncio.run(main())
