import json

import pytest

from opportunity_agent.lifecycle_agent import LifecycleAgent
from opportunity_agent.llm_client import LLMClient
from opportunity_agent.models import CandidateFact, OfficialResearchResult, OfficialSource, StudentProfile, UserState
from opportunity_agent.planning import HybridRoadmapPlanner, ModelScopeRoadmapPlanner, build_roadmap
from opportunity_agent.roadmap_article_skill import (
    ArticleQualityError, RoadmapArticleDraft, RoadmapArticleSkill, RoadmapSkillLoader,
    MAX_ARTICLE_LENGTH, SECTION_MAX_LENGTHS, article_issues, build_article_context,
)


def _draft(label="详细规划"):
    text = (label + "依据用户已确认资料和时间轴，说明行动顺序、交付物、检查节点、风险以及下一步安排。") * 5
    return {
        "current_profile_goal": text,
        "gap_analysis": text,
        "current_stage_actions": text,
        "academic_research_internship": text,
        "application_materials_timeline": text,
        "risks_next_steps": text,
    }


def _profile():
    return {
        "school": "XDU", "major": "通信工程", "academic_year": 3, "degree_years": 4,
        "graduation_year": 2028, "target_countries": ["美国"], "target_degree": "MS",
        "target_fields": ["信号处理"], "planned_enrollment_year": 2028,
    }


def test_skill_is_progressively_loaded_and_cached(monkeypatch):
    monkeypatch.delenv("PLANNING_LLM_COMPONENTS", raising=False)
    loader = RoadmapSkillLoader()
    calls = []
    client = LLMClient(api_key="test", completion_fn=lambda payload: calls.append(payload) or json.dumps(_draft(), ensure_ascii=False), retries=0)
    skill = RoadmapArticleSkill(client, loader=loader)
    planner = ModelScopeRoadmapPlanner(llm_client=client, article_skill=skill)
    agent = LifecycleAgent("lazy-skill", planner=HybridRoadmapPlanner(planner))

    agent.on_onboarding(_profile())
    assert loader.load_count == 0 and calls == []
    agent.enrich_roadmap()
    assert loader.load_count == 1 and len(calls) == 1
    agent.replan_required = True
    assert agent.replan_with_llm()
    assert loader.load_count == 1 and len(calls) == 2


def test_model_system_comes_from_skill_and_user_payload_is_structured(monkeypatch):
    monkeypatch.delenv("PLANNING_LLM_COMPONENTS", raising=False)
    calls = []
    client = LLMClient(api_key="test", completion_fn=lambda payload: calls.append(payload) or json.dumps(_draft(), ensure_ascii=False), retries=0)
    agent = LifecycleAgent("skill-prompt", planner=HybridRoadmapPlanner(ModelScopeRoadmapPlanner(llm_client=client)))
    agent.on_onboarding(_profile())
    agent.enrich_roadmap()

    system = calls[0]["messages"][0]["content"]
    user = json.loads(calls[0]["messages"][1]["content"])
    assert "# Roadmap Article Skill" in system
    assert "current_profile_goal" in user["output_schema"]["properties"]
    assert "planning_context" in user["context"]
    assert "请以简体中文写一篇" not in calls[0]["messages"][1]["content"]


def test_v2_markdown_generation_keeps_prose_outside_the_json_contract():
    calls = []
    article = "# 个性化申请规划\n\n" + ("根据已确认画像安排阶段任务、交付物、复盘节点和风险控制。" * 55)
    client = LLMClient(api_key="test", completion_fn=lambda payload: calls.append(payload) or article, retries=0)
    skill = RoadmapArticleSkill(client)

    result = skill.generate_markdown(
        {"official_sources": [], "official_requirements": [], "timeline": {}},
        user_request="请制定完整申请规划", plan_kind="roadmap",
    )

    assert result == article
    assert calls[0]["messages"][1]["content"].startswith("{")
    assert "不要输出 JSON" in calls[0]["messages"][0]["content"]


def test_compact_context_keeps_latest_fact_and_truncates_source_excerpt():
    profile = StudentProfile(user_id="compact", major="计算机", completed_courses=["数据结构"])
    profile.facts = [
        CandidateFact(field="gpa", raw_value=3.7, normalized_value=3.7, confidence=.9, source="form", evidence=None),
        CandidateFact(field="gpa", raw_value=3.9, normalized_value=3.9, confidence=.95, source="user_explicit", evidence="GPA 3.9"),
    ]
    source = OfficialSource(source_id="src1", university="CMU", title="Admissions", url="https://cmu.edu/a",
                            verified_domain="cmu.edu", evidence_excerpt="x" * 900)
    timeline = build_roadmap(profile).timeline
    context = build_article_context(profile, UserState(), timeline,
                                    OfficialResearchResult(sources=[source]), "test", {"phases": []}, "2026-09-10")
    fact = next(item for item in context["latest_accepted_facts"] if item["field"] == "gpa")
    assert fact["value"] == 3.9 and fact["evidence"] == "GPA 3.9"
    assert context["profile"]["completed_courses"] == ["数据结构"]
    assert len(context["official_sources"][0]["evidence_excerpt"]) == 700


