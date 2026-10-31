"""Three real-model existing-project cases, synthetic snapshot, no DB writes.

Run in the configured environment, or pipe into the API container's python -.
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
from opportunity_agent.v3.chat_project_updates import title_key


async def main():
    client = LLMClient(timeout_seconds=25, retries=0)
    snapshot = {'revision':4,'today':'2026-10-29','availability':{'timezone':'Asia/Shanghai'},'profile':{},
        'programs':[{'id':'program1','university':'CMU','name':'MSML','degree':'MS','home_unit':'ML','aliases':[]}],
        'offerings':[{'id':'offering1','program_id':'program1','cycle':'2027 Fall','deadline_date':'2026-12-15','deadline_at':None,
                      'date_precision':'date','timezone':'Asia/Shanghai','gre_policy':'unknown','requirements_json':{'notes':'Preserve notes'}}],
        'applications':[{'id':'application1','offering_id':'offering1','university':'CMU','program':'MSML','intake':'2027 Fall','status':'preparing'}],
        'requirements':[{'id':'req1','application_id':'application1','title':'SOP','key':'sop','kind':'document','obligation':'required','required_count':1,'criteria':{}},
                        {'id':'req2','application_id':'application1','title':'推荐信','key':'recs','kind':'recommendation','obligation':'required','required_count':3,'criteria':{}},
                        {'id':'req3','application_id':'application1','title':'CV','key':'cv','kind':'document','obligation':'required','required_count':1,'criteria':{}}],
        'materials':[{'id':f'm{i}','requirement_id':req,'slot_key':slot,'status':'missing'}
                     for i,(req,slot) in enumerate([('req1','SOP'),('req2','教授A'),('req2','教授B'),('req2','教授C'),('req3','CV')])],
        'tasks':[],'calendar_events':[]}
    async def run(message, previous=None):
        started = time.perf_counter()
        result = await handle_actions(client,{'message':message,'request_id':uuid4().hex,'workflow_snapshot':snapshot,
                                             'chat_action_draft':previous})
        assert result and result['intent_recognition']['intent']=='actions', result
        assert result['completion']['status']=='WAITING_APPROVAL',result
        command = result['proposals'][0]['payload']['commands'][0]
        assert command['type']=='project.save' and command['target_id']=='application1'
        form = command['payload']
        assert form['expected_revision']==4 and form['deadline_date']=='2026-12-15' and form['notes']=='Preserve notes'
        assert form['gre_policy']=='optional' and len(form['requirements'])==6
        reqs = {title_key(r['title']):r for r in form['requirements']}
        assert set(reqs)=={'sop','recommendation','toefl','gre','transcript','cv'}
        assert reqs['recommendation']['required_count']==3 and len(reqs['recommendation']['materials'])==3
        assert reqs['gre']['obligation']=='optional' and all(s['status']=='submitted' for s in reqs['gre']['materials'])
        assert reqs['toefl']['criteria']['minimum'] is None
        print(json.dumps({'case':'materials_update' if not previous else 'edit_pending_update','passed':True,
                          'seconds':round(time.perf_counter()-started,2),'model':client.model}),flush=True)
        return result, reqs
    first, reqs = await run('cmu msml项目的材料要求是一个sop,三个推荐信，托福，可选gre，成绩单，简历，其中gre已经提交，其他都准备好了')
    assert all(s['status']=='ready' for key,r in reqs.items() if key!='gre' for s in r['materials'])
    second, reqs = await run('SOP也已经提交了，其他保持刚才的修改', first['chat_action_draft'])
    assert all(s['status']=='submitted' for s in reqs['sop']['materials'])
    result = await handle_actions(client,{'message':'CMU MSML的材料准备进度如何？','request_id':uuid4().hex,'workflow_snapshot':snapshot})
    assert result['chat_passthrough'] and result['intent_recognition']['intent']=='query',result
    print(json.dumps({'case':'material_query','passed':True,'database_writes':0}),flush=True)


if __name__=='__main__':
    asyncio.run(main())
