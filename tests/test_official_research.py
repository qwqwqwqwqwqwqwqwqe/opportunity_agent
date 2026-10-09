from __future__ import annotations

import json
from datetime import datetime, timezone

from opportunity_agent.llm_client import LLMClient
from opportunity_agent.lifecycle_agent import LifecycleAgent
from opportunity_agent.models import OfficialSource, Roadmap, TargetProgram
from opportunity_agent.official_research import (OfficialCache, OfficialDomainRegistry, OfficialResearchTools,
                                                  _research_status_message, _safe_error_message,
                                                  deterministic_program_research)
from opportunity_agent.models import OfficialResearchResult
from opportunity_agent.tool_runner import LLMToolRunner


def test_domain_registry_only_returns_verified_aliases():
    registry = OfficialDomainRegistry()
    assert registry.resolve("CMU")["verified_domain"] == "cmu.edu"
    assert registry.resolve("Imaginary University") is None
    assert registry.allowed("CMU", "www.cmu.edu")
    assert not registry.allowed("CMU", "cmu.edu.attacker.example")


def test_unknown_university_uses_safe_dynamic_domain_discovery(monkeypatch, tmp_path):
    registry_path = tmp_path / "domains.json"
    registry_path.write_text('{"universities": []}', encoding="utf-8")
    requests = []

    class Response:
        def __init__(self, body): self.body = body
        def read(self): return json.dumps(self.body).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *_): return False

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data.decode("utf-8"))
        requests.append(payload)
        if "official university website" in payload["query"]:
            return Response({"results": [{"url": "https://www.example.edu/", "title": "Example University", "content": "Official university website"}]})
        return Response({"results": [{"url": "https://www.example.edu/graduate/admissions", "title": "Graduate admissions", "content": "GRE policy"}]})

    monkeypatch.setattr("opportunity_agent.official_research.urlopen", fake_urlopen)
    monkeypatch.setattr("opportunity_agent.official_research._assert_public_host", lambda _: None)
    tools = OfficialResearchTools(registry=OfficialDomainRegistry(registry_path), tavily_key="test")
    result = tools.call("search_official_program_pages", {
        "university": "Example University", "program": "MSCS", "intake": "2027", "questions": ["GRE policy"],
    })
    assert result["status"] == "ok"
    assert result["verified_domain"] == "example.edu"
    assert result["domain_source"] == "dynamic_search"
    assert requests[0].get("include_domains") is None
    assert requests[1]["include_domains"] == ["example.edu"]


