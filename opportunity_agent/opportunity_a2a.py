"""Stateless Opportunity domain agent exposed through openJiuwen A2A."""
from __future__ import annotations

import asyncio
import json
import logging
import signal

from .a2a_protocol import OpportunityMutationRequest, OpportunityMutationResult
from .session_state import restore_agent, snapshot

logger = logging.getLogger(__name__)


def handle_mutation(request: OpportunityMutationRequest) -> OpportunityMutationResult:
    """Validate and apply one candidate mutation without touching durable storage."""
    agent = restore_agent(request.conversation_id, request.state_snapshot)
    event = next((item for item in agent.user_events if item.event_id == request.event_id), None)
    if event is None:
        raise ValueError("source event is absent from state snapshot")
    if event.request_id != request.request_id or event.raw_text != request.delegation.raw_message:
        raise ValueError("source event does not match A2A request")

    extraction = request.delegation.to_extraction_result()
    turn = agent.apply_structured_update(
        request.delegation.raw_message,
        extraction,
        request.event_id,
        request.selected_target_id,
    )
    accepted = [
        fact for fact, decision in zip(agent.last_extraction.facts, agent.last_conflict_decisions)
        if decision.status == "applied" and fact.confidence >= 0.75 and not fact.needs_confirmation
    ]
    ignored = [
        {"fact": fact.model_dump(mode="json"), "decision": decision.model_dump(mode="json")}
        for fact, decision in zip(agent.last_extraction.facts, agent.last_conflict_decisions)
        if decision.status != "applied"
    ]
    return OpportunityMutationResult(
        request_id=request.request_id,
        base_revision=request.base_revision,
        accepted_facts=accepted,
        ignored_facts=ignored,
        progress_updates=[item.model_dump(mode="json") for item in turn.progress_updates],
        state_changes=[item.model_dump(mode="json") for item in turn.state_changes],
        pending_confirmations=[item.model_dump(mode="json") for item in turn.pending_confirmations],
        updated_state_snapshot=snapshot(agent),
        replan_required=turn.replan_required,
        reply_summary=turn.reply,
    )


async def opportunity_invoke_handler(inputs: dict):
    from openjiuwenrust.core.controller.schema.task import TaskStatus
    from openjiuwenrust.core.single_agent.schema.agent_result import AgentResult, Artifact, Part

    try:
        request = OpportunityMutationRequest.model_validate_json(inputs.get("query", ""))
        result = handle_mutation(request)
        return AgentResult(
            status=TaskStatus.COMPLETED,
            artifacts=[Artifact(
                artifactId="opportunity_mutation_result",
                parts=[Part(text=result.model_dump_json())],
            )],
        )
    except Exception as exc:
        logger.warning("Opportunity A2A request rejected: %s", exc)
        return AgentResult(
            status=TaskStatus.FAILED,
            artifacts=[Artifact(
                artifactId="opportunity_mutation_error",
                parts=[Part(text=json.dumps({
                    "error": f"{type(exc).__name__}: {exc}",
                    "protocol_version": "1",
                }, ensure_ascii=False))],
            )],
        )


def create_opportunity_a2a_server(host: str | None = None, port: int | None = None):
    from openjiuwenrust.core.single_agent.schema.agent_card import AgentCard
    from openjiuwenrust.extensions.a2a.a2a_server import A2AServer
    from .config import opportunity_a2a_host, opportunity_a2a_port

    host = host or opportunity_a2a_host()
    port = port or opportunity_a2a_port()
    card = AgentCard(
        id="opportunity_profile_timeline_agent",
        name="Opportunity Profile & Timeline Agent",
        description=(
            "Validates structured user facts and progress, resolves profile conflicts, "
            "and updates the study-abroad timeline. It does not answer general questions."
        ),
    )
    return A2AServer(
        agent_card=card,
        adapter_id="opportunity-profile-a2a",
        invoke_handler=opportunity_invoke_handler,
        interface_url=f"http://127.0.0.1:{port}/a2a/jsonrpc/",
        protocol_binding="JSONRPC",
        rpc_url="/a2a/jsonrpc/",
    )


async def run_server() -> None:
    from .config import opportunity_a2a_host, opportunity_a2a_port

    host, port = opportunity_a2a_host(), opportunity_a2a_port()
    server = create_opportunity_a2a_server(host, port)
    await server.start(host=host, port=port)
    print(f"Opportunity A2A Agent: http://127.0.0.1:{port}/a2a/jsonrpc/")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop_event.wait()
    finally:
        await server.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
