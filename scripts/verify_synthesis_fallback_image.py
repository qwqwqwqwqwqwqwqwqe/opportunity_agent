"""Run inside the deployed API image; inject timeouts in a separate process."""
import argparse
import asyncio
import json

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from opportunity_agent.config import synthesizer_config
from opportunity_agent.llm_client import LLMClient
from opportunity_agent.v2.agents.contracts import ExecutionState, ResearchResult
from opportunity_agent.v2.agents.orchestrator import CustomOrchestrator, HeuristicGoalParser, HeuristicRouter, LLMSynthesizer
from opportunity_agent.v2.core.config import settings
from opportunity_agent.v2.db.models import AgentRun


async def verify(source_run):
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            source = await session.get(AgentRun, source_run)
            if source is None or source.status != "completed":
                raise ValueError("Requires a completed run containing verified research")
            research = ResearchResult.model_validate(source.graph_state["research_result"])
        calls = []
        def timeout(payload):
            calls.append(1)
            raise TimeoutError("injected read timeout")
        class SnapshotAgent:
            async def execute(self, agent, state, missing_task=None):
                assert agent == "research"
                return research.model_copy(deep=True)
        state = ExecutionState(user_id="fault-acceptance", conversation_id="fault-acceptance", run_id="fault-acceptance",
            request_id="fault-acceptance", message="查询CMU MSAII截止日期")
        result = await CustomOrchestrator(goal_parser=HeuristicGoalParser(), router=HeuristicRouter(),
            agent_client=SnapshotAgent(), synthesizer=LLMSynthesizer(LLMClient(completion_fn=timeout, retries=1))).run(state)
        events = [e.type for e in result.events]
        deadlines = [str(p.deadline) for p in research.programs if p.deadline]
        assert len(calls) == 2
        assert result.completion.status == "PASS"
        assert "synthesizer_fallback" in events and "final_answer" in events
        assert any(d in result.answer for d in deadlines)
        assert any(str(e.url) in result.answer for p in research.programs for e in p.evidence)
        print(json.dumps({"passed": True, "source_run": source_run, "attempts": len(calls),
            "completion": result.completion.status, "events": events,
            "answer": result.answer, "config": synthesizer_config()}, ensure_ascii=True))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--source-run", required=True)
    asyncio.run(verify(parser.parse_args().source_run))
