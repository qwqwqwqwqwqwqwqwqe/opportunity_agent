import json
from datetime import date, timedelta

from opportunity_agent.lifecycle_agent import LifecycleAgent
from opportunity_agent.llm_client import LLMClient
from opportunity_agent.models import ProgressUpdate
from opportunity_agent.planning import HybridRoadmapPlanner, ModelScopeRoadmapPlanner
from opportunity_agent.profile import HybridFactExtractor
from opportunity_agent.progress import targets
from opportunity_agent.semantic_extractor import SemanticExtractor
from opportunity_agent.session_state import restore_agent, snapshot


def _form(**overrides):
    return {
        "school": "XDU", "major": "计算机", "academic_year": 2, "degree_years": 4,
        "graduation_year": 2030, "target_countries": ["美国"], "target_degree": "MS",
        "target_fields": ["AI"], "summer_preference": "both",
        "next_exam_type": "TOEFL", "next_exam_date": "2029-09-10", **overrides,
    }


def _agent():
    planner = HybridRoadmapPlanner(ModelScopeRoadmapPlanner(llm_client=LLMClient(api_key="")))
    agent = LifecycleAgent("transition", planner=planner)
    agent.on_onboarding(_form())
    return agent


def _target(agent, alias):
    return next(item for item in targets(agent.roadmap) if alias in item["aliases"])


def test_explicit_early_exam_completion_is_applied_without_moving_calendar_position():
    agent = _agent()
    reply = agent.on_user_message("我已经提前考完托福了")
    exam = next(event for event in agent.roadmap.timeline.events if event.kind == "exam")
    assert exam.execution_status == "completed"
    assert exam.time_status == "upcoming"
    assert exam.risk_status == "ahead_of_schedule"
    assert exam.actual_date == date.today()
    assert not any(item.status == "pending" for item in agent.pending_confirmations)
    assert "已完成" in reply and agent.replan_required


def test_history_is_never_achievement_and_requires_backfill_without_evidence():
    agent = _agent()
    agent.refresh_state(date(2032, 1, 1))
    background = next(phase for phase in agent.roadmap.timeline.phases if phase.phase_id == "background")
    summer = next(event for event in agent.roadmap.timeline.events if event.kind == "summer_break")
    assert background.time_status == "history" and background.execution_status == "planned"
    assert background.risk_status == "needs_backfill"
    assert summer.time_status == "history" and summer.execution_status == "planned"
    assert summer.risk_status == "needs_backfill"


def test_one_completed_task_does_not_complete_phase_but_all_terminal_tasks_do():
    agent = _agent()
    phase = next(item for item in agent.roadmap.timeline.phases if item.phase_id == "background")
    first = phase.plan.tasks[0]
    agent.on_progress(ProgressUpdate(target_id=first.progress_key, action="complete", evidence="按钮确认完成"))
    assert phase.execution_status == "in_progress"
    for task in phase.plan.tasks[1:]:
        agent.on_progress(ProgressUpdate(target_id=task.progress_key, action="complete", evidence="按钮确认完成"))
    assert phase.execution_status == "completed"


def test_terminal_state_conflict_uses_recent_target_context_and_requires_confirmation():
    agent = _agent()
    project = _target(agent, "background_portfolio")
    agent.on_progress(ProgressUpdate(target_id=project["target_id"], action="complete", evidence="项目已完成"))
    agent.on_user_message("这个又开始了")
    record = next(item for item in agent.task_progress if item.target_id == project["target_id"])
    assert record.status == "completed"
    pending = next(item for item in agent.pending_confirmations if item.status == "pending")
    assert pending.progress_update.action == "start"
    assert pending.candidate_target_ids == [project["target_id"]]


def test_exam_cancel_then_change_is_one_postponement_and_never_a_duplicate_event():
    agent = _agent()
    count = len([event for event in agent.roadmap.timeline.events if event.kind == "exam"])
    agent.on_user_message("托福考试9月不考了，改到2029-11-15")
    exam = next(event for event in agent.roadmap.timeline.events if event.kind == "exam")
    assert exam.event_date == date(2029, 11, 15)
    assert exam.execution_status == "planned"
    assert len([event for event in agent.roadmap.timeline.events if event.kind == "exam"]) == count
    assert len([item for item in agent.task_progress if item.target_kind == "event"]) == 1


def test_stage_evidence_assessment_and_references_survive_snapshot():
    agent = _agent()
    agent.on_user_message("我开始做LLM科研")
    transition = next(item for item in reversed(agent.state_transitions) if item.field == "state.research")
    assert transition.evidence_ids
    restored = restore_agent("transition", snapshot(agent))
    assert restored.stage_evidence and restored.stage_assessments
    assert restored.last_referenced_target_id == agent.last_referenced_target_id
    assessment = next(item for item in restored.stage_assessments if item.dimension == "research")
    assert assessment.state == "building" and assessment.evidence_ids


def test_expired_conversation_signal_no_longer_drives_stage():
    agent = _agent()
    agent.extractor = HybridFactExtractor(model_extractor=SemanticExtractor(completion_fn=lambda _: json.dumps({
        "intent": "profile_update", "facts": [], "stage_signals": [{
            "stage": "LANGUAGE_PREPARATION", "direction": "increase",
            "strength": 0.84, "evidence": "刷听力题",
        }],
    }, ensure_ascii=False)))
    agent.on_user_message("这周在刷听力题")
    assert agent.state.language_evidence == "preparing"
    signal = next(item for item in agent.stage_evidence if item.kind == "signal")
    signal.expires_at = signal.observed_at - timedelta(days=1)
    agent.refresh_state()
    assert agent.state.language_evidence != "preparing"
    assessment = next(item for item in agent.stage_assessments if item.dimension == "language")
    assert signal.evidence_id not in assessment.evidence_ids

