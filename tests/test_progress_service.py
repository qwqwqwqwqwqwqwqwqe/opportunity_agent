import json
import tempfile
import threading
from pathlib import Path

import pytest

from opportunity_agent.conversation_store import ConversationStore, DeletedConversation, RevisionConflict
from opportunity_agent.progress import targets
from opportunity_agent.session_service import SessionService
from opportunity_agent.session_state import restore_agent


def article_json(label="详细规划"):
    text = (label + "依据已确认画像和确定性时间轴，提供具体行动、成果、检查节点、风险和下一步顺序。") * 5
    return json.dumps({
        "current_profile_goal": text, "gap_analysis": text,
        "current_stage_actions": text, "academic_research_internship": text,
        "application_materials_timeline": text, "risks_next_steps": text,
    }, ensure_ascii=False)


@pytest.fixture
def service():
    with tempfile.TemporaryDirectory(prefix="progress-api-") as directory:
        yield SessionService(ConversationStore(Path(directory) / "conversations.json"))


def onboard(service, sid="shared"):
    service.execute("/api/onboarding", {"session_id": sid, "request_id": "form",
        "profile": {"school": "XDU", "major": "CS", "academic_year": 2, "graduation_year": 2029,
                    "target_countries": ["美国"], "target_degree": "MS", "target_fields": ["AI"]}})
    record = service.get(sid)
    agent = restore_agent(sid, record["state"])
    return next(t for t in targets(agent.roadmap) if "background_portfolio" in t["aliases"])["target_id"]


def test_raw_message_is_durable_before_inference_and_retries_are_idempotent(service, monkeypatch):
    onboard(service)
    original = service._run
    observations = []

    def inspect_run(agent, path, payload, event_id):
        state = service.store.get("shared")["state"]
        observations.append(next(e["status"] for e in state["user_events"] if e["event_id"] == event_id))
        assert any(m["content"] == "托福105" and m["processing_status"] == "received" for m in state["conversation_messages"])
        return original(agent, path, payload, event_id)

    monkeypatch.setattr(service, "_run", inspect_run)
    payload = {"session_id": "shared", "request_id": "score", "message": "托福105"}
    first = service.execute("/api/chat", payload)[1]
    second = SessionService(service.store).execute("/api/chat", payload)[1]
    assert observations == ["received"]
    assert first["reply"] == second["reply"]
    assert sum(m["content"] == "托福105" for m in second["conversation_messages"]) == 1
    assert second["profile"]["toefl_score"] == 105
    assert second["conflict_decisions"] == first["conflict_decisions"] and second["conflict_decisions"]


def test_two_clients_use_latest_disk_state_and_progress_survives_restart(service):
    key = onboard(service)
    stale = service.get("shared")["state"]
    service.execute("/api/progress", {"session_id": "shared", "request_id": "complete", "target_id": key, "action": "complete"})
    other = SessionService(ConversationStore(service.store.path))
    other.execute("/api/chat", {"session_id": "shared", "request_id": "score", "message": "托福105", "client_state": stale})
    state = other.get("shared")["state"]
    assert state["task_progress"][0]["status"] == "completed"
    assert state["profile"]["toefl_score"] == 105
    assert state["state_transitions"]
    assert other.import_conversation({"session_id": "shared", "client_state": stale})["profile"]["toefl_score"] == 105


def test_late_model_result_cannot_overwrite_new_progress(service, monkeypatch):
    key = onboard(service)
    started, release = threading.Event(), threading.Event()
    original = service._run
    errors = []

    def slow(agent, path, payload, event_id):
        if path == "/api/chat":
            started.set()
            assert release.wait(5)
        return original(agent, path, payload, event_id)

    def run():
        try:
            service.execute("/api/chat", {"session_id": "shared", "request_id": "slow", "message": "托福105"})
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(service, "_run", slow)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(3)
        service.execute("/api/progress", {"session_id": "shared", "request_id": "complete", "target_id": key, "action": "complete"})
    finally:
        release.set()
        thread.join(5)
    assert len(errors) == 1 and isinstance(errors[0], RevisionConflict)
    state = service.get("shared")["state"]
    assert state["task_progress"][0]["status"] == "completed"
    assert state["profile"]["toefl_score"] is None
    assert next(e for e in state["user_events"] if e["request_id"] == "slow")["status"] == "failed"


def test_delete_during_inference_rejects_late_save_and_cache_import(service, monkeypatch):
    onboard(service)
    stale = service.get("shared")["state"]
    original = service._run

    def delete_mid_request(agent, path, payload, event_id):
        service.store.delete("shared")
        return original(agent, path, payload, event_id)

    monkeypatch.setattr(service, "_run", delete_mid_request)
    with pytest.raises(DeletedConversation):
        service.execute("/api/chat", {"session_id": "shared", "message": "托福105"})
    with pytest.raises(DeletedConversation):
        service.import_conversation({"session_id": "shared", "client_state": stale})
    with pytest.raises(DeletedConversation):
        service.execute("/api/chat", {"session_id": "shared", "message": "你好", "client_state": stale})
    assert service.store.list() == []


