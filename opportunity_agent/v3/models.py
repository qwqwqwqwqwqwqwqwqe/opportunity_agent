"""Workflow persistence. Models share V2's metadata and authentication identity."""
from __future__ import annotations

from datetime import date, datetime
from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from ..v2.db.base import Base, Timestamped, UUIDPrimaryKey


class Owned(UUIDPrimaryKey, Timestamped):
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)


class Versioned(Owned):
    version: Mapped[int] = mapped_column(Integer, default=1)


class Program(Versioned, Base):
    __tablename__ = "workflow_programs"
    university: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    degree: Mapped[str] = mapped_column(String(80), default="")
    home_unit: Mapped[str] = mapped_column(String(200), default="")
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Offering(Versioned, Base):
    __tablename__ = "workflow_offerings"
    program_id: Mapped[str] = mapped_column(ForeignKey("workflow_programs.id"))
    cycle: Mapped[str | None] = mapped_column(String(40))
    deadline_date: Mapped[date | None] = mapped_column(Date)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai")
    date_precision: Mapped[str] = mapped_column(String(16), default="unknown")
    gre_policy: Mapped[str] = mapped_column(String(32), default="unknown")
    requirements_json: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("user_id", "program_id", "cycle", name="uq_workflow_offering"),)


class Source(Owned, Base):
    __tablename__ = "workflow_sources"
    kind: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str | None] = mapped_column(String(160))
    content_hash: Mapped[str] = mapped_column(String(64))
    excerpt: Mapped[str] = mapped_column(Text)


class Observation(Owned, Base):
    __tablename__ = "workflow_field_observations"
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(64))
    field: Mapped[str] = mapped_column(String(80))
    value: Mapped[dict] = mapped_column(JSON)
    source_id: Mapped[str] = mapped_column(ForeignKey("workflow_sources.id"))
    verification_status: Mapped[str] = mapped_column(String(32), default="user_provided")
    decision_status: Mapped[str] = mapped_column(String(32), default="accepted")


class Requirement(Versioned, Base):
    __tablename__ = "workflow_requirements"
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"))
    key: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    kind: Mapped[str] = mapped_column(String(40), default="document")
    obligation: Mapped[str] = mapped_column(String(16), default="unknown")
    required_count: Mapped[int | None] = mapped_column(Integer)
    criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_sources.id"))
    __table_args__ = (UniqueConstraint("application_id", "key", name="uq_workflow_requirement"),
                      CheckConstraint("required_count IS NULL OR required_count > 0", name="ck_workflow_requirement_count"))


class Material(Versioned, Base):
    __tablename__ = "workflow_materials"
    requirement_id: Mapped[str] = mapped_column(ForeignKey("workflow_requirements.id"))
    slot_key: Mapped[str] = mapped_column(String(160))
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(24), default="missing")
    evidence_ref: Mapped[str | None] = mapped_column(ForeignKey("workflow_sources.id"))
    __table_args__ = (UniqueConstraint("requirement_id", "slot_key", name="uq_workflow_material"),)


class Task(Versioned, Base):
    __tablename__ = "workflow_tasks"
    application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id"))
    requirement_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_requirements.id"))
    scope_key: Mapped[str] = mapped_column(String(64), default="personal")
    stable_key: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(24), default="planned")
    start_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    date_certainty: Mapped[str] = mapped_column(String(16), default="unknown")
    duration_days: Mapped[int] = mapped_column(Integer, default=1)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    show_in_calendar: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    evidence: Mapped[str] = mapped_column(Text, default="")
    legacy_task_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    __table_args__ = (UniqueConstraint("user_id", "scope_key", "stable_key", name="uq_workflow_task"),
                      CheckConstraint("duration_days > 0", name="ck_workflow_task_duration"))


class Dependency(Owned, Base):
    __tablename__ = "workflow_task_dependencies"
    predecessor_id: Mapped[str] = mapped_column(ForeignKey("workflow_tasks.id"))
    successor_id: Mapped[str] = mapped_column(ForeignKey("workflow_tasks.id"))
    lag_days: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("predecessor_id", "successor_id", name="uq_workflow_dependency"),
                      CheckConstraint("predecessor_id != successor_id", name="ck_workflow_dependency_self"))


class CalendarEvent(Versioned, Base):
    __tablename__ = "workflow_calendar_events"
    type: Mapped[str] = mapped_column(String(32), default="appointment")
    title: Mapped[str] = mapped_column(String(300))
    date: Mapped[date | None] = mapped_column(Date)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai")
    date_precision: Mapped[str] = mapped_column(String(16), default="unknown")


class PlanItem(Owned, Base):
    __tablename__ = "workflow_plan_items"
    plan_id: Mapped[str] = mapped_column(ForeignKey("application_plans.id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_tasks.id"))
    proposal_key: Mapped[str] = mapped_column(String(200))
    task_spec: Mapped[dict] = mapped_column(JSON)
    schedule_snapshot: Mapped[dict] = mapped_column(JSON)
    input_task_version: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("plan_id", "proposal_key", name="uq_workflow_plan_item"),)


class WorkflowState(Owned, Base):
    __tablename__ = "workflow_state"
    planning_revision: Mapped[int] = mapped_column(Integer, default=0)
    next_plan_version: Mapped[int] = mapped_column(Integer, default=1)
    availability: Mapped[dict] = mapped_column(JSON, default=lambda: {"weekdays": [0, 1, 2, 3, 4], "timezone": "Asia/Shanghai"})
    __table_args__ = (UniqueConstraint("user_id", name="uq_workflow_state_user"),)


class CommandReceipt(Owned, Base):
    __tablename__ = "workflow_command_receipts"
    operation: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(160))
    payload_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("user_id", "operation", "request_id", name="uq_workflow_receipt"),)


class DomainEvent(Owned, Base):
    __tablename__ = "workflow_domain_events"
    aggregate_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    aggregate_version: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON)
    delivery_status: Mapped[str] = mapped_column(String(24), default="queued", index=True)


class Job(Owned, Base):
    __tablename__ = "workflow_jobs"
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    type: Mapped[str] = mapped_column(String(64))
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    input_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class Notification(Owned, Base):
    __tablename__ = "workflow_notifications"
    type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysis_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_monitor_analyses.id"))


# Mirrors the additive migration, and also protects create_all test databases.
from ..v2.db.models import ApplicationPlan  # noqa: E402
Index("uq_workflow_active_plan", ApplicationPlan.user_id, unique=True,
      postgresql_where=text("status = 'active' AND schema_version = 3"),
      sqlite_where=text("status = 'active' AND schema_version = 3"))

from . import monitor_models  # noqa: E402,F401 -- register with shared metadata
