"""Pure Profile domain logic. Database changes are returned as proposals."""
from __future__ import annotations

import re
from typing import Any

from ...conflict_resolver import ProfileConflictResolver
from ...models import CandidateFact, ProgressUpdate, Roadmap, StudentProfile, TaskProgress, UserEvent
from ...state import derive_state
from ...state_transition import StateTransitionEngine
from ..core.telemetry import span
from .contracts import ProfileResult
from .domain_request import DomainRequest
from .profile_extraction import ProfileExtractionPipeline
from ..services.memory import explicit_preferences


def profile_from_request(request: DomainRequest) -> StudentProfile:
    raw: dict[str, Any] = dict(request.profile_payload)
    raw["user_id"] = request.user_id
    raw["facts"] = [CandidateFact.model_validate(item).model_dump(mode="python") for item in request.profile_facts]
    return StudentProfile.model_validate(raw)


def _roadmap(request: DomainRequest) -> Roadmap | None:
    value = request.current_plan.get("roadmap") if request.current_plan else None
    if not value:
        return None
    try:
        return Roadmap.model_validate(value)
    except Exception:
        return None


def _current_progress(request: DomainRequest) -> list[TaskProgress]:
    result = []
    for task in request.current_tasks:
        if task.get("status") not in {"planned", "in_progress", "completed", "cancelled"}:
            continue
        result.append(TaskProgress(
            target_id=task["stable_key"], title=task["title"],
            target_kind="event" if task["stable_key"].startswith("event:") else "task",
            category=task.get("category", "application"), status=task["status"],
            evidence=task.get("evidence") or "", source_event_id="persisted",
        ))
    return result


