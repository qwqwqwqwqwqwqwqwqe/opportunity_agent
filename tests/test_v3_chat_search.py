"""Website consent precedes page reads and summaries, through real chat APIs."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import importlib
import json
import logging
from uuid import uuid4

import httpx
import pytest

from opportunity_agent.v3.chat_actions import handle_actions
from opportunity_agent.v3.chat_search import ChatSearchService, SearchRequest, discovery_queries, search_error_details, website_control
from opportunity_agent.v3.monitor_web import parse_html, PageError
from opportunity_agent.v3.profile_assistant import ProfileAssistant
from opportunity_agent.v2.core import config
from opportunity_agent.v2.db.session import get_session
from test_v3_chat_actions import ScriptedModel
from test_v3_workflow import database

URL = 'https://www.ml.cmu.edu/academics/msml.html'
TEXT = 'For Fall 2027 applicants, the application deadline is December 15, 2026. Three recommendation letters are required.'


@pytest.fixture(autouse=True)
def no_real_search_backoff(monkeypatch):
    monkeypatch.setattr('opportunity_agent.v3.chat_search.SEARCH_RETRY_BACKOFF',0)


class Discovery:
    def __init__(self, items=None, error=None):
        self.items = items if items is not None else [{'url':URL,'title':'CMU MSML','content':'UNVERIFIED_SNIPPET_TOEFL_999'}]
        self.calls = [];self.error = error

    async def search(self, query, domains):
        self.calls.append((query,domains))
        if self.error:
            raise self.error
        return {'results':self.items}


class Fetcher:
    def __init__(self, error=None, redirect=None):
        self.calls = [];self.error = error;self.redirect = redirect

    async def fetch(self, url, hosts):
        self.calls.append((url,hosts))
        if self.error:
            raise self.error
        return parse_html('<html><main><h1>CMU MSML</h1><p>'+TEXT+'</p></main></html>',self.redirect or url)


def intent():
    return {'intent':'search','reason':'查询项目官网','search':{'query':'帮我查 CMU MSML Fall 2027 的截止日期',
        'search_keywords':'CMU MSML official admissions deadline Fall 2027','university':'CMU','program':'MSML'}}


def summary(quote=None):
    return {'claims':[{'text':'该页面说明 Fall 2027 的截止日期为2026年12月15日。','block_id':'b1','quote':quote or TEXT}]}


def initial(message='帮我查 CMU MSML 的截止日期', **extra):
    return {'message':message,'request_id':uuid4().hex,'workflow_snapshot':{'today':'2026-10-29'},**extra}


@pytest.mark.parametrize('message,control', [('确认第1个',('confirm',1)),('确认第一个官网',('confirm',1)),
    ('读取第2个链接',('confirm',2)),('取消查询',('cancel',None)),('确认官网',('confirm',None)),
    ('如果确认第1个会怎么样？',None),('网页上写着确认第1个',None),('确认第1个并保存项目',None)])
def test_website_control_is_scoped_to_whole_utterance(message,control):
    assert website_control(message)==control


def test_search_returns_candidates_without_fetch_or_summary_and_filters_domains():
    async def scenario():
        discovery = Discovery([{'url':URL,'title':'CMU MSML','content':'UNVERIFIED_SNIPPET_TOEFL_999'},
            {'url':URL,'title':'duplicate'}, {'url':'https://cmu.edu.attacker.example/admissions'},
            {'url':'https://127.0.0.1/private'}, {'url':'file:///secret'}, {'url':'http://www.cmu.edu/insecure'}])
        model = ScriptedModel(intent());fetcher = Fetcher()
        service = ChatSearchService(model,discovery,fetcher,clock=lambda:100)
        value = await handle_actions(model,initial(),service)
        assert value['completion']['status']=='NEED_USER' and not value['proposals']
        assert len(value['chat_search_pending']['candidates'])==1
        assert URL in value['answer'] and 'UNVERIFIED' not in value['answer']
        assert discovery.calls[0][1]==('cmu.edu',) and fetcher.calls==[]
        assert len(model.contexts)==1 and 'content' not in value['chat_search_pending']['candidates'][0]
        assert value['chat_search_trace'][0]['tool']=='tavily_search'
    asyncio.run(scenario())


def test_empty_search_uses_model_alternative_verbatim_with_same_official_domain():
    class RetryDiscovery(Discovery):
        async def search(self, query, domains):
            self.calls.append((query,domains))
            return {'results':[] if len(self.calls)==1 else self.items}
    async def scenario():
        planned=intent();planned['search']['alternative_keywords']=['Carnegie Mellon machine learning masters Fall 2027 deadline']
        model = ScriptedModel(planned);discovery = RetryDiscovery();fetcher = Fetcher()
        result = await handle_actions(model,initial(),ChatSearchService(model,discovery,fetcher))
        assert result['chat_search_pending'] and not fetcher.calls
        assert discovery.calls==[(planned['search']['search_keywords'],('cmu.edu',)),
            (planned['search']['alternative_keywords'][0],('cmu.edu',))]
        assert len(result['chat_search_trace'])==2
    asyncio.run(scenario())


def test_two_empty_basic_searches_can_recover_with_one_advanced_search():
    class AdvancedDiscovery(Discovery):
        async def search(self, query, domains):
            self.calls.append((query,domains,'basic'))
            return {'results':[]}
        async def search_advanced(self,query,domains):
            self.calls.append((query,domains,'advanced'))
            return {'results':self.items}
    async def scenario():
        planned=intent();planned['search']['alternative_keywords']=['CMU MSML Fall 2027 application deadline']
        model=ScriptedModel(planned);discovery=AdvancedDiscovery();fetcher=Fetcher()
        value=await handle_actions(model,initial(),ChatSearchService(model,discovery,fetcher))
        assert value['chat_search_pending'] and not fetcher.calls
        assert [t['search_depth'] for t in value['chat_search_trace']]==['basic','basic','advanced']
        assert [t['raw_result_count'] for t in value['chat_search_trace']]==[0,0,1]
        assert len(discovery.calls)==3 and all(c[1]==('cmu.edu',) for c in discovery.calls)
        assert all(c[0] in [planned['search']['search_keywords'],*planned['search']['alternative_keywords']] for c in discovery.calls)
    asyncio.run(scenario())


@pytest.mark.parametrize('keywords,alternative,suffix',[
    ('CMU MSML curriculum courses','Carnegie Mellon MSML course catalog','curriculum'),
    ('CMU MSML application materials','Carnegie Mellon MSML application checklist','admissions'),
    ('CMU MSML deferral policy Fall 2027','Carnegie Mellon machine learning masters defer admission Fall 2027','deferral'),
    ('CMU MSML health insurance dependents coverage','Carnegie Mellon masters student dependent insurance','insurance'),
    ('CMU MSML leave of absence internship international student','Carnegie Mellon machine learning masters internship leave policy','leave'),
])
def test_arbitrary_llm_queries_and_alternatives_reach_tavily_without_topic_mapping(keywords,alternative,suffix):
    class TargetDiscovery(Discovery):
        async def search(self,query,domains):
            self.calls.append((query,domains))
            if len(self.calls)==1:
                return {'results':[{'title':'MSML Handbook','url':'https://ml.cmu.edu/handbook.pdf'}]}
            return {'results':[{'title':'MSML '+suffix,'url':'https://ml.cmu.edu/'+suffix}]}
    async def scenario():
        planned=intent();planned['search'].update(query='CMU MSML 中文问题',search_keywords=keywords,alternative_keywords=[alternative])
        model=ScriptedModel(planned);discovery=TargetDiscovery();fetcher=Fetcher()
        value=await handle_actions(model,initial(),ChatSearchService(model,discovery,fetcher))
        assert value['chat_search_pending']['search_keywords']==keywords and not fetcher.calls
        assert [q for q,_ in discovery.calls]==[keywords,alternative]
        assert all(query!='CMU MSML' for query,_ in discovery.calls)
        assert value['chat_search_pending']['candidates'][0]['url'].endswith(suffix)
        assert value['chat_search_request']['search_keywords']==keywords
        assert [t['query_source'] for t in value['chat_search_trace']]==['llm.search_keywords','llm.alternative_keywords']
    asyncio.run(scenario())


def test_same_program_pages_keep_tavily_relevance_order_without_keyword_reranking():
    async def scenario():
        planned=intent();planned['search'].update(search_keywords='CMU MSML curriculum courses')
        discovery=Discovery([
            {'title':'CMU MSML course catalog','url':'https://ml.cmu.edu/courses'},
            {'title':'CMU MSML','url':URL},
        ])
        model=ScriptedModel(planned)
        value=await handle_actions(model,initial(),ChatSearchService(model,discovery,Fetcher()))
        assert value['chat_search_pending']['candidates'][0]['url'].endswith('/courses')
        assert discovery.calls==[('CMU MSML curriculum courses',('cmu.edu',))]
        assert 'topic' not in value['chat_search_request']
    asyncio.run(scenario())


def test_generic_homepage_survives_later_empty_model_queries_without_claiming_answer_found():
    class GenericOnly(Discovery):
        async def search(self,query,domains):
            self.calls.append((query,domains))
            return {'results':self.items if len(self.calls)==1 else []}
        async def search_advanced(self,query,domains):
            self.calls.append((query,domains))
            return {'results':[]}
    async def scenario():
        planned=intent();planned['search'].update(search_keywords='CMU MSML capstone project',
            alternative_keywords=['Carnegie Mellon machine learning masters capstone project'])
        model=ScriptedModel(planned);discovery=GenericOnly([{'title':'CMU Graduate Programs','url':'https://www.cmu.edu/graduate'}])
        value=await handle_actions(model,initial(),ChatSearchService(model,discovery,Fetcher()))
        assert value['chat_search_pending']['candidates'][0]['url']=='https://www.cmu.edu/graduate'
        assert '仍需你确认后读取正文核实' in value['answer']
        assert len(discovery.calls)==3 and all('capstone' in q for q,_ in discovery.calls[1:])
    asyncio.run(scenario())


def test_other_unit_title_cannot_outrank_program_in_tavily_results():
    class UnitDiscovery(Discovery):
        async def search(self,query,domains):
            self.calls.append((query,domains))
            if len(self.calls)==1:
                return {'results':[
                    {'title':'How To Apply - Information Networking Institute','url':'https://www.cmu.edu/ini/admissions/howtoapply.html'},
                    {'title':'CMU MSML','url':URL},
                ]}
            return {'results':[{'title':'CMU MSML Admission requirements','url':'https://ml.cmu.edu/admissions'}]}
    async def scenario():
        planned=intent();planned['search'].update(search_keywords='CMU MSML application requirements')
        discovery=UnitDiscovery();fetcher=Fetcher()
        value=await handle_actions(ScriptedModel(planned),initial(),ChatSearchService(ScriptedModel(),discovery,fetcher))
        candidates=value['chat_search_pending']['candidates']
        assert len(discovery.calls)==1 and candidates[0]['url']==URL
        assert candidates[-1]['url'].startswith('https://www.cmu.edu/ini/')
        assert '项目归属未确认' in value['answer'] and not fetcher.calls
    asyncio.run(scenario())


def test_specific_question_is_sent_verbatim_without_body_read():
    async def scenario():
        planned=intent();planned['search'].update(search_keywords='CMU MSML GRE policy Fall 2027')
        model=ScriptedModel(planned);fetcher=Fetcher()
        discovery=Discovery([{'title':'CMU MSML GRE policy','url':'https://ml.cmu.edu/gre-policy'}])
        value=await handle_actions(model,initial(),ChatSearchService(model,discovery,fetcher))
        assert discovery.calls==[('CMU MSML GRE policy Fall 2027',('cmu.edu',))]
        assert not fetcher.calls
    asyncio.run(scenario())


@pytest.mark.parametrize('keywords',[None,'','   '])
def test_missing_llm_keywords_never_fall_back_to_user_message_or_local_templates(keywords):
    async def scenario():
        planned=intent();planned['search']['search_keywords']=keywords
        model=ScriptedModel(planned);discovery=Discovery();fetcher=Fetcher()
        value=await handle_actions(model,initial(),ChatSearchService(model,discovery,fetcher))
        assert value['completion']['status']=='NEED_USER' and not discovery.calls and not fetcher.calls
    asyncio.run(scenario())


def test_query_planning_deduplicates_and_obeys_three_attempt_budget_without_rewriting():
    request=SearchRequest(search_keywords='University X MSML deferral + transfer credit 2027',
        alternative_keywords=['university x msml deferral + transfer credit 2027','University X machine learning masters deferral transfer credit 2027'])
    assert discovery_queries(request,advanced=True)==[
        (request.search_keywords,'basic'),(request.alternative_keywords[1],'basic'),(request.alternative_keywords[1],'advanced')]
    three=SearchRequest(search_keywords='one',alternative_keywords=['two','three'])
    assert discovery_queries(three,advanced=True)==[('one','basic'),('two','basic'),('three','basic')]


def test_legacy_pending_link_can_still_be_confirmed_without_new_keyword_fields():
    async def scenario():
        model=ScriptedModel(intent(),summary());fetcher=Fetcher()
        service=ChatSearchService(model,Discovery(),fetcher,clock=lambda:100)
        first=await handle_actions(model,initial(),service)
        legacy={k:v for k,v in first['chat_search_pending'].items() if k not in {'search_keywords','alternative_keywords'}}
        legacy.update(topic='old-topic',focus_terms=['old-term'])
        value=await handle_actions(model,initial('确认第1个',chat_search_pending=legacy),service)
        assert value['completion']['status']=='COMPLETED' and fetcher.calls
        assert model.contexts[-1]['search_query']==legacy['query']
    asyncio.run(scenario())


def test_empty_provider_result_does_not_blame_already_recognized_names():
    async def scenario():
        model=ScriptedModel(intent());discovery=Discovery([])
        value=await handle_actions(model,initial(),ChatSearchService(model,discovery,Fetcher()))
        assert '已识别学校' in value['answer'] and '无需重复补充' in value['answer']
        assert all(t['raw_result_count']==0 and t['rejected_counts']=={} for t in value['chat_search_trace'])
    asyncio.run(scenario())


@pytest.mark.parametrize('payload',[{'error':'upstream_failure'},{'unexpected':[]},{'results':None}])
def test_provider_error_or_bad_payload_is_not_reported_as_no_results(payload):
    class BadDiscovery(Discovery):
        async def search(self,query,domains):
            return payload
    async def scenario():
        model=ScriptedModel(intent())
        value=await handle_actions(model,initial(),ChatSearchService(model,BadDiscovery(),Fetcher()))
        assert value['completion']['status']=='PARTIAL' and '搜索服务暂时不可用' in value['answer']
        assert value['chat_search_trace'][0]['error_code'] in {'provider_error_response','invalid_provider_response'}
    asyncio.run(scenario())


def test_exception_group_retries_three_times_then_recovers_without_another_llm_call(caplog):
    class Flaky(Discovery):
        async def search(self,query,domains):
            self.calls.append((query,domains))
            if len(self.calls)<4:
                raise ExceptionGroup('MCP task group failed',[ExceptionGroup('connection',[TimeoutError('upstream read timed out')])])
            return {'results':self.items}
    async def scenario():
        model=ScriptedModel(intent());discovery=Flaky();fetcher=Fetcher()
        with caplog.at_level(logging.WARNING,logger='opportunity_agent.v3.chat_search'):
            value=await handle_actions(model,initial(),ChatSearchService(model,discovery,fetcher))
        assert len(model.contexts)==1 and len(discovery.calls)==4 and not fetcher.calls
        assert len(set(discovery.calls))==1 and value['chat_search_pending']
        trace=value['chat_search_trace']
        assert [t['status'] for t in trace]==['failed','failed','failed','completed']
        assert [t['attempt'] for t in trace]==[1,2,3,4]
        assert trace[0]['error_details']['exceptions'][0]['exceptions'][0]=={
            'type':'TimeoutError','message':'upstream read timed out'}
        assert len(caplog.records)==3 and 'upstream read timed out' in caplog.text
    asyncio.run(scenario())


def test_persistent_failure_stops_after_initial_plus_three_retries():
    async def scenario():
        model=ScriptedModel(intent());discovery=Discovery(error=ConnectionError('connection refused'));fetcher=Fetcher()
        value=await handle_actions(model,initial(),ChatSearchService(model,discovery,fetcher))
        assert value['completion']['status']=='PARTIAL' and len(discovery.calls)==4
        trace=value['chat_search_trace']
        assert [t['retry_scheduled'] for t in trace]==[True,True,True,False]
        assert trace[-1]['retry_stop_reason']=='retry_limit'
        assert len(set(discovery.calls))==1 and len(model.contexts)==1 and not fetcher.calls
    asyncio.run(scenario())


def test_nested_http_failure_details_are_saved_and_logged_without_credentials(monkeypatch,caplog):
    secret='private-key /?123'
    monkeypatch.setenv('TAVILY_API_KEY',secret)
    request=httpx.Request('GET','https://mcp.tavily.com/mcp/')
    error=httpx.HTTPStatusError('Server 503 at https://alice:password@mcp.tavily.com/mcp/?api_key=private-key&x=1 '
        'Bearer tvly-demo-secret api_key="other-secret" user@example.org '+secret,
        request=request,response=httpx.Response(503,request=request))
    error.__cause__=TimeoutError('upstream deadline exceeded')
    group=ExceptionGroup('MCP initialization failed',[error])
    group.search_stage='mcp_connect_initialize'
    async def scenario():
        with caplog.at_level(logging.WARNING,logger='opportunity_agent.v3.chat_search'):
            value=await handle_actions(ScriptedModel(intent()),initial(),ChatSearchService(ScriptedModel(),Discovery(error=group),Fetcher()))
        details=value['chat_search_trace'][0]['error_details']
        assert details['stage']=='mcp_connect_initialize'
        assert details['exceptions'][0]['http_status']==503
        assert details['exceptions'][0]['cause']['message']=='upstream deadline exceeded'
        saved=json.dumps(value['chat_search_trace'])+caplog.text
        for private in [secret,'alice:password','tvly-demo-secret','other-secret','user@example.org','?api_key=']:
            assert private not in saved
        assert 'https://mcp.tavily.com/mcp/' in saved and '[REDACTED]' in saved
    asyncio.run(scenario())


def test_error_details_bound_cycles_and_large_groups():
    error=RuntimeError('outer');error.__cause__=error
    assert search_error_details(error)['cause']['truncated']=='already_seen'
    group=ExceptionGroup('many',[ValueError(str(i)) for i in range(12)])
    details=search_error_details(group)
    assert len(details['exceptions'])==8 and details['omitted_exceptions']==4


def test_mcp_tool_error_retains_provider_reason_but_only_logs_redacted_details():
    from opportunity_agent.v2.research.web import TavilyMCP, MCPToolError
    class Session:
        async def call_tool(self,*args,**kwargs):
            return SimpleNamespace(isError=True,content=[SimpleNamespace(type='text',
                text='Quota exhausted for Bearer tvly-test-private')],structuredContent=None)
    async def scenario():
        tool=TavilyMCP();tool.session=Session();tool.tools={'tavily_search':('tavily_search',{'properties':{}})}
        with pytest.raises(MCPToolError) as caught:
            await tool._call('tavily_search',{})
        assert str(caught.value)=='MCP tool returned an error'
        details=search_error_details(caught.value)
        assert details['tool_error_message']=='Quota exhausted for Bearer [REDACTED]'
    asyncio.run(scenario())


def test_retry_budget_is_shared_between_primary_and_alternative_queries():
    class SharedFailures(Discovery):
        async def search(self,query,domains):
            self.calls.append((query,domains))
            if len(self.calls)==3:
                return {'results':[]}
            raise ConnectionError('temporary disconnect')
    async def scenario():
        planned=intent();planned['search']['alternative_keywords']=['CMU MSML deadline dates Fall 2027']
        discovery=SharedFailures()
        value=await handle_actions(ScriptedModel(planned),initial(),ChatSearchService(ScriptedModel(),discovery,Fetcher()))
        assert len(discovery.calls)==5 and value['completion']['status']=='PARTIAL'
        assert [q for q,_ in discovery.calls]==[planned['search']['search_keywords']]*3+[planned['search']['alternative_keywords'][0]]*2
        assert value['chat_search_trace'][-1]['retries_used']==3
    asyncio.run(scenario())


def test_time_budget_exhaustion_stops_retrying_without_waiting():
    times=[0]
    class Exhausted(Discovery):
        async def search(self,query,domains):
            self.calls.append((query,domains));times[0]+=56
            raise TimeoutError('total time consumed')
    async def scenario():
        discovery=Exhausted()
        service=ChatSearchService(ScriptedModel(),discovery,Fetcher(),monotonic=lambda:times[0])
        value=await handle_actions(ScriptedModel(intent()),initial(),service)
        assert len(discovery.calls)==1 and value['completion']['status']=='PARTIAL'
        assert value['chat_search_trace'][0]['retry_stop_reason']=='time_budget'
    asyncio.run(scenario())


def test_attempt_timeout_retries_with_fresh_invocations(monkeypatch):
    monkeypatch.setattr('opportunity_agent.v3.chat_search.SEARCH_ATTEMPT_TIMEOUT',.01)
    class Slow(Discovery):
        async def search(self,query,domains):
            self.calls.append((query,domains))
            await asyncio.sleep(1)
    async def scenario():
        discovery=Slow()
        value=await handle_actions(ScriptedModel(intent()),initial(),ChatSearchService(ScriptedModel(),discovery,Fetcher()))
        assert len(discovery.calls)==4 and all(t['error_type']=='TimeoutError' for t in value['chat_search_trace'])
        assert value['chat_search_trace'][0]['error_details']['message']
    asyncio.run(scenario())


def test_cancellation_is_not_swallowed_or_retried():
    class Cancelled(Discovery):
        async def search(self,query,domains):
            self.calls.append((query,domains))
            raise asyncio.CancelledError()
    async def scenario():
        discovery=Cancelled()
        with pytest.raises(asyncio.CancelledError):
            await handle_actions(ScriptedModel(intent()),initial(),ChatSearchService(ScriptedModel(),discovery,Fetcher()))
        assert len(discovery.calls)==1
    asyncio.run(scenario())


def test_previous_candidates_survive_exhausted_retry_on_alternative():
    class LaterFailure(Discovery):
        async def search(self,query,domains):
            self.calls.append((query,domains))
            if len(self.calls)==1:
                return {'results':[{'title':'CMU Graduate Programs','url':'https://www.cmu.edu/graduate'}]}
            raise ConnectionError('unavailable')
    async def scenario():
        planned=intent();planned['search']['alternative_keywords']=['CMU MSML application deadline']
        discovery=LaterFailure()
        value=await handle_actions(ScriptedModel(planned),initial(),ChatSearchService(ScriptedModel(),discovery,Fetcher()))
        assert len(discovery.calls)==5 and value['chat_search_pending']['candidates'][0]['url']=='https://www.cmu.edu/graduate'
        assert '部分补充检索失败' in value['answer']
    asyncio.run(scenario())


def test_filter_diagnostics_distinguish_rejected_results_from_empty_index():
    async def scenario():
        model=ScriptedModel(intent());discovery=Discovery([
            {'url':'https://attacker.example/program'},
            {'url':'https://ml.cmu.edu/handbook.pdf'},
            {'url':'https://www.lti.cs.cmu.edu/msaii.html','title':'Master of Science in Artificial Intelligence and Innovation'},
        ])
        value=await handle_actions(model,initial(),ChatSearchService(model,discovery,Fetcher()))
        assert not value['chat_search_pending'] and '没有通过' in value['answer']
        for trace in value['chat_search_trace']:
            assert trace['raw_result_count']==3 and trace['candidate_count']==0
            assert trace['rejected_counts']=={'outside_official_domain':1,'unsupported_document':1,'different_program':1}
    asyncio.run(scenario())


def test_program_metadata_rejects_other_degree_and_ranks_relevant_department_first():
    async def scenario():
        items = [
            {'title':'Masters Programs Admissions','url':'https://www.cmu.edu/graduate/admissions'},
            {'title':'Master of Science in Artificial Intelligence and Innovation','url':'https://www.lti.cs.cmu.edu/msaii.html'},
            {'title':'Master in Machine Learning','url':'https://ml.cmu.edu/academics/primary-ms-machine-learning-masters'},
        ]
        model = ScriptedModel(intent());fetcher = Fetcher()
        value = await handle_actions(model,initial(),ChatSearchService(model,Discovery(items),fetcher))
        candidates = value['chat_search_pending']['candidates']
        assert len(candidates)==2 and candidates[0]['url'].startswith('https://ml.cmu.edu/')
        assert all('msaii' not in c['url'] for c in candidates)
        assert not fetcher.calls
    asyncio.run(scenario())


def test_long_page_is_explicitly_partial_instead_of_silently_complete():
    class LargeFetcher(Fetcher):
        async def fetch(self, url, hosts):
            self.calls.append((url,hosts))
            return parse_html('<main><h1>CMU MSML</h1><p>'+TEXT+'</p>'+('<p>'+('Additional information. '*100)+'</p>')*60+'</main>',url)
    async def scenario():
        model = ScriptedModel(intent(),summary());fetcher = LargeFetcher()
        service = ChatSearchService(model,Discovery(),fetcher)
        first = await handle_actions(model,initial(),service)
        result = await handle_actions(model,initial('确认第1个',chat_search_pending=first['chat_search_pending']),service)
        assert result['completion']['status']=='PARTIAL' and '只读取了部分内容' in result['answer']
        assert result['chat_search_result']['truncated']
    asyncio.run(scenario())


def test_only_confirmed_candidate_is_read_and_exact_citations_are_required():
    async def scenario():
        model = ScriptedModel(intent(),summary());fetcher = Fetcher()
        service = ChatSearchService(model,Discovery(),fetcher,clock=lambda:100)
        first = await handle_actions(model,initial(),service)
        assert not fetcher.calls and len(model.contexts)==1
        answer = await handle_actions(model,initial('确认第1个',chat_search_pending=first['chat_search_pending']),service)
        assert answer['completion']['status']=='COMPLETED' and URL in answer['answer']
        assert answer['chat_search_pending'] is None and not answer['proposals']
        assert fetcher.calls==[(URL,['www.ml.cmu.edu'])]
        assert len(model.contexts)==2 and 'evidence_blocks' in model.contexts[-1]
        assert 'UNVERIFIED_SNIPPET' not in str(model.contexts[-1])
        assert answer['chat_search_result']['summary']['claims'][0]['quote']==TEXT
        assert answer['intent_recognition']['source']=='control'
    asyncio.run(scenario())


def test_semantic_confirmation_supports_natural_wording_and_checks_quote():
    async def scenario():
        model = ScriptedModel(intent(),{'intent':'search','search':{'operation':'confirm','candidate_index':1,
            'confirmation_quote':'第一个网站是对的，请继续总结'}},summary())
        fetcher = Fetcher();service = ChatSearchService(model,Discovery(),fetcher,clock=lambda:100)
        first = await handle_actions(model,initial(),service)
        result = await handle_actions(model,initial('第一个网站是对的，请继续总结',chat_search_pending=first['chat_search_pending']),service)
        assert result['completion']['status']=='COMPLETED' and len(fetcher.calls)==1
        # Model-generated confirmation quote absent from the user's message is not consent.
        bad_model = ScriptedModel({'intent':'search','search':{'operation':'confirm','candidate_index':1,'confirmation_quote':'确认第1个'}})
        bad_fetcher = Fetcher()
        denied = await handle_actions(bad_model,initial('如果确认第1个会怎么样？',chat_search_pending=first['chat_search_pending']),
            ChatSearchService(bad_model,Discovery(),bad_fetcher,clock=lambda:100))
        assert denied['completion']['status']=='NEED_USER' and not bad_fetcher.calls
    asyncio.run(scenario())


def test_ambiguous_selection_expiration_cancellation_and_other_conversation_do_not_read():
    async def scenario():
        model = ScriptedModel(intent());fetcher = Fetcher()
        service = ChatSearchService(model,Discovery([{'url':URL},{'url':'https://www.ml.cmu.edu/faq'}]),fetcher,clock=lambda:100)
        pending = (await handle_actions(model,initial(),service))['chat_search_pending']
        for text in ('确认官网','确认第5个'):
            result = await handle_actions(model,initial(text,chat_search_pending=pending),service)
            assert result['completion']['status']=='NEED_USER' and not fetcher.calls
        cancelled = await handle_actions(model,initial('取消查询',chat_search_pending=pending),service)
        assert cancelled['chat_search_pending'] is None and cancelled['completion']['status']=='COMPLETED'
        expired = {**pending,'expires_at':99}
        assert (await handle_actions(model,initial('确认第1个',chat_search_pending=expired),service))['chat_search_pending'] is None
        assert (await handle_actions(model,initial('确认第1个'),service))['completion']['status']=='NEED_USER'
        assert fetcher.calls==[]
    asyncio.run(scenario())


@pytest.mark.parametrize('kind',['bad_quote','bad_block','fetch_failure','search_failure'])
def test_tool_or_citation_errors_never_publish_unverified_answer(kind):
    async def scenario():
        value = summary('a fabricated quote') if kind=='bad_quote' else summary()
        if kind=='bad_block':
            value['claims'][0]['block_id']='not-a-block'
        model = ScriptedModel(intent(),value)
        fetcher = Fetcher(PageError('HTTP_403') if kind=='fetch_failure' else None)
        service = ChatSearchService(model,Discovery(error=TimeoutError() if kind=='search_failure' else None),fetcher,clock=lambda:100)
        first = await handle_actions(model,initial(),service)
        if kind=='search_failure':
            assert first['completion']['status']=='PARTIAL' and not fetcher.calls
            return
        result = await handle_actions(model,initial('确认第1个',chat_search_pending=first['chat_search_pending']),service)
        assert result['completion']['status']=='PARTIAL'
        assert '该页面说明' not in result['answer'] and 'UNVERIFIED' not in result['answer']
        assert not result['proposals'] and result['chat_search_pending']
    asyncio.run(scenario())


def test_redirect_requires_fresh_consent_before_summary():
    async def scenario():
        model = ScriptedModel(intent());fetcher = Fetcher(redirect='https://www.ml.cmu.edu/other-program')
        service = ChatSearchService(model,Discovery(),fetcher,clock=lambda:100)
        first = await handle_actions(model,initial(),service)
        second = await handle_actions(model,initial('确认第1个',chat_search_pending=first['chat_search_pending']),service)
        assert second['completion']['status']=='NEED_USER' and '跳转' in second['answer']
        assert second['chat_search_pending']['candidates'][0]['url'].endswith('other-program')
        assert len(model.contexts)==1  # No summarization of a different destination.
    asyncio.run(scenario())


def test_website_confirmation_cannot_accept_business_approval():
    async def scenario():
        model = ScriptedModel(intent(),summary());fetcher = Fetcher()
        service = ChatSearchService(model,Discovery(),fetcher,clock=lambda:100)
        pending = (await handle_actions(model,initial(),service))['chat_search_pending']
        args = {'chat_search_pending':pending,'chat_pending_approval_ids':['business-approval']}
        ambiguous = await handle_actions(model,initial('确认',**args),service)
        assert ambiguous['completion']['status']=='NEED_USER' and not fetcher.calls
        assert 'chat_approval_decision' not in ambiguous
        explicit = await handle_actions(model,initial('确认第1个官网',**args),service)
        assert explicit['completion']['status']=='COMPLETED' and not explicit['proposals']
        assert explicit['approval_ids']==['business-approval'] and 'chat_approval_decision' not in explicit
    asyncio.run(scenario())


@pytest.mark.parametrize('business_first',[False,True])
def test_http_search_consent_survives_run_reload_and_is_conversation_scoped(monkeypatch,business_first):
    api = importlib.import_module('opportunity_agent.v2.api.app')
    mode = replace(config.settings,app_mode='applyops')
    monkeypatch.setattr(config,'settings',mode);monkeypatch.setattr(api,'settings',mode)
    monkeypatch.setattr(api,'schedule_run',lambda *args:None)
    from opportunity_agent.llm_client import LLMClient
    monkeypatch.setattr(LLMClient,'enabled',property(lambda self:False))
    drafts = ([{'projects':[{'university':'CMU','name':'MSML','cycle':'2027 Fall'}]}] if business_first else [])
    model = ScriptedModel(*drafts,intent(),summary());discovery = Discovery();fetcher = Fetcher()
    assistant = ProfileAssistant(model,ChatSearchService(model,discovery,fetcher))
    monkeypatch.setattr(api,'orchestrator',assistant)
    async def scenario():
        engine,factory,_,_ = await database()
        monkeypatch.setattr(api,'SessionLocal',factory)
        async def dep():
            async with factory() as session:
                yield session
        api.app.dependency_overrides[get_session] = dep
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test') as client:
                owner = (await client.post('/api/v1/auth/register',json={'email':'search@test.local','password':'long-enough-password'})).json()['user']['id']
                conv = (await client.post('/api/v1/conversations',json={'title':'官网查询'})).json()['id']
                async def run(text,conversation=conv):
                    request = uuid4().hex
                    start = await client.post(f'/api/v1/conversations/{conversation}/runs',json={'message':text,'request_id':request})
                    identity = start.json()['run_id']
                    await api.execute_run(identity,owner,conversation,text,request)
                    result = (await client.get('/api/v1/runs/'+identity)).json()
                    assert result['status']=='completed',result
                    return result
                if business_first:
                    proposed = await run('添加 CMU MSML，2027 Fall')
                    assert len(proposed['approval_ids'])==1
                first = await run('帮我查 CMU MSML 的截止日期')
                assert first['intent_recognition']['intent']=='search'
                assert len(first['search_candidates'])==1 and not fetcher.calls
                assert len(first['approval_ids'])==(1 if business_first else 0)
                other = (await client.post('/api/v1/conversations',json={'title':'另一个对话'})).json()['id']
                assert (await run('确认第1个',other))['completion']['status']=='NEED_USER'
                assert not fetcher.calls
                if business_first:
                    ambiguous = await run('确认')
                    assert ambiguous['completion']['status']=='NEED_USER' and not fetcher.calls
                # A fresh assistant instance obtains consent state from persisted Run, not RAM.
                monkeypatch.setattr(api,'orchestrator',ProfileAssistant(model,ChatSearchService(model,discovery,fetcher)))
                answered = await run('确认第1个')
                assert answered['completion']['status']=='COMPLETED' and answered['search_result']['url']==URL
                assert len(fetcher.calls)==1 and len(answered['approval_ids'])==(1 if business_first else 0)
                snap = (await client.get('/api/v3/snapshot')).json()
                assert not snap['applications'] and not snap['tasks'] and not snap['calendar_events']
                assert 'event: search_summarized' in (await client.get('/api/v1/runs/'+answered['id']+'/events')).text
                if business_first:
                    saved = await run('确认保存')
                    assert saved['completion']['status']=='COMPLETED'
                    assert len((await client.get('/api/v3/snapshot')).json()['applications'])==1
                await client.post('/api/v1/auth/register',json={'email':'other-search@test.local','password':'long-enough-password'})
                assert (await client.get('/api/v1/runs/'+first['id'])).status_code==404
        finally:
            api.app.dependency_overrides.pop(get_session,None)
            await engine.dispose()
    asyncio.run(scenario())
