"""Journal first, run slow inference on a snapshot, then commit with a revision check."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from uuid import uuid4

from .conversation_store import ConversationStore, DeletedConversation, RevisionConflict
from .models import ChatMessage, OnboardingProfileInput, ProgressUpdate, TimelineFactUpdate
from .session_state import restore_agent, snapshot
from .planning import planning_error_details


class SessionService:
    def __init__(self, store: ConversationStore, chat_orchestrator=None) -> None:
        self.store = store
        self.chat_orchestrator = chat_orchestrator

    def confirm_resume(self, session_id, import_id, filename, draft, original_draft, expected_revision, generate_plan):
        """Commit reviewed fields under the same authority as chat/form changes.

        No model call is made under the session lock. The returned planning
        action is explicitly requested by the review button and run afterwards.
        """
        with self.store.session_lock(session_id):
            if self.store.is_deleted(session_id):
                raise DeletedConversation("会话已删除")
            record = self.store.get(session_id)
            if not record:
                raise ValueError("找不到会话")
            agent = restore_agent(session_id, record["state"])
            request_id = "resume:" + import_id
            prior = next((e for e in agent.user_events if e.request_id == request_id and e.status == "processed"), None)
            if prior:
                return {"snapshot": self.response(record, prior.reply), "planning_action": None}
            if record["revision"] != expected_revision:
                raise RevisionConflict("画像已更新，请重新核对草稿")
            previous = agent.roadmap is not None
            reply = agent.on_resume_confirmation(import_id, filename, draft, original_draft, request_id, generate_plan)
            ready = bool(agent.profile.onboarding_completed and agent.roadmap and agent.roadmap.supported)
            action = ("replan" if previous else "enrich") if generate_plan and ready else None
            agent.planning_pending = action == "enrich"
            committed = self.store.save(session_id, self.title(agent, record["title"]), snapshot(agent), expected_revision)
            return {"snapshot": self.response(committed, reply), "planning_action": action,
                    "needs_profile": not agent.profile.onboarding_completed}

    def get(self, session_id: str) -> dict | None:
        with self.store.session_lock(session_id):
            record = self.store.get(session_id)
            if not record:
                return None
            agent = restore_agent(session_id, record["state"])
            recovering = agent.planning_pending and not any(sid == session_id for sid, _ in self.store.inflight)
            if recovering:
                agent.planning_pending = False
            if record["state"].get("snapshot_version", 0) < 3 or recovering:
                record = self.store.save(session_id, record["title"], snapshot(agent), record.get("revision", 0))
                agent.state_revision = record["revision"]
            return {**record, "state": snapshot(agent)}

    def delete_message(self, session_id: str, message_id: str) -> dict:
        """Delete one displayed chat message without silently undoing state.

        Facts, task progress, and their audit evidence may have been derived
        from a message already.  Removing a transcript entry therefore does not
        roll those state changes back; users can correct them via the existing
        profile/task controls instead of creating an inconsistent timeline.
        """
        with self.store.session_lock(session_id):
            if self.store.is_deleted(session_id):
                raise DeletedConversation("会话已删除，请新建对话")
            record = self.store.get(session_id)
            if not record:
                raise ValueError("找不到会话")
            agent = restore_agent(session_id, record["state"])
            target = next((item for item in agent.conversation_messages if item.message_id == message_id), None)
            if not target or target.role == "system":
                raise ValueError("找不到可删除的消息")
            agent.conversation_messages = [item for item in agent.conversation_messages if item.message_id != message_id]
            agent.recent_messages = [item for item in agent.conversation_messages
                                     if item.processing_status in {"processed", "legacy"}][-12:]
            agent.state_revision += 1
            committed = self.store.save(session_id, record["title"], snapshot(agent), record["revision"])
            return self.response(committed, "")

    def import_conversation(self, payload: dict) -> dict:
        session_id = payload["session_id"]
        with self.store.session_lock(session_id):
            if self.store.is_deleted(session_id):
                raise DeletedConversation("会话已删除，旧缓存不会重新导入")
            existing = self.get(session_id)
            if existing:
                return self.response(existing, "")
            agent = restore_agent(session_id, payload.get("client_state"))
            if isinstance(payload.get("messages"), list):
                agent.conversation_messages = [ChatMessage.model_validate(m) for m in payload["messages"]]
                agent.recent_messages = agent.conversation_messages[-12:]
            record = self.store.save(session_id, str(payload.get("conversation_title") or "新对话"), snapshot(agent), 0)
            return self.response(record, "")

    def execute(self, path: str, payload: dict) -> tuple[int, dict]:
        session_id = payload["session_id"]
        request_id = str(payload.get("request_id") or uuid4().hex)
        if len(request_id) > 128:
            raise ValueError("invalid request_id")
        raw, source = self._validate(path, payload)
        fingerprint = hashlib.sha256(json.dumps({"path": path, "payload": {
            k: v for k, v in payload.items() if k not in {"request_id", "client_state", "conversation_title"}
        }}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        key = (session_id, request_id)
        with self.store.session_lock(session_id):
            if self.store.is_deleted(session_id):
                raise DeletedConversation("会话已删除，请新建对话")
            record = self.store.get(session_id)
            agent = restore_agent(session_id, record["state"] if record else payload.get("client_state"))
            event = next((e for e in agent.user_events if e.request_id == request_id), None)
            if event:
                if event.payload_fingerprint and event.payload_fingerprint != fingerprint:
                    raise RevisionConflict("同一请求 ID 不能用于不同操作")
                if event.status == "processed":
                    return 200, self.response(record, event.reply)
                if key in self.store.inflight:
                    return 202, {**self.response(record, "该请求仍在处理中"), "request_status": "processing"}
                if event.status == "failed":
                    return 409, {**self.response(record, ""), "error": event.error or "上次处理失败，请重新提交"}
                # Received events left by an interrupted process are retried on the same message.
            else:
                event = agent.begin_event(raw, source, request_id)
            event.payload_fingerprint = fingerprint
            title = record["title"] if record else str(payload.get("conversation_title") or "新对话")
            record = self.store.save(session_id, title, snapshot(agent), record.get("revision", 0) if record else 0)
            base_revision = record["revision"]
            working = restore_agent(session_id, record["state"])
            self.store.inflight.add(key)
        try:
            run_payload = dict(payload)
            run_payload["_base_revision"] = base_revision
            reply = self._run(working, path, run_payload, event.event_id)
            with self.store.session_lock(session_id):
                latest = self.store.get(session_id)
                if self.store.is_deleted(session_id):
                    raise DeletedConversation("会话已删除，已丢弃迟到的 AI 结果")
                if not latest or latest.get("revision", 0) != base_revision:
                    raise RevisionConflict("等待期间已有新的资料或进展，本次迟到结果未应用；原输入已保留，请刷新后重试")
                title = self.title(working, title)
                committed = self.store.save(session_id, title, snapshot(working), base_revision)
                return 200, self.response(committed, reply)
        except Exception as exc:
            # Persist failure on the latest snapshot, not on the stale inference copy.
            with self.store.session_lock(session_id):
                latest = self.store.get(session_id)
                if latest and not self.store.is_deleted(session_id):
                    agent = restore_agent(session_id, latest["state"])
                    failed = next((e for e in agent.user_events if e.event_id == event.event_id), None)
                    if failed:
                        failed.status, failed.error = "failed", str(exc)
                        for message in agent.conversation_messages:
                            if message.event_id == failed.event_id:
                                message.processing_status = "failed"
                        if source == "replan":
                            agent.planning_pending = False
                        self.store.save(session_id, latest["title"], snapshot(agent), latest["revision"])
            raise
        finally:
            with self.store.session_lock(session_id):
                self.store.inflight.discard(key)

    @staticmethod
    def _validate(path: str, payload: dict) -> tuple[str, str]:
        if path == "/api/chat":
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                raise ValueError("message must be a non-empty string")
            return message.strip(), "chat"
        if path == "/api/onboarding":
            OnboardingProfileInput.model_validate(payload.get("profile"))
            return "更新资料：" + json.dumps(payload["profile"], ensure_ascii=False), "form"
        if path == "/api/progress":
            update = ProgressUpdate.model_validate(payload)
            if not update.target_id:
                raise ValueError("target_id is required")
            if update.action == "postpone" and not update.postponed_to:
                raise ValueError("延期操作必须填写日期")
            return update.evidence or f"任务操作：{update.action}，目标：{update.target_id}" + (f"，日期：{update.postponed_to}" if update.postponed_to else ""), "progress"
        if path == "/api/timeline-update":
            update = TimelineFactUpdate.model_validate(payload)
            return f"时间轴补录：{update.node_title}；{update.detail}", "timeline"
        if path == "/api/official/research":
            return "重新查询目标项目官网", "official_research"
        if path.startswith("/api/confirmations/"):
            if not isinstance(payload.get("accept"), bool):
                raise ValueError("accept must be boolean")
            if payload.get("postponed_to"):
                date.fromisoformat(payload["postponed_to"])
            return "确认变更" if payload["accept"] else "拒绝变更", "confirmation"
        return ("首次规划文章优化" if path.endswith("enrich") else "手动重新规划"), "replan"

    def _run(self, agent, path: str, payload: dict, event_id: str) -> str:
        if path == "/api/chat":
            if self.chat_orchestrator is not None:
                return self.chat_orchestrator.process(
                    agent, payload["message"], event_id, int(payload.get("_base_revision", 0)),
                    payload.get("selected_target_id"),
                )
            return agent.process_user_message(payload["message"], event_id, payload.get("selected_target_id")).reply
        if path == "/api/onboarding":
            return agent.on_onboarding(payload["profile"], event_id)
        if path == "/api/progress":
            return agent.on_progress(ProgressUpdate.model_validate(payload), event_id).reply
        if path == "/api/timeline-update":
            return agent.on_timeline_fact(TimelineFactUpdate.model_validate(payload), event_id).reply
        if path == "/api/official/research":
            refresh_id = payload.get("refresh_id")
            applied = agent.refresh_official_sources(refresh_id if isinstance(refresh_id, str) else None)
            event = next(e for e in agent.user_events if e.event_id == event_id)
            if applied:
                verified = "、".join(sorted({item.field for item in agent.roadmap.verified_requirements})) or "页面证据"
                unresolved = "；仍待核验：" + "、".join(agent.roadmap.unresolved_requirements) if agent.roadmap.unresolved_requirements else ""
                reply = f"已查询并保存 {len(agent.roadmap.official_sources)} 个官网页面，获得：{verified}{unresolved}。原规划文章保持不变；请核对右侧“官网依据”后，按需要点击“重新规划”生成带引用的新文章。"
            else:
                reply = (
                     "官网查询未获得可用证据；当前路线图与任务进度已保留。请检查 Tavily 配置、目标学校/项目名称后重试。")
            return agent._finish(event, reply).reply
        if path.startswith("/api/confirmations/"):
            return agent.confirm_change(path.rsplit("/", 1)[-1], payload["accept"], payload.get("target_id"),
                date.fromisoformat(payload["postponed_to"]) if payload.get("postponed_to") else None, event_id).reply
        if not agent.roadmap:
            raise ValueError("请先填写资料并创建路线图")
        if path.endswith("enrich"):
            # A GET from another browser may clear an orphaned pending marker
            # between form submission and the initial enrichment POST. The
            # initial request is still allowed exactly once; later progress or
            # a prior planning request requires explicit manual replanning.
            first_planning_request = not any(e.source == "replan" and e.event_id != event_id for e in agent.user_events)
            if agent.planning_pending or (first_planning_request and not agent.replan_required
                                          and agent.roadmap.generation_mode == "rule_fallback"):
                agent.enrich_roadmap()
            applied = agent.roadmap.generation_mode == "qwen" and not agent.planner.last_error
        else:
            applied = agent.replan_with_llm()
        if applied:
            reply = "已按当前资料和进展生成个性化规划，已保存的任务进度保持不变。"
        else:
            _, reason = planning_error_details(agent.planner.last_error)
            reply = ("本次 AI 文章未更新，已保留当前路线图和任务进度。"
                     + (f"失败原因：{reason}。" if reason else "AI 未生成可用的新文章。")
                     + "可稍后重新规划。")
        event = next(e for e in agent.user_events if e.event_id == event_id)
        return agent._finish(event, reply).reply

    def retry_a2a(self, session_id: str, event_id: str) -> tuple[int, dict]:
        """Retry one durable chat event without appending the user message again."""
        if self.chat_orchestrator is None:
            raise ValueError("Chat A2A orchestrator is disabled")
        with self.store.session_lock(session_id):
            if self.store.is_deleted(session_id):
                raise DeletedConversation("会话已删除，请新建对话")
            record = self.store.get(session_id)
            if not record:
                raise ValueError("找不到会话")
            agent = restore_agent(session_id, record["state"])
            event = next((item for item in agent.user_events if item.event_id == event_id), None)
            if not event:
                raise ValueError("找不到待重试消息")
            if event.source != "chat":
                raise ValueError("只有聊天输入可以重新交给路由 Agent")
            key = (session_id, event.request_id)
            if key in self.store.inflight:
                return 202, {**self.response(record, "该请求仍在处理中"), "request_status": "processing"}
            base_revision = record["revision"]
            self.store.inflight.add(key)
        try:
            working = restore_agent(session_id, record["state"])
            reply = self.chat_orchestrator.process(
                working, event.raw_text, event.event_id, base_revision,
                force_delegation=True,
            )
            with self.store.session_lock(session_id):
                latest = self.store.get(session_id)
                if not latest or latest["revision"] != base_revision:
                    raise RevisionConflict("等待期间已有新的资料或进展，本次重试结果未应用")
                committed = self.store.save(session_id, self.title(working, record["title"]), snapshot(working), base_revision)
                return 200, self.response(committed, reply)
        finally:
            with self.store.session_lock(session_id):
                self.store.inflight.discard(key)

    @staticmethod
    def title(agent, requested: str) -> str:
        p = agent.profile
        parts = [f"大{p.academic_year}" if p.academic_year else "", p.major or "", (p.target_countries or [""])[0]]
        if len([p for p in parts if p]) >= 2:
            return " · ".join(p for p in parts if p)[:48]
        if requested != "新对话":
            return requested[:48]
        return next((m.content[:48] for m in agent.conversation_messages if m.role == "user"), "新对话")

    @staticmethod
    def response(record: dict, reply: str) -> dict:
        agent = restore_agent(record["session_id"], record["state"])
        return {**snapshot(agent), "reply": reply, "conversation_id": record["session_id"],
                "conversation_title": record["title"], "state_revision": record.get("revision", 0)}
