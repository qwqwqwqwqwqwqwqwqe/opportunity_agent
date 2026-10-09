from datetime import date, datetime, timezone

from opportunity_agent.lifecycle_agent import LifecycleAgent
from opportunity_agent.lifecycle_tools import LifecycleTools, register_rust_lifecycle_tools
from opportunity_agent.main import run_interactive
from opportunity_agent.models import CandidateFact, StudentProfile
from opportunity_agent.profile import HybridFactExtractor, ProfileExtractor, apply_facts
from opportunity_agent.planning import HybridRoadmapPlanner, build_roadmap
from opportunity_agent.models import ExternalEvent


def complete_onboarding(agent: LifecycleAgent, graduation_year: int = 2029) -> str:
    """Answer the remaining foundation questions required for automatic planning."""
    agent.on_user_message("GPA 3.9，排名 5/120")
    agent.on_user_message("我还没开始准备托福，也没有科研经历。")
    return agent.on_user_message(str(graduation_year))


def test_progressive_profile_creates_auditable_facts_and_state():
    agent = LifecycleAgent("student_001")
    reply = agent.on_user_message("我是大一 CS，想申请美国 AI 硕士。")
    assert "GPA" in reply
    final_reply = complete_onboarding(agent)
    assert "路线图 v1" in final_reply
    assert agent.profile.academic_year == 1
    assert agent.profile.target_countries == ["US"]
    assert agent.state.academic == "early_undergraduate"
    assert agent.state.application == "exploring"
    assert {fact.field for fact in agent.profile.facts} >= {"academic_year", "major", "target_degree"}
    assert agent.profile.graduation_year == 2029


def test_research_message_updates_only_research_state():
    agent = LifecycleAgent("student_001")
    agent.on_user_message("我是大一 CS，想申请美国 AI 硕士。")
    complete_onboarding(agent)
    agent.on_user_message("我最近开始做 LLM 科研。")
    assert agent.state.research == "building"
    assert agent.profile.target_degree == "MS"
    assert agent.roadmap.version == 1
    assert agent.replan_required is True
    research_task = next(task for milestone in agent.roadmap.milestones for task in milestone.tasks if task.task_id == "research_progress")
    assert research_task.execution_status == "in_progress"


def test_deadline_event_replans_and_notifies():
    agent = LifecycleAgent("student_001")
    agent.on_user_message("我是大一 CS，想申请美国 AI 硕士。")
    complete_onboarding(agent)
    event = ExternalEvent(
        event_id="event_1", type="program_deadline_changed", title="Official deadline updated",
        source_url="https://example.edu/program", published_at=datetime.now(timezone.utc),
        program_name="Example MS AI", old_deadline=date(2029, 12, 15), new_deadline=date(2029, 11, 1),
    )
    decision = agent.on_external_event(event, today=date(2029, 8, 15))
    tasks = {task.task_id: task for milestone in agent.roadmap.milestones for task in milestone.tasks}
    assert tasks["submit_application"].due_date == date(2029, 11, 1)
    assert tasks["shortlist_programs"].due_date == date(2029, 8, 30)
    assert decision.action == "immediate"
    assert decision.score >= 0.8


def test_missing_high_value_profile_field_is_asked_first():
    agent = LifecycleAgent("student_002")
    assert agent.on_user_message("我想出国。") == "你现在是本科第几年？"


def test_lifecycle_tools_only_return_json_safe_data():
    tools = LifecycleTools(LifecycleAgent("student_003"))
    payload = tools.process_user_message("我是大一 CS，想申请美国 AI 硕士。")
    assert payload["state"]["academic"] == "early_undergraduate"
    assert payload["profile"]["facts"]


def test_lifecycle_tools_register_with_openjiuwenrust():
    cards = register_rust_lifecycle_tools(LifecycleTools(LifecycleAgent("runtime_probe")))
    assert set(cards) == {
        "process_user_message",
        "get_user_state",
        "generate_roadmap",
        "process_external_event",
        "recommend_jobs",
    }


def test_interactive_onboarding_asks_only_missing_high_value_fields():
    answers = iter(["我想出国。", "大一", "CS", "硕士", "美国", "AI", "GPA 3.9", "还没开始托福，也没有科研", "2029"])
    output: list[str] = []
    agent = run_interactive(input_fn=lambda _: next(answers), output_fn=output.append)
    assert agent.roadmap is not None
    assert agent.profile.target_countries == ["US"]
    assert any("本科第几年" in line for line in output)
    assert any("专业" in line for line in output)


