"""Isolate programme degree/home-unit scopes without inferring legacy identities."""
from alembic import op
import sqlalchemy as sa

revision = '0009_research_scope_identity'
down_revision = '0008_extraction_provenance'
branch_labels = None
depends_on = None


def upgrade():
    # The legacy 0001 baseline creates current metadata on a fresh installation.
    # Do not add the same columns twice when upgrading a brand-new database.
    inspector=sa.inspect(op.get_bind())
    columns={c['name'] for c in inspector.get_columns('research_programs')}
    identity_columns=['university','program','intake','degree','degree_award','home_unit']
    constraints={c['name']:c['column_names'] for c in inspector.get_unique_constraints('research_programs')}
    if {'degree','degree_award','home_unit','program_subject'}<=columns and constraints.get('uq_research_program_identity')==identity_columns:
        return
    with op.batch_alter_table('research_programs') as batch:
        for name,length in [('degree',32),('degree_award',32),('home_unit',200),('program_subject',200)]:
            if name not in columns:
                batch.add_column(sa.Column(name,sa.String(length),nullable=False,server_default=''))
        if 'uq_research_program_identity' in constraints:
            batch.drop_constraint('uq_research_program_identity', type_='unique')
        batch.create_unique_constraint('uq_research_program_identity',
            ['university', 'program', 'intake', 'degree', 'degree_award', 'home_unit'])


def downgrade():
    # Never merge/delete scoped rows silently to satisfy the legacy uniqueness.
    duplicate = op.get_bind().execute(sa.text('SELECT 1 FROM research_programs '
        'GROUP BY university, program, intake HAVING COUNT(*) > 1 LIMIT 1')).first()
    if duplicate:
        raise RuntimeError('Scoped rows collide with legacy identity; downgrade requires explicit data reconciliation')
    with op.batch_alter_table('research_programs') as batch:
        batch.drop_constraint('uq_research_program_identity', type_='unique')
        batch.drop_column('program_subject')
        batch.drop_column('home_unit')
        batch.drop_column('degree')
        batch.drop_column('degree_award')
        batch.create_unique_constraint('uq_research_program_identity', ['university','program','intake'])
