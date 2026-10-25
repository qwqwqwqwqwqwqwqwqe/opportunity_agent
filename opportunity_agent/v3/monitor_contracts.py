"""Versioned, read-only monitoring and impact-analysis contracts."""
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MonitorConfig(Strict):
    request_id: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(default=0, ge=0)
    enabled: bool = False
    mode: Literal["daily", "manual"] = "manual"
    timezone: str = "Asia/Shanghai"
    daily_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    project_url: str = Field(min_length=1, max_length=2000)
    college_url: str = Field(default="", max_length=2000)
    faq_url: str = Field(default="", max_length=2000)
    confirmed: bool = False

    @model_validator(mode="after")
    def validate_sources(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("请输入有效的 IANA 时区，例如 Asia/Shanghai") from None
        for value in (self.project_url, self.college_url, self.faq_url):
            if value:
                p = urlsplit(value)
                if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443) or p.fragment:
                    raise ValueError("请输入不含凭证、片段的 HTTPS 官网地址")
        if not self.confirmed:
            raise ValueError("请确认这些页面属于申请项目的官方网站")
        return self


class Request(Strict):
    request_id: str = Field(min_length=1, max_length=160)


class OfficialEvidence(Strict):
    snapshot_id: str
    block_id: str
    quote: str = Field(min_length=1, max_length=4000)


class PersonalEvidence(Strict):
    ref: str
    value: object


class ImpactResult(Strict):
    schema_version: int = Field(default=1, ge=1, le=1)
    check_id: str
    change_ids: list[str]
    applicability: Literal["MATCH", "NOT_MATCH", "UNKNOWN"]
    impact: Literal["AFFECTED", "NO_DIRECT_IMPACT", "UNKNOWN"]
    direction: Literal["POSITIVE", "NEGATIVE", "MIXED", "NONE", "UNKNOWN"]
    categories: list[str] = Field(default_factory=list)
    affected_items: list[str] = Field(default_factory=list)
    official_evidence_refs: list[OfficialEvidence] = Field(default_factory=list)
    personal_basis_refs: list[PersonalEvidence] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    notification_title: str = Field(default="", max_length=200)
    notification_body: str = Field(default="", max_length=4000)


class AgentAction(Strict):
    tool: Literal["get_change_evidence", "read_official_page", "list_official_links", "get_application_context",
                  "get_user_facts", "get_materials_and_tasks", "check_deadline_impact", "finish"]
    arguments: dict = Field(default_factory=dict)
    result: ImpactResult | None = None
