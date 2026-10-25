"""Project preparation cards and opt-in task calendar projection."""
from alembic import op
import sqlalchemy as sa

revision = "0012_workspace_experience"
down_revision = "0011_applyops_workflow"
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    additions = [
        ("workflow_requirements", sa.Column("title", sa.String(200), nullable=False, server_default="")),
        ("workflow_requirements", sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false())),
        ("workflow_materials", sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false())),
        ("workflow_tasks", sa.Column("show_in_calendar", sa.Boolean(), nullable=False, server_default=sa.false())),
    ]
    for table, column in additions:
        columns = {c["name"] for c in sa.inspect(connection).get_columns(table)}
        if column.name not in columns:
            op.add_column(table, column)
            if table == "workflow_tasks":
                connection.execute(sa.text("UPDATE workflow_tasks SET show_in_calendar = true "
                    "WHERE status IN ('planned', 'in_progress') AND (due_date IS NOT NULL OR start_date IS NOT NULL)"))
    connection.execute(sa.text("UPDATE workflow_requirements SET title = key WHERE title = ''"))


def downgrade():
    raise RuntimeError("Keep user workflow history; restore an explicit backup for a downgrade")
