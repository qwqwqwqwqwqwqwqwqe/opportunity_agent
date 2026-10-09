"""Opt-in live model/A2A smoke test. Sends the documented test text only.

Run inside the API container through stdin; no business database writes.
Requires explicit permission to send these examples to the configured model.
"""
import asyncio
import json

from opportunity_agent.v2.agents.a2a import PythonOpenJiuwenDomainAgents
from opportunity_agent.v2.agents.contracts import ExecutionState
from opportunity_agent.v2.agents.orchestrator import LLMSynthesizer
from opportunity_agent.llm_context import conversation_scope


async def main():
    client = PythonOpenJiuwenDomainAgents()
    try:
        state = ExecutionState(
            user_id="smoke-no-write", conversation_id="smoke", run_id="smoke", request_id="experience",
            message="我现在有华为实习，一段西湖科研，一段本校科研发了IEEE TNSE，一个agent项目",
            profile_payload={"target_programs": ["MSCS", "MCS", "CSE"]},
        )
        result = await client.execute("profile", state)
        assert result.projected_profile["target_programs"] == ["MSCS", "MCS", "CSE"]
        for field in ("internship_experiences", "research_experiences", "paper_experiences", "project_experiences"):
            assert result.projected_profile[field], field
        assert result.extraction_mode == "hybrid", result.errors
        print(json.dumps({"case": "experiences", "mode": result.extraction_mode,
                          "fields": [f["field"] for f in result.accepted_facts], "errors": result.errors}), flush=True)
        state.request_id = "context-score"
        state.message = "我刚考到110。"
        state.profile_payload = {"toefl_score": 107}
        state.conversation_context = {"recent_messages": [
            {"role": "user", "content": "我的托福之前是107。"},
            {"role": "assistant", "content": "你的托福最近出分了吗？"},
        ]}
        result = await client.execute("profile", state)
        assert any(c["field"] == "toefl_score" and c["old_value"] == 107 and c["new_value"] == 110
                   for c in result.conflicts), result.model_dump()
        assert result.projected_profile["toefl_score"] == 107
        print(json.dumps({"case": "context-score", "mode": result.extraction_mode,
                          "conflicts": len(result.conflicts), "unchanged_until_confirmation": True}), flush=True)
        state.message = "我之前托福多少分？"
        with conversation_scope(state.conversation_context):
            answer = await LLMSynthesizer().synthesize(state)
        assert "107" in answer, answer
        print(json.dumps({"case": "synthesizer-memory", "recalled_previous_score": True}), flush=True)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
