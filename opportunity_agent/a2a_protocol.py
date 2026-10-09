"""Versioned, text-safe contracts for the local openJiuwen A2A boundary."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from .models import CandidateFact, ExtractionResult, ProgressUpdate


PROTOCOL_VERSION = "1"
PROFILE_FIELDS = {
    "school", "academic_year", "degree_years", "major", "target_countries", "target_regions",
    "target_schools", "target_programs", "target_degree", "target_fields", "graduation_year",
    "graduation_month", "planned_enrollment_year", "planned_enrollment_month", "gpa", "gpa_raw",
    "gpa_scale", "class_rank", "toefl_score", "ielts_score", "gre_score", "skills",
    "hardware_skills", "completed_courses", "research_experiences", "competition_experiences",
    "project_experiences", "paper_experiences", "internship_experiences", "budget", "exam_plan",
    "summer_preference", "career_goal", "target_locations", "current_stage",
}


class ExtractedFact(BaseModel):
    field: str
    raw_value: str | int | float | bool | list[str] | dict[str, Any]
    normalized_value: str | int | float | bool | list[str] | dict[str, Any] | None = None
    operation: Literal["set", "append", "remove"] = "set"
    statement_kind: Literal["explicit", "uncertain", "hypothetical", "negated"] = "explicit"
    evidence: str = Field(min_length=1)

    @field_validator("field")
    @classmethod
    def supported_field(cls, value: str) -> str:
        if value not in PROFILE_FIELDS:
            raise ValueError(f"unsupported profile field: {value}")
        return value


class ExtractedProgress(BaseModel):
    target_id: str | None = None
    target_kind: Literal["task", "event"] = "task"
    target_hint: str = ""
    action: Literal["start", "complete", "postpone", "cancel", "reset"]
    postponed_to: str | None = None
    actual_date: str | None = None
    statement_kind: Literal["explicit", "uncertain", "hypothetical", "negated"] = "explicit"
    evidence: str = Field(min_length=1)

    def to_progress_update(self) -> ProgressUpdate | None:
        if self.statement_kind in {"hypothetical", "negated"}:
            return None
        return ProgressUpdate(
            target_id=self.target_id,
            target_kind=self.target_kind,
            target_hint=self.target_hint,
            action=self.action,
            postponed_to=self.postponed_to,
            actual_date=self.actual_date,
            evidence=self.evidence,
            confidence=0.95 if self.statement_kind == "explicit" else 0.55,
            needs_confirmation=self.statement_kind == "uncertain",
        )


class OpportunityDelegation(BaseModel):
    route: Literal["profile_update", "progress_update", "mixed"]
    context_token: str = Field(min_length=8, max_length=128)
    raw_message: str = Field(min_length=1, max_length=10000)
    facts: list[ExtractedFact] = Field(default_factory=list)
    progress_updates: list[ExtractedProgress] = Field(default_factory=list)
    question: str = ""
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def has_mutation(self) -> "OpportunityDelegation":
        if not self.facts and not self.progress_updates:
            raise ValueError("delegation must contain facts or progress updates")
        return self

    def to_extraction_result(self) -> ExtractionResult:
        facts: list[CandidateFact] = []
        for item in self.facts:
            if item.statement_kind == "hypothetical":
                continue
            confidence = 0.95 if item.statement_kind == "explicit" else 0.55
            operation = "remove" if item.statement_kind == "negated" else item.operation
            facts.append(CandidateFact(
                field=item.field,
                raw_value=item.raw_value,
                normalized_value=item.normalized_value,
                operation=operation,
                confidence=confidence,
                needs_confirmation=item.statement_kind in {"uncertain", "negated"},
                source="conversation",
                evidence=item.evidence,
            ))
        updates = [converted for item in self.progress_updates if (converted := item.to_progress_update()) is not None]
        return ExtractionResult(
            intent=self.route,
            facts=facts,
            progress_updates=updates,
            should_replan=bool(facts or updates),
        )


class OpportunityMutationRequest(BaseModel):
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    conversation_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    base_revision: int = Field(ge=0)
    event_id: str = Field(min_length=1)
    selected_target_id: str | None = None
    delegation: OpportunityDelegation
    state_snapshot: dict[str, Any]

    @model_validator(mode="after")
    def evidence_is_from_message(self) -> "OpportunityMutationRequest":
        message = self.delegation.raw_message
        for fact in self.delegation.facts:
            if fact.evidence not in message:
                raise ValueError(f"fact evidence is not present in raw message: {fact.field}")
        for update in self.delegation.progress_updates:
            if update.evidence and update.evidence not in message:
                raise ValueError("progress evidence is not present in raw message")
        return self


class OpportunityMutationResult(BaseModel):
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    request_id: str
    base_revision: int
    accepted_facts: list[CandidateFact] = Field(default_factory=list)
    ignored_facts: list[dict[str, Any]] = Field(default_factory=list)
    progress_updates: list[dict[str, Any]] = Field(default_factory=list)
    state_changes: list[dict[str, Any]] = Field(default_factory=list)
    pending_confirmations: list[dict[str, Any]] = Field(default_factory=list)
    updated_state_snapshot: dict[str, Any]
    replan_required: bool = False
    reply_summary: str = ""


class A2ATrace(BaseModel):
    trace_id: str = Field(default_factory=lambda: uuid4().hex)
    route: str
    reason: str = ""
    agent_name: str = "Opportunity Profile & Timeline Agent"
    endpoint: str = ""
    request_id: str = ""
    task_status: Literal["not_called", "completed", "failed", "awaiting_retry"] = "not_called"
    latency_ms: int = Field(default=0, ge=0)
    extracted_facts: list[dict[str, Any]] = Field(default_factory=list)
    accepted_facts: list[dict[str, Any]] = Field(default_factory=list)
    state_changes: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
