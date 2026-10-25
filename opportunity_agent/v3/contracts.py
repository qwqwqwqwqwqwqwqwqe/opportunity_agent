"""Versioned API/agent contracts; extra fields never become ORM attributes."""
from __future__ import annotations

from datetime import date, datetime
from datetime import date as CalendarDate
from typing import Any, Literal
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Command(Contract):
    type: str
    ref: str | None = Field(default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")
    target_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)


class CommandBatch(Contract):
    schema_version: Literal[3] = 3
    request_id: str = Field(min_length=1, max_length=140)
    commands: list[Command] = Field(min_length=1, max_length=50)


class EvidenceRef(Contract):
    source_id: str | None = None
    quote: str
    start: int | None = None
    end: int | None = None
    verification_status: Literal["user_provided", "unknown"] = "user_provided"


class WorkflowGoal(Contract):
    intent: Literal["chat", "intake", "planning", "review", "intake_and_plan"] = "chat"
    target_refs: list[str] = Field(default_factory=list)
    requested_actions: list[str] = Field(default_factory=list)
    required_outputs: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)


class RouteDecision(Contract):
    mode: Literal["direct_reply", "delegate"]
    agents: list[Literal["intake", "planning", "review"]] = Field(default_factory=list)
    dependencies: list[tuple[str, str]] = Field(default_factory=list)
    reason: str = ""


class IntakeResult(Contract):
    candidate_facts: list[dict] = Field(default_factory=list)
    proposed_commands: list[Command] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class CompletionResult(Contract):
    status: Literal["COMPLETED", "WAITING_APPROVAL", "NEED_USER", "PARTIAL", "FAILED"]
    missing_outputs: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class ProgramData(Contract):
    university: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    degree: str = ""
    home_unit: str = ""
    aliases: list[str] = Field(default_factory=list)


class OfferingData(Contract):
    program_id: str
    cycle: str | None = Field(default=None, max_length=40)
    deadline_date: date | None = None
    deadline_at: datetime | None = None
    timezone: str = "Asia/Shanghai"
    date_precision: Literal["unknown", "date", "instant"] = "unknown"
    gre_policy: Literal["unknown", "required", "optional", "not_required", "not_accepted"] = "unknown"
    requirements_json: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def dates(self):
        ZoneInfo(self.timezone)
        if self.deadline_at and self.deadline_at.tzinfo is None:
            raise ValueError("deadline_at requires an explicit timezone")
        if self.date_precision == "unknown" and (self.deadline_date or self.deadline_at):
            raise ValueError("set date_precision when supplying a deadline")
        if self.date_precision == "date" and (not self.deadline_date or self.deadline_at):
            raise ValueError("date precision requires only deadline_date")
        if self.date_precision == "instant" and (not self.deadline_at or self.deadline_date):
            raise ValueError("instant precision requires only deadline_at")
        return self


class ApplicationData(Contract):
    offering_id: str
    status: Literal["considering", "preparing", "submitted", "offered", "rejected", "withdrawn"] = "considering"
    priority: int = Field(default=0, ge=0, le=100)


class ScoreAlternative(Contract):
    profile_field: Literal["toefl_score", "ielts_score", "gre_score"]
    minimum: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    subscores: dict[Literal["reading", "listening", "speaking", "writing", "verbal", "quantitative"], float] = Field(default_factory=dict)


class RequirementCriteria(Contract):
    profile_field: Literal["toefl_score", "ielts_score", "gre_score", "gpa"] | None = None
    minimum: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    required_status: Literal["ready", "submitted"] = "submitted"
    valid_until: CalendarDate | None = None
    requires_validity: bool = False
    notes: str = ""
    any_of: list[ScoreAlternative] = Field(default_factory=list, max_length=3)


class RequirementData(Contract):
    application_id: str
    key: str = Field(min_length=1, max_length=160)
    title: str = Field(default="", max_length=200)
    archived: bool = False
    kind: Literal["document", "recommendation", "score", "exam", "other"] = "document"
    obligation: Literal["required", "optional", "unknown"] = "unknown"
    required_count: int | None = Field(default=None, ge=1, le=100, strict=True)
    criteria: RequirementCriteria = Field(default_factory=RequirementCriteria)
    source_id: str | None = None


class MaterialData(Contract):
    requirement_id: str
    slot_key: str = Field(min_length=1, max_length=160)
    status: Literal["missing", "invited", "draft", "ready", "submitted"] = "missing"
    archived: bool = False
    evidence_ref: str | None = None


class TaskData(Contract):
    application_id: str | None = None
    requirement_id: str | None = None
    stable_key: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    status: Literal["planned", "in_progress", "completed", "cancelled"] = "planned"
    start_date: date | None = None
    due_date: date | None = None
    date_certainty: Literal["unknown", "estimated", "confirmed"] = "unknown"
    duration_days: int = Field(default=1, ge=1, le=365)
    locked: bool = False
    show_in_calendar: bool = False
    evidence: str = ""

    @model_validator(mode="after")
    def dates(self):
        if self.start_date and self.due_date and self.start_date > self.due_date:
            raise ValueError("task start_date must not exceed due_date")
        if self.date_certainty == "unknown" and (self.start_date or self.due_date):
            raise ValueError("task dates require a certainty label")
        if self.show_in_calendar and not (self.due_date or self.start_date):
            raise ValueError("显示在日历的待办需要填写日期")
        return self


class EventData(Contract):
    type: Literal["exam", "appointment", "unavailable", "other"] = "appointment"
    title: str = Field(min_length=1, max_length=300)
    date: CalendarDate | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str = "Asia/Shanghai"
    date_precision: Literal["unknown", "date", "instant"] = "unknown"

    @model_validator(mode="after")
    def dates(self):
        ZoneInfo(self.timezone)
        if self.date_precision == "date" and (not self.date or self.start_at or self.end_at):
            raise ValueError("date precision requires only date")
        if self.date_precision == "instant" and (not self.start_at or self.date):
            raise ValueError("instant precision requires start_at")
        if self.date_precision == "unknown" and (self.date or self.start_at or self.end_at):
            raise ValueError("event dates require date_precision")
        for value in (self.start_at, self.end_at):
            if value and value.tzinfo is None:
                raise ValueError("event timestamps require timezone")
        if self.end_at and (not self.start_at or self.end_at < self.start_at):
            raise ValueError("invalid event time range")
        return self


class DependencyData(Contract):
    predecessor_id: str
    successor_id: str
    lag_days: int = Field(default=0, ge=0, le=365)


class SourceData(Contract):
    kind: Literal["form", "chat", "import"] = "form"
    source_ref: str | None = None
    excerpt: str = Field(min_length=1, max_length=20000)


class DraftRequest(Contract):
    request_id: str = Field(min_length=1, max_length=140)
    today: date | None = None
    application_ids: list[str] = Field(default_factory=list)


class Decision(Contract):
    request_id: str = Field(min_length=1, max_length=140)
    expected_version: int = Field(ge=1)
