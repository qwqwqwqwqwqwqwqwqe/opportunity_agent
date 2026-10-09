"""Safe defaults for isolated Research evaluation databases."""
from __future__ import annotations

import os

from sqlalchemy.engine import URL, make_url


DEFAULT_EVAL_DATABASE_NAME = "opportunity_research_eval_real150_v2"
EVAL_DATABASE_PREFIX = "opportunity_research_eval_"


def default_eval_database_url() -> str:
    """Return the local PostgreSQL eval DB URL, with env overrides for Docker/dev setups."""
    configured = os.getenv("RESEARCH_EVAL_DATABASE_URL")
    if configured:
        return configured
    return URL.create(
        "postgresql+asyncpg",
        username=os.getenv("RESEARCH_EVAL_PG_USER", "opportunity"),
        password=os.getenv("RESEARCH_EVAL_PG_PASSWORD", "opportunity_dev_only"),
        host=os.getenv("RESEARCH_EVAL_PG_HOST", "127.0.0.1"),
        port=int(os.getenv("RESEARCH_EVAL_PG_PORT", "5432")),
        database=DEFAULT_EVAL_DATABASE_NAME,
    ).render_as_string(hide_password=False)


def validate_eval_database_url(database_url: str, *, require_postgresql: bool = True) -> str:
    """Prevent real evaluation jobs from writing into the application's business DB."""
    url = make_url(database_url)
    backend = url.get_backend_name()
    if require_postgresql and backend != "postgresql":
        raise ValueError("Real Research evaluation requires PostgreSQL Full Text Search; SQLite is test-only")
    if backend == "postgresql" and not (url.database or "").startswith(EVAL_DATABASE_PREFIX):
        raise ValueError(
            f"Use a dedicated database named {EVAL_DATABASE_PREFIX}*; refusing to write to a business database"
        )
    return backend
