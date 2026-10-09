from __future__ import annotations

import asyncio
import json
import socket
from datetime import date
from types import SimpleNamespace

import pytest

from opportunity_agent.v2.agents.a2a import (
    DomainA2ARequest,
    OpenJiuwenDomainAgents,
    _interface_url,
    _python_envelope,
    create_domain_a2a_server,
    request_from_state,
)
from opportunity_agent.v2.agents.contracts import Evidence, ExecutionState, ProgramResult
from opportunity_agent.v2.agents.orchestrator import (
    CustomOrchestrator,
    DeterministicSynthesizer,
    HeuristicGoalParser,
    HeuristicRouter,
)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _state(message: str, *, profile_payload: dict | None = None, memory: dict | None = None) -> ExecutionState:
    return ExecutionState(
        user_id="user-1",
        conversation_id="conversation-1",
        run_id="run-1",
        request_id="request-1",
        message=message,
        profile_payload=profile_payload or {},
        memory=memory or {},
    )


def test_research_a2a_request_is_denied_a_profile_snapshot():
    state = _state("查 CMU MSCS 的截止日期", profile_payload={"toefl_score": 105})
    request = request_from_state("research", state)

    assert request.profile_payload == {}
    with pytest.raises(ValueError, match="research requests"):
        DomainA2ARequest.model_validate({**request.model_dump(), "profile_payload": {"toefl_score": 105}})


def test_python_a2a_artifact_envelope_is_read_without_rust_binding():
    response = {
        "agent": "research",
        "run_id": "run-1",
        "request_id": "request-1",
        "result": {"programs": [], "route": "stub", "status": "no_results"},
    }
    aggregate = SimpleNamespace(artifacts=[SimpleNamespace(parts=[SimpleNamespace(text=json.dumps(response))])])

    envelope = _python_envelope(aggregate)

    assert envelope.agent == "research"
    assert envelope.result["status"] == "no_results"


def test_a2a_backend_must_be_python_or_rust():
    with pytest.raises(ValueError, match="JIUWEN_BACKEND"):
        OpenJiuwenDomainAgents(backend="unsupported")


def test_agent_card_never_advertises_the_wildcard_bind_address():
    assert _interface_url("0.0.0.0", 8771) == "http://127.0.0.1:8771/a2a/jsonrpc/"
    assert _interface_url("0.0.0.0", 8771, "profile") == "http://profile:8771/a2a/jsonrpc/"


def test_python_openjiuwen_profile_and_planning_round_trip():
    pytest.importorskip("openjiuwen")
    profile_port, planning_port = _free_port(), _free_port()

    async def scenario():
        servers = [
            create_domain_a2a_server("profile", port=profile_port, backend="python"),
            create_domain_a2a_server("planning", port=planning_port, backend="python"),
        ]
        for server, port in zip(servers, (profile_port, planning_port), strict=True):
            await server.start(host="127.0.0.1", port=port)
        client = OpenJiuwenDomainAgents(
            endpoints={"profile": f"http://127.0.0.1:{profile_port}/a2a/jsonrpc/",
                       "planning": f"http://127.0.0.1:{planning_port}/a2a/jsonrpc/"},
            timeouts={"profile": 20, "planning": 60}, backend="python",
        )
        try:
            profile = await client.execute("profile", _state("我的托福考了105分"))
            planning = await client.execute("planning", _state(
                "请制定申请规划", profile_payload={
                    "major": "Computer Science", "onboarding_completed": True,
                    "target_countries": ["US"], "target_degree": "MS", "target_fields": ["AI"],
                },
            ))
            return profile, planning
        finally:
            await client.aclose()
            for server in reversed(servers):
                await server.stop()

    profile, planning = asyncio.run(scenario())
    assert profile.proposals and profile.proposals[0]["type"] == "profile.change"
    assert planning.status == "complete" and planning.tasks


def test_real_openjiuwen_v2_domain_agents_round_trip(monkeypatch):
    monkeypatch.setenv("RESEARCH_ALLOW_SEED_FIXTURES", "1")
    pytest.importorskip("openjiuwenrust")
    profile_port, research_port, planning_port = _free_port(), _free_port(), _free_port()

    async def scenario():
        servers = [
            create_domain_a2a_server("profile", port=profile_port, backend="rust"),
            create_domain_a2a_server("research", port=research_port, backend="rust"),
            create_domain_a2a_server("planning", port=planning_port, backend="rust"),
        ]
        for server, port in zip(servers, (profile_port, research_port, planning_port), strict=True):
            await server.start(host="127.0.0.1", port=port)
        client = OpenJiuwenDomainAgents(
            endpoints={
                "profile": f"http://127.0.0.1:{profile_port}/a2a/jsonrpc/",
                "research": f"http://127.0.0.1:{research_port}/a2a/jsonrpc/",
                "planning": f"http://127.0.0.1:{planning_port}/a2a/jsonrpc/",
            },
            timeouts={"profile": 10, "research": 10, "planning": 10},
            backend="rust",
        )
        evidence = Evidence(
            source_id="seed-cmu",
            url="https://www.cmu.edu/admissions",
            authority="official",
            relevance_score=0.95,
            retrieved_at=date(2026, 1, 1),
        )
        research_state = _state(
            "查询 CMU MSCS 截止日期",
            profile_payload={"toefl_score": 105},
            memory={"seed_research_programs": [ProgramResult(
                university="CMU", program="MSCS", gre_policy="optional", evidence=[evidence],
            ).model_dump(mode="json")]},
        )
        try:
            profile = await client.execute("profile", _state("我的托福考了105分"))
            research = await client.execute("research", research_state)
            planning = await client.execute("planning", _state(
                "请帮我制定申请时间线",
                profile_payload={
                    "major": "Computer Science", "onboarding_completed": True,
                    "target_countries": ["US"], "target_degree": "MS", "target_fields": ["AI"],
                },
            ))
        finally:
            await client.aclose()
            for server in reversed(servers):
                await server.stop()
        return profile, research, planning

    profile, research, planning = asyncio.run(scenario())

    assert profile.status == "complete"
    assert profile.proposals and profile.proposals[0]["type"] == "profile.change"
    assert research.status == "complete"
    assert research.programs[0].university == "CMU"
    assert planning.status == "complete"
    assert planning.timeline


def test_custom_orchestrator_executes_profile_through_real_a2a():
    pytest.importorskip("openjiuwenrust")
    port = _free_port()

    async def scenario():
        server = create_domain_a2a_server("profile", port=port, backend="rust")
        await server.start(host="127.0.0.1", port=port)
        client = OpenJiuwenDomainAgents(
            endpoints={
                "profile": f"http://127.0.0.1:{port}/a2a/jsonrpc/",
                "research": "http://127.0.0.1:1/a2a/jsonrpc/",
                "planning": "http://127.0.0.1:1/a2a/jsonrpc/",
            },
            timeouts={"profile": 10, "research": 10, "planning": 10},
            backend="rust",
        )
        try:
            return await CustomOrchestrator(
                goal_parser=HeuristicGoalParser(),
                router=HeuristicRouter(),
                agent_client=client,
                synthesizer=DeterministicSynthesizer(),
            ).run(_state("我的托福考了105分"))
        finally:
            await client.aclose()
            await server.stop()

    result = asyncio.run(scenario())

    assert result.profile_result is not None
    assert result.profile_result.status == "complete"
    assert result.result_history[0].agent == "profile"
    assert result.agent_failures == []
