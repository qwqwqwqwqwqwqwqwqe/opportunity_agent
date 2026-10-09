from datetime import date, timedelta
import json

from opportunity_agent.advisor import AdviceResponder
from opportunity_agent.lifecycle_agent import LifecycleAgent
from opportunity_agent.llm_client import LLMClient
from opportunity_agent.models import ChatMessage, ProgressUpdate, StudentProfile, TimelineFactUpdate
from opportunity_agent.planning import HybridRoadmapPlanner, ModelScopeRoadmapPlanner
from opportunity_agent.progress import targets
from opportunity_agent.session_state import restore_agent, snapshot


def form(**overrides):
    return {"school": "XDU", "major": "计算机", "academic_year": 2, "degree_years": 4,
            "graduation_year": 2029, "target_countries": ["美国"], "target_degree": "MS",
            "target_fields": ["AI"], "summer_preference": "both",
            "next_exam_type": "TOEFL", "next_exam_date": "2028-09-10", **overrides}


def make_agent():
    calls = []
    section = "结合用户画像、确定性时间轴和实际进展制定具体行动、交付成果、检查节点及任务先后关系。" * 5
    draft = json.dumps({
        "current_profile_goal": section, "gap_analysis": section,
        "current_stage_actions": section, "academic_research_internship": section,
        "application_materials_timeline": section, "risks_next_steps": section,
    }, ensure_ascii=False)
    model = LLMClient(completion_fn=lambda payload: calls.append(payload) or draft)
    advice_calls = []
    advisor = AdviceResponder(LLMClient(completion_fn=lambda p: advice_calls.append(p) or "先核验官网的总分和单项分要求，再决定是否重考。"))
    agent = LifecycleAgent("test", planner=HybridRoadmapPlanner(ModelScopeRoadmapPlanner(llm_client=model)), responder=advisor)
    agent.on_onboarding(form())
    return agent, calls, advice_calls


def target(agent, alias):
    return next(t for t in targets(agent.roadmap) if alias in t["aliases"])


def test_question_and_hypothetical_scores_do_not_mutate_profile_or_generate_article():
    agent, calls, advice_calls = make_agent()
    article = agent.roadmap.article
    for text in ("托福105分够吗？", "如果我托福105，可以申请吗？", "我没考到105"):
        agent.on_user_message(text)
    assert agent.profile.toefl_score is None
    assert calls == [] and len(advice_calls) == 2
    assert agent.roadmap.article == article
    assert "已保留路线图" not in agent.user_events[-2].reply
    agent.on_user_message("GPA3.9，如果托福105分，可以申请吗？")
    assert agent.profile.gpa == 3.9 and agent.profile.toefl_score is None


def test_plain_chinese_requirement_query_uses_advice_llm_without_recording_state():
    agent, planning_calls, advice_calls = make_agent()
    before = agent.profile.model_dump(mode="json")
    answer = agent.on_user_message("能帮我查询 CMU SCS 学院的 GRE 要求吗")
    assert len(advice_calls) == 1 and not planning_calls
    assert "官网" in answer
    assert agent.profile.model_dump(mode="json") == before
    event = agent.user_events[-1]
    assert event.extraction.intent == "ask_advice"
    assert not event.extraction.facts and not event.extraction.progress_updates


def test_mixed_score_and_cancel_updates_only_explicit_progress_without_llm_planning():
    agent, calls, advice_calls = make_agent()
    agent.on_user_message("我托福考了105，取消托福考试，接下来怎么办？")
    assert agent.profile.toefl_score == 105
    assert agent.state.language_evidence == "score_recorded"
    exam = next(e for e in agent.roadmap.timeline.events if e.kind == "exam")
    assert exam.execution_status == "cancelled"
    assert agent.replan_required and not calls and len(advice_calls) == 1
    assert "官网" in agent.user_events[-1].reply


def test_score_alone_does_not_cancel_future_exam_or_complete_requirement_verification():
    agent, _, _ = make_agent()
    agent.on_user_message("托福105")
    exam = next(e for e in agent.roadmap.timeline.events if e.kind == "exam")
    language = next(t for p in agent.roadmap.timeline.phases for t in p.plan.tasks if t.category == "language")
    assert exam.execution_status == "planned"
    assert language.execution_status == "planned"


