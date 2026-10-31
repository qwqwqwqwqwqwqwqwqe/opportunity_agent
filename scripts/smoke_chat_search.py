"""Real MCP discovery + separate real-LLM fixture summary; never reads live URLs.

Uses two model calls, one/two Tavily queries, no DB writes. Real candidate URLs
are printed for review; only a synthetic fixture receives simulated consent.
"""
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v3.chat_actions import handle_actions
from opportunity_agent.v3.chat_search import ChatSearchService, SearchRequest
from opportunity_agent.v3.monitor_web import parse_html


class FixtureDiscovery:
    async def search(self,query,domains):
        return {'results':[{'url':'https://fixture.example.edu/program','title':'Synthetic admissions fixture'}]}


class FixtureFetcher:
    async def fetch(self,url,hosts):
        assert url=='https://fixture.example.edu/program'
        return parse_html('<main><h1>Synthetic MS Program Admissions</h1><p>For Fall 2027 applicants, '
            'the application deadline is December 15, 2026. Three letters of recommendation are required. '
            'This is an artificial offline test fixture, not a real university policy.</p></main>',url)


async def main():
    client = LLMClient(timeout_seconds=25,retries=0)
    initial = {'message':'帮我查 CMU MSML 的官网申请材料要求','request_id':uuid4().hex,
               'workflow_snapshot':{'today':'2026-10-29','applications':[],'profile':{}}}
    real = await handle_actions(client,initial)
    assert real['intent_recognition']['intent']=='search' and real['chat_search_pending'],real
    assert not any(t['tool']=='read_official_page' for t in real['chat_search_trace'])
    print(json.dumps({'real_tavily_mcp_search':'passed','read_before_user_confirmation':False,
        'candidates':[{'title':c['title'],'url':c['url']} for c in real['chat_search_pending']['candidates']]},ensure_ascii=False),flush=True)
    # The live search stops here. No user consent is fabricated for live pages.
    service = ChatSearchService(client,FixtureDiscovery(),FixtureFetcher())
    test_initial = {**initial,'message':'测试网页中 Fall 2027 的截止日期和推荐信数量是什么？'}
    pending = await service.handle(SearchRequest(search_keywords='synthetic admissions fixture'),test_initial)
    confirmed = await handle_actions(client,{**test_initial,'message':'确认第1个',
        'chat_search_pending':pending['chat_search_pending']},service)
    assert confirmed['completion']['status']=='COMPLETED',confirmed
    assert confirmed['chat_search_result']['summary']['claims']
    assert not confirmed['proposals']
    print(json.dumps({'real_model_fixture_summary':'passed','model':client.model,
        'claim_count':len(confirmed['chat_search_result']['summary']['claims']),'database_writes':0}),flush=True)


if __name__=='__main__':
    asyncio.run(main())
