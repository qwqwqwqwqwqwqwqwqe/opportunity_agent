"""Paired real-model experiment and local HTML label review. Never touches business DB."""
import argparse
import asyncio
import hashlib
import itertools
import json
from pathlib import Path
import random
import statistics
from datetime import datetime, timezone
from ..llm_client import LLMClient
from .monitor_evaluation import evaluate
from .monitor_web import FixtureLoader


def save(path,value):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def export_review(dataset_path,target):
    path=Path(dataset_path)
    data=json.loads(path.read_text(encoding='utf-8'))
    items=[]
    for case in data['cases']:
        if case['split']!='heldout':
            continue
        page={k:'\n'.join(b['text'] for b in FixtureLoader(path.parent).load(case[k],case['url']).blocks)
              for k in ('before','after')}
        items.append({**case,'pages':page})
    encoded=json.dumps(items,ensure_ascii=False).replace('<','\\u003c')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    page='''<!doctype html><meta charset="utf-8"><title>官网监控人工审核</title>
<style>body{font:16px system-ui;max-width:1100px;margin:30px auto;background:#f5f7f6}article{background:white;padding:24px;margin:20px 0;border-radius:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef2ef;padding:12px}label{display:block;margin:12px}select,input,textarea{font:inherit;padding:8px}textarea{width:90%}</style>
<h1>保留集标签审核（不是模型评分）</h1><p>全部是人工构造网页，不是真实学校历史。先独立阅读前后网页及个人资料，再核对建议标签。不能因为建议答案看起来合理就批量通过；不确定时选待澄清，补充证据后再审核。</p>
<p>示例：同周期 DDL 延长→MATCH/AFFECTED/通知；语言门槛105、个人102→MATCH/AFFECTED/通知；个人110且无新增操作→MATCH/NO_DIRECT_IMPACT/不通知；其他周期→NOT_MATCH/NO_DIRECT_IMPACT/不通知；个人成绩未知且无法判定→MATCH/UNKNOWN/不通知。</p>
<label>审核人署名 <input id="reviewer"></label><button id="download">下载审核记录 JSON</button><div id="cases"></div>
<script>
const cases=CASEDATA, digest=DIGEST;
const root=document.getElementById('cases');
function el(tag,text,parent){let node=document.createElement(tag);node.textContent=text;parent.append(node);return node;}
for(const c of cases){let a=el('article','',root);a.dataset.id=c.id;el('h2',c.id,a);
for(const k of ['before','after']){el('h3',k==='before'?'变更前网页正文':'变更后网页正文',a);el('pre',c.pages[k],a);}
el('h3','个人情况',a);el('pre',JSON.stringify(c.context,null,2),a);
el('p','建议标签（待你核对）：'+JSON.stringify(c.expected),a);
for(const [key,options] of Object.entries({applicability:['MATCH','NOT_MATCH','UNKNOWN'],impact:['AFFECTED','NO_DIRECT_IMPACT','UNKNOWN'],notify:['true','false'],status:['pending','approved','needs_clarification']})){
let label=el('label',key+' ',a),s=el('select','',label);s.dataset.key=key;for(const v of options){let o=el('option',v,s);o.value=v;}
s.value=key==='status'?'pending':String(c.expected[key]);}
let t=el('textarea','',a);t.placeholder='必须填写：前后变化原文、适用周期、个人依据，以及判定理由；若建议标签错误请修改上方选项。';}
document.getElementById('download').onclick=()=>{const reviewer=document.getElementById('reviewer').value.trim();if(!reviewer){alert('请填写审核人');return;}
let records=[...root.children].map(a=>{let r={id:a.dataset.id,reviewer,note:a.querySelector('textarea').value.trim()};for(let s of a.querySelectorAll('select'))r[s.dataset.key]=s.dataset.key==='notify'?s.value==='true':s.value;return r;});
if(records.some(r=>r.status==='approved'&&!r.note)){alert('通过的条目必须填写证据与理由');return;}
let blob=new Blob([JSON.stringify({dataset_sha256:digest,reviewed_at:new Date().toISOString(),records},null,2)],{type:'application/json'});let link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='monitor-label-review.json';link.click();URL.revokeObjectURL(link.href);};
</script>'''.replace('CASEDATA',encoded).replace('DIGEST',json.dumps(digest))
    Path(target).parent.mkdir(parents=True,exist_ok=True)
    Path(target).write_text(page,encoding='utf-8')


