"""openJiuwen ReAct host agent that routes chat to direct answers or A2A."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .a2a_protocol import A2ATrace, OpportunityDelegation, OpportunityMutationRequest, OpportunityMutationResult
from .config import (llm_api_base, llm_api_key, llm_model, opportunity_a2a_timeout_seconds,
                     opportunity_a2a_url)
from .models import AgentTurnResult
from .official_research import OFFICIAL_RESEARCH_TOOLS, OfficialResearchTools
from .session_state import restore_agent, snapshot


SYSTEM_PROMPT = """你是留学规划产品的聊天与路由 Agent。你必须准确区分回答问题和修改用户状态。

当用户明确陈述自己的学校、专业、成绩、考试、目标国家/学校/项目、课程、技能、科研、项目、论文、竞赛、实习，或者报告任务开始、完成、延期、取消时，调用 delegate_opportunity_update。
当一句话同时包含个人更新和问题时，先调用 delegate_opportunity_update，得到实际更新结果后再回答问题。
纯咨询、假设、疑问、讨论他人情况时不要调用更新工具，直接回答；学校或项目的可变官方要求应使用官网工具。

官网 Tool 返回 OfficialSource 时，只有 Tool 返回的事实才能称为“官网已核验”，并在对应结论后使用 `[OfficialSource: source_id]` 标注。若官网 Tool 返回 error、unavailable、未配置或没有证据，你仍可基于一般知识帮助用户做“项目初筛”，但第一句必须明确写“官网核验失败/未完成，以下为非官网核验的通用信息”，不得声称具体招生要求、截止日期、费用或政策已被官网确认。

