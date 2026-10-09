import asyncio
import json
import socket

import pytest
from pydantic import ValidationError

from opportunity_agent.a2a_protocol import (
    ExtractedFact, OpportunityDelegation, OpportunityMutationRequest, OpportunityMutationResult,
)
from opportunity_agent.conversation_store import ConversationStore
from opportunity_agent.lifecycle_agent import LifecycleAgent
from opportunity_agent.opportunity_a2a import create_opportunity_a2a_server, handle_mutation
from opportunity_agent.session_service import SessionService
from opportunity_agent.session_state import snapshot


def request_for(message="我的托福是105"):
    agent = LifecycleAgent("web_a2a-test")
    event = agent.begin_event(message, "chat", "request-a2a")
    delegation = OpportunityDelegation(
        route="profile_update",
        context_token="context-token-a2a",
        raw_message=message,
        facts=[ExtractedFact(
            field="toefl_score", raw_value=105, normalized_value=105,
            statement_kind="explicit", evidence=message,
        )],
        reason="用户明确陈述自己的成绩",
    )
    return OpportunityMutationRequest(
        conversation_id="a2a-test", request_id=event.request_id, base_revision=1,
        event_id=event.event_id, delegation=delegation, state_snapshot=snapshot(agent),
    )


def test_a2a_protocol_rejects_fabricated_evidence_and_unknown_fields():
    request = request_for()
    request.delegation.facts[0].evidence = "原文中不存在"
    with pytest.raises(ValidationError):
        OpportunityMutationRequest.model_validate(request.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        ExtractedFact(field="invented_field", raw_value="x", evidence="x")
    schema = OpportunityDelegation.model_json_schema()
    progress_schema = schema["$defs"]["ExtractedProgress"]["properties"]
    assert "confidence" not in progress_schema
    assert "needs_confirmation" not in progress_schema


def test_statement_kind_controls_application_without_model_confidence():
    explicit = request_for()
    result = handle_mutation(explicit)
    assert result.updated_state_snapshot["profile"]["toefl_score"] == 105
    assert result.accepted_facts[0].confidence == 0.95

    uncertain = request_for("我的托福可能是105")
    uncertain.delegation.facts[0].evidence = uncertain.delegation.raw_message
    uncertain.delegation.facts[0].statement_kind = "uncertain"
    uncertain = OpportunityMutationRequest.model_validate(uncertain.model_dump(mode="json"))
    result = handle_mutation(uncertain)
    assert result.updated_state_snapshot["profile"]["toefl_score"] is None
    assert result.pending_confirmations


def test_session_service_can_use_chat_orchestrator_without_legacy_extraction(tmp_path):
    class StubOrchestrator:
        def process(self, agent, message, event_id, base_revision, selected_target_id=None, **kwargs):
            assert message == "只是一个问题"
            agent.last_route = "direct_answer"
            event = next(item for item in agent.user_events if item.event_id == event_id)
            return agent._finish(event, "直接回答").reply

    service = SessionService(ConversationStore(tmp_path / "conversations.json"), StubOrchestrator())
    _, response = service.execute("/api/chat", {
        "session_id": "router", "request_id": "question", "message": "只是一个问题",
    })
    assert response["reply"] == "直接回答"
    assert response["last_route"] == "direct_answer"
    assert response["profile"]["facts"] == []


def test_real_openjiuwen_a2a_round_trip():
    pytest.importorskip("openjiuwenrust")
    from openjiuwenrust._rust import A2aClient

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    async def run():
        server = create_opportunity_a2a_server("127.0.0.1", port)
        await server.start(host="127.0.0.1", port=port)
        try:
            await asyncio.sleep(0.15)
            client = A2aClient(json.dumps({
                "endpoint": f"http://127.0.0.1:{port}/a2a/jsonrpc/", "streaming": True,
            }))
            try:
                request = request_for()
                wire = json.dumps({"query": request.model_dump_json(), "conversation_id": "a2a-test"})
                raw = await asyncio.to_thread(client.send_message, wire, 10.0)
            finally:
                client.destroy()
            return OpportunityMutationResult.model_validate_json(raw)
        finally:
            await server.stop()

    result = asyncio.run(run())
    assert result.accepted_facts[0].field == "toefl_score"
    assert result.updated_state_snapshot["profile"]["toefl_score"] == 105


def test_official_tool_failure_is_persisted_and_general_answer_is_labelled(monkeypatch):
    import opportunity_agent.chat_orchestrator as chat

    token = "official-tool-test"
    context = chat._RequestContext(
        conversation_id="conversation", request_id="request", base_revision=1,
        event_id="event", raw_message="CMU MSCS 需要 GRE 吗？",
        selected_target_id=None, state_snapshot={},
    )
    monkeypatch.setattr(chat._OFFICIAL_TOOLS, "call", lambda *_: (_ for _ in ()).throw(TimeoutError("Tavily timed out")))
    with chat._CONTEXT_LOCK:
        chat._CONTEXTS[token] = context
    try:
        response = chat._official_call("search_official_program_pages", token,
                                       university="CMU", program="MSCS", intake="2027",
                                       questions=["GRE policy"])
    finally:
        with chat._CONTEXT_LOCK:
            chat._CONTEXTS.pop(token, None)
    assert response["status"] == "error"
    assert "TimeoutError" in response["error"]
    saved = chat._official_research_snapshot(context)
    assert saved["tool_trace"][0]["status"] == "error"
    reply = chat._label_official_answer("可将 CMU 列入初筛。", context)
    assert reply.startswith("官网核验未完成")
    assert "非官网核验" in reply


def test_official_tool_strips_openjiuwen_call_metadata_before_schema_validation(monkeypatch):
    import opportunity_agent.chat_orchestrator as chat

    token = "official-tool-metadata"
    context = chat._RequestContext(
        conversation_id="conversation", request_id="request", base_revision=1,
        event_id="event", raw_message="CMU 官网", selected_target_id=None, state_snapshot={},
    )
    seen = {}
    monkeypatch.setattr(chat._OFFICIAL_TOOLS, "call", lambda name, args: seen.update(name=name, args=args) or {
        "status": "official_domain_unknown", "university": "CMU", "candidates": [],
    })
    with chat._CONTEXT_LOCK:
        chat._CONTEXTS[token] = context
    try:
        response = chat._official_call("resolve_official_domain", token, university="CMU",
                                       _ojw_host_tool_call_id="call_openjiuwen")
    finally:
        with chat._CONTEXT_LOCK:
            chat._CONTEXTS.pop(token, None)
    assert response["status"] == "official_domain_unknown"
    assert seen == {"name": "resolve_official_domain", "args": {"university": "CMU"}}
    assert context.official_calls[0]["tool_call_id"] == "call_openjiuwen"


def test_official_source_is_labelled_when_a_model_omits_a_citation():
    import opportunity_agent.chat_orchestrator as chat

    context = chat._RequestContext(
        conversation_id="conversation", request_id="request", base_revision=1,
        event_id="event", raw_message="", selected_target_id=None, state_snapshot={},
        official_calls=[{"tool": "read_official_program_page", "status": "ok"}],
        official_sources=[{"source_id": "cmu-guidelines", "title": "Guidelines", "url": "https://cmu.edu/g"}],
    )
    reply = chat._label_official_answer("官网页面给出了材料说明。", context)
    assert "[OfficialSource: cmu-guidelines]" in reply
