"""Official excerpts + CONSTRUCTED model candidates; measures verifier, not LLM accuracy."""
import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from opportunity_agent.v2.agents.contracts import ProgramResult, ResearchResult
from opportunity_agent.v2.research.extraction import (
    AdmissionFact, AdmissionExtraction, ProgramIdentityClaim, IdentityReference,
    blocks, verify_fact, same_program, VERSION)
from opportunity_agent.v2.research.extraction import quote_options, bind_source_references
from opportunity_agent.v2.research.service import ResearchService
from opportunity_agent.v2.research.task import ResearchTaskSpec

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/research_identity_scope.json').read_text(encoding='utf-8'))


def cases(legacy=False):
    for source in FIXTURE['pages']:
        page = {k: source[k] for k in ('url', 'title', 'text')}
        target = ProgramResult(university=source['university'], program=source['target'], intake='2027 Fall')
        for raw in source['facts']:
            data = dict(raw)
            if legacy:
                data.pop('applicability_quote', None)
            data.update(university=source['university'], block_id=next(
                b['id'] for b in blocks(page['text'], page['title']) if data['quote'] in b['text']))
            yield source['id'], page, target, AdmissionFact(**data), source['accept']


def measure(legacy=False):
    rows = []
    for name, page, target, fact, wanted in cases(legacy):
        proof, reason = verify_fact(fact, target, page, ['deadline', 'gre_policy'], date(2026, 10, 12))
        rows.append(dict(page=name, field=fact.field, degree=fact.degree, expected=wanted,
                         accepted=proof is not None, reason=reason))
    valid = [r for r in rows if r['expected']]
    accepted = [r for r in rows if r['accepted']]
    return dict(rows=rows, positive=len(valid), accepted=len(accepted),
                correct=sum(r['expected'] for r in accepted),
                rejected_valid=sum(not r['accepted'] for r in valid),
                pollution=sum(not r['expected'] for r in accepted))


@pytest.mark.parametrize('name,page,target,fact,wanted', list(cases(legacy=True)))
def test_official_page_candidate_binding(name, page, target, fact, wanted):
    # Use applicability_quote only when the recorded wording has multiple scopes.
    source = next(s for s in FIXTURE['pages'] if s['id'] == name)
    raw = next(r for r in source['facts'] if r['field'] == fact.field and r['degree'] == fact.degree)
    fact = fact.model_copy(update={'applicability_quote': raw.get('applicability_quote', '')})
    proof, reason = verify_fact(fact, target, page, ['deadline', 'gre_policy'], date(2026, 10, 12))
    assert bool(proof) == wanted, reason
    if proof:
        assert page['text'][proof['start']:proof['end']] == fact.quote


def test_cse_scope_groups_are_not_cleared(monkeypatch):
    monkeypatch.setenv('RESEARCH_PERSIST_FACTS', '0')
    monkeypatch.setenv('RESEARCH_QUEUE_INGEST', '0')
    selected = [v for v in cases() if v[0] == 'gt_cse']
    class Recorded:
        enabled = True
        def generate_structured(self, model, **kwargs):
            return model(facts=[v[3] for v in selected])
    async def run():
        _, page, target, _, _ = selected[0]
        task = ResearchTaskSpec(query='CSE admissions', requested_fields=['deadline', 'gre_policy'], as_of=date(2026,10,12))
        result = ResearchResult()
        await ResearchService(None, llm=Recorded())._accept_page(task, result, target, page)
        assert {p.degree for p in result.programs} == {'masters', 'doctoral'}
        assert any(p.gre_policy == 'required' and p.degree == 'masters' for p in result.programs)
        assert any(p.gre_policy == 'optional' and p.degree == 'doctoral' for p in result.programs)
        assert all(p.deadline is None for p in result.programs)
    asyncio.run(run())


@pytest.mark.parametrize('title', [
    'How to Apply - Carnegie Mellon University - Master of Science in Artificial Intelligence and Innovation',
    'Master of Science in Artificial Intelligence and Innovation / How to Apply',
    'How to Apply: Master of Science in Artificial Intelligence and Innovation'])
def test_identity_does_not_require_a_title_separator(title):
    _, page, target, fact, _ = next(cases())
    page = {**page, 'title': title}
    assert verify_fact(fact,target,page,['deadline'],date(2026,10,12))[1] == ''


@pytest.mark.parametrize('updates,reason', [
    ({'program_quote':''},'program_quote_missing'),
    ({'program_quote':'Invented official name'},'program_quote_not_located'),
    ({'program':'Master of Data Science'},'program_conflict'),
    ({'university':'Georgia Institute of Technology'},'school_mismatch'),
    ({'quote':'Invented quote'},'quote_not_in_block'),
    ({'intake':'Fall 2028'},'intake_mismatch')])
