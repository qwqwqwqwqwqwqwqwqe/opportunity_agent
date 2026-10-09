"""V2 foundation schema.

The initial migration intentionally reuses SQLAlchemy metadata: the same schema
is used by local SQLite smoke tests and PostgreSQL/pgvector Compose deployment.
Future revisions must use explicit Alembic operations rather than editing this
baseline.
"""
from __future__ import annotations

from alembic import op

from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db import models  # noqa: F401

revision = "0001_v2_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