def test_quality_check_requires_every_sourced_school_in_materials_section():
    values = _draft()
    context = {"official_sources": [
        {"university": "CMU", "source_id": "cmu-1"},
        {"university": "UIUC", "source_id": "uiuc-1"},
    ]}
    issues = article_issues(RoadmapArticleDraft.model_validate(values), context)
    assert "application_materials_timeline" in issues
    values["application_materials_timeline"] += "CMU 按官网证据准备（来源：cmu-1）；UIUC 按官网证据准备（来源：uiuc-1）。"
    assert "application_materials_timeline" not in article_issues(RoadmapArticleDraft.model_validate(values), context)


def test_quality_check_accepts_a_detailed_article_above_the_old_1800_character_limit():
    """A useful personalised six-section roadmap must not be rejected for detail."""
    paragraph = "结合已确认画像、时间轴和可追溯证据，明确本阶段交付物、复盘节点与后续行动。"
    values = {name: paragraph * 16 for name in _draft()}
    draft = RoadmapArticleDraft.model_validate(values)

    assert len("".join(values.values())) > 1800
    assert not article_issues(draft, {"official_sources": []})


def test_initial_draft_reserves_time_for_a_possible_quality_repair(monkeypatch):
    class RecordingClient:
        timeout_seconds = 999
        retries = 0
        last_error = None

        def __init__(self):
            self.observed_timeouts = []

        def generate_structured(self, model_type, **_kwargs):
            self.observed_timeouts.append(self.timeout_seconds)
            return model_type.model_validate(_draft())

    # Keep elapsed time deterministic: a 210-second planning budget reserves
    # 45 seconds for a potential compact repair, leaving 165 for the draft.
    monkeypatch.setattr("opportunity_agent.roadmap_article_skill.time.monotonic", lambda: 0)
    client = RecordingClient()
    skill = RoadmapArticleSkill(client, timeout_seconds=210)

    skill.generate({"official_sources": []})

    assert client.observed_timeouts == [165]


def test_malformed_json_uses_only_one_repair_and_then_fails_quality():
    calls = []
    responses = iter(["not json", json.dumps({key: "短" for key in _draft()}, ensure_ascii=False)])
    client = LLMClient(api_key="test", completion_fn=lambda payload: calls.append(payload) or next(responses), retries=0)
    skill = RoadmapArticleSkill(client)
    with pytest.raises(ArticleQualityError):
        skill.generate({"official_sources": []})
    assert len(calls) == 2


def test_transport_timeout_is_not_retried():
    calls = []

    def timeout(payload):
        calls.append(payload)
        raise TimeoutError("slow")

    skill = RoadmapArticleSkill(LLMClient(api_key="test", completion_fn=timeout, retries=0))
    with pytest.raises(TimeoutError):
        skill.generate({"official_sources": []})
    assert len(calls) == 1


def test_overlong_valid_json_is_repaired_then_sentence_clamped_when_gateway_ignores_budget():
    long = "根据已确认资料安排明确行动、证据与复盘节点。" * 300
    responses = iter([
        json.dumps({name: long for name in _draft()}, ensure_ascii=False),
        json.dumps({"sections": {name: long for name in _draft()}}, ensure_ascii=False),
    ])
    client = LLMClient(api_key="test", completion_fn=lambda _: next(responses), retries=0)
    article = RoadmapArticleSkill(client, timeout_seconds=120).generate({"official_sources": []})
    assert len(article) <= MAX_ARTICLE_LENGTH
    assert all(len(part) <= limit + 40 for part, limit in zip(article.split("\n\n"), SECTION_MAX_LENGTHS.values(), strict=True))


def test_generation_system_prompt_contains_machine_enforced_budgets():
    calls = []
    client = LLMClient(api_key="test", completion_fn=lambda payload: calls.append(payload) or json.dumps(_draft(), ensure_ascii=False), retries=0)
    RoadmapArticleSkill(client).generate({"official_sources": []})
    assert "强制长度协议" in calls[0]["messages"][0]["content"]
    assert "current_stage_actions≤" in calls[0]["messages"][0]["content"]