def test_legacy_migration_has_backup_stable_message_ids_and_unknown_dates(service):
    path = service.store.path
    legacy = {"schema_version": 1, "conversations": {"old": {"session_id": "old", "title": "旧对话", "updated_at": "2026-01-01",
              "state": {"conversation_messages": [{"role": "user", "content": "旧消息"}]}}}}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    first = service.get("old")
    second = SessionService(ConversationStore(path)).get("old")
    assert first["state"]["conversation_messages"][0]["message_id"] == second["state"]["conversation_messages"][0]["message_id"]
    assert first["state"]["conversation_messages"][0]["created_at"] is None
    assert path.with_suffix(".v1.bak").exists()


def test_consultation_failure_keeps_accepted_score_and_no_article_rewrite(service, monkeypatch):
    onboard(service)
    original = service._run
    from opportunity_agent.llm_client import LLMClient
    from opportunity_agent.advisor import AdviceResponder

    def fail_advice(agent, path, payload, event_id):
        agent.responder = AdviceResponder(LLMClient(completion_fn=lambda _: (_ for _ in ()).throw(TimeoutError("slow"))))
        return original(agent, path, payload, event_id)

    monkeypatch.setattr(service, "_run", fail_advice)
    before = service.get("shared")["state"]["roadmap"]["article"]
    _, data = service.execute("/api/chat", {"session_id": "shared", "message": "我托福考了105，接下来怎么办？"})
    assert data["profile"]["toefl_score"] == 105
    assert data["roadmap"]["article"] == before
    assert "官网" in data["reply"]
    assert "TimeoutError" in data["answer_fallback_reason"]


def test_initial_enrichment_survives_passive_read_and_is_not_repeated(service, monkeypatch):
    from opportunity_agent.llm_client import LLMClient
    onboard(service)
    original, calls = service._run, []
    def with_model(agent, path, payload, event_id):
        agent.planner.model_planner.llm_client = LLMClient(completion_fn=lambda p: calls.append(p) or article_json())
        return original(agent, path, payload, event_id)
    monkeypatch.setattr(service, "_run", with_model)
    service.get("shared")
    _, enriched = service.execute("/api/roadmap/enrich", {"session_id": "shared", "request_id": "initial"})
    assert len(calls) == 1 and enriched["roadmap"]["generation_mode"] == "qwen"
    service.get("shared")
    service.execute("/api/roadmap/enrich", {"session_id": "shared", "request_id": "repeated"})
    assert len(calls) == 1


def test_manual_replan_failure_reply_contains_actionable_reason(service, monkeypatch):
    from opportunity_agent.llm_client import LLMClient
    onboard(service)
    original = service._run
    calls = []
    short = json.dumps({key: "太短。" for key in (
        "current_profile_goal", "gap_analysis", "current_stage_actions",
        "academic_research_internship", "application_materials_timeline", "risks_next_steps")}, ensure_ascii=False)
    def with_short_model(agent, path, payload, event_id):
        if path == "/api/roadmap/replan":
            agent.planner.model_planner.llm_client = LLMClient(
                api_key="test-key", completion_fn=lambda request: calls.append(request) or short, retries=0)
        return original(agent, path, payload, event_id)
    monkeypatch.setattr(service, "_run", with_short_model)

    _, result = service.execute("/api/roadmap/replan", {
        "session_id": "shared", "request_id": "short-plan",
    })

    assert len(calls) == 2
    assert "失败原因" in result["reply"] and "章节质量校验失败" in result["reply"]
    assert result["planning_error_code"] == "article_sections_incomplete"


def test_late_replanning_article_never_overwrites_new_progress(service, monkeypatch):
    from opportunity_agent.llm_client import LLMClient
    key = onboard(service)
    previous = service.get("shared")["state"]["roadmap"]["article"]
    original = service._run
    started, release, errors = threading.Event(), threading.Event(), []
    def complete(_):
        started.set()
        assert release.wait(5)
        return article_json("迟到的规划文章")
    def with_model(agent, path, payload, event_id):
        if path == "/api/roadmap/replan":
            agent.planner.model_planner.llm_client = LLMClient(completion_fn=complete)
        return original(agent, path, payload, event_id)
    monkeypatch.setattr(service, "_run", with_model)
    def replan():
        try:
            service.execute("/api/roadmap/replan", {"session_id": "shared", "request_id": "slow-replan"})
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=replan)
    worker.start()
    try:
        assert started.wait(3)
        service.execute("/api/progress", {"session_id": "shared", "request_id": "new-progress", "target_id": key, "action": "complete"})
    finally:
        release.set()
        worker.join(5)
    assert len(errors) == 1 and isinstance(errors[0], RevisionConflict)
    state = service.get("shared")["state"]
    assert state["roadmap"]["article"] == previous
    assert state["task_progress"][0]["status"] == "completed" and state["replan_required"]


def test_timeline_form_is_idempotent_and_persists_explicit_profile_fact(service):
    onboard(service)
    payload = {"session_id": "shared", "request_id": "winter-note", "node_title": "寒假：暑研/实习准备",
               "node_date": "2026-01-15", "fact_field": "internship_experiences",
               "detail": "完成嵌入式软件实习的驱动调试", "occurred_on": "2026-02-10"}
    first = service.execute("/api/timeline-update", payload)[1]
    second = service.execute("/api/timeline-update", payload)[1]
    assert first["profile"]["internship_experiences"] == ["完成嵌入式软件实习的驱动调试"]
    assert second["profile"]["internship_experiences"] == first["profile"]["internship_experiences"]
    assert first["replan_required"] and any(event["source"] == "timeline" for event in second["user_events"])
