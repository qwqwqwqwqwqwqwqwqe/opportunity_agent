"""Central decisions for progress mutations and explainable stage assessment."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .models import (
    CandidateFact, ProgressUpdate, Roadmap, StageAssessment, StageEvidence,
    StageSignal, StudentProfile, TaskProgress, UserEvent, UserState,
)
from .progress import resolve_targets
from .state import derive_state


_FACT_DIMENSIONS = {
    "academic_year": "academic", "graduation_year": "academic", "completed_courses": "academic",
    "gpa": "academic", "gpa_raw": "academic", "class_rank": "academic",
    "toefl_score": "language", "ielts_score": "language", "gre_score": "language",
    "exam_plan": "language", "language_preparation": "language",
    "research_activity": "research", "research_experiences": "research",
    "paper_experiences": "research", "project_experiences": "research",
    "target_countries": "application", "target_schools": "application",
    "target_programs": "application", "target_degree": "application",
    "target_fields": "application", "planned_enrollment_year": "application",
    "planned_enrollment_month": "application",
    "career_goal": "career", "current_stage": "career", "internship_experiences": "career",
}
_CATEGORY_DIMENSIONS = {
    "academic": "academic", "language": "language", "language_exam": "language",
    "research": "research", "application": "application", "career": "career",
}
_SIGNAL_DIMENSIONS = {
    "ACADEMIC": "academic", "LANGUAGE": "language", "LANGUAGE_PREPARATION": "language",
    "RESEARCH": "research", "SUMMER_RESEARCH": "research", "APPLICATION": "application",
    "CAREER": "career", "INTERNSHIP": "career", "JOB_SEARCH": "career",
}


@dataclass
class ProgressDecision:
    accepted: bool
    record: TaskProgress | None = None
    candidate_target_ids: list[str] | None = None
    reason: str = ""


class StateTransitionEngine:
    """Applies evidence precedence without treating the calendar as achievement."""

    threshold = 0.75

    def decide_progress(
        self, update: ProgressUpdate, roadmap: Roadmap | None, existing: list[TaskProgress],
        event: UserEvent, *, confirmed: bool = False, today: date | None = None,
    ) -> ProgressDecision:
        today = today or date.today()
        matches = resolve_targets(update, roadmap)
        if len(matches) != 1:
            return ProgressDecision(False, candidate_target_ids=[item["target_id"] for item in matches],
                                    reason="target_not_unique")
        target = matches[0]
        previous = next((item for item in existing if item.target_id == target["target_id"]), None)
        status = {"start": "in_progress", "complete": "completed", "cancel": "cancelled",
                  "reset": "planned"}.get(update.action)
        proposed_status = status or (previous.status if previous else "planned")
        conflicts_with_terminal = bool(
            previous and previous.status in {"completed", "cancelled"}
            and proposed_status != previous.status and update.action != "postpone"
        )
        needs_confirmation = (
            update.needs_confirmation or update.confidence < self.threshold
            or update.action == "postpone" and update.postponed_to is None
            or conflicts_with_terminal
        )
        if needs_confirmation and not confirmed:
            return ProgressDecision(False, candidate_target_ids=[target["target_id"]],
                                    reason="confirmation_required")
        actual_date = (update.actual_date or today) if update.action == "complete" else (
            previous.actual_date if previous and update.action == "postpone" else None
        )
        record = TaskProgress(
            target_id=target["target_id"], target_kind=target["target_kind"], title=target["title"],
            category=target["category"], status=proposed_status,
            actual_date=actual_date,
            postponed_to=(update.postponed_to if update.action == "postpone" else
                          previous.postponed_to if previous and update.action != "reset" else None),
            evidence=update.evidence or event.raw_text, source_event_id=event.event_id,
            confidence=1.0 if confirmed else update.confidence,
        )
        return ProgressDecision(True, record=record, candidate_target_ids=[target["target_id"]], reason="accepted")

    def collect_evidence(
        self, event: UserEvent, accepted_facts: list[CandidateFact],
        progress_records: list[TaskProgress], signals: list[StageSignal],
        *, confirmed: bool = False,
    ) -> list[StageEvidence]:
        result: list[StageEvidence] = []
        kind = "confirmation" if confirmed else "fact"
        for fact in accepted_facts:
            dimension = _FACT_DIMENSIONS.get(fact.field)
            if dimension:
                result.append(StageEvidence(
                    dimension=dimension, kind=kind, confidence=1.0 if confirmed else fact.confidence,
                    direction="decrease" if fact.operation == "remove" else "increase",
                    evidence=fact.evidence or event.raw_text, source_event_id=event.event_id,
                ))
        for record in progress_records:
            dimension = _CATEGORY_DIMENSIONS.get(record.category)
            if dimension:
                result.append(StageEvidence(
                    dimension=dimension, kind="confirmation" if confirmed else
                    "task_operation" if event.source == "progress" else "progress",
                    confidence=record.confidence,
                    direction="decrease" if record.status in {"cancelled", "planned"} else "increase",
                    evidence=record.evidence, source_event_id=event.event_id, target_id=record.target_id,
                ))
        for signal in signals:
            dimension = _SIGNAL_DIMENSIONS.get(signal.stage.upper())
            if dimension:
                observed = event.received_at
                result.append(StageEvidence(
                    dimension=dimension, kind="signal", direction=signal.direction,
                    confidence=signal.strength, evidence=signal.evidence,
                    source_event_id=event.event_id, observed_at=observed,
                    expires_at=observed + timedelta(days=30),
                ))
        return result

    def assess(
        self, profile: StudentProfile, progress: list[TaskProgress], signals: list[StageSignal],
        evidences: list[StageEvidence], *, now: datetime | None = None,
    ) -> tuple[UserState, list[StageAssessment]]:
        now = now or datetime.now(timezone.utc)
        active = [item for item in evidences if item.expires_at is None or item.expires_at >= now]
        state = derive_state(profile, progress, signals)
        assessments = []
        for dimension, value in state.model_dump(mode="python").items():
            if dimension == "language_evidence":
                continue
            relevant = [item for item in active if item.dimension == dimension]
            confidence = max((item.confidence for item in relevant), default=0.5 if value != "unknown" else 0.0)
            assessments.append(StageAssessment(
                dimension=dimension, state=str(value), confidence=confidence,
                evidence_ids=[item.evidence_id for item in relevant], assessed_at=now,
            ))
        return state, assessments

    @staticmethod
    def event_evidence_ids(field: str, event_id: str, evidences: list[StageEvidence]) -> list[str]:
        dimension = field.removeprefix("state.") if field.startswith("state.") else None
        if dimension == "language_evidence":
            dimension = "language"
        return [item.evidence_id for item in evidences
                if item.source_event_id == event_id and (dimension is None or item.dimension == dimension)]
