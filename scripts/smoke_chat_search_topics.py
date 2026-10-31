"""Real LLM-generated Tavily queries, discovery only, no website reads/DB.

Uses three LLM calls and at most nine Tavily queries. Pipe into API python - to use
the configured runtime model without changing local .env.
"""
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v3.chat_actions import handle_actions


async def main():
    client=LLMClient(timeout_seconds=25,retries=0)
    for question in ['帮我查CMU MSML的课程','帮我查CMU MSML的官网申请材料要求',
                     '帮我查CMU MSML是否允许延期入学，以及国际生休学做实习的规定']:
        value=await handle_actions(client,{'message':question,'request_id':uuid4().hex,
            'workflow_snapshot':{'today':'2026-10-29','profile':{},'applications':[]}})
        request=value.get('chat_search_request') or {}
        assert value['intent_recognition']['intent']=='search' and request.get('search_keywords'),value
        assert 'topic' not in request and 'focus_terms' not in request,request
        trace=value['chat_search_trace']
        generated=[request['search_keywords'],*request['alternative_keywords']]
        assert trace and trace[0]['query']==request['search_keywords']
        assert all(t['query'] in generated and t['tool']=='tavily_search' for t in trace)
        pending=value.get('chat_search_pending') or {}
        print(json.dumps({'question':question,'search_keywords':request['search_keywords'],
            'alternative_keywords':request['alternative_keywords'],
            'trace':trace,'candidates':[{'title':c['title'],'url':c['url']}
                for c in pending.get('candidates',[])],
            'completion':value['completion']['status'],'website_reads':0,'database_writes':0},ensure_ascii=False),flush=True)


if __name__=='__main__':
    asyncio.run(main())
