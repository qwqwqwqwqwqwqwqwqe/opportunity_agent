"""Router output contracts, current-turn authority and scoped recovery."""
import asyncio
import io
import json
from urllib.error import HTTPError

import pytest

from opportunity_agent.llm_client import LLMClient
from opportunity_agent.llm_context import conversation_scope
from opportunity_agent.v2.agents.contracts import ExecutionState, RouteDecision
from opportunity_agent.v2.agents.orchestrator import LLMRouter, LLMGoalParser, RouterUnavailable
from opportunity_agent.v2.research.task import parse_task
from types import SimpleNamespace


DECISION = {"mode": "delegate", "agents": ["research"], "parallel": False,
            "reason": "查询当前项目的截止日期", "resolved_query": None}
TARGETS = "CMU MSCS\nCMU MSAII\nUIUC MCS\nUCSD MSCS\nGeorgia Tech MSCS\nUBC MSc CS"


def state(message):
    return ExecutionState(user_id="u", conversation_id="c", run_id="r", request_id="q", message=message)


def structured(client, diagnostics=None):
    return client.generate_structured(RouteDecision, system="Return JSON", context={"user_message": "private query"},
                                      max_tokens=1536, diagnostics=diagnostics)


def test_truncation_retries_with_more_tokens_and_preserves_metadata():
    calls, diagnostics = [], []
    def completion(payload):
        calls.append(payload)
        if len(calls) == 1:
            return {"choices": [{"message": {"content": '{"mode":'}, "finish_reason": "length"}],
                    "usage": {"completion_tokens": 1536}}
        return json.dumps(DECISION)
    result = structured(LLMClient(completion_fn=completion, retries=1), diagnostics)
    assert result.agents == ["research"]
    assert [p["max_tokens"] for p in calls] == [1536, 3072]
    assert diagnostics[0]["error_code"] == "truncated_output"
    assert diagnostics[0]["finish_reason"] == "length"
    assert diagnostics[0]["usage"]["completion_tokens"] == 1536
    assert "private query" not in json.dumps(diagnostics)


def test_empty_content_is_distinct_from_invalid_json():
    responses = iter([{"message": {"content": None, "reasoning_content": "not final JSON"},
                       "finish_reason": "length", "usage": {"completion_tokens": 500}}, json.dumps(DECISION)])
    diagnostics = []
    structured(LLMClient(completion_fn=lambda _: next(responses), retries=1), diagnostics)
    assert diagnostics[0]["error_code"] == "empty_content"
    assert diagnostics[0]["content_chars"] == 0
    assert diagnostics[0]["finish_reason"] == "length"
    assert "not final JSON" not in json.dumps(diagnostics)


def test_router_schema_is_requested_at_generation_time():
    calls = []
    structured(LLMClient(completion_fn=lambda p: calls.append(p) or json.dumps(DECISION),
                         structured_output_mode="json_schema"))
    fmt = calls[0]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_unsupported_schema_downgrades_explicitly_but_still_validates():
    calls, diagnostics = [], []
    def completion(payload):
        calls.append(payload)
        if payload["response_format"]["type"] == "json_schema":
            raise HTTPError("https://example.invalid", 400, "bad request", {},
                            io.BytesIO(b'{"error":"response_format json_schema not supported"}'))
        return json.dumps(DECISION)
    structured(LLMClient(completion_fn=completion, retries=0, structured_output_mode="json_schema"), diagnostics)
    assert [p["response_format"]["type"] for p in calls] == ["json_schema", "json_object"]
    assert diagnostics[0]["error_code"] == "unsupported_output_format"
    assert diagnostics[-1]["status"] == "ok"


def test_unrelated_gateway_error_does_not_disable_output_contract():
    calls = []
    def completion(payload):
        calls.append(payload)
        raise HTTPError("https://example.invalid", 400, "bad request", {}, io.BytesIO(b'invalid model'))
    with pytest.raises(HTTPError):
        structured(LLMClient(completion_fn=completion, retries=0, structured_output_mode="json_schema"))
    assert len(calls) == 1


def test_schema_validation_still_rejects_unknown_agent():
    responses = iter([json.dumps({**DECISION, "agents": ["unknown"]}), json.dumps(DECISION)])
    diagnostics = []
    result = structured(LLMClient(completion_fn=lambda _: next(responses), retries=1), diagnostics)
    assert result.agents == ["research"]
    assert diagnostics[0]["error_code"] == "schema_validation"


