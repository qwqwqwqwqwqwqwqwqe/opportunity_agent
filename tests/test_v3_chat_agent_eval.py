"""The evaluation set is satisfiable and the harness scores runs without network or database."""
import asyncio
import json

import pytest

from opportunity_agent.v3.chat_agent_eval import DATASET, evaluate, page_html
from opportunity_agent.v3.monitor_web import parse_html
from test_v3_chat_actions import ScriptedModel


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr('opportunity_agent.v3.chat_search.SEARCH_RETRY_BACKOFF', 0)


def data():
    return json.loads(DATASET.read_text(encoding='utf-8'))


def test_dataset_expectations_are_satisfiable_from_fixture_pages():
    d = data()
    ids = [c['id'] for c in d['cases']]
    assert d['synthetic'] is True and len(ids) == 21 and len(set(ids)) == 21
    for case in d['cases']:
        assert set(case['pages']) <= set(d['pages']) and case['profile'] in d['profiles']
        assert set(case.get('hidden', [])) <= set(case['pages']) and set(case.get('distractors', [])) <= set(case['pages'])
        text = ' '.join(p for i in case['pages'] for p in d['pages'][i]['paragraphs']).casefold()
        expect = case['expect']
        assert expect['web'] == bool(case['pages'])
        for group in expect.get('contains', []):
            assert any(alt.casefold() in text for alt in group), case['id']
        profile = json.dumps(d['profiles'][case['profile']], ensure_ascii=False).casefold()
        for group in expect.get('personal', []):
            assert any(alt.casefold() in profile for alt in group), case['id']
        for value in expect.get('forbidden', []):
            assert value.casefold() in text, case['id']
    # Cross-page cases hide the answer page from search, so it is reachable only via links.
    for case in d['cases']:
        for hidden in case.get('hidden', []):
            url = d['pages'][hidden]['url']
            assert any(url in d['pages'][i]['links'] for i in case['pages'] if i != hidden)


def test_loop_case_scores_success_with_scripted_agent():
    async def scenario():
        d = data()
        page = d['pages']['msaii_curriculum']
        parsed = parse_html(page_html(page), page['url'])
        block = next(b for b in parsed.blocks if '195 units' in b['text'])
        model = ScriptedModel(
            {'intent':'search', 'reason':'官网查询', 'search':{'query':'课程总学分', 'search_keywords':'CMU MSAII curriculum units',
             'university':'CMU', 'program':'MSAII'}},
            {'tool':'read_official_page', 'arguments':{'url':page['url']}},
            {'tool':'finish', 'result':{'claims':[{'kind':'official_fact', 'text':'课程总计195 units。',
             'official_refs':[{'page_id':'pg_'+parsed.content_hash[:12], 'block_id':block['id'], 'quote':'195 units'}]}]}})
        report = await evaluate(model, ('loop',), case_ids={'f1'})
        row = report['modes']['loop']['cases'][0]
        assert row['success'], row
        assert row['model_calls'] == 3 and row['searches'] == 1 and row['fetches'] == 1 and not row['unsafe_write']
        assert report['modes']['loop']['summary']['success_rate'] == 1.0
    asyncio.run(scenario())


def test_read_only_query_route_never_searches():
    async def scenario():
        report = await evaluate(ScriptedModel({'intent':'query', 'scope':'profile'}), ('loop',), case_ids={'n1'})
        row = report['modes']['loop']['cases'][0]
        assert row['route_ok'] and row['success'] and row['searches'] == 0 and row['fetches'] == 0
    asyncio.run(scenario())


def test_wrong_route_for_web_question_is_not_counted_as_success():
    async def scenario():
        report = await evaluate(ScriptedModel({'intent':'query', 'scope':'profile'}), ('single',), case_ids={'p2'})
        row = report['modes']['single']['cases'][0]
        assert not row['route_ok'] and not row['success']
    asyncio.run(scenario())
