import asyncio
import json

import httpx

from opportunity_agent.v2.research.official_search import search_official_index
from opportunity_agent.v2.research.web import OfficialWeb
from opportunity_agent.v2.research.repair import classify_failure


def test_index_search_filters_domains_credentials_and_preserves_real_titles(monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'fixture-token')
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'results': [
            {'url': 'https://cs.brown.edu/apply', 'title': 'Official programme admissions', 'content': 'untrusted snippet'},
            {'url': 'https://brown.edu.attacker.test/apply'},
            {'url': 'https://user:password@brown.edu/apply'},
            {'url': 'http://brown.edu/apply'},
            {'url': 'https://brown.edu:8443/apply'},
            {'url': 'https://cs.brown.edu/apply'},
        ]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            results = await search_official_index('Masters admissions', ['brown.edu'], client=client)
        assert len(results) == 1 and results[0]['title'] == 'Official programme admissions'
        assert requests[0]['include_domains'] == ['brown.edu']
        assert requests[0]['include_answer'] is False
    asyncio.run(run())


def test_real_index_hits_are_not_replaced_by_guessed_paths(monkeypatch):
    async def index(self, query, domains):
        return [('https://cs.brown.edu/real-unusual-path', 'Official programme')]
    async def sitemap(*args):
        raise AssertionError('Do not crawl the site when index already returned candidates')
    monkeypatch.setattr(OfficialWeb, '_search_engine', index)
    monkeypatch.setattr(OfficialWeb, '_sitemap_urls', sitemap)
    result = asyncio.run(OfficialWeb().search('deadline GRE', ['brown.edu']))
    assert [r['url'] for r in result['results']] == ['https://cs.brown.edu/real-unusual-path']
    assert result['diagnostic']['guessed_candidates'] == 0


def test_empty_discovery_returns_empty_not_fabricated_urls(monkeypatch):
    async def empty(*args): return []
    monkeypatch.setattr(OfficialWeb, '_search_engine', empty)
    monkeypatch.setattr(OfficialWeb, '_sitemap_urls', empty)
    result = asyncio.run(OfficialWeb().search('admissions', ['brown.edu']))
    assert result['results'] == []


def test_same_path_at_different_hosts_survives_real_search(monkeypatch):
    async def index(*args):
        return [('https://cs.brown.edu/apply','CS'),('https://engineering.brown.edu/apply','Engineering')]
    monkeypatch.setattr(OfficialWeb,'_search_engine',index)
    result = asyncio.run(OfficialWeb().search('masters', ['brown.edu']))
    assert len(result['results']) == 2


def test_modelscope_selects_only_its_own_token(monkeypatch):
    from opportunity_agent.config import llm_api_key
    monkeypatch.setenv('OPPORTUNITY_AGENT_DISABLE_DOTENV','1')
    monkeypatch.setenv('LLM_PROVIDER','modelscope')
    monkeypatch.setenv('LLM_API_KEY','wrong-provider-token')
    monkeypatch.setenv('MODELSCOPE_API_KEY','fixture-modelscope-token')
    assert llm_api_key() == 'fixture-modelscope-token'
    monkeypatch.delenv('MODELSCOPE_API_KEY')
    assert llm_api_key() is None


def test_missing_page_is_not_an_unknown_tool_failure():
    response = httpx.Response(404, request=httpx.Request('GET','https://brown.edu/missing'))
    exc = httpx.HTTPStatusError('not found',request=response.request,response=response)
    assert classify_failure(exc,'read_official_page').code == 'HTTP_404'


def test_model_permission_denial_is_not_a_webpage_fallback():
    response = httpx.Response(403, request=httpx.Request('POST','https://api-inference.modelscope.cn/v1/chat/completions'))
    exc = httpx.HTTPStatusError('verify account',request=response.request,response=response)
    assert classify_failure(exc,'extract_program_facts').code == 'SERVICE_AUTH_OR_QUOTA'
    assert classify_failure(exc,'read_official_page').code == 'HTTP_403'
