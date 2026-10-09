from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel

from .models import CandidateFact, ProfileChange, StudentProfile
from .profile import apply_facts


class ConflictDecision(BaseModel):
    status: Literal["applied", "ignored", "confirmation_required"]
    field: str
    reason: str


class ConflictResolution(BaseModel):
    profile: StudentProfile
    decisions: list[ConflictDecision]


class ProfileConflictResolver:
    """Apply candidate facts using explicit source priority and change history."""

    SOURCE_PRIORITY = {
        "conversation": 100,
        "user_explicit": 100,
        "user_confirmed": 90,
        "resume": 70,
        "model": 60,
        "llm": 60,
        "behavior": 40,
        "system": 20,
    }
    PROFILE_FIELDS = {
        "school", "academic_year", "degree_years", "major", "target_countries", "target_regions", "target_schools", "target_programs",
        "target_degree", "target_fields", "graduation_year", "gpa", "class_rank",
        "graduation_month", "planned_enrollment_year", "planned_enrollment_month",
        "gpa_raw", "gpa_scale", "gpa_4_reference", "toefl_score", "ielts_score", "gre_score",
        "skills", "hardware_skills", "completed_courses", "research_experiences",
        "competition_experiences", "project_experiences", "paper_experiences",
        "internship_experiences", "budget", "exam_plan", "summer_preference",
        "onboarding_completed", "planning_domain", "career_goal", "target_locations", "current_stage",
    }

    def resolve(self, profile: StudentProfile, facts: list[CandidateFact]) -> ConflictResolution:
        updated = profile.model_copy(deep=True)
        decisions: list[ConflictDecision] = []
        for original in facts:
            fact = original.model_copy(deep=True)
            if fact.field not in self.PROFILE_FIELDS:
                updated = apply_facts(updated, [fact])
                decisions.append(ConflictDecision(status="applied", field=fact.field, reason="audit_only_fact"))
                continue

            old_value = getattr(updated, fact.field)
            new_value = fact.value
            if fact.confidence < 0.75 or fact.needs_confirmation:
                fact.needs_confirmation = True
                updated = apply_facts(updated, [fact])
                decisions.append(ConflictDecision(
                    status="confirmation_required", field=fact.field,
                    reason="uncertain_candidate",
                ))
                continue

            if fact.operation == "append":
                before = _json_value(old_value)
                updated = apply_facts(updated, [fact])
                after = _json_value(getattr(updated, fact.field))
                status = "applied" if before != after else "ignored"
                decisions.append(ConflictDecision(status=status, field=fact.field, reason="explicit_append"))
                if status == "applied":
                    self._record(updated, fact, old_value, getattr(updated, fact.field), "explicit_append")
                continue

            if fact.operation == "remove":
                if not self._may_replace(updated, fact, old_value):
                    updated, decision = self._hold_for_confirmation(updated, fact, "lower_priority_removal")
                    decisions.append(decision)
                    continue
                before = _json_value(old_value)
                updated = apply_facts(updated, [fact])
                after_value = getattr(updated, fact.field)
                status = "applied" if before != _json_value(after_value) else "ignored"
                decisions.append(ConflictDecision(status=status, field=fact.field, reason="explicit_negation"))
                if status == "applied":
                    self._record(updated, fact, old_value, after_value, "explicit_negation")
                continue

            if _equivalent(old_value, new_value):
                updated = apply_facts(updated, [fact])
                decisions.append(ConflictDecision(status="ignored", field=fact.field, reason="same_value"))
                continue

            has_existing = old_value not in (None, [], "")
            if has_existing and not self._may_replace(updated, fact, old_value):
                updated, decision = self._hold_for_confirmation(updated, fact, "lower_priority_conflict")
                decisions.append(decision)
                continue

            updated = apply_facts(updated, [fact])
            after_value = getattr(updated, fact.field)
            decisions.append(ConflictDecision(
                status="applied", field=fact.field,
                reason="explicit_overwrite" if has_existing else "new_value",
            ))
            self._record(
                updated, fact, old_value, after_value,
                "explicit_overwrite" if has_existing else "new_value",
            )
        return ConflictResolution(profile=updated, decisions=decisions)

    def _may_replace(self, profile: StudentProfile, fact: CandidateFact, old_value: Any) -> bool:
        existing_source = self._source_of_current_value(profile, fact.field, old_value)
        return self._priority(fact.source) >= self._priority(existing_source)

    def _source_of_current_value(self, profile: StudentProfile, field: str, value: Any) -> str:
        for change in reversed(profile.change_history):
            if change.field == field:
                return change.source
        for previous in reversed(profile.facts):
            if previous.field == field and previous.operation != "remove" and _equivalent(previous.value, value):
                return previous.source
        return "system"

    def _hold_for_confirmation(
        self, profile: StudentProfile, fact: CandidateFact, reason: str
    ) -> tuple[StudentProfile, ConflictDecision]:
        fact.needs_confirmation = True
        updated = apply_facts(profile, [fact])
        return updated, ConflictDecision(
            status="confirmation_required", field=fact.field, reason=reason,
        )

    def _record(
        self, profile: StudentProfile, fact: CandidateFact,
        old_value: Any, new_value: Any, reason: str,
    ) -> None:
        profile.change_history.append(ProfileChange(
            field=fact.field, old_value=old_value, new_value=new_value,
            operation=fact.operation, source=fact.source, confidence=fact.confidence,
            evidence=fact.evidence, reason=reason,
        ))

    def _priority(self, source: str) -> int:
        return self.SOURCE_PRIORITY.get(source.casefold(), 0)


def _equivalent(left: Any, right: Any) -> bool:
    return _json_value(left).casefold() == _json_value(right).casefold()


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
