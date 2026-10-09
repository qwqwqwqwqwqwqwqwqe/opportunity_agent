from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .matcher import match_job_to_user
from .models import Recommendation
from .prompts import SYSTEM_PROMPT
from .repository import LocalRepository
from .tools import OpportunityTools, register_rust_tools


class OpportunityAgent:
    """V1 event handler: deterministic decision, optional Rust ReAct narration."""

    def __init__(self, repository: LocalRepository | None = None, state_dir: Path | None = None) -> None:
        self.repository = repository or LocalRepository()
        self.tools = OpportunityTools(self.repository)
        self.state_dir = state_dir or Path(__file__).resolve().parent.parent / ".state"
        self._runtime_ready = False

    @property
    def llm_enabled(self) -> bool:
        return all(os.getenv(name, "").strip() for name in (
            "OPPORTUNITY_AGENT_API_KEY", "OPPORTUNITY_AGENT_API_BASE", "OPPORTUNITY_AGENT_MODEL"
        ))

    async def on_new_job(self, user_id: str, job_id: str) -> Recommendation:
        """Handle a NEW_JOB event for one user; no user prompt is required."""
        user = self.repository.get_user(user_id)
        job = self.repository.get_job(job_id)
        match = match_job_to_user(user, job)
        message = self._offline_message(user.career_goal, job.company, job.title, match)

        if self.llm_enabled:
            output = await self._run_react_agent(user_id, job_id)
            if output:
                message = output

        return Recommendation(
            user_id=user_id,
            job_id=job_id,
            score=match.score,
            matched_skills=match.matched_skills,
            missing_skills=match.missing_skills,
            reason=match.reason,
            should_notify=match.should_notify,
            message=message,
        )

    @staticmethod
    def _offline_message(career_goal: str, company: str, title: str, match) -> str:
        if not match.should_notify:
            return f"暂不通知：{company} - {title} 与当前“{career_goal}”目标的匹配度为 {match.score:.0%}。"
        skills = "、".join(match.matched_skills) or "当前技能"
        gaps = "、".join(match.missing_skills) or "暂无明显技能缺口"
        return (
            f"发现一个可能适合你的岗位：{company} - {title}。匹配度：{match.score:.0%}。"
            f"匹配原因：{skills}；待补足：{gaps}。{match.reason}"
        )

    async def _run_react_agent(self, user_id: str, job_id: str) -> str | None:
        """Use the Rust runtime only when an explicit model configuration exists."""
        try:
            if not self._runtime_ready:
                await self._initialize_runtime()
            from openjiuwenrust.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
            from openjiuwenrust.core.single_agent.schema.agent_card import AgentCard

            config = ReActAgentConfig()
            config.configure_model_client(
                provider="OpenAI",
                api_key=os.environ["OPPORTUNITY_AGENT_API_KEY"],
                api_base=os.environ["OPPORTUNITY_AGENT_API_BASE"],
                model_name=os.environ["OPPORTUNITY_AGENT_MODEL"],
                verify_ssl=True,
            )
            config.configure_max_iterations(6)
            config.configure_prompt_template([{"role": "system", "content": SYSTEM_PROMPT}])
            agent = ReActAgent(card=AgentCard(
                id=f"opportunity_agent_{user_id}", name="Opportunity Agent", description="Proactively assesses new jobs"
            ))
            agent.configure(config)
            for name in ("get_user_profile", "get_job", "search_jobs", "match_job_to_user"):
                # Tool cards are already in Rust's resource registry; attach by stable id.
                # `ability_manager.add` accepts the card object only, so the registered tools
                # are added during initialization below and cached on this instance.
                agent.ability_manager.add(self._tool_cards[name])
            result: dict[str, Any] = await agent.invoke({
                "query": f"NEW_JOB event: assess job_id={job_id} for user_id={user_id}.",
                "conversation_id": f"opportunity:{user_id}",
            })
            return str(result.get("output", "")).strip() or None
        except Exception as exc:  # Keep proactive offline delivery reliable if an optional provider fails.
            print(f"[optional LLM unavailable] {type(exc).__name__}: {exc}")
            return None

    async def _initialize_runtime(self) -> None:
        from openjiuwenrust.core.runner import CheckpointerConfig, Runner, RunnerConfig

        self.state_dir.mkdir(parents=True, exist_ok=True)
        Runner.set_config(RunnerConfig(
            distributed_mode=False,
            checkpointer_config=CheckpointerConfig(
                type="persistence",
                conf={"db_type": "sqlite", "db_path": str(self.state_dir / "opportunity_agent.db")},
            ),
        ))
        await Runner.start()
        self._tool_cards = register_rust_tools(self.tools)
        self._runtime_ready = True
