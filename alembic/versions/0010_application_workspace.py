"""Private project library, durable imports and application calendar.

Revision ID: 0010_application_workspace
Revises: 0009_research_scope_identity
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = '0010_application_workspace'
down_revision = '0009_research_scope_identity'
branch_labels = None
depends_on = None


def col(name, typ, *, fk=None, nullable=False, unique=False):
    args = [sa.ForeignKey(fk)] if fk else []
    return sa.Column(name, typ, *args, nullable=nullable, unique=unique)


def table(name, *columns, timestamps=True):
    if sa.inspect(op.get_bind()).has_table(name):
        return  # 0001 legacy baseline may already have created current metadata.
    common = [sa.Column('id', sa.String(), primary_key=True)]
    if timestamps:
        common += [col('created_at', sa.DateTime(timezone=True)), col('updated_at', sa.DateTime(timezone=True))]
    op.create_table(name, *common, *columns)


def upgrade():
    J, I, B, T, D = sa.JSON(), sa.Integer(), sa.Boolean(), sa.Text(), sa.DateTime(timezone=True)
    U = lambda: col('user_id',sa.String(),fk='users.id')
    V = lambda: col('version',I)
    P = lambda: col('project_id',sa.String(),fk='user_projects.id')
    table('user_projects',U(),V(),col('published_version',I),col('archived',B),col('draft',J),
        col('published',J),col('confirmations',J),col('index_status',sa.String(32)))
    table('project_sources',P(),V(),col('kind',sa.String(32)),col('url',sa.String(2048)),col('title',sa.String(500)),
        col('content',T),col('content_hash',sa.String(64)),col('status',sa.String(32)),col('method',sa.String(32)),col('error_code',sa.String(100)))
    table('project_import_jobs',U(),P(),V(),col('status',sa.String(32)),col('attempts',I),col('model_calls',I),
        col('lease_until',D,nullable=True),col('error_code',sa.String(100)))
    table('user_project_chunks',U(),P(),V(),col('source_id',sa.String(),fk='project_sources.id',nullable=True),
        col('content',T),col('embedding',Vector(384).with_variant(sa.JSON(),'sqlite'),nullable=True),timestamps=False)
    table('project_tracking',U(),P(),col('application_id',sa.String(),fk='applications.id',unique=True),V(),
        col('project_version',I),col('snapshot',J),col('checklist_draft',J),sa.UniqueConstraint('user_id','project_id'))
    table('workspace_task_details',col('task_id',sa.String(),fk='application_tasks.id',unique=True),V(),col('payload',J),timestamps=False)
    table('calendar_events',U(),col('application_id',sa.String(),fk='applications.id',nullable=True),
        col('task_id',sa.String(),fk='application_tasks.id',nullable=True),col('origin_key',sa.String(200),nullable=True),
        V(),col('payload',J),col('cancelled',B),sa.UniqueConstraint('user_id','origin_key'))
    table('event_reminders',U(),col('event_id',sa.String(),fk='calendar_events.id'),col('event_version',I),
        col('due_at',D),col('email',B),col('status',sa.String(32)),col('attempts',I),col('lease_until',D,nullable=True))
    table('workspace_notifications',U(),col('reminder_id',sa.String(),fk='event_reminders.id',unique=True),
        col('title',sa.String(300)),col('is_read',B),col('delayed',B),col('email_status',sa.String(32)))
    table('workspace_receipts',U(),col('operation',sa.String(200)),col('request_id',sa.String(128)),
        col('fingerprint',sa.String(64)),col('response',J),sa.UniqueConstraint('user_id','operation','request_id'),timestamps=False)
    for name in ['user_projects','project_import_jobs','user_project_chunks','project_tracking','calendar_events','event_reminders','workspace_notifications']:
        index('ix_'+name+'_user_id',name,['user_id'])
    index('ix_event_reminders_due_at','event_reminders',['due_at'])
    index('ix_user_project_chunks_project_id','user_project_chunks',['project_id'])


def index(name,table_name,columns):
    if name not in {i['name'] for i in sa.inspect(op.get_bind()).get_indexes(table_name)}:
        op.create_index(name,table_name,columns)


def downgrade():
    for name in ['workspace_receipts','workspace_notifications','event_reminders','calendar_events',
                 'workspace_task_details','project_tracking','user_project_chunks','project_import_jobs','project_sources','user_projects']:
        op.drop_table(name)
