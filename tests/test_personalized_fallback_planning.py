from opportunity_agent.models import StudentProfile
from opportunity_agent.planning import ROADMAP_USER_PROMPT_TEMPLATE, build_roadmap
from opportunity_agent.profile import apply_facts
from opportunity_agent.models import CandidateFact
from opportunity_agent.state import derive_state


def _fact(field, value):
    return CandidateFact(field=field, raw_value=value, normalized_value=value,
                         source="conversation", confidence=0.99, evidence=str(value))


def test_recorded_toefl_score_is_saved_and_does_not_trigger_default_exam_task():
    profile = apply_facts(StudentProfile(user_id="toefl"), [_fact("toefl_score", 105)])
    roadmap = build_roadmap(profile, derive_state(profile))
    language_task = next(task for milestone in roadmap.milestones for task in milestone.tasks if task.task_id == "language_test")
    assert profile.toefl_score == 105
    assert derive_state(profile).language == "completed"
    assert "TOEFL 105" in language_task.title
    assert "门槛" in language_task.title
    assert "默认重复考试" in language_task.reason


def test_llm_roadmap_prompt_template_contains_profile_slot_and_score_policy():
    rendered = ROADMAP_USER_PROMPT_TEMPLATE.format(
        profile_json='{"toefl_score": 105}', facts_json="[]", state_json="{}",
        roadmap_json="null", revision_reason="test", today="2026-08-26",
    )
    assert "CURRENT_PROFILE" in rendered
    assert "toefl_score" in rendered


def test_detailed_article_uses_target_school_program_and_background():
    profile = StudentProfile(
        user_id="dream-school", academic_year=3, major="Economics", gpa=3.8,
        toefl_score=105, target_countries=["US", "Canada"],
        target_schools=["Stanford University", "多伦多大学"],
        target_programs=["MS Economics"], graduation_year=2027,
    )
    roadmap = build_roadmap(profile, derive_state(profile))
    assert len(roadmap.article) > 900
    assert "Stanford University" in roadmap.article
    assert "多伦多大学" in roadmap.article
    assert "MS Economics" in roadmap.article
    assert "GPA 3.80" in roadmap.article
    assert "TOEFL 105" in roadmap.article
    assert "不能直接断言" in roadmap.article
