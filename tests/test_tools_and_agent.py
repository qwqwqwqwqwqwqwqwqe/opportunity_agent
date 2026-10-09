import asyncio
import json

from opportunity_agent.agent import OpportunityAgent
from opportunity_agent.repository import LocalRepository
from opportunity_agent.tools import OpportunityTools


def test_tools_return_json_serializable_structures():
    tools = OpportunityTools(LocalRepository())
    payload = {
        "profile": tools.get_user_profile("user_001"),
        "job": tools.get_job("job_nvidia_ai_intern"),
        "matches": tools.search_jobs(location="US", skills=["Python"]),
        "score": tools.match_job_to_user("user_001", "job_nvidia_ai_intern"),
    }
    assert json.loads(json.dumps(payload))["score"]["should_notify"] is True


def test_search_filters_all_requested_skills():
    jobs = OpportunityTools(LocalRepository()).search_jobs(skills=["Python", "PyTorch", "LLM"])
    assert jobs
    assert all({"Python", "PyTorch", "LLM"}.issubset(set(job["skills"])) for job in jobs)


def test_new_job_event_works_without_llm(monkeypatch):
    monkeypatch.delenv("OPPORTUNITY_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("OPPORTUNITY_AGENT_API_BASE", raising=False)
    monkeypatch.delenv("OPPORTUNITY_AGENT_MODEL", raising=False)
    result = asyncio.run(OpportunityAgent().on_new_job("user_001", "job_nvidia_ai_intern"))
    assert result.should_notify is True
    assert "NVIDIA - AI Engineer Intern" in result.message


def test_event_uses_stable_user_conversation_by_design(monkeypatch):
    """The public handler can be called twice for one user without local state mutation."""
    monkeypatch.delenv("OPPORTUNITY_AGENT_API_KEY", raising=False)
    agent = OpportunityAgent()
    first = asyncio.run(agent.on_new_job("user_001", "job_nvidia_ai_intern"))
    second = asyncio.run(agent.on_new_job("user_001", "job_nvidia_ai_intern"))
    assert first.user_id == second.user_id == "user_001"
    assert first.score == second.score