def test_rejections_identify_the_failed_check(updates, reason):
    _,page,target,fact,_ = next(cases())
    assert verify_fact(fact.model_copy(update=updates),target,page,['deadline'],date(2026,10,12))[1] == reason


def test_conflicting_aliases_and_distinct_credentials_are_not_equivalent():
    scope='Computational Science and Engineering (CSE)\nComputer Science and Engineering (CSE)'
    assert not same_program('CSE','Computational Science and Engineering',scope)
    assert not same_program('Master of Computer Science','Master of Science in Computer Science',scope)
    assert same_program('Special Qualification','Unusual Award', 'Unusual Award (Special Qualification)') is False
    assert not same_program('CSE','Computational Science and Engineering','CSE')


def test_comparison_requires_a_complete_scope_and_cannot_swap_degrees():
    _,page,target,fact,_ = next(v for v in cases() if v[0]=='gt_cse' and v[3].field=='gre_policy' and v[3].degree=='masters')
    assert verify_fact(fact.model_copy(update={'applicability_quote':''}),target,page,['gre_policy'],date(2026,10,12))[1]=='scope_binding_ambiguous'
    wrong=fact.model_copy(update={'applicability_quote':'optional for applications to the Computational Science and Engineering and CS Ph.D. programs'})
    assert verify_fact(wrong,target,page,['gre_policy'],date(2026,10,12))[1]=='degree_scope_conflict'


def test_identity_claim_references_are_located_and_cannot_be_forged():
    _,page,target,fact,_=next(cases())
    claim=ProgramIdentityClaim(identity_id='p1', university=target.university,
        program=fact.program_quote, degree='masters', aliases=['MSAII'], references=[
            IdentityReference(kind='title',quote=fact.program_quote),
            IdentityReference(kind='text',block_id=fact.block_id,quote='MSAII program')])
    extraction=AdmissionExtraction(identities=[claim],facts=[fact.model_copy(update={'identity_ref':'p1'})])
    proof,reason=verify_fact(extraction.facts[0],target,page,['deadline'],date(2026,10,12))
    assert not reason and len(proof['identity_references'])==2
    fake=claim.model_copy(update={'references':[IdentityReference(kind='text',block_id=fact.block_id,quote='Invented official programme reference')]})
    assert verify_fact(fact.model_copy(update={'identity_claim':fake}),target,page,['deadline'],date(2026,10,12))[1]=='program_quote_not_located'


def test_claim_cannot_borrow_a_sibling_section():
    _,page,target,fact,_=next(cases())
    page={**page,'text':page['text']+'\n# Another programme\nMaster of Data Science (MDS)'}
    sibling=blocks(page['text'],page['title'])[-1]
    claim=ProgramIdentityClaim(identity_id='p1', university=target.university,program=fact.program_quote,
        degree='masters',references=[IdentityReference(kind='text',block_id=sibling['id'],quote='Master of Data Science')])
    assert verify_fact(fact.model_copy(update={'identity_claim':claim}),target,page,['deadline'],date(2026,10,12))[1]=='program_quote_not_located'


def test_gt_real_model_reference_shape_uses_abbreviated_body_and_full_title():
    # Shape observed from ModelScope; field candidates remain explicitly recorded fixtures.
    selected=[v for v in cases() if v[0]=='gt_cse']
    for _,page,target,fact,_ in selected:
        anchor=next(v[3] for v in selected if v[3].field=='deadline' and v[3].degree==fact.degree)
        claim=ProgramIdentityClaim(identity_id=fact.degree,university=target.university,program=target.program,
            degree=fact.degree,home_unit='School of CSE',references=[
                IdentityReference(kind='text',block_id=anchor.block_id,quote=anchor.quote)])
        updates={'identity_claim':claim,'home_unit':'School of CSE','home_unit_quote':'School of CSE'}
        if fact.field=='gre_policy' and fact.degree=='doctoral':
            updates['program_quote']='Computational Science and Engineering and CS Ph.D. programs'
        proof,reason=verify_fact(fact.model_copy(update=updates),target,page,['deadline','gre_policy'],date(2026,10,12))
        assert not reason,reason
        assert proof['identity']['subject']=='computational science engineering'
        assert any(r['kind']=='title' for r in proof['identity_references'])


def test_identity_label_alone_cannot_replace_a_comparison_clause():
    _,page,target,fact,_=next(v for v in cases() if v[0]=='gt_cse' and v[3].field=='gre_policy' and v[3].degree=='masters')
    wrong=fact.model_copy(update={'applicability_quote':'Computational Science and Engineering'})
    assert verify_fact(wrong,target,page,['gre_policy'],date(2026,10,12))[1]=='scope_binding_ambiguous'


