from datetime import datetime
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from ..db.base import Base, UUIDPrimaryKey, Timestamped


class Project(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = 'user_projects'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    published_version: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    draft: Mapped[dict] = mapped_column(JSON, default=dict)
    published: Mapped[dict] = mapped_column(JSON, default=dict)
    confirmations: Mapped[list] = mapped_column(JSON, default=list)
    index_status: Mapped[str] = mapped_column(String(32), default='pending')


class Source(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = 'project_sources'
    project_id: Mapped[str] = mapped_column(ForeignKey('user_projects.id', ondelete='CASCADE'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32), default='overview')
    url: Mapped[str] = mapped_column(String(2048), default='')
    title: Mapped[str] = mapped_column(String(500), default='')
    content: Mapped[str] = mapped_column(Text, default='')
    content_hash: Mapped[str] = mapped_column(String(64), default='')
    status: Mapped[str] = mapped_column(String(32), default='pending')
    method: Mapped[str] = mapped_column(String(32), default='')
    error_code: Mapped[str] = mapped_column(String(100), default='')


class ImportJob(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = 'project_import_jobs'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey('user_projects.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default='queued')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str] = mapped_column(String(100), default='')


class Chunk(UUIDPrimaryKey, Base):
    __tablename__ = 'user_project_chunks'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey('user_projects.id', ondelete='CASCADE'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[str | None] = mapped_column(ForeignKey('project_sources.id'), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(Vector(384).with_variant(JSON, 'sqlite'), nullable=True)


class Tracking(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = 'project_tracking'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey('user_projects.id'), index=True)
    application_id: Mapped[str] = mapped_column(ForeignKey('applications.id'), unique=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    project_version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    checklist_draft: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = (UniqueConstraint('user_id', 'project_id'),)


class TaskDetail(UUIDPrimaryKey, Base):
    __tablename__ = 'workspace_task_details'
    task_id: Mapped[str] = mapped_column(ForeignKey('application_tasks.id', ondelete='CASCADE'), unique=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class CalendarEvent(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = 'calendar_events'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    application_id: Mapped[str | None] = mapped_column(ForeignKey('applications.id'), nullable=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey('application_tasks.id'), nullable=True)
    origin_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint('user_id', 'origin_key'),)


class Reminder(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = 'event_reminders'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey('calendar_events.id'), index=True)
    event_version: Mapped[int] = mapped_column(Integer)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    email: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Notification(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = 'workspace_notifications'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    reminder_id: Mapped[str] = mapped_column(ForeignKey('event_reminders.id'), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    delayed: Mapped[bool] = mapped_column(Boolean, default=False)
    email_status: Mapped[str] = mapped_column(String(32), default='disabled')


class Receipt(UUIDPrimaryKey, Base):
    __tablename__ = 'workspace_receipts'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    operation: Mapped[str] = mapped_column(String(200))
    request_id: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint('user_id', 'operation', 'request_id'),)
