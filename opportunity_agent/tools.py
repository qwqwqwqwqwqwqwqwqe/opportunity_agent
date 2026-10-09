from __future__ import annotations

from typing import Any

from .matcher import match_job_to_user
from .repository import LocalRepository


class OpportunityTools:
    """Business tools exposed both to the offline workflow and Rust ReAct runtime."""

    def __init__(self, repository: LocalRepository) -> None:
        self.repository = repository

    def get_user_profile(self, user_id: str) -> dict[str, Any]:
        return self.repository.get_user(user_id).model_dump(mode="json")

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self.repository.get_job(job_id).model_dump(mode="json")

    def search_jobs(
        self,
        keyword: str | None = None,
        location: str | None = None,
        skills: list[str] | None = None,
        company: str | None = None,
    ) -> list[dict[str, Any]]:
        return [job.model_dump(mode="json") for job in self.repository.search_jobs(keyword, location, skills, company)]

    def match_job_to_user(self, user_id: str, job_id: str) -> dict[str, Any]:
        result = match_job_to_user(self.repository.get_user(user_id), self.repository.get_job(job_id))
        return result.model_dump(mode="json")


def register_rust_tools(tools: OpportunityTools) -> dict[str, Any]:
    """Register LocalFunction tools with openjiuwenrust's process-wide registry."""
    from openjiuwenrust.core.foundation.tool.base import ToolCard
    from openjiuwenrust.core.foundation.tool.function.function import LocalFunction
    from openjiuwenrust.core.runner import Runner

    definitions = [
        ("get_user_profile", "Get one user's persistent career profile.", tools.get_user_profile,
         {"user_id": {"type": "string"}}),
        ("get_job", "Get the complete details of a newly detected job.", tools.get_job,
         {"job_id": {"type": "string"}}),
        ("search_jobs", "Search local jobs by keyword, location, skills, or company.", tools.search_jobs,
         {"keyword": {"type": "string"}, "location": {"type": "string"}, "skills": {"type": "array", "items": {"type": "string"}}, "company": {"type": "string"}}),
        ("match_job_to_user", "Authoritatively score a job for a user and decide notification.", tools.match_job_to_user,
         {"user_id": {"type": "string"}, "job_id": {"type": "string"}}),
    ]
    cards: dict[str, Any] = {}
    for name, description, function, properties in definitions:
        card = ToolCard(
            id=f"opportunity_{name}",
            name=name,
            description=description,
            input_params={"type": "object", "properties": properties, "required": ["user_id"] if name == "get_user_profile" else (["job_id"] if name == "get_job" else (["user_id", "job_id"] if name == "match_job_to_user" else []))},
        )
        tool = LocalFunction(card=card, func=function)
        Runner.resource_mgr.add_tool(tool, refresh=True)
        cards[name] = card
    return cards
