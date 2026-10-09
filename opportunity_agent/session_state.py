"""One snapshot contract for HTTP, durable state, and legacy migration."""
from __future__ import annotations

from .lifecycle_agent import LifecycleAgent
from .planning import planning_error_details
from .config import official_search_enabled
from .conflict_resolver import ConflictDecision
from .models import (AgentTurnResult, ChatMessage, ExtractionResult, JobRecommendation,
                     PendingConfirmation, Roadmap, StageAssessment, StageEvidence, StateTransition,
                     StudentProfile, TaskProgress, UserEvent)
from .profile import hydrate_score_fields_from_facts


def restore_agent(session_id: str, state: dict | None = None) -> LifecycleAgent:
    agent = LifecycleAgent(f"web_{session_id}")
    if not state:
        return agent
    if isinstance(state.get("profile"), dict):
        agent.profile = hydrate_score_fields_from_facts(StudentProfile.model_validate(state["profile"]))
        agent.profile.user_id = f"web_{session_id}"
    if isinstance(state.get("roadmap"), dict):
        agent.roadmap = Roadmap.model_validate(state["roadmap"])
    messages = state.get("conversation_messages") or state.get("recent_messages") or []
    agent.conversation_messages = [ChatMessage.model_validate(m) for m in messages]
    agent.recent_messages = [m for m in agent.conversation_messages if m.processing_status in {"legacy", "processed"}][-12:]
    for name, model in (("user_events", UserEvent), ("task_progress", TaskProgress),
                        ("state_transitions", StateTransition), ("pending_confirmations", PendingConfirmation),
                        ("job_recommendations", JobRecommendation), ("stage_evidence", StageEvidence),
                        ("stage_assessments", StageAssessment)):
        setattr(agent, name, [model.model_validate(item) for item in state.get(name, [])])
    agent.last_extraction = ExtractionResult.model_validate(state.get("extraction_result") or {})
    agent.last_conflict_decisions = [ConflictDecision.model_validate(item) for item in state.get("conflict_decisions", [])]
    agent.last_turn = AgentTurnResult.model_validate(state.get("last_turn") or {})
    agent.last_official_research = state.get("official_research") or agent.last_turn.official_research
    agent.state_revision = state.get("state_revision", 0)
    agent.pending_information_field = state.get("pending_information_field")
    agent.planning_pending = bool(state.get("planning_pending"))
    agent.replan_required = bool(state.get("replan_required"))
    agent.stop_followups = bool(state.get("stop_followups"))
    agent.last_referenced_target_id = state.get("last_referenced_target_id")
    agent.last_route = state.get("last_route", "")
    agent.last_route_reason = state.get("route_reason", "")
    agent.last_a2a_trace = state.get("a2a_trace")
    agent.pending_a2a_retry = state.get("pending_a2a_retry")
    agent.extractor.last_mode = state.get("extraction_mode", "rule")
    agent.extractor.last_error = state.get("fallback_reason")
    agent.planner.last_mode = agent.roadmap.generation_mode if agent.roadmap else "rule_fallback"
    agent.planner.last_error = state.get("planning_fallback_reason")
    agent.refresh_state()
    return agent


def snapshot(agent: LifecycleAgent) -> dict:
    planning_error_code, planning_error_message = planning_error_details(agent.planner.last_error)
    result = {
        "snapshot_version": 4, "profile": agent.profile.model_dump(mode="json"),
        "state": agent.state.model_dump(mode="json"),
        "roadmap": agent.roadmap.model_dump(mode="json") if agent.roadmap else None,
        "state_revision": agent.state_revision, "planning_pending": agent.planning_pending,
        "replan_required": agent.replan_required, "stop_followups": agent.stop_followups,
        "last_referenced_target_id": agent.last_referenced_target_id,
        "pending_information_field": agent.pending_information_field,
        "extraction_mode": agent.extractor.last_mode, "fallback_reason": agent.extractor.last_error,
        "planning_mode": "waiting_for_profile" if not agent.roadmap else "enriching" if agent.planning_pending else agent.planner.last_mode,
        "planning_fallback_reason": agent.planner.last_error,
        "planning_error_code": planning_error_code,
        "planning_error_message": planning_error_message,
        "extraction_result": agent.last_extraction.model_dump(mode="json"),
        "last_turn": agent.last_turn.model_dump(mode="json"),
        "answer_fallback_reason": agent.last_turn.answer_fallback_reason,
        "official_research": agent.last_official_research,
        "official_search_configured": official_search_enabled(),
        "last_route": agent.last_route,
        "route_reason": agent.last_route_reason,
        "a2a_trace": agent.last_a2a_trace,
        "pending_a2a_retry": agent.pending_a2a_retry,
        "conflict_decisions": [item.model_dump(mode="json") for item in agent.last_conflict_decisions],
    }
    for name in ("conversation_messages", "recent_messages", "user_events", "task_progress",
                 "state_transitions", "pending_confirmations", "job_recommendations",
                 "stage_evidence", "stage_assessments"):
        result[name] = [item.model_dump(mode="json") for item in getattr(agent, name)]
    result["progress_updates"] = agent.last_turn.model_dump(mode="json")["progress_updates"]
    result["state_changes"] = agent.last_turn.model_dump(mode="json")["state_changes"]
    return result
