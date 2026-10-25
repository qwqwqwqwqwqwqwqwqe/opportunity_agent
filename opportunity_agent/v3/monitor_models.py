"""Monitoring records are tenant-owned and never mutate application facts."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from ..v2.db.base import Base
from .models import Owned, Versioned


class Monitor(Versioned, Base):
    __tablename__ = "workflow_monitor_configs"
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str] = mapped_column(String(12), default="manual")
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai")
    daily_time: Mapped[str] = mapped_column(String(5), default="09:00")
    next_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    active_check_id: Mapped[str | None] = mapped_column(String(64))
    last_manual_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), default="uninitialized")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MonitorSource(Owned, Base):
    __tablename__ = "workflow_monitor_sources"
    monitor_id: Mapped[str] = mapped_column(ForeignKey("workflow_monitor_configs.id"), index=True)
    roles: Mapped[list] = mapped_column(JSON)
    url: Mapped[str] = mapped_column(String(2000))
    host: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    latest_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    etag: Mapped[str | None] = mapped_column(String(500))
    modified: Mapped[str | None] = mapped_column(String(500))
    last_fetch_status: Mapped[str] = mapped_column(String(30), default="uninitialized")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("monitor_id", "url", name="uq_monitor_source_url"),)


class MonitorSnapshot(Owned, Base):
    __tablename__ = "workflow_monitor_snapshots"
    monitor_id: Mapped[str] = mapped_column(ForeignKey("workflow_monitor_configs.id"), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_monitor_sources.id"))
    url: Mapped[str] = mapped_column(String(2000))
    html: Mapped[str] = mapped_column(Text)
    blocks: Mapped[list] = mapped_column(JSON)
    links: Mapped[list] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(30))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MonitorCheck(Owned, Base):
    __tablename__ = "workflow_monitor_checks"
    monitor_id: Mapped[str] = mapped_column(ForeignKey("workflow_monitor_configs.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(160))
    trigger: Mapped[str] = mapped_column(String(20))
    monitor_version: Mapped[int] = mapped_column(Integer)
    scope_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(40), default="queued")
    coverage: Mapped[list] = mapped_column(JSON, default=list)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(200))
    __table_args__ = (UniqueConstraint("monitor_id", "request_id", name="uq_monitor_check_request"),)


class MonitorChange(Owned, Base):
    __tablename__ = "workflow_monitor_changes"
    check_id: Mapped[str] = mapped_column(ForeignKey("workflow_monitor_checks.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("workflow_monitor_sources.id"))
    before_id: Mapped[str] = mapped_column(ForeignKey("workflow_monitor_snapshots.id"))
    after_id: Mapped[str] = mapped_column(ForeignKey("workflow_monitor_snapshots.id"))
    diff: Mapped[list] = mapped_column(JSON)


class MonitorAnalysis(Owned, Base):
    __tablename__ = "workflow_monitor_analyses"
    check_id: Mapped[str] = mapped_column(ForeignKey("workflow_monitor_checks.id"), index=True)
    monitor_id: Mapped[str] = mapped_column(ForeignKey("workflow_monitor_configs.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="queued")
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    input_refs: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    trace: Mapped[list] = mapped_column(JSON, default=list)
    snapshot_ids: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(String(200))
