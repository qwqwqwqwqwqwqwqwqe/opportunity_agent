"""Host-side real Docker process tests. Standard library only; no production DB URL."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import time
import traceback
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / 'data' / 'concurrency-control'
COMPOSE = ['docker','compose','-p','applyops-concurrency','-f',str(ROOT/'compose.concurrency.yml')]


def docker(*args):
    return subprocess.check_output(COMPOSE+list(args),cwd=ROOT,text=True,encoding='utf-8',errors='replace',
                                   stderr=subprocess.STDOUT,timeout=90).strip()


def sql(query):
    return docker('exec','-T','postgres','psql','-X','-v','ON_ERROR_STOP=1','-U','concurrency',
                  '-d','applyops_concurrency','-At','-c',query)


def wait(fn, seconds=40):
    until = time.monotonic()+seconds
    while time.monotonic()<until:
        value = fn()
        if value:
            return value
        time.sleep(.2)
    raise AssertionError('timed out: '+repr(fn))


def control(**kw):
    CONTROL.mkdir(parents=True,exist_ok=True)
    # Atomic rename so worker processes never read half a JSON document.
    temp = CONTROL/'settings.tmp'
    temp.write_text(json.dumps(kw),encoding='utf-8')
    # Docker Desktop's Windows bind mount may briefly hold a read handle.
    # Retry only this file transport operation, never failed test assertions.
    for attempt in range(20):
        try:
            temp.replace(CONTROL/'settings.json')
            return
        except PermissionError:
            if attempt==19: raise
            time.sleep(.1)


def http(port, path, body=None, token=None, method=None, headers=None):
    h = {'Content-Type':'application/json',**(headers or {})}
    if token:
        h['Cookie'] = token
    req = Request(f'http://127.0.0.1:{port}'+path,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers=h,method=method)
    try:
        with urlopen(req,timeout=40) as r:
            raw = r.read().decode()
            data = json.loads(raw) if 'application/json' in r.headers.get('Content-Type','') else raw
            return r.status,data,r.headers
    except HTTPError as e:
        return e.code,e.read().decode(),e.headers


class Scenario:
    def __init__(self):
        status,data,headers = http(18081,'/api/v1/auth/register',
            {'email':uuid4().hex+'@concurrency.test','password':'isolated-long-password'})
        assert status==201,(status,data)
        self.user = data['user']['id']
        self.cookie = '; '.join(v.split(';',1)[0] for v in headers.get_all('Set-Cookie',[]))
        assert self.cookie

    def request(self,path,body=None,port=18081,expected=200,method=None,headers=None):
        status,data,_ = http(port,path,body,self.cookie,method,headers)
        assert status==expected,(path,status,data)
        return data

    def command(self,kind,payload,target=None,version=None,request=None):
        cmd = {'type':kind,'payload':payload}
        if target:
            cmd.update(target_id=target)
        if version is not None:
            cmd['expected_version']=version
        return self.request('/api/v3/commands',{'request_id':request or uuid4().hex,'commands':[cmd]})

    def prepare(self):
        p = self.command('program.create',{'university':'Concurrency University','name':'MSCS'})['changed_resources'][0]
        self.program = p['id']
        o = self.command('offering.create',{'program_id':p['id'],'cycle':'2027 Fall'})['changed_resources'][0]
        self.offering = o['id']
        a = self.command('application.create',{'offering_id':o['id']})['changed_resources'][0]
        self.app = a['id']
        self.request(f'/api/v3/applications/{self.app}/monitor',{'request_id':uuid4().hex,
            'project_url':'https://example.edu/program','confirmed':True,'enabled':True},method='PUT')

    def enqueue(self):
        # Test-only SQL advances cooldown; never exposed through API.
        sql(f"update workflow_monitor_configs set last_manual_at=NULL where application_id='{self.app}'")
        self.check = self.request(f'/api/v3/applications/{self.app}/monitor/checks',
                                 {'request_id':uuid4().hex},expected=202)['id']
        return self.check

    def finished(self,expected):
        row = wait(lambda: (r if r['status'] not in ['queued','running','analyzing'] else None)
            if (r:=self.request('/api/v3/monitor-checks/'+self.check)) else None)
        assert row['status']==expected,row
        return row

    def notifications(self):
        return int(sql(f"select count(*) from workflow_notifications where user_id='{self.user}' and type='official_impact'"))

    def baseline(self):
        control(enabled=['worker1','worker2'])
        self.prepare()
        self.enqueue()
        self.finished('baseline')
        assert self.notifications()==0
        control(enabled=[])


def api_concurrency():
    s = Scenario()
    body = {'request_id':uuid4().hex,'commands':[{'type':'program.create','payload':{'university':'A','name':'B'}}]}
    def post(i):
        return http(18081+i%2,'/api/v3/commands',body,s.cookie)[:2]
    with ThreadPoolExecutor(20) as pool:
        responses = list(pool.map(post,range(20)))
    assert all(status==200 for status,_ in responses),responses
    assert all(data==responses[0][1] for _,data in responses)
    assert int(sql(f"select count(*) from workflow_programs where user_id='{s.user}'"))==1
    assert int(sql(f"select count(*) from workflow_command_receipts where user_id='{s.user}'"))==1
    pid = responses[0][1]['changed_resources'][0]['id']
    body['commands'][0]['payload']['name']='different'
    assert http(18082,'/api/v3/commands',body,s.cookie)[0]==409
    def update(i):
        return http(18081+i,'/api/v3/commands',{'request_id':uuid4().hex,'commands':[
            {'type':'program.update','target_id':pid,'expected_version':1,'payload':{'name':str(i)}}]},s.cookie)[0]
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(update,range(2)))==[200,409]
    assert sql(f"select version from workflow_programs where id='{pid}'")=='2'
    s.prepare()
    request = {'request_id':uuid4().hex}
    with ThreadPoolExecutor(2) as pool:
        checks = list(pool.map(lambda p:s.request(f'/api/v3/applications/{s.app}/monitor/checks',
                            request,port=p,expected=202),[18081,18082]))
    assert checks[0]['id']==checks[1]['id']
    other = Scenario()
    other.request('/api/v3/monitor-checks/'+checks[0]['id'],expected=404)
    other.request(f'/api/v3/applications/{s.app}/monitor',expected=404)
    status,_,_ = http(18082,'/api/v3/commands',{'request_id':uuid4().hex,'commands':[
        {'type':'task.create','payload':{'application_id':s.app,'title':'forbidden','stable_key':uuid4().hex}}]},other.cookie)
    assert status==404,status
    assert sql(f"select count(*) from workflow_tasks where user_id='{other.user}'")=='0'
    control(enabled=['worker1','worker2'])
    s.check=checks[0]['id']; s.finished('baseline')
    control(enabled=[])


def chat_cross_api():
    s = Scenario()
    c = s.request('/api/v1/conversations',{'title':'Cross instance'},expected=201)
    body = {'message':'我的托福是105分','request_id':uuid4().hex}
    with ThreadPoolExecutor(2) as pool:
        runs=list(pool.map(lambda p:s.request(f"/api/v1/conversations/{c['id']}/runs",body,
                                              port=p,expected=202)['run_id'],[18081,18082]))
    assert runs[0]==runs[1],runs
    run=runs[0]
    result = wait(lambda: r if (r:=s.request('/api/v1/runs/'+run,port=18082))['status'] in ['completed','failed'] else None)
    assert result['status']=='completed',result
    events = s.request('/api/v1/runs/'+run+'/events',port=18082)
    ids = [int(line[4:]) for line in events.splitlines() if line.startswith('id: ')]
    assert ids==list(range(1,max(ids)+1)),ids
    assert 'event: run_completed' in events
    cursor = ids[len(ids)//2]
    resumed = s.request('/api/v1/runs/'+run+'/events',port=18081,headers={'Last-Event-ID':str(cursor)})
    assert [int(l[4:]) for l in resumed.splitlines() if l.startswith('id: ')]==[i for i in ids if i>cursor]
    assert sql(f"select count(*) from messages where role='assistant' and conversation_id='{c['id']}'")=='1'
    assert len(result['approval_ids'])==1,result
    assert sql(f"select count(*) from approval_requests a join change_proposals p on p.id=a.proposal_id where p.run_id='{run}'")=='1'


def fault(stage,action):
    s = Scenario(); s.baseline()
    marker = CONTROL/'worker1.barrier'
    marker.unlink(missing_ok=True)
    control(enabled=['worker1'],date='December 15, 2026',barrier=stage,barrier_worker='worker1')
    s.enqueue()
    wait(marker.exists)
    evidence = json.loads(marker.read_text())
    if action=='kill':
        docker('kill','-s','SIGKILL','worker1')
    else:
        docker('pause','worker1')
    try:
        control(enabled=['worker2'],date='December 15, 2026')
        s.finished('affected')
        assert s.notifications()==1
    finally:
        docker('start','worker1') if action=='kill' else docker('unpause','worker1')
    time.sleep(1)
    assert s.notifications()==1
    if stage!='committed':
        condition = (f"scope->>'check_id'='{s.check}' and type='monitor.analyze'" if stage=='analyzing'
                     else f"id='{evidence['job_id']}'")
        assert sql(f"select lease_token is null from workflow_jobs where {condition}")=='t'
    control(enabled=[])
    assert sql(f"select count(*) from workflow_monitor_analyses where check_id='{s.check}' and status='affected'")=='1'


def stale_input(kind):
    s = Scenario(); s.baseline()
    marker = CONTROL/'worker1.barrier'; marker.unlink(missing_ok=True)
    control(enabled=['worker1'],date='December 15, 2026',barrier='before_commit',barrier_worker='worker1')
    s.enqueue(); wait(marker.exists)
    if kind=='pause':
        cfg=s.request(f'/api/v3/applications/{s.app}/monitor')
        s.request(f'/api/v3/applications/{s.app}/monitor',{'request_id':uuid4().hex,
            'expected_version':cfg['version'],'project_url':'https://example.edu/program',
            'confirmed':True,'enabled':False},method='PUT')
    elif kind=='cycle':
        s.command('offering.update',{'cycle':'2028 Fall'},s.offering,1)
    else:
        profile=s.request('/api/v1/profile')
        s.command('profile.change',{'facts':[{'field':'toefl_score','normalized_value':110,
            'confidence':1.0,'evidence':'我的托福是110分'}]},version=profile['version'])
    control(enabled=['worker1','worker2'],date='December 15, 2026')
    s.finished('affected' if kind=='profile' else 'superseded')
    assert s.notifications()==(1 if kind=='profile' else 0)
    if kind=='profile':
        assert int(sql(f"select max(attempt) from workflow_jobs where type='monitor.analyze' and user_id='{s.user}'"))>=2
    control(enabled=[])


def daily_and_failure():
    s=Scenario(); s.baseline()
    sql(f"update workflow_monitor_configs set mode='daily',next_due=now()-interval '3 days' where application_id='{s.app}'")
    control(enabled=['worker1','worker2'])
    wait(lambda: sql(f"select count(*) from workflow_monitor_checks where monitor_id=(select id from workflow_monitor_configs where application_id='{s.app}') and trigger='daily'")=='1')
    wait(lambda: sql(f"select active_check_id is null from workflow_monitor_configs where application_id='{s.app}'")=='t')
    sql(f"update workflow_monitor_configs set mode='manual' where application_id='{s.app}'")
    fail_key=uuid4().hex
    control(enabled=['worker1'],fail_key=fail_key,fail_count=100)
    s.enqueue(); s.finished('fetch_failed')
    assert s.notifications()==0
    assert (CONTROL/('failures-'+fail_key)).read_text()=='3'
    fail_key=uuid4().hex
    control(enabled=['worker1'],date='December 15, 2026',fail_key=fail_key,fail_count=2)
    s.enqueue(); s.finished('affected')
    assert s.notifications()==1
    assert (CONTROL/('failures-'+fail_key)).read_text()=='2'
    control(enabled=['worker1','worker2'],date='December 20, 2026',model_error=True)
    s.enqueue(); s.finished('analysis_failed')
    assert s.notifications()==1
    assert sql(f"select attempt from workflow_jobs where scope->>'check_id'='{s.check}' and type='monitor.analyze'")=='3'
    control(enabled=[])


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--rounds',type=int,default=10)
    args=parser.parse_args()
    assert args.rounds>0
    CONTROL.mkdir(parents=True,exist_ok=True)
    report={'topology':'2 API + 2 Worker + PostgreSQL','rounds_requested':args.rounds,'cases':[]}
    folder=ROOT/'data'/'concurrency-reports'/time.strftime('%Y%m%d-%H%M%S'); folder.mkdir(parents=True)
    try:
        # Refuse to operate unless Compose resolves exactly our dedicated DB.
        cfg=json.loads(docker('config','--format','json'))
        assert cfg['name']=='applyops-concurrency'
        for service in ['api1','api2','worker1','worker2']:
            assert cfg['services'][service]['environment']['DATABASE_URL'].endswith('@postgres:5432/applyops_concurrency')
        for port in (18081,18082):
            assert http(port,'/healthz')[0]==200
        assert sql('select current_database()')=='applyops_concurrency'
        control(enabled=[])
        # A previous failed run may leave queued fixture jobs. Preserve rows and
        # reports, but fence their effects before starting a fresh acceptance run.
        sql("update workflow_jobs set status='cancelled',lease_token=NULL,lease_until=NULL where status in ('queued','running')")
        sql("update workflow_monitor_configs set active_check_id=NULL,mode='manual'")
        report['postgres']=sql('select version()')
        report['revision']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        report['dirty']=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip())
        cases=[('api',api_concurrency),('chat',chat_cross_api),
               *[(f'{stage}-{action}',lambda st=stage,a=action:fault(st,a)) for stage,action in
                 [('claimed','kill'),('analyzing','kill'),('before_commit','pause'),('before_commit','kill'),('committed','kill')]],
               *[(f'stale-{kind}',lambda k=kind:stale_input(k)) for kind in ['pause','cycle','profile']],
               ('daily-failure',daily_and_failure)]
        for round_no in range(args.rounds):
            for name,fn in cases:
                started=time.monotonic()
                row={'round':round_no+1,'case':name}
                report['cases'].append(row)
                print(f'Round {round_no+1}: {name}',flush=True)
                try:
                    control(enabled=[]); fn(); row['status']='passed'
                except BaseException:
                    row['status']='failed'; row['error']=traceback.format_exc(); raise
                finally:
                    row['seconds']=round(time.monotonic()-started,3)
    except BaseException:
        report['error']=traceback.format_exc()
        raise
    finally:
        control(enabled=[])
        # Always retain evidence, including the FIRST failure. No automatic retry.
        try:
            (folder/'containers.json').write_text(docker('ps','-a','--format','json'),encoding='utf-8')
            (folder/'services.log').write_text(docker('logs','--no-color','--tail','500'),encoding='utf-8')
            (folder/'jobs.json').write_text(sql('select coalesce(json_agg(t),\'[]\') from (select id,type,status,attempt,lease_token,lease_until,error,scope from workflow_jobs) t'),encoding='utf-8')
        finally:
            (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print('Report:',folder)


if __name__=='__main__':
    main()
