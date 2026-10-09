"""Versioned preference authority, audit history and consolidation outbox."""
import json
from uuid import uuid4
from datetime import datetime, timezone
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from opportunity_agent.v2.db.models import MemoryAudit, MemoryOutbox

revision = "0007_preference_memory"
down_revision = "0006_run_reliability"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    MemoryAudit.__table__.create(bind, checkfirst=True)
    MemoryOutbox.__table__.create(bind, checkfirst=True)
    existing = {c["name"] for c in sa.inspect(bind).get_columns("memory_items")}
    additions = [sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("evidence", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_conversation_id", sa.String(64)), sa.Column("source_message_id", sa.String(64)),
        sa.Column("embedding", Vector(384).with_variant(sa.JSON(), "sqlite")),
        sa.Column("embedding_model", sa.String(256))]
    with op.batch_alter_table("memory_items") as batch:
        for column in additions:
            if column.name not in existing:
                batch.add_column(column)
    seen = set()
    rows = bind.execute(sa.text("SELECT * FROM memory_items ORDER BY updated_at DESC, created_at DESC, id DESC")).mappings().all()
    for row in rows:
        identity = (row["user_id"], row["memory_type"], row["key"])
        if identity not in seen:
            seen.add(identity)
            continue
        snapshot = {key: str(value) if isinstance(value, datetime) else value for key, value in row.items()}
        if isinstance(snapshot.get("value"), str):
            snapshot["value"] = json.loads(snapshot["value"])
        bind.execute(MemoryAudit.__table__.insert().values(id=uuid4().hex, user_id=row["user_id"],
            memory_id=row["id"], event_key="migration:"+row["id"], snapshot={"archived_duplicate": snapshot},
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)))
        bind.execute(sa.text("DELETE FROM memory_items WHERE id=:id"), {"id": row["id"]})
    if "uq_memory_preference" not in {c["name"] for c in sa.inspect(bind).get_unique_constraints("memory_items")}:
        with op.batch_alter_table("memory_items") as batch:
            batch.create_unique_constraint("uq_memory_preference", ["user_id", "memory_type", "key"])


def downgrade():
    # Preserve audit/outbox records rather than silently destroy preference history.
    with op.batch_alter_table("memory_items") as batch:
        batch.drop_constraint("uq_memory_preference", type_="unique")
        for name in ("embedding_model", "embedding", "source_message_id", "source_conversation_id", "evidence", "active", "version"):
            batch.drop_column(name)