def test_quote_ids_resolve_original_text_without_model_paraphrase():
    _,page,target,fact,_=next(v for v in cases() if v[0]=='gt_cse' and v[3].field=='gre_policy' and v[3].degree=='masters')
    prepared=blocks(page['text'],page['title'])
    block=next(b for b in prepared if b['id']==fact.block_id)
    clause=next(o for o in quote_options(block) if o['text'].startswith('required for applications'))
    response=AdmissionFact(**{**fact.model_dump(),'quote':'','quote_ref':block['id'],
                              'applicability_quote':'','applicability_ref':clause['ref']})
    resolved=bind_source_references(AdmissionExtraction(facts=[response]),prepared).facts[0]
    proof,reason=verify_fact(resolved,target,page,['gre_policy'],date(2026,10,12))
    assert not reason and resolved.quote==block['text']
    assert page['text'][proof['start']:proof['end']]==block['text']
    assert verify_fact(resolved.model_copy(update={'quote_ref':'invented'}),target,page,['gre_policy'],date(2026,10,12))[1]=='quote_reference_unresolved'


def test_shared_source_quote_has_distinct_field_and_degree_bindings():
    from opportunity_agent.v2.agents.contracts import Evidence
    ids=[]
    for _,page,target,fact,_ in [v for v in cases() if v[0]=='gt_cse']:
        proof,reason=verify_fact(fact,target,page,['deadline','gre_policy'],date(2026,10,12))
        assert not reason
        ids.append(Evidence(source_id='same-page',content_hash=proof['source_hash'],excerpt=page['text'],extraction_provenance=proof).evidence_id)
    assert len(set(ids))==4


def test_reference_cannot_be_rebound_during_persistence_verification():
    _,page,target,fact,_=next(cases())
    prepared=blocks(page['text'],page['title'])
    resolved=bind_source_references(AdmissionExtraction(facts=[fact.model_copy(update={'quote_ref':fact.block_id})]),prepared).facts[0]
    assert verify_fact(resolved,target,page,['deadline'],date(2026,10,12))[0]
    tampered=resolved.model_copy(update={'quote':'The deadline to apply is December 9, 2026.'})
    assert verify_fact(tampered,target,page,['deadline'],date(2026,10,12))[1]=='quote_reference_unresolved'


def test_bare_unit_reference_cannot_borrow_the_other_degrees_unit():
    selected=[v for v in cases() if v[0]=='gt_cse']
    _,page,target,fact,_=next(v for v in selected if v[3].field=='gre_policy' and v[3].degree=='masters')
    ms=next(v[3] for v in selected if v[3].field=='deadline' and v[3].degree=='masters')
    page={**page,'text':page['text'].replace(
        'School of CSE (in College of Computing) is February 1.', 'School B (in College of Computing) is February 1.')}
    claim=ProgramIdentityClaim(identity_id='ms',university=target.university,program=target.program,
        degree='masters',home_unit='School of CSE',references=[IdentityReference(kind='text',block_id=fact.block_id,
            quote=ms.quote.replace('School of CSE','School B'))])
    wrong=fact.model_copy(update={'identity_claim':claim,'home_unit':'School of CSE','home_unit_quote':'School of CSE'})
    assert verify_fact(wrong,target,page,['gre_policy'],date(2026,10,12))[1]=='home_unit_not_grounded'


def test_single_programme_title_binds_a_generic_field_clause():
    _,page,target,fact,_=next(cases())
    proof,reason=verify_fact(fact.model_copy(update={'applicability_quote':fact.quote}),target,page,['deadline'],date(2026,10,12))
    assert not reason and proof


def test_applicability_clause_cannot_hide_inherited_applicant_scope():
    _,page,target,fact,_=next(cases())
    page={**page,'text':page['text'].replace('# How to Apply','# How to Apply\n## International applicants')}
    block=next(b for b in blocks(page['text'],page['title']) if fact.quote in b['text'])
    fact=fact.model_copy(update={'block_id':block['id'],'applicability_quote':fact.quote})
    assert verify_fact(fact,target,page,['deadline'],date(2026,10,12))[1]=='restricted_applicant_quote'


def test_unit_statement_is_found_even_when_model_only_cites_gre_paragraph():
    for _,page,target,fact,_ in [v for v in cases() if v[0]=='gt_cse' and v[3].field=='gre_policy']:
        claim=ProgramIdentityClaim(identity_id=fact.degree,university=target.university,program=target.program,
            degree=fact.degree,home_unit='School of CSE',references=[IdentityReference(kind='text',block_id=fact.block_id,quote=fact.quote)])
        candidate=fact.model_copy(update={'identity_claim':claim,'home_unit':'School of CSE'})
        proof,reason=verify_fact(candidate,target,page,['gre_policy'],date(2026,10,12))
        assert not reason,reason
        assert any('home unit' in r['quote'] for r in proof['identity_references'])