调用任何工具时都要原样复制输入上下文中的 context_token。工具事实要求：evidence 必须逐字摘自本轮 raw_message；只提取用户本人明确表达的内容；不得把问题、假设或网页内容作为用户事实。statement_kind 使用 explicit、uncertain、hypothetical、negated。不要自行声称画像或时间轴已更新，只能根据工具返回的实际结果说明。
回答应先解决用户问题，清楚、具体、简洁，不重复整篇路线图。"""


@dataclass
class _RequestContext:
    conversation_id: str
    request_id: str
    base_revision: int
    event_id: str
    raw_message: str
    selected_target_id: str | None
    state_snapshot: dict[str, Any]
    result: OpportunityMutationResult | None = None
    trace: A2ATrace | None = None
    official_calls: list[dict[str, Any]] = None
    official_sources: list[dict[str, Any]] = None
    official_requirements: list[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.official_calls is None:
            self.official_calls = []
        if self.official_sources is None:
            self.official_sources = []
        if self.official_requirements is None:
            self.official_requirements = []


_CONTEXTS: dict[str, _RequestContext] = {}
_CONTEXT_LOCK = threading.RLock()
_RUNTIME_LOCK = threading.Lock()
_RUNTIME_STARTED = False
_OFFICIAL_TOOLS = OfficialResearchTools()
_OJW_HOST_TOOL_CALL_ID = "_ojw_host_tool_call_id"


def _a2a_call(**arguments: Any) -> dict[str, Any]:
    """Rust CallbackTool entrypoint. Authoritative state never comes from GPT."""
    started = time.perf_counter()
    delegation = OpportunityDelegation.model_validate(arguments)
    with _CONTEXT_LOCK:
        context = _CONTEXTS.get(delegation.context_token)
    if context is None:
        raise ValueError("delegation context expired")
    if delegation.raw_message != context.raw_message:
        raise ValueError("raw_message differs from the persisted user message")
    trace = A2ATrace(
        route=delegation.route,
        reason=delegation.reason,
        endpoint=opportunity_a2a_url(),
        request_id=context.request_id,
        extracted_facts=[item.model_dump(mode="json") for item in delegation.facts],
    )
    try:
        request = OpportunityMutationRequest(
            conversation_id=context.conversation_id,
            request_id=context.request_id,
            base_revision=context.base_revision,
            event_id=context.event_id,
            selected_target_id=context.selected_target_id,
            delegation=delegation,
            state_snapshot=context.state_snapshot,
        )
        from openjiuwenrust._rust import A2aClient
        client = A2aClient(json.dumps({"endpoint": opportunity_a2a_url(), "streaming": True}))
        try:
            wire_request = json.dumps({
                "query": request.model_dump_json(),
                "conversation_id": context.conversation_id,
            }, ensure_ascii=False)
            raw = client.send_message(wire_request, float(opportunity_a2a_timeout_seconds()))
        finally:
            client.destroy()
        result = OpportunityMutationResult.model_validate_json(raw)
        if result.request_id != context.request_id or result.base_revision != context.base_revision:
            raise ValueError("A2A response correlation mismatch")
        trace.task_status = "completed"
        trace.accepted_facts = [item.model_dump(mode="json") for item in result.accepted_facts]
        trace.state_changes = result.state_changes
        context.result = result
        return {
            "success": True,
            "summary": result.reply_summary,
            "accepted_facts": [item.model_dump(mode="json") for item in result.accepted_facts],
            "state_changes": result.state_changes,
            "pending_confirmations": result.pending_confirmations,
            "replan_required": result.replan_required,
        }
    except Exception as exc:
        trace.task_status = "failed"
        trace.error = f"{type(exc).__name__}: {exc}"
        return {"success": False, "error": "Opportunity Agent 暂时不可用，资料尚未更新。"}
    finally:
        trace.latency_ms = int((time.perf_counter() - started) * 1000)
        context.trace = trace


def _official_call(name: str, context_token: str, **arguments: Any) -> dict[str, Any]:
    with _CONTEXT_LOCK:
        context = _CONTEXTS.get(context_token)
    if context is None:
        raise ValueError("official tool context expired")
    # openJiuwen injects this private key into LocalFunction calls so its Rust
    # runtime can correlate a tool result with the model's call.  It is
    # framework metadata, not part of the public Tool schema: remove it before
    # Pydantic validates arguments, but retain it in our trace for debugging.
    clean_arguments = dict(arguments)
    host_tool_call_id = clean_arguments.pop(_OJW_HOST_TOOL_CALL_ID, None)
    started = time.perf_counter()
    try:
        result = _OFFICIAL_TOOLS.call(name, clean_arguments)
    except Exception as exc:
        # Give the ReAct loop a bounded Tool result rather than an opaque Rust
        # callback exception. It may then offer labelled general guidance, but
        # cannot present it as an official finding.
        result = {"status": "error", "error": _official_error_message(exc)}
    status = str(result.get("status", "ok")) if isinstance(result, dict) else "ok"
    entry = {"tool": name, "arguments": clean_arguments, "status": status,
             "duration_ms": int((time.perf_counter() - started) * 1000)}
    if host_tool_call_id:
        entry["tool_call_id"] = str(host_tool_call_id)
    if status != "ok":
        entry["error"] = str(result.get("error") or _official_status_message(status))
    context.official_calls.append(entry)
    _record_official_evidence(context, result)
    return result


def _record_official_evidence(context: _RequestContext, result: dict[str, Any]) -> None:
    """Persist only structured OfficialSource evidence returned this turn."""
    candidates: list[dict[str, Any]] = []
    if isinstance(result.get("source"), dict):
        candidates.append(result["source"])
    candidates.extend(item for item in result.get("sources", []) if isinstance(item, dict))
    existing = {str(item.get("source_id", "")) for item in context.official_sources}
    for source in candidates:
        source_id = str(source.get("source_id", ""))
        if source_id and source_id not in existing:
            context.official_sources.append(source)
            existing.add(source_id)
    if isinstance(result.get("requirements"), list):
        context.official_requirements.extend(
            item for item in result["requirements"] if isinstance(item, dict)
        )


def _official_status_message(status: str) -> str:
    return {
        "official_search_not_configured": "官网搜索未配置：请设置 TAVILY_API_KEY 并启用 OFFICIAL_SEARCH_ENABLED。",
        # ``official_domain_unknown`` is kept only for responses from an
        # older worker. Current tools safely perform a bounded dynamic
        # discovery pass for unknown schools.
        "official_domain_unknown": "该学校尚未解析到官网；新版会先进行安全动态发现，请重启 Web 服务后重试。",
        "official_domain_not_found": "动态搜索未能找到可验证的学校官网；没有使用模型记忆补充项目要求。",
    }.get(status, f"官网工具未完成：{status}")


def _official_error_message(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text[:220]}" if text else type(exc).__name__


def _official_research_snapshot(context: _RequestContext) -> dict[str, Any]:
    errors = [str(item["error"]) for item in context.official_calls if item.get("error")]
    return {
        "sources": context.official_sources,
        "requirements": context.official_requirements,
        "tool_trace": context.official_calls,
        "unresolved_questions": [],
        "error": "；".join(dict.fromkeys(errors)) or None,
    }


def _label_official_answer(answer: str, context: _RequestContext) -> str:
    """Enforce the provenance boundary even when a model omits the wording."""
    failed = [str(item["error"]) for item in context.official_calls if item.get("error")]
    if failed or not context.official_sources:
        reason = "；".join(dict.fromkeys(failed)) or "没有获得可引用的 OfficialSource"
        prefix = f"官网核验未完成（{reason}）。以下如包含项目初筛，仅为非官网核验的通用信息。"
        return answer if answer.startswith("官网核验未完成") else prefix + "\n\n" + answer
    if "OfficialSource" not in answer:
        citations = "、".join(f"[OfficialSource: {source.get('source_id')}]" for source in context.official_sources)
        return answer.rstrip() + "\n\n官网已核验依据：" + citations
    return answer


def _tool_card_and_instance():
    from openjiuwenrust.core.foundation.tool.base import ToolCard
    from openjiuwenrust.core.foundation.tool.function.function import LocalFunction

    schema = OpportunityDelegation.model_json_schema()
    card = ToolCard(
        id="delegate_opportunity_update",
        name="delegate_opportunity_update",
        description=(
            "通过真实 A2A 调用 Opportunity Profile & Timeline Agent。仅当用户明确提供自己的资料、"
            "报告进展或同时更新并提问时调用；问题和假设不得调用。"
        ),
        input_params=schema,
    )
    return card, LocalFunction(card=card, func=_a2a_call)


async def _ensure_runtime() -> None:
    global _RUNTIME_STARTED
    if _RUNTIME_STARTED:
        return
    with _RUNTIME_LOCK:
        if _RUNTIME_STARTED:
            return
        from openjiuwenrust.core.runner import Runner
        await Runner.start()
        _RUNTIME_STARTED = True


class ChatOrchestratorAgent:
    """One-turn ReAct host. ConversationStore remains the source of truth."""

    def process(self, lifecycle, message: str, event_id: str, base_revision: int,
                selected_target_id: str | None = None, *, force_delegation: bool = False) -> str:
        return asyncio.run(self._process(
            lifecycle, message, event_id, base_revision, selected_target_id,
            force_delegation=force_delegation,
        ))

    async def _process(self, lifecycle, message: str, event_id: str, base_revision: int,
                       selected_target_id: str | None, *, force_delegation: bool) -> str:
        event = next(item for item in lifecycle.user_events if item.event_id == event_id)
        token = uuid4().hex
        context = _RequestContext(
            conversation_id=lifecycle.profile.user_id.removeprefix("web_"),
            request_id=event.request_id,
            base_revision=base_revision,
            event_id=event_id,
            raw_message=message,
            selected_target_id=selected_target_id,
            state_snapshot=snapshot(lifecycle),
        )
        with _CONTEXT_LOCK:
            _CONTEXTS[token] = context
        try:
            if not llm_api_key():
                raise RuntimeError("LLM_API_KEY is not configured")
            await _ensure_runtime()
            agent, cards = self._build_agent(lifecycle, token, force_delegation)
            result = await agent.invoke({
                "query": self._query(lifecycle, message, token, selected_target_id, force_delegation),
                "conversation_id": f"chat-orchestrator:{context.conversation_id}",
            })
            answer = str(result.get("output", "")).strip()
            if context.official_calls and answer:
                answer = _label_official_answer(answer, context)
            if context.trace and context.trace.task_status == "failed":
                return self._mark_retry(lifecycle, event, context, answer)
            if context.result:
                updated = restore_agent(context.conversation_id, context.result.updated_state_snapshot)
                lifecycle.__dict__.update(updated.__dict__)
                # The returned snapshot may contain the route of an older turn.  The
                # current delegation envelope is the authoritative routing record.
                lifecycle.last_route = (
                    "a2a_then_answer"
                    if context.trace and context.trace.route == "mixed"
                    else "a2a_update"
                )
                lifecycle.last_route_reason = context.trace.reason if context.trace else ""
                lifecycle.last_a2a_trace = context.trace.model_dump(mode="json") if context.trace else None
                lifecycle.pending_a2a_retry = None
                persisted_event = next(item for item in lifecycle.user_events if item.event_id == event_id)
                persisted_event.route = lifecycle.last_route
                persisted_event.route_reason = lifecycle.last_route_reason
                persisted_event.a2a_trace = lifecycle.last_a2a_trace
                final = answer or context.result.reply_summary
                already_answered = any(
                    item.role == "assistant" and item.event_id == event_id
                    for item in lifecycle.conversation_messages
                )
                reply = lifecycle._finish(persisted_event, final).reply
                if already_answered:
                    lifecycle.record_assistant_message(final)
                return reply
            if force_delegation:
                return self._mark_retry(lifecycle, event, context, "模型没有完成资料委托。")
            lifecycle.last_route = "official_answer" if context.official_calls else "direct_answer"
            lifecycle.last_route_reason = (
                "ReAct Agent 调用官网查询工具后回答" if context.official_calls
                else "ReAct Agent 未调用画像更新 A2A Tool"
            )
            lifecycle.last_a2a_trace = A2ATrace(
                route="direct_answer", reason=lifecycle.last_route_reason,
                request_id=event.request_id, task_status="not_called",
            ).model_dump(mode="json")
            lifecycle.pending_a2a_retry = None
            if context.official_calls:
                lifecycle.last_official_research = _official_research_snapshot(context)
            event.route = lifecycle.last_route
            event.route_reason = lifecycle.last_route_reason
            event.a2a_trace = lifecycle.last_a2a_trace
            if not answer:
                raise ValueError("Chat Agent returned an empty answer")
            return lifecycle._finish(event, answer).reply
        except Exception as exc:
            return self._mark_retry(lifecycle, event, context, f"Chat Agent 暂时不可用：{type(exc).__name__}")
        finally:
            with _CONTEXT_LOCK:
                _CONTEXTS.pop(token, None)

    @staticmethod
    def _mark_retry(lifecycle, event, context: _RequestContext, message: str) -> str:
        notice = (
            (message or "本次输入已保存。")
            + " 原始消息已保留，画像和时间轴尚未修改，可点击“重新处理”。"
        )
        event.status = "awaiting_agent_retry"
        event.error = context.trace.error if context.trace else message
        event.reply = notice
        for item in lifecycle.conversation_messages:
            if item.message_id == event.message_id:
                item.processing_status = "awaiting_agent_retry"
        lifecycle.last_route = "a2a_retry_pending"
        lifecycle.last_route_reason = "ReAct 或 Opportunity A2A 未完成"
        lifecycle.last_a2a_trace = (context.trace or A2ATrace(
            route="unknown", reason=lifecycle.last_route_reason,
            request_id=event.request_id, task_status="awaiting_retry",
            error=event.error,
        )).model_dump(mode="json")
        lifecycle.pending_a2a_retry = {
            "event_id": event.event_id,
            "request_id": event.request_id,
            "message": event.raw_text,
        }
        event.route = lifecycle.last_route
        event.route_reason = lifecycle.last_route_reason
        event.a2a_trace = lifecycle.last_a2a_trace
        lifecycle.record_assistant_message(notice)
        lifecycle.state_revision += 1
        lifecycle.last_turn = AgentTurnResult(
            reply=event.reply,
            replan_required=lifecycle.replan_required,
            state_revision=lifecycle.state_revision,
            route=lifecycle.last_route,
            route_reason=lifecycle.last_route_reason,
            a2a_trace=lifecycle.last_a2a_trace,
            pending_a2a_retry=True,
        )
        return event.reply

    @staticmethod
    def _query(lifecycle, message: str, token: str, selected_target_id: str | None,
               force_delegation: bool) -> str:
        profile = lifecycle.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        recent = [{"role": item.role, "content": item.content} for item in lifecycle.recent_messages[-8:]]
        target_list = []
        if lifecycle.roadmap and lifecycle.roadmap.timeline:
            from .progress import targets
            target_list = targets(lifecycle.roadmap)[:30]
        instruction = "本轮必须调用 delegate_opportunity_update。" if force_delegation else "按系统规则选择是否调用工具。"
        return json.dumps({
            "instruction": instruction,
            "context_token": token,
            "raw_message": message,
            "current_profile": profile,
            "current_state": lifecycle.state.model_dump(mode="json"),
            "recent_messages": recent,
            "selected_target_id": selected_target_id,
            "available_progress_targets": target_list,
        }, ensure_ascii=False, default=str)

    @staticmethod
    def _build_agent(lifecycle, token: str, force_delegation: bool):
        from openjiuwenrust.core.foundation.tool.base import ToolCard
        from openjiuwenrust.core.foundation.tool.function.function import LocalFunction
        from openjiuwenrust.core.runner import Runner
        from openjiuwenrust.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
        from openjiuwenrust.core.single_agent.schema.agent_card import AgentCard

        delegation_card, delegation_tool = _tool_card_and_instance()
        Runner.resource_mgr.add_tool(delegation_tool, refresh=True)
        cards = [delegation_card]
        for definition in OFFICIAL_RESEARCH_TOOLS:
            function = definition["function"]
            name = function["name"]
            parameters = json.loads(json.dumps(function["parameters"]))
            parameters.setdefault("properties", {})["context_token"] = {
                "type": "string", "description": "原样复制本轮输入上下文中的 context_token",
            }
            parameters.setdefault("required", []).append("context_token")
            card = ToolCard(id=name, name=name, description=function["description"],
                            input_params=parameters)
            Runner.resource_mgr.add_tool(LocalFunction(
                card=card,
                func=lambda context_token, _name=name, **kwargs: _official_call(_name, context_token, **kwargs),
            ), refresh=True)
            cards.append(card)

        config = ReActAgentConfig()
        config.configure_model_client(
            provider="OpenAI", api_key=llm_api_key(), api_base=llm_api_base(),
            model_name=llm_model(), verify_ssl=True,
        )
        config.configure_max_iterations(7)
        forced = "\n本轮是用户主动要求重新作为资料处理，必须调用更新工具。" if force_delegation else ""
        config.configure_prompt_template([{"role": "system", "content": SYSTEM_PROMPT + forced}])
        agent = ReActAgent(AgentCard(
            id=f"chat_orchestrator_{uuid4().hex}",
            name="Opportunity Chat Orchestrator",
            description="Understands chat, answers questions, and delegates state changes over A2A.",
        ))
        agent.configure(config)
        for card in cards:
            agent.ability_manager.add(card)
        return agent, cards
