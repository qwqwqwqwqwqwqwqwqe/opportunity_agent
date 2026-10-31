import asyncio
import json
from pathlib import Path
import pytest
from opportunity_agent.v3.monitor_experiment import compare, export_review, import_review, run
from types import SimpleNamespace


DATA=Path('tests/fixtures/official_monitor/dataset.json')


def test_review_html_is_local_and_cannot_execute_fixture_html(tmp_path):
    out=tmp_path/'review.html';export_review(DATA,out)
    text=out.read_text(encoding='utf-8')
    assert 'textContent' in text and 'fetch(' not in text
    assert text.count('</script>')==1 and 'monitor-label-review.json' in text
    assert 'needs_clarification' in text


def test_review_import_rejects_modified_dataset(tmp_path):
    r=tmp_path/'review.json';r.write_text(json.dumps({'dataset_sha256':'wrong','records':[]}))
    with pytest.raises(ValueError,match='changed'):
        import_review(DATA,r,tmp_path/'dataset.json')


def test_unreviewed_heldout_stops_before_model_call(tmp_path):
    args=SimpleNamespace(dataset=str(DATA),split='heldout',limit=None,diagnostics=False,output=tmp_path/'out')
    with pytest.raises(ValueError,match='review'):
        asyncio.run(run(args))
    assert not args.output.exists()


def test_group_statistics_do_not_count_variants_as_independent_samples():
    good=[{'id':str(i),'notify':True,'expected':{'notify':True}} for i in range(20)]
    bad=[{**r,'notify':False} for r in good]
    result=compare({'agent':{'details':good,'precision':1,'recall':1},
                    'single':{'details':bad,'precision':None,'recall':0}},
                   {str(i):str(i//10) for i in range(20)})
    assert result['group_count']==2
    assert result['two_sided_group_permutation_p']==.5
    assert not result['claim_significant_superiority']


def test_structured_model_request_explicitly_requests_json():
    from pydantic import BaseModel
    from opportunity_agent.llm_client import LLMClient
    class Result(BaseModel):
        ok:bool
    payloads=[]
    def complete(payload):
        payloads.append(payload)
        return '{"ok":true}'
    output=LLMClient(completion_fn=complete).generate_structured(Result,system='Return a result',context={})
    assert output.ok
    assert 'json' in payloads[0]['messages'][0]['content'].lower()


def test_evidence_accuracy_requires_signed_semantic_review(tmp_path):
    from opportunity_agent.v3.monitor_experiment import score_evidence
    report={'arms':{'agent':{'details':[{'id':'x','result':{'impact':'UNKNOWN'}}]}}}
    records=[{'id':'x','arm':'agent','result':{'impact':'UNKNOWN'},
              'semantic_evidence_correct':None,'reviewer':'','note':''}]
    rp=tmp_path/'report.json';rv=tmp_path/'review.json';out=tmp_path/'scored.json'
    rp.write_text(json.dumps(report));rv.write_text(json.dumps(records))
    with pytest.raises(ValueError,match='signed'):
        score_evidence(rp,rv,out)
    records[0].update(semantic_evidence_correct=True,reviewer='Reviewer',note='Scope uncertain; abstention supported by source.')
    rv.write_text(json.dumps(records));score_evidence(rp,rv,out)
    assert json.loads(out.read_text())['arms']['agent']['semantic_evidence_accuracy']==1


def test_user_confirmation_preserves_labels_without_fabricated_notes(tmp_path):
    from opportunity_agent.v3.monitor_experiment import confirm_labels
    source=tmp_path/'dataset.json'
    original={'cases':[{'id':'a','split':'heldout','human_review':'pending','expected':{'notify':False}},
                       {'id':'b','split':'development','human_review':'pending','expected':{'notify':True}}]}
    source.write_text(json.dumps(original));target=tmp_path/'confirmed.json'
    with pytest.raises(ValueError,match='confirmation'):
        confirm_labels(source,target,'')
    confirm_labels(source,target,'已经审核完了，全部通过，可以继续了')
    reviewed=json.loads(target.read_text(encoding='utf-8'))
    assert json.loads(source.read_text())==original
    assert reviewed['cases'][0]['human_review']=='approved'
    assert reviewed['cases'][1]['human_review']=='pending'
    assert reviewed['review_provenance']['per_case_review_notes_provided'] is False
    assert reviewed['review_provenance']['independent_review_verified'] is False


@pytest.mark.parametrize('fail',[False,True])
def test_single_baseline_contract_and_failed_call_accounting(fail):
    from opportunity_agent.v3.monitor_evaluation import single_shot,tools_for_case
    from opportunity_agent.v3.monitor_web import FixtureLoader
    from opportunity_agent.v3.monitor_contracts import ImpactResult
    case=json.loads(DATA.read_text(encoding='utf-8'))['cases'][0]
    tools=tools_for_case(case,FixtureLoader(DATA.parent))
    class Client:
        def generate_structured(self,model_type,**kwargs):
            assert model_type is ImpactResult
            assert '每次返回 AgentAction' not in kwargs['system']
            assert '不输出 tool/result 包装' in kwargs['system']
            if fail: raise RuntimeError('test unavailable')
            return ImpactResult(check_id=case['id'],change_ids=['change1'],
                                applicability='UNKNOWN',impact='UNKNOWN',direction='UNKNOWN')
    if fail:
        with pytest.raises(RuntimeError): asyncio.run(single_shot(Client(),tools))
    else:
        assert asyncio.run(single_shot(Client(),tools))[1] is False
    calls=[t for t in tools.trace if t.get('model_call')]
    assert len(calls)==1
    assert calls[0]['status']==('failed' if fail else 'ok')
