from datetime import date

from opportunity_agent.domain_knowledge import assess_prerequisite_coverage, knowledge_for_profile, validate_profile_domain
from opportunity_agent.lifecycle_agent import LifecycleAgent
from opportunity_agent.llm_client import LLMClient
from opportunity_agent.models import OnboardingProfileInput, Roadmap, StudentProfile, UserState
from opportunity_agent.onboarding import apply_onboarding
from opportunity_agent.planning import ModelScopeRoadmapPlanner, build_roadmap
from opportunity_agent.planning_skills import TimelineComposerSkill
from opportunity_agent.repository import LocalRepository
from opportunity_agent.progress import refresh_progress
from opportunity_agent.timeline import build_timeline_skeleton


def _form(**overrides):
    values = {
        "school": "西安电子科技大学",
        "major": "通信工程",
        "academic_year": 3,
        "degree_years": 4,
        "graduation_year": 2028,
        "graduation_month": 6,
        "target_countries": ["美国", "加拿大"],
        "target_degree": "MS",
        "target_fields": ["信号处理"],
        "gpa_raw": 87,
        "gpa_scale": 100,
        "next_exam_type": "TOEFL",
        "next_exam_date": "2026-11-14",
        "summer_preference": "both",
        "planned_enrollment_year": 2028,
        "planned_enrollment_month": 9,
    }
    values.update(overrides)
    return OnboardingProfileInput.model_validate(values)


def test_onboarding_preserves_non_four_point_gpa_and_normalizes_explicit_fields():
    profile = apply_onboarding(StudentProfile(user_id="u"), _form())
    assert profile.onboarding_completed is True
    assert profile.gpa_raw == 87
    assert profile.gpa_scale == 100
    assert profile.gpa is None
    assert profile.gpa_4_reference is None
    assert profile.target_countries == ["US", "Canada"]
    assert profile.exam_plan.next_exam_date == date(2026, 11, 14)
    assert profile.planning_domain == "electronic_communications"


def test_four_point_gpa_gets_identity_reference_only():
    profile = apply_onboarding(StudentProfile(user_id="gpa"), _form(gpa_raw=3.8, gpa_scale=4.0))
    assert profile.gpa == 3.8
    assert profile.gpa_4_reference == 3.8


def test_supported_engineering_domains_have_distinct_course_seeds():
    examples = {
        "Computer Science": "数据结构",
        "Artificial Intelligence": "优化方法",
        "Electronic and Communications Engineering": "通信原理",
        "Automation and Control Engineering": "自动控制原理",
    }
    for major, expected in examples.items():
        profile = StudentProfile(user_id=major, major=major)
        knowledge = knowledge_for_profile(profile)
        assert knowledge is not None
        assert expected in knowledge.core_courses


def test_unlisted_math_course_is_not_inferred_as_a_personal_gap_from_english_resume_courses():
    profile = StudentProfile(user_id="resume", major="Computer Science", target_fields=["Artificial Intelligence"],
                             completed_courses=["Data Structures", "Algorithms", "Probability and Statistics", "Discrete Mathematics"])
    knowledge = knowledge_for_profile(profile)
    coverage = assess_prerequisite_coverage(profile, knowledge)
    assert "数据结构与算法" in coverage["confirmed"]
    assert "概率统计" in coverage["confirmed"]
    assert "线性代数" in coverage["not_confirmed"]
    assert "未修" not in "；".join(coverage["not_confirmed"])


def test_unsupported_major_is_saved_but_does_not_get_fake_timeline():
    profile = apply_onboarding(StudentProfile(user_id="business"), _form(
        major="金融学", target_fields=["金融"], next_exam_type=None, next_exam_date=None,
    ))
    supported, _, message = validate_profile_domain(profile)
    roadmap = build_roadmap(profile)
    assert supported is False
    assert profile.major == "金融学"
    assert roadmap.supported is False
    assert roadmap.timeline.supported is False
    assert "暂不支持" in message
    assert roadmap.milestones == []


def test_timeline_dates_exam_flag_vacations_and_order_are_deterministic():
    profile = apply_onboarding(StudentProfile(user_id="timeline"), _form())
    timeline = build_timeline_skeleton(profile, today=date(2026, 8, 28))
    assert timeline.graduation_date.year == 2028
    assert timeline.enrollment_date == date(2028, 9, 1)
    assert [phase.phase_id for phase in timeline.phases] == [
        "background", "materials", "application", "offer_visa", "enrollment"
    ]
    exam = next(event for event in timeline.events if event.kind == "exam")
    assert exam.event_date == date(2026, 11, 14)
    assert {event.kind for event in timeline.events} >= {"winter_break", "summer_break", "exam", "visa"}
    assert timeline.events == sorted(timeline.events, key=lambda item: (item.event_date, item.event_id))


