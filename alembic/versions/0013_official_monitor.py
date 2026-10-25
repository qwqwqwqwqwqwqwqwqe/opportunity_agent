"""Add opt-in official monitoring and impact inbox; preserve all history."""
from alembic import op
import sqlalchemy as sa

revision = "0013_official_monitor"
down_revision = "0012_workspace_experience"
branch_labels = depends_on = None


def upgrade():
    from opportunity_agent.v3 import models  # noqa: F401
    from opportunity_agent.v2.db.base import Base
    connection = op.get_bind()
    tables = [t for t in Base.metadata.sorted_tables if t.name.startswith("workflow_monitor_")]
    Base.metadata.create_all(connection, tables=tables, checkfirst=True)
    if "analysis_id" not in {c["name"] for c in sa.inspect(connection).get_columns("workflow_notifications")}:
        # SQLite cannot add an FK in ALTER TABLE. Tenant checks remain enforced
        # by the service; PostgreSQL and create_all databases also have the FK.
        column = sa.Column("analysis_id", sa.String(64), nullable=True)
        if connection.dialect.name != "sqlite":
            column = sa.Column("analysis_id", sa.String(64), sa.ForeignKey("workflow_monitor_analyses.id"), nullable=True)
        op.add_column("workflow_notifications", column)


def downgrade():
    raise RuntimeError("Monitoring evidence and inbox history must not be dropped; restore an explicit backup")
