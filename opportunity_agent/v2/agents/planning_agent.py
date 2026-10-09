"""Read-only V2 Planning Agent backed by the V1 planning domain rules."""
from __future__ import annotations

from datetime import date
from urllib.parse import urlparse

from ...llm_client import LLMClient
from ...models import OfficialResearchResult, OfficialRequirement, OfficialSource, Roadmap, TaskProgress, UserState
from ...planning import _fallback_article, _timeline_to_milestones, build_roadmap
from ...progress import inherit_timeline_progress, refresh_progress
from ...roadmap_article_skill import RoadmapArticleSkill, build_article_context
from ..core.telemetry import span
from .contracts import PlanResult, ResearchResult, SuccessCriteria, research_revision
from ..research.quality import usable, field_supported
from .domain_request import DomainRequest
from .profile_agent import profile_from_request


class _EmptyJobs:
    """The V1 skill only needs this read method; V2 has no local JSON job store."""

    def list_jobs(self) -> list:
        return []


def _verified_research(raw: dict) -> OfficialResearchResult:
    """Preserve evidence scope and admit only field-level verified requirements."""
    research = ResearchResult.model_validate(raw) if raw else ResearchResult()
    sources: list[OfficialSource] = []
    requirements: list[OfficialRequirement] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    criteria = SuccessCriteria(accepted_authorities=["official"], evidence_required=True)
    for program in research.programs:
        evidence_by_id = {item.evidence_id: item for item in [*research.evidence, *program.evidence]}
        for evidence in evidence_by_id.values():
            if (not usable(evidence, criteria)
                    or evidence.source_id in seen):
                continue
            seen.add(evidence.source_id)
            url = str(evidence.url)
            exact = evidence.program_match == "exact"
            sources.append(OfficialSource(
                source_id=evidence.source_id, university=program.university, program=program.program,
                intake=evidence.intake or program.intake,
                title=evidence.title or f"{program.university} {program.program}", url=url,
                verified_domain=urlparse(url).hostname or "", evidence_excerpt=evidence.excerpt,
                content_hash=evidence.content_hash,
                scope="program" if exact else "university_wide",
                program_match="exact" if exact else "generic",
            ))
        verified_fields: set[str] = set()
        for fact in program.facts:
            field = "gre" if fact.field == "gre_policy" else fact.field
            if fact.verification_status != "verified" or field not in {
                    "deadline", "gre", "toefl", "ielts", "prerequisite", "tuition", "material"}:
                if fact.verification_status in {"unknown", "conflicting", "stale"}:
                    unresolved.append(f"{program.university} {program.program} 的 {field} 尚未核实")
                continue
            supporting = [evidence_by_id[item] for item in fact.evidence_ids if item in evidence_by_id]
            supporting = [item for item in supporting if usable(item, criteria)
                          and item.program_match == "exact"
                          and item.intake.casefold() == program.intake.casefold()
                          and fact.field in item.supports_fields]
            if not field_supported(program, fact.field, criteria):
                supporting = []
            if not supporting:
                unresolved.append(f"{program.university} {program.program} 的 {field} 缺少项目级对应证据")
                continue
            verified_fields.add(field)
            qualifier = ("required" if str(fact.value) == "required" else
                         "optional" if str(fact.value) in {"optional", "not_required"} else
                         "not_accepted" if str(fact.value) == "not_accepted" else "unknown")
            requirements.append(OfficialRequirement(
                field=field, value=str(fact.value), qualifier=qualifier,
                source_ids=list(dict.fromkeys(item.source_id for item in supporting)),
                confidence=min(item.relevance_score or 0.7 for item in supporting),
                scope="program", program_match="exact",
            ))
        for required in program.required_fields:
            normalized = "gre" if required == "gre_policy" else required
            if normalized not in verified_fields:
                unresolved.append(f"{program.university} {program.program} 的 {normalized} 待核验")
    for item in research.missing_items:
        unresolved.append(str(item.get("reason") or item.get("field") or item.get("kind") or "研究信息待补充"))
    return OfficialResearchResult(
        sources=sources,
        requirements=requirements,
        unresolved_questions=list(dict.fromkeys(unresolved)),
    )


def _plan_kind(message: str) -> str:
    full_terms = ("完整规划", "申请规划", "申请计划", "时间线", "路线图", "重新规划", "重规划")
    advice_terms = ("sop", "文书", "推荐信", "cv", "简历", "essay", "选校策略", "怎么准备", "如何准备")
    lowered = message.casefold()
    if any(term in lowered for term in full_terms):
        return "roadmap"
    return "advice" if any(term in lowered for term in advice_terms) else "roadmap"


def _fallback_advice(message: str, roadmap: Roadmap) -> str:
    next_actions = [phase.plan.summary for phase in roadmap.timeline.phases if phase.plan][:3] if roadmap.timeline else []
    actions = "\n".join(f"- {item}" for item in next_actions)
    return (
        f"## 针对本次问题的建议\n\n你问的是：{message}\n\n"
        "建议先以已确认画像和当前申请进度为基础，整理对应材料的真实经历、行动和可验证成果；"
        "涉及学校或项目的截止日期、字数、GRE、语言及材料要求时，应逐项核对官网，当前未核实的信息保留为待核验。"
        + (f"\n\n## 可衔接的当前行动\n\n{actions}" if actions else "")
    )


