from opportunity_agent.config import chat_completions_url
import pytest

from opportunity_agent.lifecycle_agent import LifecycleAgent
from opportunity_agent.llm_client import LLMClient
from opportunity_agent.planning import HybridRoadmapPlanner, ModelScopeRoadmapPlanner
from opportunity_agent.session_state import snapshot
import json


def _draft(label="个性化规划"):
    section = (label + "应基于用户已确认的画像、时间轴和当前进展，给出具体行动、交付物、检查节点与先后关系。") * 5
    return json.dumps({
        "current_profile_goal": section,
        "gap_analysis": section,
        "current_stage_actions": section,
        "academic_research_internship": section,
        "application_materials_timeline": section,
        "risks_next_steps": section,
    }, ensure_ascii=False)


def _profile():
    return {
        "school": "XDU", "major": "通信工程", "academic_year": 3,
        "degree_years": 4, "graduation_year": 2028, "graduation_month": 6,
        "target_countries": ["美国"], "target_degree": "MS",
        "target_fields": ["信号处理"], "planned_enrollment_year": 2028,
        "planned_enrollment_month": 9,
    }


def test_yibu_base_url_is_normalized_to_chat_completions():
    assert chat_completions_url("https://yibuapi.com") == "https://yibuapi.com/v1/chat/completions"
    assert chat_completions_url("https://yibuapi.com/v1") == "https://yibuapi.com/v1/chat/completions"
    assert chat_completions_url("https://yibuapi.com/v1/chat/completions") == "https://yibuapi.com/v1/chat/completions"


def test_onboarding_returns_before_model_and_enrichment_uses_one_request(monkeypatch):
    monkeypatch.delenv("PLANNING_LLM_COMPONENTS", raising=False)
    calls = []
    article = _draft()
    client = LLMClient(api_key="test-key", model="gpt-5.5", base_url="https://yibuapi.com/v1",
                       completion_fn=lambda payload: calls.append(payload) or article, retries=0)
    planner = HybridRoadmapPlanner(ModelScopeRoadmapPlanner(llm_client=client))
    agent = LifecycleAgent("async", planner=planner)

    agent.on_onboarding(_profile())
    assert calls == []
    assert agent.roadmap is not None
    assert agent.planning_pending is True

    agent.enrich_roadmap()
    assert len(calls) == 1
    assert agent.planning_pending is False
    assert agent.roadmap.generation_mode == "qwen"
    assert "一、当前画像与目标" in agent.roadmap.article
    assert "六、风险、待核验项与下一步" in agent.roadmap.article


def test_manual_replan_replaces_only_with_successful_ai_result(monkeypatch):
    monkeypatch.delenv("PLANNING_LLM_COMPONENTS", raising=False)
    article = _draft("新的个性化规划")
    client = LLMClient(api_key="test-key", completion_fn=lambda _payload: article, retries=0)
    agent = LifecycleAgent("manual", planner=HybridRoadmapPlanner(ModelScopeRoadmapPlanner(llm_client=client)))
    agent.on_onboarding(_profile())
    original = agent.roadmap
    assert agent.replan_with_llm() is True
    assert agent.roadmap.version == original.version + 1
    assert "新的个性化规划" in agent.roadmap.article

    failing = LLMClient(api_key="test-key", completion_fn=lambda _payload: (_ for _ in ()).throw(TimeoutError("slow")), retries=0)
    agent.planner = HybridRoadmapPlanner(ModelScopeRoadmapPlanner(llm_client=failing))
    preserved = agent.roadmap
    assert agent.replan_with_llm() is False
    assert agent.roadmap is preserved
    assert "新的个性化规划" in agent.roadmap.article


def test_short_planning_article_is_retried_once_and_recovered(monkeypatch):
    monkeypatch.delenv("PLANNING_LLM_COMPONENTS", raising=False)
    calls = []
    incomplete = json.loads(_draft())
    incomplete["current_profile_goal"] = "规划摘要太短。"
    repaired = {"sections": {"current_profile_goal": ("重新生成的完整个性化工科规划，明确画像、目标和行动依据。" * 8)}}
    responses = iter([json.dumps(incomplete, ensure_ascii=False), json.dumps(repaired, ensure_ascii=False)])
    client = LLMClient(api_key="test-key", completion_fn=lambda payload: calls.append(payload) or next(responses), retries=0)
    agent = LifecycleAgent("short-retry", planner=HybridRoadmapPlanner(ModelScopeRoadmapPlanner(llm_client=client)))
    agent.on_onboarding(_profile())

    assert agent.replan_with_llm() is True
    assert len(calls) == 2
    repair_payload = json.loads(calls[1]["messages"][1]["content"])
    assert repair_payload["context"]["required_section_names"] == ["current_profile_goal"]
    assert "重新生成的完整" in agent.roadmap.article
    assert agent.planner.last_error is None


def test_planning_prompt_requires_a_personalized_six_section_article():
    from opportunity_agent.planning import ROADMAP_USER_PROMPT_TEMPLATE, _validate_planning_article
    from opportunity_agent.roadmap_article_skill import RoadmapSkillLoader

    skill = RoadmapSkillLoader().load("roadmap_article")
    for field in ("current_profile_goal", "gap_analysis", "current_stage_actions",
                  "academic_research_internship", "application_materials_timeline", "risks_next_steps"):
        assert field in skill.body
    assert "CURRENT_PROFILE" in ROADMAP_USER_PROMPT_TEMPLATE
    assert "1,800–3,600" in skill.body
    with pytest.raises(ValueError, match="too short"):
        _validate_planning_article("具体规划。" * 99)
    _validate_planning_article("具体规划。" * 120)


def test_two_short_articles_keep_previous_plan_and_expose_safe_reason(monkeypatch):
    monkeypatch.delenv("PLANNING_LLM_COMPONENTS", raising=False)
    calls = []
    short = json.dumps({key: "仍然太短。" for key in (
        "current_profile_goal", "gap_analysis", "current_stage_actions",
        "academic_research_internship", "application_materials_timeline", "risks_next_steps")}, ensure_ascii=False)
    client = LLMClient(api_key="test-key", completion_fn=lambda payload: calls.append(payload) or short, retries=0)
    agent = LifecycleAgent("short-fail", planner=HybridRoadmapPlanner(ModelScopeRoadmapPlanner(llm_client=client)))
    agent.on_onboarding(_profile())
    previous = agent.roadmap

    assert agent.replan_with_llm() is False
    assert agent.roadmap is previous
    assert len(calls) == 2
    state = snapshot(agent)
    assert state["planning_error_code"] == "article_sections_incomplete"
    assert "章节质量校验失败" in state["planning_error_message"]