def test_hybrid_extractor_does_not_call_model_when_rules_are_sufficient():
    class FakeModel:
        last_error = None

        def __init__(self):
            self.called = False

        def extract(self, _message):
            self.called = True
            return [
                CandidateFact(field="academic_year", value=4, source="model", confidence=0.99),
                CandidateFact(field="gpa", value=3.8, source="model", confidence=0.96),
            ]

    model = FakeModel()
    facts = HybridFactExtractor(ProfileExtractor(), model).extract("我是大一 CS")
    by_field = {fact.field: fact for fact in facts}
    assert by_field["academic_year"].value == 1
    assert "gpa" not in by_field
    assert model.called is False


def test_gpa_with_rank_text_is_parsed_without_crashing():
    profile = LifecycleAgent("gpa_student").profile
    facts = [CandidateFact(field="gpa", value="3.9, Rank 5/120", source="model", confidence=0.98)]
    updated = apply_facts(profile, facts)
    assert updated.gpa == 3.9


def test_gpa_and_bare_rank_advance_to_the_next_question():
    agent = LifecycleAgent("gpa_rank_student")
    agent.on_user_message("我是大三计算机专业，想申请美国 AI 硕士。")
    reply = agent.on_user_message("绩点3.9, 5/120")
    assert agent.profile.gpa == 3.9
    assert agent.profile.class_rank == "5/120"
    assert "GPA 或专业排名" not in reply
    assert "托福或雅思" in reply


def test_roadmap_waits_for_foundation_and_maps_a_bare_graduation_year():
    agent = LifecycleAgent("foundation_student")
    assert "GPA" in agent.on_user_message("我是大三计算机专业，想申请美国 AI 硕士。")
    assert agent.roadmap is None
    assert "托福或雅思" in agent.on_user_message("GPA 3.9")
    assert agent.roadmap is None
    assert "科研" in agent.on_user_message("我还没开始准备托福")
    assert agent.roadmap is None
    assert "毕业" in agent.on_user_message("暂无科研")
    agent.pending_information_field = None  # Simulate a pre-upgrade restored browser snapshot.
    reply = agent.on_user_message("2027")
    assert agent.profile.graduation_year == 2027
    assert agent.roadmap is not None
    assert "路线图 v1" in reply


def test_profile_repairs_character_split_model_lists():
    profile = StudentProfile(user_id="repair", target_fields=["计", "算", "机"])
    assert profile.target_fields == ["计算机"]
    profile = StudentProfile(user_id="repair", target_fields="计算机")
    assert profile.target_fields == ["计算机"]


def test_negative_language_and_research_answers_keep_exploring_state():
    agent = LifecycleAgent("negative_state")
    agent.on_user_message("我是大三 CS，想申请美国 AI 硕士。")
    agent.on_user_message("还没开始准备托福，也没有科研经历。")
    assert agent.state.language == "not_started"
    assert agent.state.research == "exploring"


def test_job_search_stage_generates_explainable_recommendations():
    agent = LifecycleAgent("career_student")
    agent.on_user_message("我是大三 CS，想申请美国 AI 硕士。")
    complete_onboarding(agent)
    reply = agent.on_user_message("我会 Python、PyTorch、LLM，目标是 AI Engineer，现在正在找实习。")
    assert agent.state.career == "internship_search"
    assert agent.job_recommendations
    assert "匹配岗位" in reply
    top = agent.job_recommendations[0]
    assert top.reason
    assert top.source.startswith("demo:")
    assert top.confidence >= 0.65


def test_repeated_information_does_not_create_another_roadmap_version():
    agent = LifecycleAgent("stable_student")
    message = "我是大一 CS，想申请美国 AI 硕士。"
    agent.on_user_message(message)
    complete_onboarding(agent)
    agent.on_user_message(message)
    assert agent.roadmap.version == 1


def test_qwen_planner_result_replaces_fallback_and_receives_current_roadmap():
    class FakeQwenPlanner:
        last_error = None

        def __init__(self):
            self.current_versions = []

        def generate(self, profile, state, current, revision_reason):
            self.current_versions.append(current.version if current else None)
            roadmap = build_roadmap(
                profile,
                state,
                version=(current.version + 1 if current else 1),
                revision_reason=revision_reason,
            )
            roadmap.generation_mode = "qwen"
            roadmap.milestones[0].tasks[0].title = "Qwen personalized academic task"
            return roadmap

    fake = FakeQwenPlanner()
    agent = LifecycleAgent("qwen_student", planner=HybridRoadmapPlanner(fake))
    agent.on_user_message("我是大一 CS，想申请美国 AI 硕士。")
    complete_onboarding(agent)
    agent.on_user_message("我开始做 LLM 科研。")
    assert fake.current_versions == [None]
    assert agent.replan_required is True
    assert agent.replan_with_llm() is True
    assert fake.current_versions == [None, 1]
    assert agent.roadmap.version == 2
    assert agent.roadmap.generation_mode == "qwen"
    assert agent.roadmap.milestones[0].tasks[0].title == "Qwen personalized academic task"
