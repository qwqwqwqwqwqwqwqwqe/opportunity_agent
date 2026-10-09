from __future__ import annotations

from datetime import date, datetime, timezone
import inspect
import json
from uuid import uuid4

from .career import recommend_jobs
from .conflict_resolver import ConflictDecision, ProfileConflictResolver
from .models import CandidateFact, ChatMessage, ExtractionResult, ExternalEvent, JobRecommendation, NotificationDecision, Roadmap, StudentProfile, UserState
from .onboarding import apply_onboarding
from .planning import HybridRoadmapPlanner, _target_pairs, build_roadmap, replan_deadline
from .profile import HybridFactExtractor, hydrate_score_fields_from_facts, next_roadmap_requirement
from .recommendation import decide_deadline_notification
from .repository import LocalRepository
from .state import derive_state
from .advisor import AdviceResponder
from .models import AgentTurnResult, UserEvent, TaskProgress, StateTransition, PendingConfirmation, ProgressUpdate, TimelineFactUpdate
from .progress import migrate_progress, refresh_progress, targets, resolve_targets
from .state_transition import StateTransitionEngine
from .turn_understanding import assertion_text
from .normalizer import ProfileNormalizer


class LifecycleAgent:
    """Offline core of the phase-aware study-abroad agent.

    The deterministic services are also suitable as openJiuwen Tools. A future
    ReAct workflow should orchestrate them, but must not bypass their validation.
    """

    def __init__(self, user_id: str, planner: HybridRoadmapPlanner | None = None,
                 repository: LocalRepository | None = None,
                 extractor: HybridFactExtractor | None = None,
                 conflict_resolver: ProfileConflictResolver | None = None,
                 responder: AdviceResponder | None = None) -> None:
        self.profile = StudentProfile(user_id=user_id)
        self.state = UserState()
        self.roadmap: Roadmap | None = None
        self.job_recommendations: list[JobRecommendation] = []
        self.extractor = extractor or HybridFactExtractor()
        self.last_extraction = ExtractionResult()
        self.last_conflict_decisions: list[ConflictDecision] = []
        self.recent_messages: list[ChatMessage] = []
        self.conversation_messages: list[ChatMessage] = []
        self.pending_information_field: str | None = None
        self.planner = planner or HybridRoadmapPlanner()
        self.repository = repository or LocalRepository()
        self.conflict_resolver = conflict_resolver or ProfileConflictResolver()
        self.planning_pending = False
        self.user_events: list[UserEvent] = []
        self.task_progress: list[TaskProgress] = []
        self.state_transitions: list[StateTransition] = []
        self.stage_evidence = []
        self.stage_assessments = []
        self.pending_confirmations: list[PendingConfirmation] = []
        self.replan_required = False
        self.state_revision = 0
        self.stop_followups = False
        self.last_turn = AgentTurnResult()
        self.last_official_research: dict | None = None
        self.last_referenced_target_id: str | None = None
        self.last_route = ""
        self.last_route_reason = ""
        self.last_a2a_trace: dict | None = None
        self.pending_a2a_retry: dict | None = None
        self.responder = responder or AdviceResponder()
        self.transition_engine = StateTransitionEngine()

    def begin_event(self, raw_text: str, source: str = "chat", request_id: str | None = None) -> UserEvent:
        request_id = request_id or uuid4().hex
        existing = next((e for e in self.user_events if e.request_id == request_id), None)
        if existing:
            return existing
        event = UserEvent(request_id=request_id, source=source, raw_text=raw_text)
        message = ChatMessage(role="user", content=raw_text, event_id=event.event_id,
                              created_at=event.received_at, processing_status="received")
        event.message_id = message.message_id
        self.user_events.append(event)
        self.conversation_messages.append(message)
        self.state_revision += 1
        return event

    def _event(self, event_id: str | None, text: str, source: str) -> UserEvent:
        event = next((e for e in self.user_events if e.event_id == event_id), None)
        return event or self.begin_event(text, source)

    def _finish(self, event: UserEvent, reply: str) -> AgentTurnResult:
        event.status, event.reply = "processed", reply
        for message in self.conversation_messages:
            if message.message_id == event.message_id:
                message.processing_status = "processed"
        if not any(m.role == "assistant" and m.event_id == event.event_id for m in self.conversation_messages):
            self.record_assistant_message(reply, event.event_id)
        self.recent_messages = [m for m in self.conversation_messages if m.processing_status in {"processed", "legacy"}][-12:]
        self.state_revision += 1
        self.last_turn = AgentTurnResult(
            reply=reply,
            progress_updates=[p for p in self.task_progress if p.source_event_id == event.event_id],
            state_changes=[s for s in self.state_transitions if s.source_event_id == event.event_id],
            pending_confirmations=[p for p in self.pending_confirmations if p.status == "pending"],
            replan_required=self.replan_required, state_revision=self.state_revision,
            answer_fallback_reason=self.responder.last_error,
            official_research=self.last_official_research,
            route=self.last_route,
            route_reason=self.last_route_reason,
            a2a_trace=self.last_a2a_trace,
            pending_a2a_retry=bool(self.pending_a2a_retry),
        )
        return self.last_turn

    def _log(self, field, old, new, event: UserEvent, reason: str, confidence: float = 1.0,
             evidence: str | None = None, evidence_ids: list[str] | None = None) -> None:
        if old != new:
            if field.startswith("state.") and event.extraction:
                strengths = [f.confidence for f in event.extraction.facts if not f.needs_confirmation and f.confidence >= .75]
                strengths += [s.strength for s in event.extraction.stage_signals if s.strength >= .75]
                strengths += [p.confidence for p in event.extraction.progress_updates if not p.needs_confirmation and p.confidence >= .75]
                if strengths:
                    confidence = min(strengths)
            self.state_transitions.append(StateTransition(
                field=field, old_value=old, new_value=new, reason=reason,
                evidence=evidence or event.raw_text, source_event_id=event.event_id, confidence=confidence,
                evidence_ids=evidence_ids or self.transition_engine.event_evidence_ids(
                    field, event.event_id, self.stage_evidence),
            ))

    def _collect_stage_evidence(self, event: UserEvent, facts=None, *, confirmed: bool = False) -> None:
        existing = {item.evidence_id for item in self.stage_evidence}
        records = [item for item in self.task_progress if item.source_event_id == event.event_id]
        extracted = event.extraction or ExtractionResult()
        additions = self.transition_engine.collect_evidence(
            event, list(facts or []), records, extracted.stage_signals, confirmed=confirmed,
        )
        signatures = {(item.dimension, item.kind, item.evidence, item.source_event_id, item.target_id)
                      for item in self.stage_evidence}
        self.stage_evidence.extend(item for item in additions
            if item.evidence_id not in existing and
            (item.dimension, item.kind, item.evidence, item.source_event_id, item.target_id) not in signatures)

    def refresh_state(self, today: date | None = None) -> None:
        migrate_progress(self.roadmap, self.profile, self.task_progress)
        refresh_progress(self.roadmap, self.task_progress, today)
        active_ids = {item["target_id"] for item in targets(self.roadmap)}
        active = [p for p in self.task_progress if p.target_id in active_ids]
        now = datetime.now(timezone.utc)
        active_signal_events = {item.source_event_id for item in self.stage_evidence
                                if item.kind == "signal" and (item.expires_at is None or item.expires_at >= now)}
        recorded_signal_events = {item.source_event_id for item in self.stage_evidence if item.kind == "signal"}
        signals = [s for event in self.user_events if event.extraction and event.status != "failed"
                   and (event.event_id not in recorded_signal_events or event.event_id in active_signal_events)
                   for s in event.extraction.stage_signals]
        self.state, self.stage_assessments = self.transition_engine.assess(
            self.profile, active, signals, self.stage_evidence, now=now)

    def _refresh_plan(self, changed: bool, event: UserEvent, allow_initial: bool = True) -> None:
        current = self.roadmap
        if not current:
            if allow_initial and not next_roadmap_requirement(self.profile):
                self.roadmap = self.planner.generate(self.profile, self.state, revision_reason="initial_profile")
        elif changed:
            updated = build_roadmap(self.profile, self.state, version=current.version,
                                    revision_reason="profile_updated", repository=self.repository)
            # The generated article stays visible until the user requests a replacement.
            updated.article, updated.generation_mode = current.article, current.generation_mode
            self.roadmap = updated
            self.replan_required = True
            self.planning_pending = False
        self.refresh_state()
        self.job_recommendations = recommend_jobs(self.profile, self.state, self.repository) or self._timeline_job_recommendations()


    def on_resume_confirmation(self, import_id, filename, draft, original_draft, request_id, generate_plan=False) -> str:
        from .resume_models import EXPERIENCE_FIELDS
        from .normalizer import ProfileNormalizer
        from .domain_knowledge import validate_profile_domain
        event = self.begin_event(f"确认导入简历：{filename}", "resume", request_id)
        if event.status == "processed":
            return event.reply
        before = self.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        facts = []
        originals = {f.field: f for f in (original_draft.facts if original_draft else [])}
        for item in draft.facts:
            if not item.selected or item.value is None:
                continue
            original = originals.get(item.field)
            evidence = (f"简历 {import_id}；字段 {item.field}；用户审核确认；"
                        f"原始抽取置信度 {original.confidence if original else '手工填写'}；"
                        f"{original.evidence if original else ''}；位置 {','.join(original.block_ids) if original else '用户补充'}")
            facts.append(CandidateFact(field=item.field,
                raw_value=original.raw_value if original else item.value, normalized_value=item.value,
                source="user_explicit", confidence=1.0, evidence=evidence))
        original_experiences = {e.experience_id: e for e in (original_draft.experiences if original_draft else [])}
        for item in draft.experiences:
            if item.selected:
                description = item.description()
                original = original_experiences.get(item.experience_id)
                facts.append(CandidateFact(field=EXPERIENCE_FIELDS[item.kind], raw_value=[original.description() if original else description],
                    normalized_value=[description], source="user_explicit", confidence=1.0, operation="append",
                    evidence=f"简历 {import_id}；用户审核确认；原始抽取置信度 {original.confidence if original else '手工填写'}；{original.evidence if original else ''}；位置 {','.join(original.block_ids) if original else '用户补充'}"))
        normalized = ProfileNormalizer().normalize(facts)
        resolution = self.conflict_resolver.resolve(self.profile, normalized)
        self.profile, self.last_conflict_decisions = resolution.profile, resolution.decisions
        if any(f.field in {"gpa_raw", "gpa_scale"} for f in facts):
            reference = self.profile.gpa_raw if self.profile.gpa_scale == 4 and self.profile.gpa_raw is not None and 0 <= self.profile.gpa_raw <= 4 else None
            self.profile.gpa = self.profile.gpa_4_reference = reference
        ready = all((self.profile.school, self.profile.major, self.profile.academic_year,
                     self.profile.degree_years, self.profile.graduation_year, self.profile.target_countries,
                     self.profile.target_degree, self.profile.target_fields))
        if ready:
            self.profile.onboarding_completed = True
        supported, domain, _ = validate_profile_domain(self.profile)
        self.profile.planning_domain = domain if supported else None
        after = self.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        for field, value in after.items():
            self._log("profile." + field, before.get(field), value, event, "用户审核简历后确认")
        self.last_extraction = event.extraction = ExtractionResult(intent="profile_update", facts=normalized,
                                                                   should_replan=before != after)
        self._collect_stage_evidence(event, normalized, confirmed=True)
        if not self.roadmap and ready and generate_plan:
            self.roadmap = build_roadmap(self.profile, repository=self.repository, revision_reason="resume_confirmed")
        self._refresh_plan(before != after, event, allow_initial=False)
        reply = f"已确认简历中的 {len(facts)} 项资料，原文证据已记录。"
        reply += "可按此次确认生成规划；已有任务进度保留。" if ready else "请在资料表补齐年级、毕业时间和申请目标后生成规划。"
        return self._finish(event, reply).reply
    def on_onboarding(self, payload: dict, event_id: str | None = None) -> str:
        event = self._event(event_id, json.dumps(payload, ensure_ascii=False), "form")
        if event.status == "processed":
            return event.reply
        first = self.roadmap is None
        before = self.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        prior_fact_count = len(self.profile.facts)
        old_state = self.state.model_dump(mode="json")
        self.profile = apply_onboarding(self.profile, payload,
            normalizer=getattr(self.extractor, "normalizer", None), resolver=self.conflict_resolver)
        onboarding_facts = self.profile.facts[prior_fact_count:]
        event.extraction = ExtractionResult(intent="profile_update", facts=onboarding_facts,
                                            should_replan=bool(onboarding_facts))
        self._collect_stage_evidence(event, onboarding_facts, confirmed=True)
        after = self.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        for field in after:
            self._log("profile." + field, before.get(field), after[field], event, "用户提交资料表")
        self.state = derive_state(self.profile)
        if first:
            self.roadmap = build_roadmap(self.profile, self.state, repository=self.repository, revision_reason="onboarding_submitted")
        self._refresh_plan(not first and before != after, event)
        self.pending_information_field = None
        client = getattr(getattr(self.planner, "model_planner", None), "llm_client", None)
        self.planning_pending = bool(first and client and client.enabled and self.roadmap.supported)
        for field, value in self.state.model_dump(mode="json").items():
            self._log("state." + field, old_state.get(field), value, event, "资料与已记录进展")
        reply = (self.roadmap.support_message if not self.roadmap.supported else
                 f"已根据完整资料生成路线图 v{self.roadmap.version}，可点击时间轴查看阶段计划。" if first else
                 "资料已保存，任务和时间轴已同步更新；原文章保留，可点击“重新规划”更新文章。")
        return self._finish(event, reply).reply

    def enrich_roadmap(self) -> Roadmap:
        current, revision = self.roadmap, self.state_revision
        generated = self.planner.generate(self.profile.model_copy(deep=True), self.state.model_copy(deep=True), current, "llm_article_enrichment")
        if revision != self.state_revision or current is not self.roadmap:
            return self.roadmap
        if generated and generated.generation_mode == "qwen":
            generated.version = current.version if current else 1
            self.roadmap = generated
            self.replan_required = False
        self.planning_pending = False
        self.refresh_state()
        return self.roadmap

    def replan_with_llm(self) -> bool:
        current, revision = self.roadmap, self.state_revision
        model = getattr(self.planner, "model_planner", None)
        if current is None or model is None:
            return False
        generated = model.generate(self.profile.model_copy(deep=True), self.state.model_copy(deep=True), current, "manual_llm_replan")
        if revision != self.state_revision or current is not self.roadmap:
            return False
        if generated is None or generated.generation_mode != "qwen":
            self.planner.last_mode = current.generation_mode
            self.planner.last_error = getattr(model, "last_error", None) or "AI 未生成新的个性化规划"
            self.planning_pending = False
            return False
        generated.version = current.version + 1
        self.roadmap = generated
        self.replan_required, self.planning_pending = False, False
        self.planner.last_mode, self.planner.last_error = "qwen", getattr(model, "last_error", None)
        self.refresh_state()
        self.job_recommendations = self._timeline_job_recommendations()
        self.state_revision += 1
        return True

    def refresh_official_sources(self, refresh_id: str | None = None) -> bool:
        """Refresh evidence only; the user still chooses whether to rewrite article."""
        current = self.roadmap
        model = getattr(self.planner, "model_planner", None)
        if not current or not model or not hasattr(model, "_research"):
            return False
        # Re-evaluate persisted evidence first.  A same-university page may
        # have been accepted by an older matcher even though it belongs to an
        # unrelated faculty (for example UofT Civil & Mineral, not CS).
        old_valid, old_revoked = list(current.official_sources), []
        official_tools = getattr(model, "official_tools", None)
        if official_tools is not None and hasattr(official_tools, "validate_sources"):
            pairs = [(item.school, item.program) for item in _target_pairs(self.profile)]
            old_valid, old_revoked = official_tools.validate_sources(current.official_sources, pairs)
        research = model._research(self.profile.model_copy(deep=True), fresh_turn=True)
        # The right-side "官网查询状态" is the result of this refresh, not a
        # stale trace from an earlier chat question.  Persist it even when no
        # eligible page survived matching so the user can see what was tried.
        self.last_official_research = research.model_dump(mode="json")
        # Echoed to the browser so an updated HTML file can reliably detect a
        # still-running older Python process rather than silently rendering a
        # stale prior research result.
        self.last_official_research["refresh_id"] = refresh_id
        self.last_official_research["refreshed_at"] = datetime.now(timezone.utc).isoformat()
        if not research.sources and not research.requirements:
            self.planner.last_error = "官网查询未找到可用的官方项目证据"
            if old_revoked:
                updated = current.model_copy(deep=True)
                updated.official_sources = old_valid
                updated.revoked_official_sources = [*current.revoked_official_sources, *old_revoked]
                valid_source_ids = {item.source_id for item in old_valid}
                updated.verified_requirements = [item for item in current.verified_requirements
                                                 if set(item.source_ids).issubset(valid_source_ids)]
                self.roadmap = updated
                # Include the automatic cleanup in the diagnostic shown by
                # this button click, even though no new source was found.
                self.last_official_research["revoked_sources"] = [
                    item.model_dump(mode="json") for item in old_revoked
                ]
            self.state_revision += 1
            return False
        updated = current.model_copy(deep=True)
        updated.official_sources = research.sources
        updated.verified_requirements = research.requirements
        updated.unresolved_requirements = research.unresolved_questions
        updated.revoked_official_sources = [*research.revoked_sources, *old_revoked]
        self.roadmap = updated
        self.replan_required = True
        self.planner.last_error = None
        self.state_revision += 1
        return True

    def on_user_message(self, message: str) -> str:
        return self.process_user_message(message).reply

    def process_user_message(self, message: str, event_id: str | None = None,
                             selected_target_id: str | None = None) -> AgentTurnResult:
        event = self._event(event_id, message, "chat")
        if event.status == "processed":
            return self._finish(event, event.reply)
        self.profile = hydrate_score_fields_from_facts(StudentProfile.model_validate(self.profile.model_dump(mode="json")))
        self.refresh_state()
        old_state = self.state.model_dump(mode="json")
        before = self.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        assertions, asking = assertion_text(message)
        pending = [c for c in self.pending_confirmations if c.status == "pending"]
        if pending and message.strip() in {"确认", "是的", "确定", "拒绝", "不是", "不用了"}:
            return self.confirm_change(pending[0].confirmation_id, message.strip() in {"确认", "是的", "确定"},
                                       event_id=event.event_id)
        if any(word in message for word in ("不想补充", "不要再问", "暂不补充")):
            self.stop_followups = True
            return self._finish(event, "好的，暂不追问资料。你可以继续咨询，或直接在任务卡上更新进度。")
        if not assertions:
            self.last_extraction = ExtractionResult(intent="ask_advice" if asking else "no_change")
        elif hasattr(self.extractor, "extract_result"):
            kwargs = dict(profile=self.profile, state=self.state, recent_messages=self.recent_messages,
                          progress_targets=targets(self.roadmap))
            signature = inspect.signature(self.extractor.extract_result)
            kwargs = {k: v for k, v in kwargs.items() if k in signature.parameters}
            self.last_extraction = self.extractor.extract_result(assertions, **kwargs)
        else:
            self.last_extraction = ExtractionResult(facts=self.extractor.extract(assertions))
        if asking:
            self.last_extraction.intent = "mixed" if assertions else "ask_advice"
        event.extraction = self.last_extraction.model_copy(deep=True)
        facts = self.last_extraction.facts
        if assertions and not asking:
            facts = self._apply_pending_answer_context(assertions, facts)
        self.last_extraction.facts = facts
        event.extraction = self.last_extraction.model_copy(deep=True)
        resolution = self.conflict_resolver.resolve(self.profile, facts)
        self.profile, self.last_conflict_decisions = resolution.profile, resolution.decisions
        accepted_facts = [fact for fact, decision in zip(facts, resolution.decisions)
                          if decision.status != "confirmation_required" and fact.confidence >= 0.75]
        for fact, decision in zip(facts, resolution.decisions):
            if decision.status == "confirmation_required" or fact.needs_confirmation or fact.confidence < 0.75:
                self._add_confirmation(PendingConfirmation(source_event_id=event.event_id, fact=fact,
                    question=f"是否确认将 {fact.field} 更新为 {fact.value}？"))
        after = self.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        for field in after:
            self._log("profile." + field, before.get(field), after[field], event, "用户明确陈述")
        for update in self.last_extraction.progress_updates:
            if not update.target_id and not update.target_hint and selected_target_id:
                update.target_id = selected_target_id
            elif (not update.target_id and not update.target_hint and self.last_referenced_target_id
                  and any(token in message for token in ("这个", "那个", "这项", "那项", "它"))):
                update.target_id = self.last_referenced_target_id
            self._apply_progress(update, event)
        event_records = [item for item in self.task_progress if item.source_event_id == event.event_id]
        if len(event_records) == 1:
            self.last_referenced_target_id = event_records[0].target_id
        self._collect_stage_evidence(event, accepted_facts)
        self.refresh_state()
        old_roadmap = self.roadmap
        self._refresh_plan(before != after, event, allow_initial=not asking)
        for field, value in self.state.model_dump(mode="json").items():
            self._log("state." + field, old_state.get(field), value, event, "已确认事实与任务证据")
        changes = [c for c in self.state_transitions if c.source_event_id == event.event_id]
        pending = [c for c in self.pending_confirmations if c.status == "pending"]
        if asking:
            reply = self.responder.answer(message, self.profile, self.state, self.roadmap, selected_target_id)
            if self.responder.last_research and self.roadmap:
                # A consultation records citations only on the answer/snapshot;
                # it must not silently replace the saved planning evidence.
                self.last_official_research = self.responder.last_research.model_dump(mode="json")
            if changes:
                reply += "\n\n" + self._change_reply(event)
        elif old_roadmap is None and self.roadmap is not None:
            reply = f"已生成路线图 v{self.roadmap.version}。可以点击任务查看计划，并报告你的进展。"
        else:
            requirement = next_roadmap_requirement(self.profile) if not self.profile.onboarding_completed and not self.roadmap else None
            if requirement and not self.stop_followups and not pending:
                self.pending_information_field = requirement[0]
                reply = requirement[1]
            else:
                self.pending_information_field = None
                reply = self._change_reply(event)
        if pending:
            reply += "\n" + pending[0].question
        return self._finish(event, reply)

    def apply_structured_update(
        self,
        message: str,
        extraction: ExtractionResult,
        event_id: str,
        selected_target_id: str | None = None,
    ) -> AgentTurnResult:
        """Apply already-understood user facts without running an extractor or answer LLM.

        This is the domain boundary used by the stateless Opportunity A2A
        worker.  The caller may propose facts, but normalization, conflict
        handling, progress targeting and state transitions remain authoritative
        here.
        """
        event = self._event(event_id, message, "chat")
        self.profile = hydrate_score_fields_from_facts(
            StudentProfile.model_validate(self.profile.model_dump(mode="json"))
        )
        self.refresh_state()
        old_state = self.state.model_dump(mode="json")
        before = self.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        normalized = ProfileNormalizer().normalize(extraction.facts)
        extraction = extraction.model_copy(deep=True)
        extraction.facts = normalized
        self.last_extraction = extraction
        event.extraction = extraction.model_copy(deep=True)

        resolution = self.conflict_resolver.resolve(self.profile, normalized)
        self.profile, self.last_conflict_decisions = resolution.profile, resolution.decisions
        accepted_facts = [
            fact for fact, decision in zip(normalized, resolution.decisions)
            if decision.status == "applied" and fact.confidence >= 0.75 and not fact.needs_confirmation
        ]
        for fact, decision in zip(normalized, resolution.decisions):
            if decision.status == "confirmation_required" or fact.needs_confirmation or fact.confidence < 0.75:
                self._add_confirmation(PendingConfirmation(
                    source_event_id=event.event_id,
                    fact=fact,
                    question=f"是否确认将 {fact.field} 更新为 {fact.value}？",
                ))

        after = self.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        for field in after:
            self._log("profile." + field, before.get(field), after[field], event, "Opportunity Agent 校验的用户陈述")

        for update in extraction.progress_updates:
            update = update.model_copy(deep=True)
            if not update.target_id and not update.target_hint and selected_target_id:
                update.target_id = selected_target_id
            elif not update.target_id and not update.target_hint and self.last_referenced_target_id:
                update.target_id = self.last_referenced_target_id
            self._apply_progress(update, event)

        records = [item for item in self.task_progress if item.source_event_id == event.event_id]
        if len(records) == 1:
            self.last_referenced_target_id = records[0].target_id
        self._collect_stage_evidence(event, accepted_facts)
        self.refresh_state()
        old_roadmap = self.roadmap
        self._refresh_plan(before != after, event, allow_initial=True)
        for field, value in self.state.model_dump(mode="json").items():
            self._log("state." + field, old_state.get(field), value, event, "Opportunity Agent 接受的事实与进展")

        if old_roadmap is None and self.roadmap is not None:
            reply = f"已生成路线图 v{self.roadmap.version}。可以点击任务查看计划，并报告你的进展。"
        else:
            reply = self._change_reply(event)
        pending = [item for item in self.pending_confirmations if item.status == "pending"]
        if pending:
            reply += "\n" + pending[0].question
        return AgentTurnResult(
            reply=reply,
            progress_updates=records,
            state_changes=[item for item in self.state_transitions if item.source_event_id == event.event_id],
            pending_confirmations=pending,
            replan_required=self.replan_required,
            state_revision=self.state_revision,
        )

    def _add_confirmation(self, candidate: PendingConfirmation) -> None:
        signature = (candidate.fact.model_dump_json() if candidate.fact else candidate.progress_update.model_dump_json())
        for existing in self.pending_confirmations:
            previous = existing.fact.model_dump_json() if existing.fact else existing.progress_update.model_dump_json()
            if signature == previous:
                return
        self.pending_confirmations.append(candidate)

    def _apply_progress(self, update: ProgressUpdate, event: UserEvent, confirmed: bool = False) -> bool:
        decision = self.transition_engine.decide_progress(
            update, self.roadmap, self.task_progress, event, confirmed=confirmed)
        if not decision.accepted:
            choices = ([t for t in targets(self.roadmap) if t["target_id"] in (decision.candidate_target_ids or [])]
                       or [t for t in targets(self.roadmap) if t["target_kind"] == update.target_kind])
            self._add_confirmation(PendingConfirmation(source_event_id=event.event_id, progress_update=update,
                candidate_target_ids=[t["target_id"] for t in choices],
                question="请确认要更新的任务／事件及日期，或拒绝本次变更。"))
            return False
        record = decision.record
        previous = next((p for p in self.task_progress if p.target_id == record.target_id), None)
        old = previous.model_dump(mode="json", exclude={"updated_at", "source_event_id", "evidence", "confidence"}) if previous else None
        new = record.model_dump(mode="json", exclude={"updated_at", "source_event_id", "evidence", "confidence"})
        self.task_progress = [p for p in self.task_progress if p.target_id != record.target_id] + [record]
        self._collect_stage_evidence(event, confirmed=confirmed)
        self._log("progress." + record.target_id, old, new, event, f"用户操作：{update.action}", record.confidence, record.evidence)
        if old != new:
            self.replan_required = True
            self.planning_pending = False
        self.refresh_state()
        return old != new

    def on_progress(self, update: ProgressUpdate, event_id: str | None = None) -> AgentTurnResult:
        event = self._event(event_id, update.evidence or f"任务操作：{update.action}", "progress")
        event.extraction = ExtractionResult(intent="progress_update", progress_updates=[update.model_copy(deep=True)])
        before = self.state.model_dump(mode="json")
        if not resolve_targets(update, self.roadmap):
            raise ValueError("找不到该任务，请刷新后选择当前任务")
        self._apply_progress(update, event, confirmed=True)
        records = [item for item in self.task_progress if item.source_event_id == event.event_id]
        if len(records) == 1:
            self.last_referenced_target_id = records[0].target_id
        self._collect_stage_evidence(event, confirmed=True)
        self.refresh_state()
        for field, value in self.state.model_dump(mode="json").items():
            self._log("state." + field, before.get(field), value, event, "用户任务操作")
        return self._finish(event, self._change_reply(event))

    def on_timeline_fact(self, update: TimelineFactUpdate, event_id: str | None = None) -> AgentTurnResult:
        """Persist a user-authored historical-node entry as an explicit fact."""
        raw = f"时间轴补录：{update.node_title}；{update.detail}"
        event = self._event(event_id, raw, "timeline")
        if event.status == "processed":
            return self._finish(event, event.reply)
        fact = CandidateFact(
            field=update.fact_field, raw_value=[update.detail], normalized_value=[update.detail],
            operation="append", source="user_explicit", confidence=1.0,
            evidence=raw + (f"；发生日期：{update.occurred_on.isoformat()}" if update.occurred_on else ""),
        )
        event.extraction = ExtractionResult(intent="profile_update", facts=[fact], should_replan=True)
        before = self.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        old_state = self.state.model_dump(mode="json")
        resolution = self.conflict_resolver.resolve(self.profile, [fact])
        self.profile, self.last_conflict_decisions = resolution.profile, resolution.decisions
        after = self.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        changed = before != after
        if changed:
            self._log("profile." + fact.field, before.get(fact.field), after.get(fact.field), event,
                      "用户从时间轴补录", fact.confidence, fact.evidence)
        self._collect_stage_evidence(event, [fact] if changed else [], confirmed=True)
        self._refresh_plan(changed, event)
        for field, value in self.state.model_dump(mode="json").items():
            self._log("state." + field, old_state.get(field), value, event, "时间轴补录的显式事实", fact.confidence, fact.evidence)
        if changed:
            reply = f"已将“{update.detail}”加入{update.node_title}的{fact.field}事实。规划文章尚未改写；是否现在重新生成？"
        else:
            reply = "这条补录已存在，画像与规划保持不变。"
        return self._finish(event, reply)

    def confirm_change(self, confirmation_id: str, accept: bool, target_id: str | None = None,
                       postponed_to: date | None = None, event_id: str | None = None) -> AgentTurnResult:
        event = self._event(event_id, "确认变更" if accept else "拒绝变更", "confirmation")
        item = next((c for c in self.pending_confirmations if c.confirmation_id == confirmation_id), None)
        if item is None:
            raise ValueError("找不到待确认变更")
        if item.status != "pending":
            return self._finish(event, "这项变更已经处理，不会重复应用。")
        if not accept:
            item.status = "rejected"
            return self._finish(event, "已拒绝这项变更，当前状态保持不变。")
        before = self.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        old_state = self.state.model_dump(mode="json")
        accepted_facts = []
        if item.fact:
            fact = item.fact.model_copy(deep=True)
            fact.needs_confirmation, fact.confidence, fact.source = False, 1.0, "user_explicit"
            self.profile = self.conflict_resolver.resolve(self.profile, [fact]).profile
            accepted_facts.append(fact)
        if item.progress_update:
            update = item.progress_update.model_copy(deep=True)
            update.needs_confirmation, update.confidence = False, 1.0
            if target_id:
                if target_id not in item.candidate_target_ids:
                    raise ValueError("请选择该变更关联的任务")
                update.target_id = target_id
            if postponed_to:
                update.postponed_to = postponed_to
            if len(resolve_targets(update, self.roadmap)) != 1 or update.action == "postpone" and not update.postponed_to:
                raise ValueError("请明确选择任务；延期操作还需要填写日期")
            self._apply_progress(update, event, confirmed=True)
        item.status = "accepted"
        event.extraction = ExtractionResult(
            intent="progress_update" if item.progress_update else "profile_update",
            facts=accepted_facts,
            progress_updates=[item.progress_update] if item.progress_update else [],
        )
        self._collect_stage_evidence(event, accepted_facts, confirmed=True)
        after = self.profile.model_dump(mode="json", exclude={"facts", "change_history"})
        for field in after:
            self._log("profile." + field, before.get(field), after[field], event, "用户确认变更")
        self._refresh_plan(before != after, event)
        for field, value in self.state.model_dump(mode="json").items():
            self._log("state." + field, old_state.get(field), value, event, "用户确认的状态变化")
        return self._finish(event, self._change_reply(event))

    def _change_reply(self, event: UserEvent) -> str:
        changes = [c for c in self.state_transitions if c.source_event_id == event.event_id]
        labels = {"planned": "未开始", "in_progress": "进行中", "completed": "已完成", "cancelled": "已取消"}
        lines = []
        for record in self.task_progress:
            if record.source_event_id == event.event_id:
                lines.append(f"“{record.title}”已更新为{labels[record.status]}" + (f"，延期至 {record.postponed_to}" if record.postponed_to else "") + "。")
                timeline = self.roadmap.timeline if self.roadmap else None
                projected = next((task for phase in (timeline.phases if timeline else [])
                                  for task in (phase.plan.tasks if phase.plan else [])
                                  if task.progress_key == record.target_id), None)
                projected = projected or next((item for item in (timeline.events if timeline else [])
                                                if item.progress_key == record.target_id), None)
                if projected and projected.risk_status == "ahead_of_schedule":
                    current = next((phase.title for phase in timeline.phases if phase.time_status == "current"), "当前日期所在阶段")
                    lines.append(f"时间轴已标记为“提前完成”；紫色当前位置仍停留在{current}。")
                elif projected and projected.risk_status == "schedule_conflict":
                    lines.append("新的日期超出原阶段边界，系统已保留该日期并标记计划冲突。")
        for change in changes:
            if change.field.startswith("profile."):
                name = change.field.removeprefix("profile.")
                if name in {"toefl_score", "ielts_score", "gre_score"}:
                    lines.append(f"已记录 {name.split('_')[0].upper()} {change.new_value}，是否满足目标项目要求仍待项目官网核验。")
        if not lines:
            lines.append("信息已记录，画像与时间轴状态已同步。" if changes else "没有确认到新的状态变化，现有进度保持不变。")
        if self.job_recommendations and self.state.career in {"internship_search", "full_time_search"}:
            lines.append(f"已更新 {len(self.job_recommendations)} 个本地匹配岗位，可在侧栏查看依据。")
        if self.replan_required:
            lines.append("原规划文章已保留；资料或进展已更新，可点击“重新规划”。")
        unfinished = next((t for p in (self.roadmap.timeline.phases if self.roadmap and self.roadmap.timeline else [])
                           for t in (p.plan.tasks if p.plan else []) if t.execution_status not in {"completed", "cancelled"}), None)
        if unfinished:
            lines.append(f"下一步可推进：{unfinished.title}。")
        return "\n".join(lines)

    def record_assistant_message(self, message: str, event_id: str | None = None) -> None:
        item = ChatMessage(role="assistant", content=message, created_at=datetime.now(timezone.utc),
                           processing_status="processed", event_id=event_id)
        self.conversation_messages.append(item)
        self.recent_messages = [*self.recent_messages, item][-12:]

    def _apply_pending_answer_context(self, message: str, facts: list[CandidateFact]) -> list[CandidateFact]:
        """Interpret a short reply using the exact question asked in the last turn."""
        field = self.pending_information_field
        # Older browser snapshots did not persist the pending field. A bare
        # four-digit year is still unambiguous enough when graduation is the
        # only missing onboarding value.
        if field is None and self.profile.graduation_year is None:
            import re

            if re.fullmatch(r"(?:预计)?\s*20\d{2}\s*(?:年)?", message.strip()):
                field = "graduation_year"
        if not field or any(fact.field == field or (field == "gpa_or_rank" and fact.field in {"gpa", "class_rank"}) for fact in facts):
            return facts
        normalized = message.strip()
        if field == "graduation_year":
            import re

            match = re.fullmatch(r"(?:预计)?\s*(20\d{2})\s*(?:年)?", normalized)
            if match:
                return [*facts, CandidateFact(
                    field="graduation_year", raw_value=normalized, normalized_value=int(match.group(1)),
                    source="conversation", confidence=0.99, evidence=message,
                )]
        if field == "language_preparation" and normalized:
            negative = any(word in normalized for word in ("没", "未", "没有", "暂不"))
            return [*facts, CandidateFact(
                field="language_preparation", raw_value=normalized, normalized_value=not negative,
                source="conversation", confidence=0.9, evidence=message,
            )]
        if field == "research_activity" and normalized:
            no_research = any(word in normalized for word in ("没", "未", "没有", "暂无", "无"))
            return [*facts, CandidateFact(
                field="research_activity", raw_value=normalized,
                normalized_value="none" if no_research else normalized,
                source="conversation", confidence=0.9, evidence=message,
            )]
        return facts

    def generate_roadmap(self) -> Roadmap:
        self.roadmap = self.planner.generate(self.profile, self.state, self.roadmap, "manual_regeneration")
        self.job_recommendations = recommend_jobs(self.profile, self.state, self.repository)
        return self.roadmap

    def _timeline_job_recommendations(self) -> list[JobRecommendation]:
        if not self.roadmap or not self.roadmap.timeline:
            return []
        results: list[JobRecommendation] = []
        for phase in self.roadmap.timeline.phases:
            if phase.plan:
                results.extend(phase.plan.recommendations)
        deduped = {item.job_id: item for item in results}
        return sorted(deduped.values(), key=lambda item: item.score, reverse=True)

    def on_external_event(self, event: ExternalEvent, today: date) -> NotificationDecision:
        if self.roadmap is None:
            self.roadmap = self.planner.generate(self.profile, self.state, revision_reason="external_event_bootstrap")
        self.roadmap = replan_deadline(self.roadmap, event.new_deadline)
        return decide_deadline_notification(event, self.profile, self.state, today)