class ProfileAgent:
    """Validate extracted facts and user progress against authoritative snapshots."""

    def execute(self, request: DomainRequest) -> ProfileResult:
        profile = profile_from_request(request)
        with span("a2a.profile.extract", user_id=request.user_id):
            extracted = ProfileExtractionPipeline().extract(request.message, request.conversation_context, mode="auto")
        normalized = extracted.accepted_facts
        projected = profile.model_copy(deep=True)
        proposals: list[dict[str, Any]] = []
        clarifications: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        accepted = []
        for fact in normalized:
            old = projected.model_dump(mode="json")[fact.field]
            trial = fact.model_copy(update={"needs_confirmation": False, "confidence": 1.0})
            candidate_resolution = ProfileConflictResolver().resolve(projected, [trial])
            new = candidate_resolution.profile.model_dump(mode="json")[fact.field]
            if old == new:
                decisions.append({"field": fact.field, "status": "ignored", "reason": "same_value"})
                continue
            if fact.needs_confirmation or (old not in (None, [], "") and fact.operation != "append"):
                conflicts.append({"field": fact.field, "old_value": old, "new_value": new,
                                  "old_source": next((f.source for f in reversed(profile.facts) if f.field == fact.field), None),
                                  "new_source": fact.source, "new_evidence": fact.evidence,
                                  "candidate": fact.model_dump(mode="json"), "status": "pending",
                                  "expected_version": request.profile_version})
                decisions.append({"field": fact.field, "status": "confirmation_required", "reason": "modal_conflict"})
                continue
            projected = candidate_resolution.profile
            accepted.append(fact.model_dump(mode="json"))
            decisions.append({"field": fact.field, "status": "applied", "reason": "candidate_change"})
        if accepted:
            before = profile.model_dump(mode="json")
            after = projected.model_dump(mode="json")
            proposals.append({
                "type": "profile.change",
                "payload": {"facts": accepted, "expected_version": request.profile_version,
                            "before": {item["field"]: before.get(item["field"]) for item in accepted},
                            "after": {item["field"]: after.get(item["field"]) for item in accepted}},
                "reason": "请确认画像变更；确认前不会写入。",
            })
        for pref in extracted.preferences:
            proposals.append({"type": "preference.change", "payload": pref.model_dump(mode="json"),
                              "reason": "请确认这项偏好；它将与画像事实分开保存。"})

        event = UserEvent(request_id=request.request_id, source="chat", raw_text=request.message)
        engine = StateTransitionEngine()
        roadmap = _roadmap(request)
        progress = _current_progress(request)
        accepted_progress = []
        for raw in extracted.progress_updates:
            update = ProgressUpdate.model_validate(raw)
            if update.action == "cancel" and any(
                update.evidence and (update.evidence in p["evidence"] or p["evidence"] in update.evidence)
                for p in explicit_preferences(request.message)
            ):
                continue
            decision = engine.decide_progress(update, roadmap, progress, event)
            if not decision.accepted or decision.record is None:
                clarifications.append({"kind": "progress", "reason": decision.reason,
                                       "candidate": update.model_dump(mode="json"),
                                       "target_ids": decision.candidate_target_ids or []})
                continue
            matched = next((task for task in request.current_tasks
                            if task["stable_key"] == decision.record.target_id), None)
            if matched is None:
                clarifications.append({"kind": "progress", "reason": "target_not_saved",
                                       "candidate": update.model_dump(mode="json")})
                continue
            accepted_progress.append(decision.record.model_dump(mode="json"))
            progress = [item for item in progress if item.target_id != decision.record.target_id]
            progress.append(decision.record)
            proposals.append({
                "type": "task.command",
                "payload": {"task_id": matched["id"], "action": update.action,
                            "title": matched["title"],
                            "evidence": update.evidence or request.message,
                            "due_at": update.postponed_to.isoformat() if update.postponed_to else None,
                            "expected_status": matched["status"]},
                "reason": "请确认任务进度变更。",
            })

        status_terms = {"已提交": "submitted", "提交了": "submitted", "收到offer": "offered",
                        "录取了": "offered", "已拒绝": "rejected", "已撤回": "withdrawn"}
        target_status = next((value for term, value in status_terms.items() if term in request.message.casefold()), None)
        application_match = re.search(
            r"我(?:已经|已|准备|计划|要)?(?:申请了|申请|投递了)\s*([^\s，。的]{2,60})\s*(?:的|\s+)\s*([^\s，。]{2,80})",
            request.message,
        )
        if application_match:
            university, program = application_match.group(1).strip(), application_match.group(2).strip()
            if not any(item["university"].casefold() == university.casefold()
                       and item["program"].casefold() == program.casefold() for item in request.applications):
                proposals.append({"type": "application.create",
                                  "payload": {"university": university, "program": program,
                                              "status": target_status or "considering", "evidence": application_match.group(0)},
                                  "reason": "请确认新增申请项目。"})
        if target_status and not application_match and any(term in request.message for term in ("我", "我的")):
            matches = [item for item in request.applications
                       if item["university"].casefold() in request.message.casefold()
                       or item["program"].casefold() in request.message.casefold()]
            if len(matches) == 1:
                application = matches[0]
                if application["status"] != target_status:
                    proposals.append({"type": "application.change",
                                      "payload": {"application_id": application["id"], "status": target_status,
                                                  "university": application["university"],
                                                  "program": application["program"],
                                                  "before_status": application["status"],
                                                  "expected_version": application["version"],
                                                  "evidence": request.message},
                                      "reason": "请确认申请状态变更。"})
            else:
                clarifications.append({"kind": "application", "reason": "application_not_unique",
                                       "application_ids": [item["id"] for item in matches]})

        return ProfileResult(
            facts=[item.model_dump(mode="json") for item in normalized],
            extracted_facts=[item.model_dump(mode="json") for item in extracted.extracted_facts],
            accepted_facts=[item.model_dump(mode="json") for item in normalized],
            proposed_changes=proposals, preference_candidates=[p.model_dump(mode="json") for p in extracted.preferences],
            requires_confirmation=bool(proposals or conflicts), errors=extracted.errors,
            extraction_mode=extracted.mode, extraction_route=extracted.route_path,
            extraction_route_reason=extracted.route_reason, decisions=decisions, conflicts=conflicts,
            projected_profile=projected.model_dump(mode="json", exclude={"facts", "change_history"}),
            derived_state=derive_state(projected, progress).model_dump(mode="json"),
            progress_updates=accepted_progress, clarifications=clarifications,
            proposals=proposals,
            status=("partial" if proposals or conflicts else "failed") if extracted.semantic_failed else
                   "needs_confirmation" if conflicts else "complete" if proposals or clarifications else "no_change",
        )
