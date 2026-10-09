"""Key V1 conversation import on the legacy session id.

The V1 migration used ``(user_id, title)`` as its idempotency key, but V1 leaves
most sessions titled "新对话", so every collision after the first was silently
dropped. This revision adds the column that makes re-running the import safe.

The 0001 baseline calls ``Base.metadata.create_all``, so on a fresh database the
column and constraint already exist by the time this revision runs (the model
declares them). Each step is therefore applied only when missing.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_conversation_legacy_session_id"
down_revision = "0001_v2_foundation"
branch_labels = None
depends_on = None

_TABLE = "conversations"
_COLUMN = "legacy_session_id"
_CONSTRAINT = "uq_conversation_legacy_session"
_INDEX = "ix_conversations_legacy_session_id"


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    inspector = _inspector()

    if _COLUMN not in {column["name"] for column in inspector.get_columns(_TABLE)}:
        # batch_alter_table keeps this working on SQLite, whose ALTER TABLE
        # cannot add a constrained column in place.
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(sa.Column(_COLUMN, sa.String(128), nullable=True))

    inspector = _inspector()
    if _INDEX not in {item["name"] for item in inspector.get_indexes(_TABLE)}:
        op.create_index(_INDEX, _TABLE, [_COLUMN])

    if _CONSTRAINT not in {item["name"] for item in inspector.get_unique_constraints(_TABLE)}:
        with op.batch_alter_table(_TABLE) as batch:
            batch.create_unique_constraint(_CONSTRAINT, ["user_id", _COLUMN])


def downgrade() -> None:
    inspector = _inspector()

    if _CONSTRAINT in {item["name"] for item in inspector.get_unique_constraints(_TABLE)}:
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_constraint(_CONSTRAINT, type_="unique")

    inspector = _inspector()
    if _INDEX in {item["name"] for item in inspector.get_indexes(_TABLE)}:
        op.drop_index(_INDEX, table_name=_TABLE)

    if _COLUMN in {column["name"] for column in inspector.get_columns(_TABLE)}:
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column(_COLUMN)