def import_review(dataset_path,review_path,target):
    path=Path(dataset_path); data=json.loads(path.read_text(encoding='utf-8'))
    review=json.loads(Path(review_path).read_text(encoding='utf-8'))
    if review['dataset_sha256']!=hashlib.sha256(path.read_bytes()).hexdigest():
        raise ValueError('Dataset changed after export')
    records={r['id']:r for r in review['records']}
    if len(records)!=len(review['records']):
        raise ValueError('Duplicate review IDs')
    for row in data['cases']:
        if row['split']!='heldout': continue
        r=records[row['id']]
        if r['status']!='approved' or not r['reviewer'].strip() or not r['note'].strip():
            raise ValueError('All heldout cases need signed evidence-backed approval')
        if r['applicability'] not in {'MATCH','NOT_MATCH','UNKNOWN'} or r['impact'] not in {'AFFECTED','NO_DIRECT_IMPACT','UNKNOWN'} or type(r['notify']) is not bool:
            raise ValueError('Invalid label')
        if r['notify'] and (r['applicability']!='MATCH' or r['impact']!='AFFECTED'):
            raise ValueError('Inconsistent notification label')
        row['expected'].update({k:r[k] for k in ['applicability','impact','notify']})
        row['human_review']='approved';row['review_record']=r
    data['reviewed_at']=review['reviewed_at']
    data['label_status']='heldout_human_reviewed_development_unreviewed'
    # FixtureLoader forbids paths outside its root: require a sibling reviewed copy.
    if Path(target).resolve().parent!=path.resolve().parent:
        raise ValueError('Reviewed dataset must be saved beside original fixture files')
    save(target,data)


