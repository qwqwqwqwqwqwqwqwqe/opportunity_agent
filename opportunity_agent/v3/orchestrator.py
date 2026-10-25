"""Legacy V3 workflow/evaluation blocks; production chat uses ProfileAssistant."""
from __future__ import annotations

import asyncio
import json
import re
from ..llm_client import LLMClient
from ..v2.agents.profile_extraction import ProfileExtractionPipeline
from .contracts import Command, CompletionResult, IntakeResult, RouteDecision, WorkflowGoal
from .engine import review, schedule
from .service import ENTITIES


class GoalParser:
    def parse(self, message: str) -> WorkflowGoal:
        planning = bool(re.search(r"规划|计划|排期|时间表|安排|schedule|plan", message, re.I))
        checking = bool(re.search(r"检查|缺什么|缺哪些|缺少|齐全|风险|查看|列出|有哪些|进度|review|check", message, re.I))
        mutation = bool(re.search(r"更新|修改|改为|改成|新增|添加|记录|保存|完成了|提交了|不想考\s*GRE|更看重就业|我的(?:托福|雅思|成绩|专业|本科|目标|GPA)|我本科|我毕业|托福.*\d|雅思.*\d", message, re.I))
        intent = "intake_and_plan" if mutation and planning else "intake" if mutation else "planning" if planning else "review" if checking else "chat"
        outputs = ["intake"] if intent == "intake" else ["intake", "planning"] if intent == "intake_and_plan" else [intent] if intent != "chat" else []
        if checking and "review" not in outputs:
            outputs.append("review")
        return WorkflowGoal(intent=intent, required_outputs=outputs)


class Router:
    def route(self, goal: WorkflowGoal) -> RouteDecision:
        if goal.intent == "chat":
            return RouteDecision(mode="direct_reply", reason="无需业务操作")
        agents = goal.required_outputs or (["intake", "planning"] if goal.intent == "intake_and_plan" else [goal.intent])
        return RouteDecision(mode="delegate", agents=agents,
                             dependencies=[("intake", agent) for agent in agents if agent != "intake"] if "intake" in agents else [])


class IntakeAgent:
    def __init__(self, client):
        self.client = client

    def execute(self, message, snapshot, conversation_context):
        profile = ProfileExtractionPipeline(self.client).extract(message, conversation_context, mode="auto")
        facts = [fact.model_dump(mode="json") for fact in profile.accepted_facts]
        result = IntakeResult(candidate_facts=facts)
        if facts:
            result.proposed_commands.append(Command(type="profile.change", expected_version=snapshot["profile_version"] or 1,
                                                   payload={"facts": facts}))
        for preference in profile.preferences:
            result.proposed_commands.append(Command(type="preference.change", payload={"key": preference.key,
                "value": preference.value, "evidence": preference.evidence,
                "expected_version": snapshot.get("preference_versions", {}).get(preference.key, 0)}))
        if not self.client.enabled:
            if not result.proposed_commands:
                result.missing_fields.append("请使用表单录入，或配置模型后提供要修改的项目、字段和值")
            return result
        # Profile extraction handles the common profile-only path without a
        # second model request. Domain intake uses a typed schema and evidence.
        if result.proposed_commands and not re.search(r"项目|截止|材料|推荐信|任务|日程", message):
            return result
        semantic = self.client.generate_structured(IntakeResult, system=(
            "你是申请管理 Intake。只提取当前用户明确表达的业务修改，返回结构化 JSON。"
            "不直接写入。歧义、假设、他人情况、问题不能变成命令。"
            "仅可使用上下文中的实体ID，无法唯一匹配时填 ambiguities。"
            "每个 proposed_commands 必须按顺序对应一项 evidence_refs，quote 必须逐字摘自原文。"
            "同一批创建项目、周期、申请时，为前置命令设 ref（例如 p）；后续 payload.program_id 可写 $ref:p。禁止前向引用。"
            "创建用 *.create，修改用 *.update 并携带 target_id 与当前 expected_version。"
            "允许 program/offering/application/requirement/material/task/calendar_event 的 create/update；"
            "不要生成 profile、plan、source 或其他命令。日期精度和时区必须遵守 schema。"
            "所有命令均等待用户批准。"), context={"message": message, "snapshot": snapshot,
                "conversation_context": conversation_context,
                "schemas": {kind: schema.model_json_schema() for kind, (_, schema) in ENTITIES.items()}},
                temperature=0, max_tokens=3000)
        if semantic.ambiguities or semantic.missing_fields:
            result.ambiguities.extend(semantic.ambiguities)
            result.missing_fields.extend(semantic.missing_fields)
            return result
        if len(semantic.proposed_commands) != len(semantic.evidence_refs):
            raise ValueError("every intake command requires its own evidence quote")
        collections = {"program": "programs", "offering": "offerings", "application": "applications",
                       "requirement": "requirements", "material": "materials", "task": "tasks", "calendar_event": "calendar_events"}
        for command, evidence in zip(semantic.proposed_commands, semantic.evidence_refs):
            kind, _, action = command.type.partition(".")
            if kind not in ENTITIES or action not in {"create", "update"} or evidence.quote not in message:
                raise ValueError("unsupported intake command or ungrounded evidence")
            data = command.payload
            if action == "update":
                current = next((row for row in snapshot[collections[kind]] if row["id"] == command.target_id), None)
                if not current or command.expected_version != current["version"]:
                    raise ValueError("intake entity is ambiguous or stale")
                schema = ENTITIES[kind][1]
                data = {**{k: current[k] for k in schema.model_fields}, **data}
            ENTITIES[kind][1].model_validate(data)
            # Model cannot invent source identifiers or evidence provenance.
            command.evidence_refs = []
            for key in ("source_id", "evidence_ref"):
                if command.payload.get(key):
                    raise ValueError("source identifiers must be supplied by the application")
        result.proposed_commands.extend(semantic.proposed_commands)
        result.evidence_refs.extend(semantic.evidence_refs)
        return result