def test_research_and_project_progress_stay_after_replan_and_legacy_aliases_resolve():
    agent, calls, _ = make_agent()
    agent.on_user_message("我开始做LLM科研")
    assert agent.state.research == "building" and calls == []
    agent.on_user_message("项目完成了")
    project = target(agent, "background_portfolio")
    assert any(p.target_id == project["target_id"] and p.status == "completed" for p in agent.task_progress)
    assert agent.replan_with_llm()
    assert len(calls) == 1
    prompt = calls[0]["messages"][1]["content"]
    assert '"execution_status": "completed"' in prompt
    assert "项目完成了" in prompt
    assert any(t.execution_status == "completed" for p in agent.roadmap.timeline.phases for t in p.plan.tasks if t.progress_key == project["target_id"])
    agent.on_progress(ProgressUpdate(target_id="research_progress", action="complete"))
    legacy = next(t for m in agent.roadmap.milestones for t in m.tasks if t.task_id == "research_progress")
    assert legacy.execution_status == "completed"


def test_ambiguous_and_uncertain_updates_require_confirmation_and_survive_restore():
    agent, _, _ = make_agent()
    agent.on_user_message("任务完成了")
    pending = next(c for c in agent.pending_confirmations if c.status == "pending")
    assert not agent.task_progress
    restored = restore_agent("test", snapshot(agent))
    project = target(restored, "background_portfolio")
    restored.confirm_change(pending.confirmation_id, True, project["target_id"])
    assert restored.task_progress[0].status == "completed"
    restored.on_user_message("可能取消托福考试")
    pending = next(c for c in restored.pending_confirmations if c.status == "pending")
    restored.confirm_change(pending.confirmation_id, False)
    restored.on_user_message("可能取消托福考试")
    assert not any(c.status == "pending" for c in restored.pending_confirmations)
    assert next(e for e in restored.roadmap.timeline.events if e.kind == "exam").execution_status == "planned"


def test_postponement_beyond_boundary_is_preserved_and_completed_never_overdue():
    agent, calls, _ = make_agent()
    key = target(agent, "background_portfolio")["target_id"]
    agent.on_progress(ProgressUpdate(target_id=key, action="postpone", postponed_to=date(2031, 1, 5)))
    project = next(t for p in agent.roadmap.timeline.phases for t in p.plan.tasks if t.progress_key == key)
    assert project.due_date == date(2031, 1, 5) and project.boundary_conflict
    agent.on_progress(ProgressUpdate(target_id=key, action="complete"))
    article = agent.roadmap.article
    agent.refresh_state(date(2032, 1, 1))
    assert project.execution_status == "completed" and not project.is_overdue
    assert agent.roadmap.article == article and calls == []


def test_new_goal_does_not_inherit_old_task_completion():
    agent, _, _ = make_agent()
    project = target(agent, "background_portfolio")
    agent.on_progress(ProgressUpdate(target_id=project["target_id"], action="complete"))
    agent.on_onboarding(form(target_fields=["信号处理"], major="通信工程"))
    assert target(agent, "background_portfolio")["target_id"] != project["target_id"]
    assert any(p.target_id == project["target_id"] for p in agent.task_progress)
    assert all(t.execution_status != "completed" for p in agent.roadmap.timeline.phases for t in p.plan.tasks)


def test_legacy_message_timestamp_is_unknown_and_new_messages_are_journaled():
    assert ChatMessage(role="user", content="旧记录").created_at is None
    agent, _, _ = make_agent()
    agent.on_user_message("项目完成了")
    event = agent.user_events[-1]
    message = next(m for m in agent.conversation_messages if m.message_id == event.message_id)
    assert message.created_at and message.processing_status == "processed"
    assert event.extraction.progress_updates
    assert agent.state_transitions[-1].source_event_id == event.event_id


def test_edit_form_never_triggers_another_background_article():
    agent, calls, _ = make_agent()
    agent.enrich_roadmap()
    article = agent.roadmap.article
    agent.on_onboarding(form(gpa_raw=3.9, gpa_scale=4))
    assert not agent.planning_pending and agent.replan_required
    assert agent.roadmap.article == article and len(calls) == 1


def test_exam_postponement_and_cancellation_are_in_replan_prompt_and_reset_restores_date():
    agent, calls, _ = make_agent()
    exam = next(e for e in agent.roadmap.timeline.events if e.kind == "exam")
    key, original = exam.progress_key, exam.event_date
    agent.on_progress(ProgressUpdate(target_id=key, target_kind="event", action="postpone", postponed_to=date(2028, 10, 15)))
    agent.on_progress(ProgressUpdate(target_id=key, target_kind="event", action="cancel"))
    assert agent.replan_with_llm()
    prompt = calls[-1]["messages"][1]["content"]
    assert '"execution_status": "cancelled"' in prompt and "2028-10-15" in prompt
    agent.on_progress(ProgressUpdate(target_id=key, target_kind="event", action="reset"))
    exam = next(e for e in agent.roadmap.timeline.events if e.kind == "exam")
    assert exam.event_date == original and exam.execution_status == "planned"


