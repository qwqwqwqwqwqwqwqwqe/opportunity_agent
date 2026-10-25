"""Run with uvicorn opportunity_agent.v3.app:app. Shares V2 auth/SSE."""
import os

os.environ.setdefault("APP_MODE", "applyops")

from ..v2.api.app import app  # noqa: E402

app.title = "ApplyOps V3.2"
app.version = "3.2.0"