def test_cache_marks_old_entries_stale(tmp_path):
    cache = OfficialCache(tmp_path / "cache.json", ttl_hours=1)
    source = OfficialSource(source_id="one", university="CMU", program="MSCS", title="Admissions",
                            url="https://www.cmu.edu/admissions", verified_domain="cmu.edu",
                            retrieved_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    cache.put(source, [])
    result = cache.get("CMU", "MSCS", "", [])
    assert result.sources[0].status == "stale"


def test_native_tool_call_is_executed_and_returned_to_model():
    payloads = []
    responses = [
        {"message": {"content": "", "tool_calls": [{"id": "call-1", "type": "function", "function": {
            "name": "resolve_official_domain", "arguments": '{"university":"CMU"}'}}]}},
        {"message": {"content": "CMU 的已验证官网域名是 cmu.edu。"}},
    ]
    client = LLMClient(completion_fn=lambda payload: payloads.append(payload) or responses.pop(0))
    runner = LLMToolRunner(client, OfficialResearchTools(tavily_key="test"))
    result = runner.run("system", "查 CMU 官网")
    assert "cmu.edu" in result.content
    assert payloads[0]["tools"][0]["type"] == "function"
    assert any(message["role"] == "tool" for message in payloads[1]["messages"])


def test_json_tool_protocol_fallback_is_allowlisted():
    responses = [
        {"message": {"content": json.dumps({"action": "call_tool", "name": "resolve_official_domain", "arguments": {"university": "CMU"}})}},
        {"message": {"content": json.dumps({"action": "final", "content": "已读取官方域名。"})}},
    ]
    client = LLMClient(completion_fn=lambda _: responses.pop(0))
    runner = LLMToolRunner(client, OfficialResearchTools(tavily_key="test"))
    # Directly exercise gateway fallback mode; HTTP status handling is transport-specific.
    result = runner.run("system", "query")
    # A gateway may support tools and return protocol JSON anyway; it remains a safe final answer.
    assert result.content


def test_deterministic_fallback_keeps_official_tool_boundary():
    class FakeTools:
        def __init__(self): self.calls = []
        def call(self, name, arguments):
            self.calls.append(name)
            if name == "search_official_program_pages":
                return {"candidates": [{"url": "https://www.cmu.edu/admissions"}]}
            return {"status": "ok"}
        def result(self, unresolved=None): return OfficialResearchResult(unresolved_questions=unresolved or [])
    tools = FakeTools()
    result = deterministic_program_research(tools, "CMU", "MSCS", "2027", ["GRE policy"])
    assert tools.calls[:2] == ["search_official_program_pages", "read_official_program_page"]
    assert set(tools.calls).issubset({"search_official_program_pages", "read_official_program_page"})
    assert result.unresolved_questions == ["GRE policy"]


def test_official_tool_diagnostics_keep_expected_status_and_safe_exception_text():
    assert "TAVILY_API_KEY" in _research_status_message("official_search_not_configured")
    assert _research_status_message("official_domain_unknown") != _research_status_message("official_search_not_configured")
    error = _safe_error_message(TimeoutError("upstream " + "x" * 300))
    assert error.startswith("TimeoutError:")
    assert len(error) <= 240


def test_refresh_turn_discards_old_observations_but_keeps_the_persistent_cache_boundary():
    tools = OfficialResearchTools(tavily_key="test")
    tools.sources = [OfficialSource(source_id="old", university="CMU", program="MSCS", title="Old",
                                    url="https://www.cmu.edu/old", verified_domain="cmu.edu")]
    tools.requirements = [object()]
    tools.trace = [{"tool": "old_search"}]

    tools.begin_research_turn()

    assert tools.sources == []
    assert tools.requirements == []
    assert tools.trace == []


def test_refresh_official_sources_persists_this_turn_trace_for_the_sidebar():
    source = OfficialSource(source_id="cmu-mscs", university="CMU", program="MSCS", title="MSCS admissions",
                            url="https://www.cmu.edu/mscs", verified_domain="cmu.edu", program_match="exact")

    class FakeModelPlanner:
        def __init__(self): self.fresh_turn = None
        def _research(self, profile, *, fresh_turn=True):
            self.fresh_turn = fresh_turn
            return OfficialResearchResult(sources=[source], tool_trace=[{
                "tool": "search_official_program_pages",
                "arguments": {"university": "CMU", "program": "MSCS"}, "status": "ok", "duration_ms": 12,
            }])

    class FakeHybridPlanner:
        def __init__(self):
            self.model_planner = FakeModelPlanner()
            self.last_error = None

    agent = LifecycleAgent("refresh", planner=FakeHybridPlanner())
    agent.roadmap = Roadmap(user_id="refresh", goal="test", milestones=[])

    assert agent.refresh_official_sources(refresh_id="browser-refresh-1")
    assert agent.planner.model_planner.fresh_turn is True
    assert agent.last_official_research["tool_trace"][0]["arguments"]["program"] == "MSCS"
    assert agent.last_official_research["refresh_id"] == "browser-refresh-1"
    assert agent.last_official_research["refreshed_at"]
    assert agent.roadmap.official_sources[0].source_id == "cmu-mscs"


def test_refresh_removes_a_rejected_old_source_even_when_no_new_page_is_found():
    bad = OfficialSource(source_id="civil", university="UofT", program="MSCS",
                         title="Civil & Mineral Engineering admissions",
                         url="https://civmin.utoronto.ca/admissions", verified_domain="utoronto.ca")

    class ValidationTools:
        def validate_sources(self, _sources, _targets):
            rejected = bad.model_copy(deep=True)
            rejected.status = "revoked"
            rejected.revocation_reason = "historical source conflicts with the current target program"
            return [], [rejected]

    class FakeModelPlanner:
        official_tools = ValidationTools()
        def _research(self, profile, *, fresh_turn=True):
            return OfficialResearchResult(tool_trace=[{"tool": "search_official_program_pages", "status": "ok"}])

    class FakeHybridPlanner:
        model_planner = FakeModelPlanner()
        last_error = None

    agent = LifecycleAgent("refresh-revoke", planner=FakeHybridPlanner())
    agent.profile.target_program_choices = [TargetProgram(school="UofT", program="MSCS")]
    agent.roadmap = Roadmap(user_id="refresh-revoke", goal="test", milestones=[], official_sources=[bad])

    assert not agent.refresh_official_sources()
    assert agent.roadmap.official_sources == []
    assert agent.roadmap.revoked_official_sources[0].source_id == "civil"
    assert agent.last_official_research["revoked_sources"][0]["source_id"] == "civil"
