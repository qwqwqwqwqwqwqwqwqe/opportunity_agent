"""Durable request identity, execution leases and atomic event sequences."""
import json
from alembic import op
import sqlalchemy as sa

revision = "0006_run_reliability"
down_revision = "0005_research_catalog"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("agent_runs")}
    with op.batch_alter_table("agent_runs") as batch:
        for column in (
            sa.Column("request_id", sa.String(128), nullable=True),
            sa.Column("execution_token", sa.String(64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("event_sequence", sa.Integer(), server_default="0", nullable=False),
        ):
            if column.name not in existing:
                batch.add_column(column)
    # Retain historical duplicate runs. Only the first owns the idempotency key.
    seen = set()
    for row in bind.execute(sa.text("SELECT id, conversation_id, graph_state FROM agent_runs ORDER BY created_at, id")).mappings().all():
        state = row["graph_state"] or {}
        if isinstance(state, str):
            state = json.loads(state)
        request_id = state.get("request_id")
        key = (row["conversation_id"], request_id)
        if request_id and key not in seen:
            bind.execute(sa.text("UPDATE agent_runs SET request_id=:request WHERE id=:id"),
                         {"request": request_id, "id": row["id"]})
            seen.add(key)
    bind.execute(sa.text("UPDATE agent_runs SET event_sequence = COALESCE((SELECT MAX(sequence) FROM agent_events WHERE run_id=agent_runs.id), 0)"))
    constraints = {c["name"] for c in sa.inspect(bind).get_unique_constraints("agent_runs")}
    if "uq_run_request" not in constraints:
        with op.batch_alter_table("agent_runs") as batch:
            batch.create_unique_constraint("uq_run_request", ["conversation_id", "request_id"])
    if "messages" in sa.inspect(bind).get_table_names():
        with op.batch_alter_table("messages") as batch:
            batch.alter_column("request_id", existing_type=sa.String(64), type_=sa.String(128))


def downgrade():
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_constraint("uq_run_request", type_="unique")
        for name in ("event_sequence", "lease_expires_at", "execution_token", "request_id"):
            batch.drop_column(name)