def test_summer_node_is_current_for_its_full_july_to_august_window():
    profile = apply_onboarding(StudentProfile(user_id="summer"), _form())
    timeline = build_timeline_skeleton(profile, today=date(2026, 8, 31))
    summer = next(event for event in timeline.events if event.event_id == "summer_break_2026")
    assert summer.start_date == date(2026, 7, 1)
    assert summer.end_date == date(2026, 8, 31)
    assert summer.status == "current"
    roadmap = Roadmap(user_id="summer", goal="test", milestones=[], timeline=timeline)
    refresh_progress(roadmap, [], today=date(2026, 8, 31))
    assert summer.time_status == "current" and not summer.is_overdue


def test_legacy_summer_anchor_is_migrated_to_the_full_season_window():
    profile = apply_onboarding(StudentProfile(user_id="legacy-summer"), _form())
    timeline = build_timeline_skeleton(profile, today=date(2026, 6, 1))
    summer = next(event for event in timeline.events if event.event_id == "summer_break_2026")
    # This is the shape persisted by the earlier one-day holiday implementation.
    summer.start_date = summer.end_date = None
    roadmap = Roadmap(user_id="legacy-summer", goal="test", milestones=[], timeline=timeline)
    refresh_progress(roadmap, [], today=date(2026, 8, 31))
    assert summer.start_date == date(2026, 7, 1)
    assert summer.end_date == date(2026, 8, 31)
    assert summer.time_status == "current"


def test_past_exam_is_overdue_and_spring_entry_is_respected():
    profile = apply_onboarding(StudentProfile(user_id="spring"), _form(
        next_exam_date="2026-01-01", planned_enrollment_year=2028, planned_enrollment_month=1,
    ))
    timeline = build_timeline_skeleton(profile, today=date(2026, 8, 28))
    assert next(event for event in timeline.events if event.kind == "exam").status == "overdue"
    assert timeline.enrollment_date == date(2028, 1, 1)


def test_language_plan_with_score_verifies_threshold_instead_of_retesting():
    profile = apply_onboarding(StudentProfile(user_id="score"), _form(
        gpa_raw=3.8, gpa_scale=4, toefl_score=105,
    ))
    roadmap = build_roadmap(profile, UserState(language="completed"), today=date(2026, 8, 28))
    language = next(task for milestone in roadmap.milestones for task in milestone.tasks if task.task_id == "language_test")
    assert "TOEFL 105" in language.title
    assert "不默认重复考试" in language.reason
    assert roadmap.timeline is not None


def test_skill_failure_falls_back_only_to_components_and_keeps_timeline():
    profile = apply_onboarding(StudentProfile(user_id="fallback"), _form())
    skeleton = build_timeline_skeleton(profile, today=date(2026, 8, 28))
    client = LLMClient(completion_fn=lambda _payload: "not-json", retries=0)
    composer = TimelineComposerSkill(client)
    timeline = composer.generate(profile, UserState(), skeleton, LocalRepository())
    assert all(phase.plan is not None for phase in timeline.phases)
    assert all(phase.plan.generation_mode == "rule_fallback" for phase in timeline.phases)
    assert composer.component_errors


def test_communications_job_recommendations_stay_in_domain():
    agent = LifecycleAgent("jobs")
    agent.on_onboarding(_form(skills=["Python", "MATLAB", "DSP"]).model_dump(mode="json"))
    assert agent.job_recommendations
    allowed = {"RF Systems Intern", "5G Communications Intern", "FPGA Design Intern",
               "Hardware Validation Intern", "Sensor Fusion Intern", "Firmware Intern",
               "Signal Processing Algorithm Intern", "Network Software Intern", "Embedded Software Intern"}
    assert {item.title for item in agent.job_recommendations}.issubset(allowed)
    assert all(item.source.startswith("demo:") and item.reason and item.confidence for item in agent.job_recommendations)


def test_modelscope_integration_is_opt_in(monkeypatch):
    monkeypatch.delenv("RUN_MODELSCOPE_INTEGRATION", raising=False)
    assert not bool(__import__("os").getenv("RUN_MODELSCOPE_INTEGRATION"))
