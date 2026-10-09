from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator


class UserProfile(BaseModel):
    user_id: str
    major: str
    degree: str
    school: str
    graduation_year: int
    skills: list[str]
    career_goal: str
    target_locations: list[str] = Field(default_factory=list)
    target_companies: list[str] = Field(default_factory=list)
    current_stage: str
    interests: list[str] = Field(default_factory=list)


class Job(BaseModel):
    job_id: str
    company: str
    title: str
    location: str
    description: str
    requirements: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    employment_type: Literal["internship", "full_time", "contract"]
    deadline: date | None = None
    source: str
    created_at: datetime
    domains: list[str] = Field(default_factory=list)


class MatchResult(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    matched_skills: list[str]
    missing_skills: list[str]
    reason: str
    should_notify: bool


class Recommendation(BaseModel):
    recommendation_id: str = Field(default_factory=lambda: f"rec_{uuid4().hex[:12]}")
    user_id: str
    job_id: str
    score: float
    matched_skills: list[str]
    missing_skills: list[str]
    reason: str
    should_notify: bool
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# Lifecycle-planning models. These coexist with the job models above so the
# existing job-matching spike remains a reusable Phase-4 extension.
class CandidateFact(BaseModel):
    field: str
    raw_value: Any
    normalized_value: Any | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    source: str = Field(min_length=1)
    needs_confirmation: bool = False
    evidence: str | None = None
    operation: Literal["set", "append", "remove"] = "set"

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_value(cls, value: Any) -> Any:
        """Accept V1 facts that only contain ``value``.

        Browser snapshots and external Tool callers may keep the old wire shape
        for a while. Preserve that input and expose the same value on output.
        """
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "raw_value" not in migrated and "value" in migrated:
            migrated["raw_value"] = migrated["value"]
        if "normalized_value" not in migrated and "value" in migrated:
            migrated["normalized_value"] = migrated["value"]
        return migrated

    @computed_field(return_type=Any)
    @property
    def value(self) -> Any:
        """V1-compatible effective value used by existing consumers."""
        return self.normalized_value if self.normalized_value is not None else self.raw_value


class StageSignal(BaseModel):
    stage: str = Field(min_length=1)
    direction: Literal["increase", "decrease"]
    strength: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(min_length=1)


class InformationNeed(BaseModel):
    """Forward-compatible data contract; Task-013 will implement its policy."""

    field: str = Field(min_length=1)
    information_gain: float = Field(default=0.0, ge=0.0, le=1.0)
    planning_importance: float = Field(default=0.0, ge=0.0, le=1.0)
    stage_importance: float = Field(default=0.0, ge=0.0, le=1.0)
    uncertainty: float = Field(default=0.0, ge=0.0, le=1.0)
    user_burden: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


class ExtractionDiagnostics(BaseModel):
    latency_ms: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    fallback_reason: str | None = None
    malformed_outputs: list[str] = Field(default_factory=list)


class ProgressUpdate(BaseModel):
    target_id: str | None = None
    target_kind: Literal["task", "event"] = "task"
    target_hint: str = ""
    action: Literal["start", "complete", "postpone", "cancel", "reset"]
    postponed_to: date | None = None
    actual_date: date | None = None
    evidence: str = ""
    confidence: float = Field(default=1.0, ge=0, le=1)
    needs_confirmation: bool = False

    @model_validator(mode="after")
    def require_postponement_date(self) -> "ProgressUpdate":
        if self.action == "postpone" and self.postponed_to is None:
            self.needs_confirmation = True
        return self


class TimelineFactUpdate(BaseModel):
    """Explicit profile evidence submitted from a timeline-node form."""

    node_title: str = Field(min_length=1, max_length=120)
    node_date: date | None = None
    fact_field: Literal["research_experiences", "internship_experiences", "project_experiences", "completed_courses"]
    detail: str = Field(min_length=2, max_length=2000)
    occurred_on: date | None = None


class ExtractionResult(BaseModel):
    intent: str | None = None
    facts: list[CandidateFact] = Field(default_factory=list)
    stage_signals: list[StageSignal] = Field(default_factory=list)
    information_needs: list[InformationNeed] = Field(default_factory=list)
    should_replan: bool = False
    diagnostics: ExtractionDiagnostics = Field(default_factory=ExtractionDiagnostics)
    progress_updates: list[ProgressUpdate] = Field(default_factory=list)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1)
    message_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime | None = None  # Unknown for legacy messages; never invent their date.
    event_id: str | None = None
    processing_status: Literal["received", "processed", "failed", "awaiting_agent_retry", "legacy"] = "legacy"


class UserEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    request_id: str
    source: Literal["chat", "form", "timeline", "progress", "confirmation", "replan", "resume"]
    raw_text: str
    message_id: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    extraction: ExtractionResult | None = None
    status: Literal["received", "processed", "failed", "awaiting_agent_retry"] = "received"
    reply: str = ""
    error: str | None = None
    payload_fingerprint: str = ""
    route: str = ""
    route_reason: str = ""
    a2a_trace: dict[str, Any] | None = None


class TaskProgress(BaseModel):
    target_id: str
    target_kind: Literal["task", "event"] = "task"
    title: str
    category: str = ""
    status: Literal["planned", "in_progress", "completed", "cancelled"] = "planned"
    actual_date: date | None = None
    postponed_to: date | None = None
    evidence: str
    source_event_id: str
    confidence: float = Field(default=1.0, ge=0, le=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StateTransition(BaseModel):
    field: str
    old_value: Any = None
    new_value: Any = None
    reason: str
    evidence: str
    source_event_id: str
    confidence: float = Field(default=1.0, ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StageEvidence(BaseModel):
    """One auditable input used to derive a multi-dimensional user stage."""

    evidence_id: str = Field(default_factory=lambda: uuid4().hex)
    dimension: Literal["academic", "language", "research", "application", "career"]
    kind: Literal["fact", "progress", "signal", "task_operation", "confirmation", "timeline"]
    direction: Literal["increase", "decrease", "neutral"] = "increase"
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(min_length=1)
    source_event_id: str
    target_id: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None


class StageAssessment(BaseModel):
    dimension: Literal["academic", "language", "research", "application", "career"]
    state: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    assessed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PendingConfirmation(BaseModel):
    confirmation_id: str = Field(default_factory=lambda: uuid4().hex)
    source_event_id: str
    question: str
    fact: CandidateFact | None = None
    progress_update: ProgressUpdate | None = None
    candidate_target_ids: list[str] = Field(default_factory=list)
    status: Literal["pending", "accepted", "rejected"] = "pending"


class AgentTurnResult(BaseModel):
    reply: str = ""
    progress_updates: list[TaskProgress] = Field(default_factory=list)
    state_changes: list[StateTransition] = Field(default_factory=list)
    pending_confirmations: list[PendingConfirmation] = Field(default_factory=list)
    replan_required: bool = False
    state_revision: int = 0
    answer_fallback_reason: str | None = None
    official_research: dict[str, Any] | None = None
    route: str = ""
    route_reason: str = ""
    a2a_trace: dict[str, Any] | None = None
    pending_a2a_retry: bool = False


class Budget(BaseModel):
    amount: float | None = Field(default=None, ge=0)
    currency: str = "CNY"
    period: Literal["total", "annual", "unknown"] = "unknown"


class ExamPlan(BaseModel):
    exam_type: Literal["TOEFL", "IELTS", "GRE", "other"] = "other"
    next_exam_date: date
    target_score: float | None = None


class TargetProgram(BaseModel):
    """An explicit school--program pair used for scoped official research."""

    school: str = Field(min_length=1, max_length=160)
    program: str = Field(default="", max_length=200)

    @field_validator("school", "program")
    @classmethod
    def strip_target_text(cls, value: str) -> str:
        return value.strip()


def legacy_target_program_pairs(
    schools: list[str], programs: list[str],
) -> tuple[list[TargetProgram], bool]:
    """Make old parallel target lists safe without inventing a pairing.

    One historic program was conventionally applied to every listed school, so
    that unambiguous case remains usable.  All other uneven lists require an
    explicit confirmation in the profile form.
    """
    clean_schools = list(dict.fromkeys(item.strip() for item in schools if item and item.strip()))
    clean_programs = list(dict.fromkeys(item.strip() for item in programs if item and item.strip()))
    if not clean_schools:
        return [], False
    if len(clean_programs) == 1:
        return [TargetProgram(school=school, program=clean_programs[0]) for school in clean_schools], False
    if len(clean_programs) == len(clean_schools):
        return [TargetProgram(school=school, program=program)
                for school, program in zip(clean_schools, clean_programs, strict=True)], False
    return [TargetProgram(school=school) for school in clean_schools], bool(clean_programs)


class OnboardingProfileInput(BaseModel):
    school: str = Field(min_length=1)
    major: str = Field(min_length=1)
    academic_year: int | None = Field(default=None, ge=1, le=8)
    degree_years: int | None = Field(default=4, ge=3, le=8)
    graduation_year: int | None = Field(default=None, ge=2020, le=2100)
    graduation_month: int | None = Field(default=6, ge=1, le=12)
    target_countries: list[str] = Field(min_length=1)
    target_degree: str = Field(min_length=1)
    target_fields: list[str] = Field(min_length=1)
    gpa_raw: float | None = Field(default=None, ge=0)
    gpa_scale: float | None = Field(default=None, gt=0)
    class_rank: str | None = None
    toefl_score: int | None = Field(default=None, ge=0, le=120)
    ielts_score: float | None = Field(default=None, ge=0, le=9)
    gre_score: int | None = Field(default=None, ge=260, le=340)
    next_exam_type: Literal["TOEFL", "IELTS", "GRE", "other"] | None = None
    next_exam_date: date | None = None
    target_schools: list[str] = Field(default_factory=list)
    target_programs: list[str] = Field(default_factory=list)
    target_program_choices: list[TargetProgram] = Field(default_factory=list)
    budget: Budget | None = None
    completed_courses: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    hardware_skills: list[str] = Field(default_factory=list)
    research_experiences: list[str] = Field(default_factory=list)
    competition_experiences: list[str] = Field(default_factory=list)
    project_experiences: list[str] = Field(default_factory=list)
    paper_experiences: list[str] = Field(default_factory=list)
    internship_experiences: list[str] = Field(default_factory=list)
    summer_preference: Literal["research", "internship", "both", "unknown"] = "unknown"
    planned_enrollment_year: int | None = Field(default=None, ge=2020, le=2100)
    planned_enrollment_month: int | None = Field(default=9, ge=1, le=12)

    @field_validator(
        "target_countries", "target_fields", "target_schools", "target_programs",
        "completed_courses", "skills", "hardware_skills", mode="before",
    )
    @classmethod
    def split_form_lists(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in re.split(r"[,，、;；\n]+", value) if part.strip()]
        return value

    @field_validator("research_experiences", "competition_experiences", "project_experiences",
                     "paper_experiences", "internship_experiences", mode="before")
    @classmethod
    def split_experience_lines(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        return value

    @model_validator(mode="after")
    def validate_exam_plan(self) -> "OnboardingProfileInput":
        if self.next_exam_date and not self.next_exam_type:
            raise ValueError("next_exam_type is required when next_exam_date is provided")
        if self.target_program_choices:
            self.target_schools = [item.school for item in self.target_program_choices]
            self.target_programs = list(dict.fromkeys(item.program for item in self.target_program_choices if item.program))
        return self


class ProfileChange(BaseModel):
    field: str
    old_value: Any = None
    new_value: Any = None
    operation: Literal["set", "append", "remove"] = "set"
    source: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str | None = None
    reason: str
    changed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StudentProfile(BaseModel):
    user_id: str
    school: str | None = None
    academic_year: int | None = Field(default=None, ge=1, le=8)
    degree_years: int | None = Field(default=4, ge=3, le=8)
    major: str | None = None
    target_countries: list[str] = Field(default_factory=list)
    target_regions: list[str] = Field(default_factory=list)
    target_schools: list[str] = Field(default_factory=list)
    target_programs: list[str] = Field(default_factory=list)
    target_program_choices: list[TargetProgram] = Field(default_factory=list)
    target_program_mapping_needs_review: bool = False
    target_degree: str | None = None
    target_fields: list[str] = Field(default_factory=list)
    graduation_year: int | None = None
    graduation_month: int | None = Field(default=6, ge=1, le=12)
    planned_enrollment_year: int | None = None
    planned_enrollment_month: int | None = Field(default=9, ge=1, le=12)
    gpa: float | None = Field(default=None, ge=0.0, le=4.0)
    gpa_raw: float | None = Field(default=None, ge=0)
    gpa_scale: float | None = Field(default=None, gt=0)
    gpa_4_reference: float | None = Field(default=None, ge=0, le=4)
    class_rank: str | None = None
    toefl_score: int | None = Field(default=None, ge=0, le=120)
    ielts_score: float | None = Field(default=None, ge=0.0, le=9.0)
    gre_score: int | None = Field(default=None, ge=260, le=340)
    skills: list[str] = Field(default_factory=list)
    hardware_skills: list[str] = Field(default_factory=list)
    completed_courses: list[str] = Field(default_factory=list)
    research_experiences: list[str] = Field(default_factory=list)
    competition_experiences: list[str] = Field(default_factory=list)
    project_experiences: list[str] = Field(default_factory=list)
    paper_experiences: list[str] = Field(default_factory=list)
    internship_experiences: list[str] = Field(default_factory=list)
    budget: Budget | None = None
    exam_plan: ExamPlan | None = None
    summer_preference: Literal["research", "internship", "both", "unknown"] = "unknown"
    onboarding_completed: bool = False
    planning_domain: str | None = None
    career_goal: str | None = None
    target_locations: list[str] = Field(default_factory=list)
    current_stage: str | None = None
    facts: list[CandidateFact] = Field(default_factory=list)
    change_history: list[ProfileChange] = Field(default_factory=list)

    @field_validator(
        "target_countries", "target_regions", "target_schools", "target_programs",
        "target_fields", "skills", "hardware_skills", "completed_courses",
        "research_experiences", "competition_experiences", "project_experiences",
        "paper_experiences", "internship_experiences", "target_locations", mode="before",
    )
    @classmethod
    def normalize_string_lists(cls, value: Any) -> Any:
        """Normalize model strings and repair old character-split snapshots."""
        if isinstance(value, str):
            return [part.strip() for part in re.split(r"[,，、;；]+", value) if part.strip()]
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            if len(items) > 1 and all(len(item) == 1 and "\u4e00" <= item <= "\u9fff" for item in items):
                return ["".join(items)]
            return items
        return value

    @model_validator(mode="after")
    def migrate_target_program_choices(self) -> "StudentProfile":
        if self.target_program_choices:
            self.target_schools = [item.school for item in self.target_program_choices]
            self.target_programs = list(dict.fromkeys(item.program for item in self.target_program_choices if item.program))
            return self
        pairs, needs_review = legacy_target_program_pairs(self.target_schools, self.target_programs)
        self.target_program_choices = pairs
        self.target_program_mapping_needs_review = needs_review
        return self


class UserState(BaseModel):
    academic: Literal["unknown", "early_undergraduate", "mid_undergraduate", "senior"] = "unknown"
    language: Literal["unknown", "not_started", "preparing", "completed"] = "unknown"
    research: Literal["unknown", "exploring", "building"] = "unknown"
    application: Literal["unknown", "exploring", "preparing", "applying"] = "unknown"
    career: Literal["unknown", "exploring", "internship_search", "full_time_search"] = "unknown"
    language_evidence: Literal["unknown", "preparing", "exam_taken", "score_recorded", "requirements_verified"] = "unknown"


class PlanTask(BaseModel):
    task_id: str
    title: str
    category: Literal["academic", "language", "research", "application", "career"]
    due_date: date | None = None
    depends_on: list[str] = Field(default_factory=list)
    status: Literal["planned", "in_progress", "completed", "cancelled", "overdue"] = "planned"
    progress_key: str = ""
    aliases: list[str] = Field(default_factory=list)
    execution_status: Literal["planned", "in_progress", "completed", "cancelled"] = "planned"
    is_overdue: bool = False
    boundary_conflict: bool = False
    risk_status: Literal["none", "overdue", "needs_backfill", "schedule_conflict", "ahead_of_schedule"] = "none"
    progress_evidence: str = ""
    progress_confidence: float = Field(default=0.0, ge=0, le=1)
    actual_date: date | None = None
    original_due_date: date | None = None
    reason: str
    source: str
    confidence: float = Field(ge=0.0, le=1.0)


class Milestone(BaseModel):
    milestone_id: str
    title: str
    tasks: list[PlanTask]


class JobRecommendation(BaseModel):
    job_id: str
    company: str
    title: str
    location: str
    score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    matched_skills: list[str]
    missing_skills: list[str]
    reason: str
    source: str
    should_notify: bool


class SkillPlanResult(BaseModel):
    skill_name: str
    title: str
    summary: str
    tasks: list[PlanTask] = Field(default_factory=list)
    recommendations: list[JobRecommendation] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=lambda: ["internal_seed"])
    confidence: float = Field(default=0.8, ge=0, le=1)
    generation_mode: Literal["qwen", "rule_fallback"] = "rule_fallback"
    fallback_reason: str | None = None


class TimelineEvent(BaseModel):
    event_id: str
    title: str
    event_date: date
    start_date: date | None = None
    end_date: date | None = None
    kind: Literal["exam", "winter_break", "summer_break", "materials", "application", "offer", "visa", "enrollment", "general"]
    status: Literal["history", "current", "upcoming", "overdue"] = "upcoming"
    phase_id: str
    detail: str = ""
    source: str = "timeline_rule"
    confidence: float = Field(default=0.98, ge=0, le=1)
    verification_required: bool = False
    progress_key: str = ""
    execution_status: Literal["planned", "in_progress", "completed", "cancelled"] = "planned"
    time_status: Literal["history", "current", "upcoming"] = "upcoming"
    is_overdue: bool = False
    boundary_conflict: bool = False
    risk_status: Literal["none", "overdue", "needs_backfill", "schedule_conflict", "ahead_of_schedule"] = "none"
    progress_evidence: str = ""
    progress_confidence: float = Field(default=0.0, ge=0, le=1)
    actual_date: date | None = None
    original_event_date: date | None = None


class TimelinePhase(BaseModel):
    phase_id: str
    title: str
    start_date: date
    end_date: date
    kind: Literal["background", "winter", "summer", "materials", "application", "offer_visa", "enrollment"]
    status: Literal["history", "current", "upcoming", "overdue"] = "upcoming"
    skill_name: str
    plan: SkillPlanResult | None = None
    execution_status: Literal["planned", "in_progress", "completed", "cancelled"] = "planned"
    time_status: Literal["history", "current", "upcoming"] = "upcoming"
    is_overdue: bool = False
    risk_status: Literal["none", "overdue", "needs_backfill", "schedule_conflict", "ahead_of_schedule"] = "none"


class PlanningTimeline(BaseModel):
    domain: str
    supported: bool = True
    support_message: str = ""
    graduation_date: date | None = None
    enrollment_date: date | None = None
    phases: list[TimelinePhase] = Field(default_factory=list)
    events: list[TimelineEvent] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OfficialSource(BaseModel):
    source_id: str
    university: str
    program: str = ""
    intake: str = ""
    title: str
    url: str
    verified_domain: str
    evidence_excerpt: str = ""
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    page_updated_at: str | None = None
    content_hash: str = ""
    status: Literal["verified", "stale", "unavailable", "revoked"] = "verified"
    scope: Literal["program", "department", "university_wide"] = "university_wide"
    program_match: Literal["exact", "generic", "rejected"] = "generic"
    match_evidence: list[str] = Field(default_factory=list)
    revoked_at: datetime | None = None
    revocation_reason: str | None = None


class OfficialRequirement(BaseModel):
    field: Literal["gre", "toefl", "ielts", "prerequisite", "deadline", "tuition", "material"]
    value: str
    qualifier: Literal["required", "optional", "not_accepted", "minimum", "unknown"] = "unknown"
    source_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0, le=1)
    scope: Literal["program", "department", "university_wide"] = "university_wide"
    program_match: Literal["exact", "generic"] = "generic"


class OfficialResearchResult(BaseModel):
    requirements: list[OfficialRequirement] = Field(default_factory=list)
    sources: list[OfficialSource] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    revoked_sources: list[OfficialSource] = Field(default_factory=list)


class Roadmap(BaseModel):
    user_id: str
    goal: str
    article: str = ""
    milestones: list[Milestone]
    timeline: PlanningTimeline | None = None
    supported: bool = True
    support_message: str = ""
    version: int = Field(default=1, ge=1)
    revision_reason: str = "initial_profile"
    generation_mode: Literal["qwen", "rule_fallback"] = "rule_fallback"
    official_sources: list[OfficialSource] = Field(default_factory=list)
    verified_requirements: list[OfficialRequirement] = Field(default_factory=list)
    unresolved_requirements: list[str] = Field(default_factory=list)
    revoked_official_sources: list[OfficialSource] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ExternalEvent(BaseModel):
    event_id: str
    type: Literal["program_deadline_changed"]
    title: str
    source_url: str
    published_at: datetime
    program_name: str
    old_deadline: date | None = None
    new_deadline: date


class NotificationDecision(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    action: Literal["immediate", "digest", "store", "ignore"]
    reason: str
