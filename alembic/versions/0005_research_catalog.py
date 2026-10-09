"""Public research catalogue and versioned field evidence (no user migration)."""
from alembic import op
import sqlalchemy as sa

revision = "0005_research_catalog"
down_revision = "0004_profile_context"
branch_labels = depends_on = None


def common():
    return [sa.Column("id", sa.String(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "research_programs" not in tables:
        op.create_table("research_programs", *common(),
            sa.Column("university", sa.String(200), nullable=False),
            sa.Column("program", sa.String(200), nullable=False),
            sa.Column("intake", sa.String(40), nullable=False),
            sa.Column("country", sa.String(80), nullable=False),
            sa.Column("aliases", sa.JSON(), nullable=False),
            sa.UniqueConstraint("university", "program", "intake", name="uq_research_program_identity"))
        for field in ("university", "program"):
            op.create_index(f"ix_research_programs_{field}", "research_programs", [field])
    if "research_requirements" not in tables:
        op.create_table("research_requirements", *common(),
            sa.Column("program_id", sa.String(), sa.ForeignKey("research_programs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_id", sa.String(), sa.ForeignKey("official_sources.id", ondelete="CASCADE"), nullable=False),
            sa.Column("field", sa.String(64), nullable=False), sa.Column("value", sa.JSON(), nullable=False),
            sa.Column("date_value", sa.Date()), sa.Column("qualifier", sa.String(64), nullable=False),
            sa.Column("excerpt", sa.Text(), nullable=False), sa.Column("content_hash", sa.String(128), nullable=False),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True)), sa.Column("status", sa.String(32), nullable=False),
            sa.Column("program_match", sa.String(32), nullable=False))
        for field in ("program_id", "source_id", "field", "date_value"):
            op.create_index(f"ix_research_requirements_{field}", "research_requirements", [field])


def downgrade():
    op.drop_table("research_requirements")
    op.drop_table("research_programs")