def test_current_deadline_query_cannot_be_replaced_by_historical_gre_task():
    calls = []
    old = {**DECISION, "reason": "比较六个项目 GRE 政策", "resolved_query": "查询 CMU UIUC UCSD UBC GRE 政策"}
    client = LLMClient(completion_fn=lambda p: calls.append(p) or json.dumps(old))
    execution = state("帮我查询cmu msaii的截止日期")
    execution.recent_messages = [{"role": "user", "content": "六个学校不提交GRE的影响"}]
    execution.conversation_context = {"recent_messages": execution.recent_messages, "summary": "旧 GRE 任务"}
    async def run():
        with conversation_scope(execution.conversation_context):
            return await LLMRouter(client).route(execution)
    result = asyncio.run(run())
    assert result.resolved_query == execution.message
    assert "截止日期" in result.reason and "GRE" not in result.reason
    assert result.agents == ["research"]
    assert len(calls[0]["messages"]) == 2  # No ambient role/history injection.
    context = json.loads(calls[0]["messages"][-1]["content"])["context"]
    assert context["recent_messages"] == [] and context["summary"] == ""
    assert execution.routing_diagnostics["model_reason"] == old["reason"]


def test_invalid_model_output_recovers_only_for_explicit_research_request():
    client = LLMClient(completion_fn=lambda _: "", retries=0)
    execution = state("查询CMU MSAII截止日期")
    result = asyncio.run(LLMRouter(client).route(execution))
    assert result.resolved_query == execution.message
    assert execution.routing_diagnostics["source"] == "explicit_request_recovery"
    with pytest.raises(RouterUnavailable):
        asyncio.run(LLMRouter(client).route(state("继续")))


@pytest.mark.parametrize("content", ["", json.dumps({**DECISION, "mode": "direct_reply", "agents": [], "reason": "闲聊"})])
def test_clarification_target_list_keeps_prior_goal_and_exact_current_pairs(content):
    execution = state(TARGETS)
    execution.recent_messages = [
        {"role": "user", "content": "如果我不考虑GRE，会有什么影响？"},
        {"role": "assistant", "content": "请指定要评估的学校和具体项目，发我学校列表。"}]
    result = asyncio.run(LLMRouter(LLMClient(completion_fn=lambda _: content, retries=0)).route(execution))
    assert result.agents == ["research"]
    assert "GRE" in result.resolved_query and TARGETS in result.resolved_query
    task = parse_task(SimpleNamespace(message=result.resolved_query, success_criteria=None, missing_task=None, conversation_context={}))
    assert len(task.entities.targets) == 6
    assert not task.clarifications


def test_target_list_without_pending_research_clarification_does_not_trigger_recovery():
    execution = state(TARGETS)
    execution.recent_messages = [{"role": "assistant", "content": "请介绍你的申请目标，我会更新画像。"}]
    with pytest.raises(RouterUnavailable):
        asyncio.run(LLMRouter(LLMClient(completion_fn=lambda _: "", retries=0)).route(execution))


def test_goal_parser_does_not_receive_old_gre_history():
    calls = []
    client = LLMClient(completion_fn=lambda p: calls.append(p) or '{"evidence_required":true}')
    async def run():
        with conversation_scope({"recent_messages": [{"role": "user", "content": "不要GRE项目"}]}):
            return await LLMGoalParser(client).parse("查询CMU MSAII截止日期")
    criteria = asyncio.run(run())
    assert criteria.gre_policy == "any"
    assert len(calls[0]["messages"]) == 2


def test_router_diagnostics_event_is_retained_on_unrecoverable_failure():
    from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator, HeuristicGoalParser
    execution = state("继续")
    orchestrator = CustomOrchestrator(goal_parser=HeuristicGoalParser(),
        router=LLMRouter(LLMClient(completion_fn=lambda _: "", retries=0)))
    with pytest.raises(RouterUnavailable):
        asyncio.run(orchestrator.run(execution))
    diagnostic = next(e.payload for e in execution.events if e.type == "router_diagnostics")
    assert diagnostic["attempts"][0]["error_code"] == "empty_content"


def test_synthesis_timeout_cannot_replace_agent_failure_with_generic_partial():
    from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator, HeuristicGoalParser, HeuristicRouter
    class Agents:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("research transport timed out")
    class SlowSynthesizer:
        async def synthesize(self, state):
            await asyncio.sleep(1)
    execution = state("查询CMU MSAII截止日期")
    result = asyncio.run(CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
        agent_client=Agents(), synthesizer=SlowSynthesizer(), execution_budget_seconds=.05).run(execution))
    assert result.completion.status == "FAIL"
    assert "research transport timed out" in result.answer
    assert "执行时间预算" not in result.answer


