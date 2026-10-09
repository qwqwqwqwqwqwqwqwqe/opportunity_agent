from __future__ import annotations

from datetime import date
from typing import Any

from .career import recommend_jobs
from .lifecycle_agent import LifecycleAgent
from .models import ExternalEvent


class LifecycleTools:
    """JSON-only Tool facade for an openJiuwen ReAct/Workflow integration."""

    def __init__(self, agent: LifecycleAgent) -> None:
        self.agent = agent

    def process_user_message(self, message: str) -> dict[str, Any]:
        turn = self.agent.process_user_message(message)
        return {
            **turn.model_dump(mode="json"),
            "profile": self.agent.profile.model_dump(mode="json"),
            "state": self.agent.state.model_dump(mode="json"),
            "roadmap": self.agent.roadmap.model_dump(mode="json") if self.agent.roadmap else None,
            "job_recommendations": [item.model_dump(mode="json") for item in self.agent.job_recommendations],
            "planning_mode": self.agent.planner.last_mode,
            "extraction_result": self.agent.last_extraction.model_dump(mode="json"),
            "conflict_decisions": [item.model_dump(mode="json") for item in self.agent.last_conflict_decisions],
        }

    def get_user_state(self) -> dict[str, Any]:
        return self.agent.state.model_dump(mode="json")

    def generate_roadmap(self) -> dict[str, Any]:
        return self.agent.generate_roadmap().model_dump(mode="json")

    def recommend_jobs(self) -> list[dict[str, Any]]:
        self.agent.job_recommendations = recommend_jobs(self.agent.profile, self.agent.state, self.agent.repository)
        return [item.model_dump(mode="json") for item in self.agent.job_recommendations]

    def process_external_event(self, event: dict[str, Any], today: str) -> dict[str, Any]:
        decision = self.agent.on_external_event(ExternalEvent.model_validate(event), date.fromisoformat(today))
        return {"decision": decision.model_dump(mode="json"), "roadmap": self.agent.roadmap.model_dump(mode="json"),
                "job_recommendations": [item.model_dump(mode="json") for item in self.agent.job_recommendations]}

    def official_research_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Expose the same allow-listed official tools to optional openJiuwen runs."""
        model = getattr(self.agent.planner, "model_planner", None)
        tools = getattr(model, "official_tools", None)
        if tools is None:
            raise ValueError("official research tools are unavailable")
        return tools.call(name, arguments)


def register_rust_lifecycle_tools(tools: LifecycleTools) -> dict[str, Any]:
    """Optional binding adapter; only called in a configured openjiuwenrust runtime."""
    from openjiuwenrust.core.foundation.tool.base import ToolCard
    from openjiuwenrust.core.foundation.tool.function.function import LocalFunction
    from openjiuwenrust.core.runner import Runner

    definitions = [
        ("process_user_message", "Extract candidate facts, update profile/state, and return one next question.", tools.process_user_message, {"message": {"type": "string"}}, ["message"]),
        ("get_user_state", "Get the user's multi-axis academic, language, research and application state.", tools.get_user_state, {}, []),
        ("generate_roadmap", "Generate a structured goal, milestone and task roadmap.", tools.generate_roadmap, {}, []),
        ("process_external_event", "Process a source-attributed external deadline event and replan.", tools.process_external_event, {"event": {"type": "object"}, "today": {"type": "string"}}, ["event", "today"]),
        ("recommend_jobs", "Rank local jobs for a user in internship or full-time search and explain every recommendation.", tools.recommend_jobs, {}, []),
    ]
    # Keep OpenAI and openJiuwen tool contracts sourced from one schema list.
    from .official_research import OFFICIAL_RESEARCH_TOOLS
    for definition in OFFICIAL_RESEARCH_TOOLS:
        function = definition["function"]
        definitions.append((function["name"], function["description"],
                            lambda _name=function["name"], **kwargs: tools.official_research_tool(_name, kwargs),
                            function["parameters"].get("properties", {}), function["parameters"].get("required", [])))
    cards: dict[str, Any] = {}
    for name, description, func, properties, required in definitions:
        card = ToolCard(id=f"lifecycle_{name}", name=name, description=description,
                        input_params={"type": "object", "properties": properties, "required": required})
        Runner.resource_mgr.add_tool(LocalFunction(card=card, func=func), refresh=True)
        # The return value is the historical lifecycle facade contract. Official
        # cards are nevertheless registered in Runner.resource_mgr above.
        if not name.startswith(("resolve_official_", "search_official_", "read_official_", "get_cached_official_")):
            cards[name] = card
    return cards
