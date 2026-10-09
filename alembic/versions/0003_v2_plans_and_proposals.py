"""Versioned plans, plan tasks, and reviewable proposal reasons."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_v2_plans_and_proposals"
down_revision = "0002_conversation_legacy_session_id"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _uniques(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_unique_constraints(table)}


def _has_foreign_key(table: str, column: str) -> bool:
    return any(item["constrained_columns"] == [column]
               for item in sa.inspect(op.get_bind()).get_foreign_keys(table))


def upgrade() -> None:
    if "status" not in _columns("application_plans"):
        op.add_column("application_plans", sa.Column("status", sa.String(32), nullable=False, server_default="active"))
    if "revision_reason" not in _columns("application_plans"):
        op.add_column("application_plans", sa.Column("revision_reason", sa.String(160), nullable=False, server_default="initial_profile"))
    if "source_run_id" not in _columns("application_plans"):
        op.add_column("application_plans", sa.Column("source_run_id", sa.String(32), nullable=True))
    if not _has_foreign_key("application_plans", "source_run_id"):
        with op.batch_alter_table("application_plans") as batch:
            batch.create_foreign_key("fk_application_plans_source_run_id", "agent_runs", ["source_run_id"], ["id"], ondelete="SET NULL")
    # Older deployments may have several plans with the default version=1.
    # Preserve their order before adding the per-user unique constraint.
    connection = op.get_bind()
    rows = connection.execute(sa.text(
        "SELECT id, user_id FROM application_plans ORDER BY user_id, created_at, id"
    )).fetchall()
    groups: dict[str, list[str]] = {}
    for plan_id, user_id in rows:
        groups.setdefault(user_id, []).append(plan_id)
    for ids in groups.values():
        for version, plan_id in enumerate(ids, 1):
            connection.execute(sa.text(
                "UPDATE application_plans SET version=:version, status=:status WHERE id=:id"
            ), {"version": version, "status": "active" if version == len(ids) else "superseded", "id": plan_id})
    if "uq_application_plan_user_version" not in _uniques("application_plans"):
        with op.batch_alter_table("application_plans") as batch:
            batch.create_unique_constraint("uq_application_plan_user_version", ["user_id", "version"])

    if "plan_id" not in _columns("application_tasks"):
        op.add_column("application_tasks", sa.Column("plan_id", sa.String(32), nullable=True))
    if not _has_foreign_key("application_tasks", "plan_id"):
        with op.batch_alter_table("application_tasks") as batch:
            batch.create_foreign_key("fk_application_tasks_plan_id", "application_plans", ["plan_id"], ["id"], ondelete="CASCADE")
    if "category" not in _columns("application_tasks"):
        op.add_column("application_tasks", sa.Column("category", sa.String(32), nullable=False, server_default="application"))
    if not sa.inspect(op.get_bind()).get_columns("application_tasks"):
        raise RuntimeError("application_tasks table is missing")
    application_id = next(item for item in sa.inspect(op.get_bind()).get_columns("application_tasks")
                          if item["name"] == "application_id")
    if not application_id["nullable"]:
        with op.batch_alter_table("application_tasks") as batch:
            batch.alter_column("application_id", existing_type=sa.String(32), nullable=True)
    if "uq_plan_task_stable_key" not in _uniques("application_tasks"):
        with op.batch_alter_table("application_tasks") as batch:
            batch.create_unique_constraint("uq_plan_task_stable_key", ["plan_id", "stable_key"])
    if "ix_application_tasks_plan_id" not in {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("application_tasks")}:
        op.create_index("ix_application_tasks_plan_id", "application_tasks", ["plan_id"])

    if "reason" not in _columns("change_proposals"):
        op.add_column("change_proposals", sa.Column("reason", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    # Keep versioned user plans recoverable. Reversing this migration requires
    # an explicit data export rather than silently dropping plan ownership.
    raise RuntimeError("export versioned plans before downgrading this migration")
