"""Persist conversation summaries and independently resolvable profile conflicts."""
from alembic import op
import sqlalchemy as sa

revision = "0004_profile_context"
down_revision = "0003_v2_plans_and_proposals"
branch_labels = None
depends_on = None


def common():
    return [sa.Column("id", sa.String(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "conversation_summaries" not in tables:
        op.create_table("conversation_summaries", *common(),
                        sa.Column("conversation_id", sa.String(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), unique=True, nullable=False),
                        sa.Column("summary", sa.Text(), nullable=False),
                        sa.Column("through_message_id", sa.String(32)),
                        sa.Column("through_created_at", sa.DateTime(timezone=True)),
                        sa.Column("mode", sa.String(32), nullable=False))
    if "profile_conflicts" not in tables:
        op.create_table("profile_conflicts", *common(),
                        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
                        sa.Column("run_id", sa.String(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
                        sa.Column("field", sa.String(120), nullable=False),
                        sa.Column("old_value", sa.JSON()), sa.Column("new_value", sa.JSON()),
                        sa.Column("old_source", sa.String(64)), sa.Column("new_source", sa.String(64), nullable=False),
                        sa.Column("new_evidence", sa.Text(), nullable=False), sa.Column("candidate", sa.JSON(), nullable=False),
                        sa.Column("expected_version", sa.Integer(), nullable=False), sa.Column("status", sa.String(32), nullable=False),
                        sa.UniqueConstraint("run_id", "field", name="uq_profile_conflict_run_field"))
        op.create_index("ix_profile_conflicts_user_id", "profile_conflicts", ["user_id"])
        op.create_index("ix_profile_conflicts_run_id", "profile_conflicts", ["run_id"])


def downgrade():
    op.drop_table("profile_conflicts")
    op.drop_table("conversation_summaries")
