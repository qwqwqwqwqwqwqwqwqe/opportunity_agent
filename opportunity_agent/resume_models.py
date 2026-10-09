"""Resume drafts are separate from the authoritative, confirmed profile."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from .models import CandidateFact, ExamPlan, OnboardingProfileInput

EXPERIENCE_FIELDS = {
    "research": "research_experiences", "project": "project_experiences",
    "internship": "internship_experiences", "competition": "competition_experiences",
    "paper": "paper_experiences",
}
RESUME_FIELDS = {
    "school", "major", "academic_year", "degree_years", "graduation_year", "graduation_month",
    "target_countries", "target_degree", "target_fields", "target_schools", "target_programs",
    "gpa_raw", "gpa_scale", "class_rank", "toefl_score", "ielts_score", "gre_score",
    "completed_courses", "skills", "hardware_skills", "budget", "exam_plan",
    "planned_enrollment_year", "planned_enrollment_month", "summer_preference",
}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_PAGES = 20
MAX_TEXT_CHARS = 60000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResumeBlock(BaseModel):
    block_id: str
    text: str = Field(max_length=MAX_TEXT_CHARS)
    page: int | None = None
    locator: str


class ParsedResumeDocument(BaseModel):
    blocks: list[ResumeBlock] = Field(default_factory=list, max_length=2000)
    parser: str = "local"
    page_count: int | None = None
    needs_cloud: bool = False
    warnings: list[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.blocks)


class ResumeFact(CandidateFact):
    selected: bool = True
    block_ids: list[str] = Field(default_factory=list)
    source: str = "resume"
    evidence: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_field(self):
        if self.field not in RESUME_FIELDS:
            raise ValueError(f"不支持的简历字段：{self.field}")
        value = self.value
        if value is not None:
            if self.field == "exam_plan":
                if not isinstance(value, dict) or not all(k in value for k in ("exam_type", "next_exam_date")):
                    raise ValueError("考试类型和日期必须明确提供")
                ExamPlan.model_validate(value)
            else:
                if self.field == "budget" and (not isinstance(value, dict) or
                        not all(k in value for k in ("amount", "currency", "period"))):
                    raise ValueError("预算需提供金额、币种及周期；不推测缺失币种")
                # Reuse existing value constraints, without supplying any of
                # these placeholder/default values to the extraction result.
                base = {"school": "未确定", "major": "未确定", "target_countries": ["未确定"],
                        "target_degree": "未确定", "target_fields": ["未确定"]}
                OnboardingProfileInput.model_validate({**base, self.field: value})
        return self


class ResumeExperience(BaseModel):
    experience_id: str = Field(default_factory=lambda: uuid4().hex, max_length=100)
    kind: Literal["research", "project", "internship", "competition", "paper"]
    name: str = Field(min_length=1, max_length=300)
    organization: str = Field(default="", max_length=300)
    period: str = Field(default="", max_length=100)
    role: str = Field(default="", max_length=500)
    methods: str = Field(default="", max_length=2000)
    outcomes: str = Field(default="", max_length=2000)
    evidence: str = Field(default="", max_length=5000)
    block_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=.75, ge=0, le=1)
    selected: bool = True

    def description(self) -> str:
        return "；".join(part for part in [
            self.name, self.organization, self.period,
            f"职责：{self.role}" if self.role else "",
            f"技术与方法：{self.methods}" if self.methods else "",
            f"成果：{self.outcomes}" if self.outcomes else "",
        ] if part)


class ResumeDraft(BaseModel):
    facts: list[ResumeFact] = Field(default_factory=list, max_length=40)
    experiences: list[ResumeExperience] = Field(default_factory=list, max_length=50)
    warnings: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_fields(self):
        names = [fact.field for fact in self.facts]
        if len(set(names)) != len(names):
            raise ValueError("每个画像字段只能出现一次")
        values = {f.field: f.value for f in self.facts if f.selected}
        raw, scale = values.get("gpa_raw"), values.get("gpa_scale")
        if raw is not None and scale is not None and float(raw) > float(scale):
            raise ValueError("GPA 原值不能高于量表")
        ids = [item.experience_id for item in self.experiences]
        if len(set(ids)) != len(ids):
            raise ValueError("经历标识不能重复")
        return self


class ResumeImportJob(BaseModel):
    import_id: str
    session_id: str
    request_id: str
    file_hash: str
    filename: str
    status: Literal["queued", "reading", "awaiting_consent", "cloud_parsing", "extracting",
                    "review", "failed", "interrupted", "confirming", "confirmed", "cancelled"] = "queued"
    revision: int = 1
    base_profile_revision: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    temp_path: str | None = None
    parsed: ParsedResumeDocument | None = None
    draft: ResumeDraft = Field(default_factory=ResumeDraft)
    original_draft: ResumeDraft | None = None
    error: str | None = None
    mode: str = "pending"
    cloud_consent: bool = False
    cloud_consent_at: datetime | None = None
    attempt: int = 0
    # Components are deliberately persisted independently from the draft.  A
    # retry can then skip the LLM calls that have already produced reviewable
    # data instead of replacing a partial draft after a later timeout.
    completed_components: list[str] = Field(default_factory=list, max_length=200)
    confirmation_request: dict[str, Any] | None = None
    confirmed_revision: int | None = None