class CompletionChecker:
    def check(self, goal, results, proposals, failures, deferred):
        missing = [name for name in goal.required_outputs if name not in results and name not in deferred]
        intake = results.get("intake", {})
        if intake.get("ambiguities") or intake.get("missing_fields"):
            return CompletionResult(status="NEED_USER", reasons=intake.get("ambiguities", []) + intake.get("missing_fields", []))
        if failures or missing:
            return CompletionResult(status="PARTIAL" if results or proposals else "FAILED", missing_outputs=missing, reasons=failures)
        if results.get("planning", {}).get("conflicts"):
            return CompletionResult(status="NEED_USER", reasons=results["planning"]["conflicts"])
        if proposals or deferred:
            return CompletionResult(status="WAITING_APPROVAL")
        if "planning" in results:
            return CompletionResult(status="WAITING_APPROVAL")
        return CompletionResult(status="COMPLETED")


class WorkflowOrchestrator:
    def __init__(self, client=None, agents=None):
        self.client = client or LLMClient(timeout_seconds=25, retries=0)
        self.parser = GoalParser()
        self.router = Router()
        self.agents = agents or {}

    async def aclose(self):
        pass

    async def ainvoke(self, initial, event_queue=None):
        async def emit(kind, payload):
            events.append({"type": kind, "payload": payload})
            if event_queue is not None:
                event_queue.put_nowait((kind, payload))

        events, results, failures, proposals, deferred, history = [], {}, [], [], [], []
        message, snapshot = initial["message"], initial["workflow_snapshot"]
        goal = self.parser.parse(message)
        from ..v2.core.config import settings
        retired_planning = settings.app_mode == "applyops" and "planning" in goal.required_outputs
        if retired_planning:
            goal.required_outputs = [name for name in goal.required_outputs if name != "planning"]
            if not goal.required_outputs:
                goal.intent = "chat"
            elif goal.intent == "intake_and_plan":
                goal.intent = "intake"
        route = self.router.route(goal)
        await emit("route_selected", route.model_dump())
        async def business():
            for name in route.agents:
                if name in {"planning", "review"} and results.get("intake", {}).get("proposed_commands"):
                    deferred.append(name)
                    continue
                if name in {"planning", "review"} and (results.get("intake", {}).get("ambiguities") or results.get("intake", {}).get("missing_fields")):
                    deferred.append(name)
                    continue
                await emit("agent_started", {"agent": name})
                for attempt in range(2):
                    try:
                        if name in self.agents:
                            value = await self.agents[name](message, snapshot)
                        elif name == "intake":
                            value = (await asyncio.to_thread(IntakeAgent(self.client).execute, message, snapshot,
                                                    initial.get("conversation_context", {}))).model_dump(mode="json")
                        elif name == "planning":
                            value = schedule(snapshot)
                        else:
                            value = review(snapshot)
                        results[name] = value
                        history.append({"agent": name, "attempt": attempt + 1, "result": value})
                        break
                    except Exception as exc:
                        history.append({"agent": name, "attempt": attempt + 1, "error": type(exc).__name__})
                        if attempt:
                            failures.append(f"{name}: {type(exc).__name__}")
                await emit("agent_completed", {"agent": name, "status": "complete" if name in results else "failed"})
        try:
            await asyncio.wait_for(business(), timeout=100)
        except TimeoutError:
            failures.append("执行时间预算耗尽")
        if results.get("intake", {}).get("proposed_commands"):
            proposals.append({"type": "workflow.commands", "payload": {
                "schema_version": 3, "request_id": f"{initial['request_id']}:intake",
                "commands": results["intake"]["proposed_commands"]}, "reason": "请确认提取的业务信息",
                "deferred_actions": deferred})
        completion = CompletionChecker().check(goal, results, proposals, failures, deferred)
        await emit("completion_checked", completion.model_dump())
        status_text = {"WAITING_APPROVAL": "变更待确认，尚未保存。", "NEED_USER": "需要补充或确认信息。",
                       "PARTIAL": "已完成部分处理，仍有缺口。", "FAILED": "本次未能完成，请使用表单或稍后重试。", "COMPLETED": "处理完成。"}[completion.status]
        if route.mode == "direct_reply":
            answer = "你好，我可以帮你整理申请项目、材料和任务，也可以和你聊聊申请准备。"
        elif "review" in results:
            answer = "已检查记录中的申请要求：\n" + json.dumps(results["review"], ensure_ascii=False, indent=2)
        elif "planning" in results:
            answer = f"已计算计划草稿，共 {len(results['planning']['items'])} 个任务。请在计划中查看差异并确认。"
        else:
            answer = "已整理你的信息，请在待确认中查看具体变更。"
        fallback_answer = answer
        if retired_planning:
            answer += "\n自动排期和计划版本已停用。请在待办中维护安排，在官网监控中配置项目页面以接收个人影响通知。"
            fallback_answer = answer
        if self.client.enabled and completion.status not in {"FAILED"}:
            try:
                answer = await asyncio.wait_for(asyncio.to_thread(self.client.generate,
                    system="你是独立的申请助手 Synthesizer。根据业务结果用中文回答。闲聊自然回复。"
                           "待审批的修改必须称待确认，不得声称已经保存。未知要求明确未知，不编造学校规定。"
                           "不要替用户批准任何操作；材料完成情况仅限已记录要求。"
                           + ("自动排期已停用，不生成排期、不声称创建计划；引导用户使用待办和官网监控。" if retired_planning else ""),
                    user=json.dumps({"query": message, "results": results, "snapshot": snapshot, "completion": completion.model_dump(),
                                     "context": initial.get("conversation_context", {})}, ensure_ascii=False), max_tokens=1400), timeout=19)
            except Exception:
                pass
        if completion.status == "WAITING_APPROVAL" and re.search(r"已(?:经)?(?:保存|更新|修改|启用|生效)", answer):
            answer = fallback_answer
        if route.mode != "direct_reply":
            answer += "\n\n" + status_text
            if completion.reasons:
                answer += "\n" + "\n".join(completion.reasons)
        await emit("answer_snapshot", {"text": answer})
        return {**initial, "schema_version": 3, "answer": answer, "events": events,
                "goal": goal.model_dump(), "route_decision": route.model_dump(), "results": results,
                "result_history": history, "failures": failures, "completion": completion.model_dump(),
                "proposals": proposals, "approval_required": bool(proposals), "deferred_actions": deferred,
                "plan_draft_requested": "planning" in results}


from .profile_assistant import ProfileAssistant  # noqa: E402

orchestrator = ProfileAssistant()