def test_router_wall_budget_cancels_further_background_retries(monkeypatch):
    import time
    import opportunity_agent.v2.agents.orchestrator as module
    original_timeout = asyncio.timeout
    monkeypatch.setattr(module.asyncio, "timeout", lambda seconds: original_timeout(.01 if seconds == 45 else seconds))
    calls = []
    def completion(payload):
        calls.append(payload)
        time.sleep(.05)
        return ""
    execution = state("查询CMU MSAII截止日期")
    result = asyncio.run(LLMRouter(LLMClient(completion_fn=completion, retries=1)).route(execution))
    assert result.resolved_query == execution.message
    assert len(calls) == 1  # Cancelled invocation must not start its second request.
    assert execution.routing_diagnostics["error_type"] == "TimeoutError"


def test_token_usage_diagnostics_cannot_persist_arbitrary_response_text():
    diagnostics = []
    response = {"message": {"content": json.dumps(DECISION)}, "finish_reason": "stop",
                "usage": {"completion_tokens": 10, "echoed_prompt": "private text",
                          "completion_tokens_details": {"reasoning_tokens": 3, "raw_text": "secret"}}}
    structured(LLMClient(completion_fn=lambda _: response), diagnostics)
    assert diagnostics[0]["usage"] == {"completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 3}}
    assert "private text" not in json.dumps(diagnostics)


OUT_OF_SCOPE = {"mode": "delegate", "agents": ["research"], "parallel": False,
                "reason": "比较六个项目 GRE 政策",
                "resolved_query": "请查询并比较以下项目的 GRE 政策：CMU MSCS、CMU MSAII、"
                                  "UIUC MCS、UCSD MSCS、Georgia Tech MSCS、UBC MSc CS"}


def test_rewrite_introducing_unrelated_entities_is_discarded_but_route_survives():
    from opportunity_agent.v2.agents.orchestrator import _validated_rewrite
    execution = state("帮我查询cmu msaii的截止日期")
    decision = _validated_rewrite(execution, RouteDecision(**OUT_OF_SCOPE))
    # The route itself was correct; only the polluted query is dropped so that
    # request_from_state falls back to the user's own words.
    assert decision.resolved_query is None
    assert decision.agents == ["research"]
    rejected = execution.routing_diagnostics["rewrite_rejected"]
    assert "gre_policy" in rejected
    assert "university of illinois urbana-champaign" in rejected
    assert "carnegie mellon university" not in rejected  # Already in the current message.
    assert execution.routing_diagnostics["model_resolved_query"] == OUT_OF_SCOPE["resolved_query"]


def test_narrowing_rewrite_is_preserved():
    from opportunity_agent.v2.agents.orchestrator import _validated_rewrite
    execution = state("帮我查询cmu msaii的截止日期")
    narrowed = {**OUT_OF_SCOPE, "reason": "补充入学年份",
                "resolved_query": "查询 CMU MSAII 2027 Fall 的截止日期"}
    decision = _validated_rewrite(execution, RouteDecision(**narrowed))
    assert decision.resolved_query == narrowed["resolved_query"]
    assert "rewrite_rejected" not in execution.routing_diagnostics


def test_message_without_entities_cannot_be_validated_and_is_not_rejected():
    from opportunity_agent.v2.agents.orchestrator import _validated_rewrite
    execution = state("帮我看看吧")
    decision = _validated_rewrite(execution, RouteDecision(**OUT_OF_SCOPE))
    assert decision.resolved_query == OUT_OF_SCOPE["resolved_query"]
    assert execution.routing_diagnostics["rewrite_unverifiable"] is True
    assert "rewrite_rejected" not in execution.routing_diagnostics


def test_anaphoric_message_may_resolve_to_entities_outside_itself():
    from opportunity_agent.v2.agents.orchestrator import _validated_rewrite
    execution = state("这些项目的GRE政策呢？")
    decision = _validated_rewrite(execution, RouteDecision(**OUT_OF_SCOPE))
    assert decision.resolved_query == OUT_OF_SCOPE["resolved_query"]
    assert "rewrite_rejected" not in execution.routing_diagnostics


def test_router_applies_rewrite_validation_on_the_model_path():
    execution = state("帮我查询这个项目的截止日期")  # Anaphora keeps the pin guard out.
    client = LLMClient(completion_fn=lambda _: json.dumps(OUT_OF_SCOPE))
    result = asyncio.run(LLMRouter(client).route(execution))
    assert result.resolved_query == OUT_OF_SCOPE["resolved_query"]
    execution = state("查询CMU MSAII的学费")
    narrowed = {**OUT_OF_SCOPE, "resolved_query": "查询 CMU MSAII 的学费与截止日期"}
    result = asyncio.run(LLMRouter(LLMClient(completion_fn=lambda _: json.dumps(narrowed))).route(execution))
    assert result.resolved_query == execution.message  # Pin guard owns this shape.