def test_mixed_numeric_and_experience_uses_semantics_without_losing_rule_progress():
    import json
    from opportunity_agent.profile import HybridFactExtractor
    from opportunity_agent.semantic_extractor import SemanticExtractor

    calls = []
    def complete(payload):
        calls.append(payload)
        return json.dumps({"intent": "mixed", "facts": [{"field": "internship_experiences",
            "value": ["拿到机器人实习"], "source": "conversation", "operation": "append", "evidence": "拿到机器人实习", "confidence": .95}],
            "progress_updates": [{"target_hint": "实习", "action": "start", "evidence": "拿到机器人实习", "confidence": .95}]}, ensure_ascii=False)
    extractor = HybridFactExtractor(model_extractor=SemanticExtractor(completion_fn=complete))
    result = extractor.extract_result("托福105，项目完成了，拿到机器人实习")
    assert len(calls) == 1
    assert {f.field for f in result.facts} >= {"toefl_score", "internship_experiences"}
    assert {p.target_hint for p in result.progress_updates} == {"项目", "实习"}
    calls.clear()
    for message in ("2027", "预计2027年毕业", "我托福考了105", "GPA3.9，排名5/120"):
        extractor.extract_result(message)
    assert calls == []


def test_stage_signal_is_auxiliary_evidence_never_task_completion_and_is_logged():
    import json
    from opportunity_agent.profile import HybridFactExtractor
    from opportunity_agent.semantic_extractor import SemanticExtractor
    agent, planning_calls, _ = make_agent()
    agent.extractor = HybridFactExtractor(model_extractor=SemanticExtractor(completion_fn=lambda _: json.dumps({
        "intent": "profile_update", "facts": [], "stage_signals": [{"stage": "LANGUAGE_PREPARATION",
        "direction": "increase", "strength": .84, "evidence": "刷听力题"}]})))
    agent.on_user_message("这周在刷听力题")
    assert agent.state.language_evidence == "preparing"
    assert not agent.task_progress and planning_calls == []
    evidence = next(t for t in agent.state_transitions if t.field == "state.language_evidence")
    assert evidence.confidence == .84 and "听力" in evidence.evidence
    restored = restore_agent("test", snapshot(agent))
    assert restored.state.language_evidence == "preparing"
    assert all(e.execution_status == "planned" for e in restored.roadmap.timeline.events)


def test_manual_planning_timeout_keeps_article_and_completed_progress():
    agent, _, _ = make_agent()
    key = target(agent, "background_portfolio")["target_id"]
    agent.on_progress(ProgressUpdate(target_id=key, action="complete"))
    previous = agent.roadmap.article
    def timeout(_):
        raise TimeoutError("model unavailable")
    agent.planner.model_planner.llm_client = LLMClient(completion_fn=timeout)
    assert not agent.replan_with_llm()
    assert agent.roadmap.article == previous and agent.replan_required
    assert agent.task_progress[0].status == "completed"


def test_legacy_alias_migration_and_unmapped_progress_are_not_fuzzy_matched():
    from opportunity_agent.models import TaskProgress
    agent, _, _ = make_agent()
    data = snapshot(agent)
    data["task_progress"] = [TaskProgress(target_id="research_progress", title="old research", category="research",
        status="completed", evidence="saved", source_event_id="legacy").model_dump(mode="json"),
        TaskProgress(target_id="no-longer-present", title="完成一个端到端机器学习项目", status="completed",
                     evidence="historical task", source_event_id="legacy").model_dump(mode="json")]
    restored = restore_agent("test", data)
    research = target(restored, "research_progress")
    assert any(p.target_id == research["target_id"] and p.status == "completed" for p in restored.task_progress)
    assert any(p.target_id == "no-longer-present" for p in restored.task_progress)
    assert next(t for p in restored.roadmap.timeline.phases for t in p.plan.tasks if t.task_id == "background_portfolio").execution_status == "planned"


def test_existing_cli_demo_still_completes(capsys):
    from opportunity_agent.main import run_demo
    run_demo()
    output = capsys.readouterr().out
    assert "[1] Progressive profiling" in output and "[5] Replanning completed" in output
    assert "Updated application deadline: 2029-11-01" in output


def test_historical_timeline_form_writes_explicit_fact_and_requires_manual_replan():
    agent, calls, _ = make_agent()
    result = agent.on_timeline_fact(TimelineFactUpdate(
        node_title="寒假：暑研/实习准备", node_date=date(2026, 1, 15),
        fact_field="research_experiences", detail="完成了实验室的信号分类复现实验", occurred_on=date(2026, 2, 3),
    ))
    assert agent.profile.research_experiences == ["完成了实验室的信号分类复现实验"]
    assert agent.profile.facts[-1].source == "user_explicit"
    assert agent.user_events[-1].source == "timeline"
    assert agent.replan_required and calls == []
    assert "是否现在重新生成" in result.reply
