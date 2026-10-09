"""A real stdio MCP surface, with a framework-neutral ToolService behind it."""
from __future__ import annotations

from ..db.session import SessionLocal
from .tools import V2ToolService


def create_mcp_server():
    from mcp.server.fastmcp import FastMCP

    app = FastMCP("opportunity-agent-v2")

    @app.tool()
    async def search_official_requirements(query: str, school: str = "", program: str = "") -> dict:
        """Search cited, official application requirements only."""
        async with SessionLocal() as session:
            return await V2ToolService(session).search_official_requirements(query, school, program)

    @app.tool()
    async def get_official_source(source_id: str) -> dict | None:
        """Read a single stored official source by its stable ID."""
        async with SessionLocal() as session:
            return await V2ToolService(session).get_official_source(source_id)

    @app.tool()
    async def list_application_tasks(user_id: str) -> list[dict]:
        """List a user's application tasks; callers must enforce user identity."""
        async with SessionLocal() as session:
            return await V2ToolService(session).list_application_tasks(user_id)

    # write tools intentionally return an approval token; direct state mutation is unavailable.
    @app.tool()
    async def propose_application_change(user_id: str, proposal_type: str, payload: dict, request_id: str) -> dict:
        """Create a user-visible change proposal; it does not write application state."""
        async with SessionLocal.begin() as session:
            return await V2ToolService(session).propose_application_change(user_id, proposal_type, payload, request_id)

    return app


if __name__ == "__main__":
    create_mcp_server().run()
