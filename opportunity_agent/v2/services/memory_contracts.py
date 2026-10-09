"""User preference contracts, deliberately separate from working memory."""
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

PreferenceKey = Literal["avoid_gre", "employment_priority", "fallback_country", "budget_preference"]


class PreferenceView(BaseModel):
    memory_id: str
    key: PreferenceKey
    value: Any
    source: str
    confidence: float
    version: int
    evidence: str = ""
    source_conversation_id: str | None = None
    source_message_id: str | None = None
    expires_at: datetime | None = None
    relevance_method: str = "rule"


class PreferenceSnapshot(BaseModel):
    schema_version: Literal["1"] = "1"
    retrieval_status: Literal["ok", "unavailable"] = "ok"
    preferences: list[PreferenceView] = Field(default_factory=list, max_length=2)


class UserMemoryMessage(BaseModel):
    message_id: str
    content: str = Field(max_length=10000)


class ConsolidationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    user_id: str
    conversation_id: str
    round_id: int = 0
    completion: dict
    user_messages: list[UserMemoryMessage] = Field(default_factory=list, max_length=6)
    preference_memory: PreferenceSnapshot = Field(default_factory=PreferenceSnapshot)
    preference_versions: dict[str, int] = Field(default_factory=dict)
    preference_candidates: list[dict] = Field(default_factory=list)
    agent_statuses: dict[str, str] = Field(default_factory=dict)


class ConsolidationResult(BaseModel):
    status: Literal["no_change", "proposed", "failed"]
    proposal_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class RevokePreference(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