def confirm_labels(dataset_path,target,confirmation):
    """Record an explicit user attestation; never fabricate per-case reasoning."""
    if not confirmation or not confirmation.strip():
        raise ValueError('Explicit user confirmation is required')
    path=Path(dataset_path)
    if Path(target).resolve()==path.resolve() or Path(target).resolve().parent!=path.resolve().parent:
        raise ValueError('Keep original data unchanged; save a sibling confirmed copy')
    data=json.loads(path.read_text(encoding='utf-8'))
    for row in data['cases']:
        if row['split']=='heldout': row['human_review']='approved'
    data['review_provenance']={'type':'user_confirmation_in_conversation','statement':confirmation,
        'recorded_at':datetime.now(timezone.utc).isoformat(),
        'original_dataset_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'per_case_review_notes_provided':False,'independent_review_verified':False}
    data['label_status']='user_confirmed_heldout_labels_no_per_case_audit'
    save(target,data)


def compare(reports,groups):
    a=reports['agent']; b=reports['single']; wins=losses=0; grouped={}
    for x,y in zip(a['details'],b['details'],strict=True):
        if x['id']!=y['id']: raise ValueError('Unpaired reports')
        truth=x['expected']['notify']
        delta=int(x['notify']==truth)-int(y['notify']==truth)
        wins+=delta>0;losses+=delta<0
        grouped.setdefault(groups[x['id']],[]).append(delta)
    values=[statistics.mean(v) for v in grouped.values()]
    observed=statistics.mean(values)
    # Exact paired sign permutation at template/group level (not independent variants).
    p=sum(abs(statistics.mean(s*v for s,v in zip(signs,values)))>=abs(observed)-1e-12
          for signs in itertools.product([-1,1],repeat=len(values)))/(2**len(values)) if len(values)<=16 else None
    return {'agent_wins':wins,'single_wins':losses,'group_count':len(values),
        'group_mean_accuracy_delta':observed,'two_sided_group_permutation_p':p,
        'precision_delta':a['precision']-b['precision'] if a['precision'] is not None and b['precision'] is not None else None,
        'recall_delta':a['recall']-b['recall'] if a['recall'] is not None and b['recall'] is not None else None,
        'claim_significant_superiority':False,
        'note':'Exploratory only until labels AND semantic evidence are independently reviewed. Two heldout templates are insufficient for a general superiority claim.'}


def score_evidence(report_path,review_path,target):
    report=json.loads(Path(report_path).read_text(encoding='utf-8'))
    review=json.loads(Path(review_path).read_text(encoding='utf-8'))
    records={(r['id'],r['arm']):r for r in review}
    if len(records)!=len(review): raise ValueError('Duplicate evidence reviews')
    for arm,summary in report['arms'].items():
        correct=count=0
        for detail in summary['details']:
            if not detail.get('result'): continue
            row=records[(detail['id'],arm)]
            if row.get('result')!=detail['result']: raise ValueError('Reviewed result changed')
            if type(row.get('semantic_evidence_correct')) is not bool or not row.get('reviewer','').strip() or not row.get('note','').strip():
                raise ValueError('Each result requires signed semantic evidence review')
            count+=1;correct+=row['semantic_evidence_correct']
        summary['semantic_evidence_accuracy']=correct/count if count else None
        summary['evidence_reviewed_results']=count
    report['evidence_review_sha256']=hashlib.sha256(Path(review_path).read_bytes()).hexdigest()
    save(target,report)


def export_evidence(review_path,target):
    records=json.loads(Path(review_path).read_text(encoding='utf-8'))
    data=json.dumps(records,ensure_ascii=False).replace('<','\\u003c')
    text='''<!doctype html><meta charset="utf-8"><title>模型证据审核</title>
<style>body{max-width:1000px;margin:30px auto;font:16px system-ui;background:#f5f7f6}article{background:white;padding:20px;margin:20px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}textarea{width:95%;height:70px}</style>
<h1>模型结果证据审核</h1><p>对照标签审核页中的前后网页与个人资料，核对引文含义、周期、个人依据及遗漏冲突。不是只看原文是否存在。此页显示实验臂，属于非盲审，报告中不得称为双盲。</p>
<label>审核人 <input id="reviewer"></label><button id="save">导出证据审核 JSON</button><div id="items"></div>
<script>const rows=RECORDS;const root=document.getElementById('items');
function add(t,s,p){let n=document.createElement(t);n.textContent=s;p.append(n);return n;}
rows.forEach((r,i)=>{let a=add('article','',root);a.dataset.i=i;add('h2',r.id+' / '+r.arm,a);add('pre',JSON.stringify(r.result,null,2),a);
if(!r.result){add('p','没有结构化结果：保留为运行失败，不填写证据正确率。',a);return;}
let s=add('select','',a);for(let [v,t] of [['','待审核'],['true','证据正确且支持结论'],['false','证据错误或不足以支持结论']]){let o=add('option',t,s);o.value=v;}
let note=add('textarea','',a);note.placeholder='必须填写：对应原文/个人依据、遗漏冲突或错误点。';});
document.getElementById('save').onclick=()=>{let reviewer=document.getElementById('reviewer').value.trim();if(!reviewer){alert('请署名');return;}
for(let a of root.children){let r=rows[Number(a.dataset.i)],s=a.querySelector('select');if(!s)continue;
if(!s.value||!a.querySelector('textarea').value.trim()){alert('每条有效结果都需判断并填写理由');return;}
r.semantic_evidence_correct=s.value==='true';r.reviewer=reviewer;r.note=a.querySelector('textarea').value.trim();}
let link=document.createElement('a');link.href=URL.createObjectURL(new Blob([JSON.stringify(rows,null,2)],{type:'application/json'}));link.download='evidence-review.completed.json';link.click();URL.revokeObjectURL(link.href);};</script>'''.replace('RECORDS',data)
    Path(target).write_text(text,encoding='utf-8')


async def run(args):
    path=Path(args.dataset); data=json.loads(path.read_text(encoding='utf-8'))
    dataset_digest=hashlib.sha256(path.read_bytes()).hexdigest()
    rows=[r for r in data['cases'] if r['split']==args.split]
    if args.limit:
        if args.split!='development' or not args.diagnostics: raise ValueError('Limit is for development diagnostics only')
        rows=rows[:args.limit]
    reviewed=all(r.get('human_review')=='approved' for r in rows)
    if not reviewed and not args.diagnostics: raise ValueError('Human label review required before real heldout experiment')
    client=LLMClient(timeout_seconds=25,retries=0)
    if not client.enabled: raise ValueError('Model not configured')
    out=Path(args.output);out.mkdir(parents=True,exist_ok=False)
    reports={mode:[] for mode in ['agent','single']}
    order=random.Random(20261027)
    evidence=[]
    for row in rows:
        modes=['agent','single'];order.shuffle(modes)
        for mode in modes:
            if hashlib.sha256(path.read_bytes()).hexdigest()!=dataset_digest:
                raise ValueError('Dataset modified during experiment; preserve partial reports and restart with a new output folder')
            report=await evaluate(path,client,args.split,mode,args.diagnostics,{row['id']})
            detail=report['details'][0];reports[mode].append((row['id'],report))
            save(out/(row['id']+'-'+mode+'.json'),report)
            evidence.append({'id':row['id'],'arm':mode,'result':detail.get('result'),
                'semantic_evidence_correct':None,'reviewer':'','note':'',
                'note_instruction':'Check whether quotes support the claim, correct cycle, personal basis and contradictions; quote existence alone is not semantic correctness.'})
    summaries={}
    loader=FixtureLoader(path.parent)
    active_ids={r['id'] for r in rows if loader.load(r['before'],r['url']).content_hash
                !=loader.load(r['after'],r['url']).content_hash}
    for row in rows:
        pair=[next(r for identity,r in reports[mode] if identity==row['id']) for mode in ['agent','single']]
        if pair[0]['fixtures_sha256']!=pair[1]['fixtures_sha256']:
            raise ValueError('Evidence changed between paired runs')
    for mode,entries in reports.items():
        details=[next(r['details'][0] for identity,r in entries if identity==row['id'])
                 for row in rows if row['id'] in active_ids]
        if not details:
            raise ValueError('No effective changes: pipeline negatives cannot measure model performance')
        tp=sum(d['notify'] and d['expected']['notify'] for d in details)
        fp=sum(d['notify'] and not d['expected']['notify'] for d in details)
        fn=sum(not d['notify'] and d['expected']['notify'] for d in details)
        summaries[mode]={'details':details,'precision':tp/(tp+fp) if tp+fp else None,
            'recall':tp/(tp+fn) if tp+fn else None,'tp':tp,'fp':fp,'fn':fn,
            'mean_tools':statistics.mean(d.get('tool_calls',0) for d in details),
            'mean_latency_ms':statistics.mean(d['elapsed_ms'] for d in details),
            'errors':sum('error' in d for d in details),'semantic_evidence_accuracy':None}
        summaries[mode]['model_calls']=sum(r['model_calls'] for _,r in entries)
        summaries[mode]['reported_total_tokens']=sum(r['reported_total_tokens'] for _,r in entries)
        summaries[mode]['grounding_validation_passed_results']=sum(bool(d.get('result')) and 'error' not in d for d in details)
    save(out/'evidence-review.json',evidence)
    export_evidence(out/'evidence-review.json',out/'evidence-review.html')
    code_hash=hashlib.sha256()
    for code_file in [Path(__file__),Path(__file__).with_name('monitor_evaluation.py'),Path(__file__).with_name('impact_agent.py'),Path(__file__).parents[1]/'llm_client.py']:
        code_hash.update(code_file.read_bytes())
    result={'model':client.model,'real_model':True,'human_labels_reviewed':reviewed,'cases':len(rows),
        'model_comparison_cases':len(active_ids),'pipeline_only_cases':len(rows)-len(active_ids),
        'label_review_provenance':data.get('review_provenance','signed_per_case_records'),
        'dataset_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'experiment_code_sha256':code_hash.hexdigest(),
        'created_at':datetime.now(timezone.utc).isoformat(),'arms':summaries,
        'comparison':compare(summaries,{r['id']:r['group'] for r in rows}),
        'baseline':'Single call with configured evidence and personal context; no adaptive supplemental-page retrieval. Same validation gate as agent.'}
    save(out/'comparison.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='arms'},ensure_ascii=False,indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['export-review','import-review','confirm-labels','score-evidence','run'])
    p.add_argument('--dataset',default='tests/fixtures/official_monitor/dataset.json')
    p.add_argument('--review');p.add_argument('--output',required=True)
    p.add_argument('--report')
    p.add_argument('--confirmation')
    p.add_argument('--split',choices=['development','heldout'],default='heldout')
    p.add_argument('--diagnostics',action='store_true');p.add_argument('--limit',type=int)
    a=p.parse_args()
    if a.limit is not None and a.limit<1: p.error('limit must be positive')
    if a.action=='export-review': export_review(a.dataset,a.output)
    elif a.action=='import-review': import_review(a.dataset,a.review,a.output)
    elif a.action=='confirm-labels': confirm_labels(a.dataset,a.output,a.confirmation)
    elif a.action=='score-evidence': score_evidence(a.report,a.review,a.output)
    else: asyncio.run(run(a))


if __name__=='__main__': main()