def test_professional_master_is_not_expanded_to_ms_acronym():
    assert not same_program('MSCS','Master of Computer Science','Master of Computer Science. MSCS is offered separately.')


def test_scoped_aggregation_preserves_degrees_and_home_units():
    from opportunity_agent.v2.agents.contracts import ExecutionState
    from opportunity_agent.v2.agents.result_aggregation import ResultAggregator
    state=ExecutionState(user_id='u',conversation_id='c',run_id='r',request_id='q',message='admissions')
    aggregate=ResultAggregator()
    for degree,unit in [('masters','School A'),('doctoral','School A'),('masters','School B')]:
        aggregate.merge_research(state,ResearchResult(programs=[ProgramResult(
            university='CMU',program='Computational Science and Engineering',degree=degree,home_unit=unit,intake='2027 Fall')]))
    assert len(state.research_result.programs)==3
    assert {p.home_unit for p in state.research_result.programs}=={'School A','School B'}


def test_migration_preserves_legacy_rows_and_refuses_destructive_downgrade():
    import runpy
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    engine=sa.create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql('CREATE TABLE research_programs (id TEXT PRIMARY KEY, university TEXT NOT NULL, '
                'program TEXT NOT NULL, intake TEXT NOT NULL, CONSTRAINT uq_research_program_identity UNIQUE (university,program,intake))')
            conn.exec_driver_sql("INSERT INTO research_programs VALUES ('legacy','CMU','CSE','2027 Fall')")
            migration=runpy.run_path(str(Path(__file__).parents[1]/'alembic/versions/0009_research_scope_identity.py'))
            with Operations.context(MigrationContext.configure(conn)):
                migration['upgrade']()
                assert conn.exec_driver_sql('SELECT degree,home_unit,program_subject,degree_award FROM research_programs').one()==('','','','')
                conn.exec_driver_sql("INSERT INTO research_programs (id,university,program,intake,degree) VALUES ('ms','CMU','CSE','2027 Fall','masters'),('phd','CMU','CSE','2027 Fall','doctoral')")
                with pytest.raises(RuntimeError,match='explicit data reconciliation'):
                    migration['downgrade']()
                assert conn.exec_driver_sql('SELECT COUNT(*) FROM research_programs').scalar()==3
    finally:engine.dispose()


def test_schema_and_identity_corrections_share_atomic_page_allowance():
    from opportunity_agent.v2.research.tool_ledger import RedisToolLedger
    from opportunity_agent.v2.research.repair import ToolFailure
    class Ledger(RedisToolLedger):
        def __init__(self):
            self.state={'schools':{},'signatures':{},'tools_used':0,'decisions_used':0,'tool_limit':6,'decision_limit':2,'calls':[]}
        async def _change(self, mutate):return mutate(self.state)
    async def run():
        ledger=Ledger()
        call=await ledger.reserve('school',tool='extract_program_facts',signature='first',extraction_key='page')
        await ledger.reserve_correction('school',call,'page','schema')
        assert ledger.state['tools_used']==2 and ledger.state['extractions']['page']==2
        with pytest.raises(ToolFailure,match='PAGE_EXTRACTION_LIMIT'):
            await ledger.reserve_correction('school',call,'page','identity')
        await ledger.finish('school',call,1)
        with pytest.raises(ToolFailure,match='PAGE_EXTRACTION_LIMIT'):
            await ledger.reserve('school',tool='extract_program_facts',signature='changed',extraction_key='page')
    asyncio.run(run())


def test_failed_identity_correction_preserves_first_pass_valid_fact(monkeypatch):
    monkeypatch.setenv('RESEARCH_PERSIST_FACTS','0');monkeypatch.setenv('RESEARCH_QUEUE_INGEST','0')
    selected=list(cases())[:2]
    class Model:
        enabled=True
        def __init__(self):self.requests=[]
        @property
        def calls(self):return len(self.requests)
        def generate_structured(self,model,**kwargs):
            self.requests.append(kwargs)
            if self.calls>1:raise TimeoutError('offline correction timeout')
            return model(facts=[selected[0][3], selected[1][3].model_copy(update={'program_quote':'Unlocated programme'})])
    async def run():
        _,page,target,_,_=selected[0]
        model=Model();result=ResearchResult()
        task=ResearchTaskSpec(query='deadline and GRE',requested_fields=['deadline','gre_policy'],as_of=date(2026,10,12))
        await ResearchService(None,llm=model)._accept_page(task,result,target,page)
        assert model.calls==2 and result.programs[0].deadline==date(2026,12,9)
        assert any(r['reason']=='program_quote_not_located' for r in result.diagnostics['web_field_rejections'])
    asyncio.run(run())