class PlanningAgent:
    """Build an actionable draft; applying it belongs to the approval service."""

    def execute(self, request: DomainRequest) -> PlanResult:
        profile = profile_from_request(request)
        plan_kind = _plan_kind(request.message)
        if request.current_plan.get("roadmap"):
            try:
                current = Roadmap.model_validate(request.current_plan["roadmap"])
            except Exception:
                current = None
        else:
            current = None
        with span("a2a.planning.roadmap", user_id=request.user_id):
            roadmap = build_roadmap(profile, version=request.current_plan_version + 1,
                                    revision_reason="user_requested_replan" if current else "initial_profile",
                                    repository=_EmptyJobs())
        if not roadmap.supported or not roadmap.timeline:
            return PlanResult(
                plan_kind=plan_kind,
                status="no_plan",
                assumptions=[roadmap.support_message or "画像尚不足以生成计划"],
                input_versions={"profile": request.profile_version, "plan": request.current_plan_version},
            )

        # Persisted task state is authoritative, including progress made after
        # the previous roadmap JSON was generated.
        if current:
            inherit_timeline_progress(current, roadmap.timeline)
        records = [TaskProgress(target_id=task["stable_key"], title=task["title"],
                                target_kind="event" if task["stable_key"].startswith("event:") else "task",
                                category=task.get("category", "application"), status=task["status"],
                                evidence=task.get("evidence") or "", source_event_id="persisted")
                   for task in request.current_tasks if task.get("status") in
                   {"planned", "in_progress", "completed", "cancelled"}]
        refresh_progress(roadmap, records)
        roadmap.milestones = _timeline_to_milestones(roadmap.timeline)
        # build_roadmap creates its fallback before persisted progress is applied.
        # Rebuild it so completed/cancelled work is not presented as unfinished.
        roadmap.article = _fallback_article(profile, roadmap.timeline)
        if plan_kind == "advice":
            roadmap.article = _fallback_advice(request.message, roadmap)
        research = _verified_research(request.research_result)
        research_contract = ResearchResult.model_validate(request.research_result) if request.research_result else ResearchResult()
        roadmap.official_sources = research.sources
        roadmap.verified_requirements = research.requirements
        roadmap.unresolved_requirements = list(research.unresolved_questions)
        if not research.sources:
            roadmap.unresolved_requirements.append("具体项目要求尚无可靠来源，需到官网核验")
        roadmap.unresolved_requirements = list(dict.fromkeys(roadmap.unresolved_requirements))

        llm_budget = max(1, min(90, int(request.remaining_budget_seconds)))
        client = LLMClient(timeout_seconds=llm_budget, retries=0)
        error = None
        if client.enabled:
            try:
                state = UserState.model_validate(request.relevant_memory.get("derived_state", {}))
                context = build_article_context(profile, state, roadmap.timeline, research,
                                                roadmap.revision_reason,
                                                roadmap.timeline.model_dump(mode="json"), date.today().isoformat())
                context["preference_memory"] = getattr(request, "preference_memory", {})
                context["turn_preferences"] = getattr(request, "turn_preferences", [])
                roadmap.article = RoadmapArticleSkill(client, timeout_seconds=llm_budget).generate_markdown(
                    context, user_request=request.message, plan_kind=plan_kind,
                )
                roadmap.generation_mode = "qwen"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        if research.sources:
            references = "\n".join(f"- [{item.source_id}] {item.university} {item.program}：{item.url}（{item.evidence_excerpt[:180]}）"
                                   for item in research.sources)
            roadmap.article += "\n\n附录：已有项目证据（具体条件以对应原文为准）\n" + references

        tasks = []
        for phase in roadmap.timeline.phases:
            for task in phase.plan.tasks if phase.plan else []:
                tasks.append({"stable_key": task.progress_key, "phase_id": phase.phase_id,
                              **task.model_dump(mode="json")})
        for event in roadmap.timeline.events:
            if event.kind in {"winter_break", "summer_break"}:
                continue
            tasks.append({"stable_key": event.progress_key, "phase_id": event.phase_id,
                          "target_kind": "event", "title": event.title,
                          "category": "language_exam" if event.kind == "exam" else event.kind,
                          "due_date": event.event_date.isoformat(), "reason": event.detail,
                          "execution_status": event.execution_status})
        is_roadmap = plan_kind == "roadmap"
        accepted_source_ids = {item.source_id for item in research.sources}
        evidence_ids = list(dict.fromkeys(
            item.evidence_id
            for item in [*research_contract.evidence,
                         *(evidence for program in research_contract.programs for evidence in program.evidence)]
            if item.source_id in accepted_source_ids
        ))
        return PlanResult(
            plan_kind=plan_kind,
            article_markdown=roadmap.article,
            timeline=([*({"kind": "phase", **phase.model_dump(mode="json")}
                         for phase in roadmap.timeline.phases),
                       *({"kind": "event", **event.model_dump(mode="json")}
                         for event in roadmap.timeline.events)] if is_roadmap else []),
            recommendations=[phase.plan.summary for phase in roadmap.timeline.phases if phase.plan],
            roadmap=roadmap.model_dump(mode="json") if is_roadmap else {},
            tasks=tasks if is_roadmap else [],
            evidence_ids=evidence_ids,
            assumptions=roadmap.unresolved_requirements,
            input_versions={
                "profile": request.profile_version,
                "plan": request.current_plan_version,
                "research_schema": research_contract.schema_version if request.research_result else None,
                "research_revision": research_revision(research_contract) if request.research_result else None,
            },
            generation_mode=roadmap.generation_mode, error=error, status="complete",
        )
