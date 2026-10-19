from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=10, max_length=256)


class LoginRequest(RegisterRequest):
    pass


class UserOut(BaseModel):
    id: str
    email: str
    role: str


class ProfilePatch(BaseModel):
    payload: dict[str, Any]
    expected_version: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=128)


class ConversationCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=160)


class RunCreate(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    request_id: str = Field(min_length=1, max_length=128)


class RunCreated(BaseModel):
    run_id: str = Field(description="执行任务 ID；不是消息 ID，也不是 Trace ID")
    status: str


class RunSummary(RunCreated):
    trace_id: str | None = None
    created_at: datetime
    error: str | None = None


class ApplicationCreate(BaseModel):
    university: str = Field(min_length=1, max_length=200)
    program: str = Field(min_length=1, max_length=200)
    intake: str = Field(default="", max_length=40)
    deadline: datetime | None = None


class TaskCommand(BaseModel):
    action: Literal["start", "complete", "postpone", "cancel", "reset"]
    title: str | None = Field(default=None, max_length=300)
    stable_key: str | None = Field(default=None, max_length=160)
    due_at: datetime | None = None
    evidence: str = Field(default="", max_length=2000)
    request_id: str = Field(min_length=1, max_length=128)


class ApprovalDecision(BaseModel):
    expected_proposal_status: Literal["pending"] = "pending"


class ConflictDecision(BaseModel):
    choice: Literal["new", "old"]


class EventOut(BaseModel):
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
