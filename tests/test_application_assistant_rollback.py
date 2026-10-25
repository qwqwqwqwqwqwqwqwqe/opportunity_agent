"""Guard the selective rollback without dropping existing database history."""
from pathlib import Path


def test_legacy_chat_routes_without_workspace():
    from opportunity_agent.v2.api.app import app
    from opportunity_agent.v2.agents.contracts import ExecutionState
    from opportunity_agent.v2.agents.a2a import DomainA2ARequest
    # FastAPI may retain included routers lazily. OpenAPI exposes the actual
    # public routes across both flat and included routers.
    paths=set(app.openapi()['paths'])
    assert '/api/v1/applications' in paths
    assert '/api/v1/runs/{run_id}' in paths
    assert not any(p.startswith('/api/v1/'+prefix) for p in paths for prefix in
        ('project-library','project-imports','workspace','calendar-events','notifications','recommendations'))
    assert 'workspace_mode' not in ExecutionState.model_fields
    assert 'workspace_mode' not in DomainA2ARequest.model_fields


def test_keeps_research_fixes_and_existing_migration_head():
    from opportunity_agent.v2.db.base import Base
    from opportunity_agent.v2.db import models
    assert 'user_projects' not in Base.metadata.tables
    assert 'home_unit' in models.ResearchProgram.__table__.columns
    root=Path(__file__).resolve().parents[1]
    assert (root/'alembic/versions/0010_application_workspace.py').is_file()
    html=(root/'opportunity_agent/v2/web/index.html').read_text(encoding='utf-8')
    assert 'workspace.js' not in html and 'workspace-nav' not in html
    assert 'id="chat-form"' in html and 'id="auth-form"' in html
