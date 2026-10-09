from __future__ import annotations

import json
import os
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .domain_knowledge import DOMAIN_KNOWLEDGE, identify_domain, knowledge_for_profile, validate_profile_domain
from .config import roadmap_article_max_tokens, roadmap_timeout_seconds
from .llm_client import LLMClient
from .models import (Milestone, OfficialResearchResult, PlanTask, PlanningTimeline, Roadmap,
                     StudentProfile, TargetProgram, UserState, legacy_target_program_pairs)
from .official_research import OfficialResearchTools, deterministic_program_research
from .planning_skills import TimelineComposerSkill
from .progress import inherit_timeline_progress
from .repository import LocalRepository
from .roadmap_article_skill import RoadmapArticleSkill, build_article_context
from .timeline import build_timeline_skeleton


class _RoadmapPromptTemplate(str):
    """Keeps third-party/test callers using the pre-official-source slots working."""
    def format(self, *args, **kwargs):  # type: ignore[override]
        kwargs.setdefault("official_requirements_json", "[]")
        kwargs.setdefault("official_sources_json", "[]")
        kwargs.setdefault("unresolved_questions_json", "[]")
        return super().format(*args, **kwargs)


ROADMAP_USER_PROMPT_TEMPLATE = _RoadmapPromptTemplate("""TODAY: {today}
CURRENT_PROFILE:
{profile_json}
AUDIT_FACTS:
{facts_json}
CURRENT_STATE:
{state_json}
CURRENT_ROADMAP:
{roadmap_json}
REVISION_REASON: {revision_reason}
OFFICIAL_REQUIREMENTS:
{official_requirements_json}
OFFICIAL_SOURCES:
{official_sources_json}
UNRESOLVED_OFFICIAL_QUESTIONS:
{unresolved_questions_json}
""")


def build_roadmap(profile: StudentProfile, state: UserState | None = None,
                  application_deadline: date | None = None, version: int = 1,
                  revision_reason: str = "initial_profile", today: date | None = None,
                  repository: LocalRepository | None = None) -> Roadmap:
    """Offline planner using the same timeline and skill contracts as online mode."""
    state = state or UserState()
    timeline = build_timeline_skeleton(profile, today=today)
    goal = _goal(profile)
    if not timeline.supported:
        # Pre-onboarding callers belong to the V1 compatibility path. The new
        # onboarding API always marks explicit profiles completed and therefore
        # never uses this adapter for unsupported majors.
        if not profile.onboarding_completed:
            planning_profile = profile.model_copy(deep=True)
            planning_profile.major = "Computer Science"
            planning_profile.target_fields = ["Artificial Intelligence"]
            timeline = build_timeline_skeleton(planning_profile, today=today)
            timeline.support_message = "V1 compatibility timeline; submit onboarding for domain validation."
            timeline = TimelineComposerSkill().generate(planning_profile, state, timeline, repository or LocalRepository())
            if application_deadline:
                _set_application_deadline(timeline, application_deadline)
            return Roadmap(user_id=profile.user_id, goal=goal,
                           article=_fallback_article(profile, timeline, DOMAIN_KNOWLEDGE["computer_science"]),
                           milestones=_timeline_to_milestones(timeline), timeline=timeline,
                           supported=True, support_message=timeline.support_message, version=version,
                           revision_reason=revision_reason, generation_mode="rule_fallback")
        return Roadmap(user_id=profile.user_id, goal=goal, article=timeline.support_message,
                       milestones=[], timeline=timeline, supported=False,
                       support_message=timeline.support_message, version=version,
                       revision_reason=revision_reason, generation_mode="rule_fallback")
    timeline = TimelineComposerSkill().generate(profile, state, timeline, repository or LocalRepository())
    if application_deadline:
        _set_application_deadline(timeline, application_deadline)
    return Roadmap(user_id=profile.user_id, goal=goal,
                   article=_fallback_article(profile, timeline),
                   milestones=_timeline_to_milestones(timeline), timeline=timeline,
                   supported=True, support_message=timeline.support_message, version=version,
                   revision_reason=revision_reason, generation_mode="rule_fallback")


@dataclass
class ModelScopeRoadmapPlanner:
    api_key: str | None = None
    model: str | None = None
    endpoint: str | None = None
    timeout_seconds: int | None = None
    last_error: str | None = None
    llm_client: LLMClient | None = None
    repository: LocalRepository | None = None
    official_tools: OfficialResearchTools | None = None
    article_skill: RoadmapArticleSkill | None = None

    def __post_init__(self) -> None:
        self.timeout_seconds = self.timeout_seconds or roadmap_timeout_seconds()
        self.llm_client = self.llm_client or LLMClient(api_key=self.api_key, model=self.model,
            base_url=self.endpoint, timeout_seconds=self.timeout_seconds, retries=0)
        self.api_key, self.model, self.endpoint = self.llm_client.api_key, self.llm_client.model, self.llm_client.base_url
        self.repository = self.repository or LocalRepository()
        self.official_tools = self.official_tools or OfficialResearchTools()

    def generate(self, profile: StudentProfile, state: UserState, current: Roadmap | None,
                 revision_reason: str) -> Roadmap | None:
        if not self.llm_client or not self.llm_client.enabled:
            self.last_error = "MODELSCOPE_API_KEY is not configured"
            return None
        if not validate_profile_domain(profile)[0]:
            self.last_error = None
            return build_roadmap(profile, state, version=(current.version + 1 if current else 1),
                                 revision_reason=revision_reason, repository=self.repository)
        timeline = build_timeline_skeleton(profile)
        # Deterministic skill content is immediate and stable. Deployments may
        # explicitly enable per-component LLM calls; the default performs one
        # final article request, avoiding 8 network round trips per form.
        component_client = self.llm_client if os.getenv("PLANNING_LLM_COMPONENTS") == "1" else None
        composer = TimelineComposerSkill(component_client)
        timeline = composer.generate(profile, state, timeline, self.repository)
        inherit_timeline_progress(current, timeline)
        fallback_article = _fallback_article(profile, timeline)
        article_error = None
        article_generated = False
        research = self._research(profile)
        if current:
            # Keep evidence that the user explicitly refreshed and reviewed in
            # the sidebar. A later variable search must not erase UIUC simply
            # because it returned a different first page.
            valid_targets = {(pair.school.casefold(), pair.program.casefold()) for pair in _target_pairs(profile)}
            previous_sources, revoked_sources = self.official_tools.validate_sources(
                current.official_sources, list(valid_targets)) if self.official_tools else (current.official_sources, [])
            research.revoked_sources.extend(revoked_sources)
            known_sources = {item.source_id for item in research.sources}
            research.sources.extend(item.model_copy(deep=True) for item in previous_sources
                                    if item.source_id not in known_sources and item.status != "revoked"
                                    and (item.university.casefold(), item.program.casefold()) in valid_targets)
            active_source_ids = {item.source_id for item in research.sources}
            known_requirements = {(item.field, item.value, tuple(item.source_ids)) for item in research.requirements}
            research.requirements.extend(item.model_copy(deep=True) for item in current.verified_requirements
                                         if (item.field, item.value, tuple(item.source_ids)) not in known_requirements
                                         and any(source_id in active_source_ids for source_id in item.source_ids))
            research.unresolved_questions = list(dict.fromkeys([*research.unresolved_questions,
                                                                  *current.unresolved_requirements]))
        article_context = build_article_context(
            profile, state, timeline, research, revision_reason,
            _article_timeline_context(timeline), date.today().isoformat(),
        )
        if self.article_skill is None:
            self.article_skill = RoadmapArticleSkill(
                self.llm_client, timeout_seconds=self.timeout_seconds,
                max_tokens=roadmap_article_max_tokens(),
            )
        else:
            # Tests and hosts may replace the shared client after construction.
            self.article_skill.llm_client = self.llm_client
            self.article_skill.timeout_seconds = self.timeout_seconds
            self.article_skill.max_tokens = roadmap_article_max_tokens()
        try:
            article = self.article_skill.generate(article_context)
            article_generated = True
        except Exception as exc:
            article = fallback_article
            article_error = self.article_skill.last_error or f"{type(exc).__name__}: {exc}"
        generation_mode = "qwen" if article_generated else "rule_fallback"
        errors = [*composer.component_errors.values(), *([article_error] if article_error else [])]
        self.last_error = "；".join(dict.fromkeys(errors)) or None
        return Roadmap(user_id=profile.user_id, goal=_goal(profile), article=article,
                       milestones=_timeline_to_milestones(timeline), timeline=timeline,
                       supported=True, support_message=timeline.support_message,
                       version=(current.version + 1 if current else 1), revision_reason=revision_reason,
                       generation_mode=generation_mode, generated_at=datetime.now(timezone.utc),
                       official_sources=research.sources, verified_requirements=research.requirements,
                       unresolved_requirements=research.unresolved_questions,
                       revoked_official_sources=research.revoked_sources)

    def _research(self, profile: StudentProfile, *, fresh_turn: bool = True) -> OfficialResearchResult:
        """Ask the configured model to research only explicit school/program targets."""
        if not self.official_tools or not self.official_tools.enabled:
            return OfficialResearchResult(unresolved_questions=["官网搜索未配置；未使用模型记忆补充项目要求"])
        if fresh_turn:
            # A fresh button click always calls Tavily/page-read again for every
            # explicit target.  This resets in-memory observations only; it
            # does not delete the auditable local official-source cache.
            begin_turn = getattr(self.official_tools, "begin_research_turn", None)
            if callable(begin_turn):
                begin_turn()
        pairs = _target_pairs(profile)
        if not pairs:
            return OfficialResearchResult(unresolved_questions=["尚未提供目标学校和项目，无法查询官网"])
        questions = ["GRE policy", "English language test policy", "application deadline",
                     "required application materials", "personal statement SOP essay short answer word limit"]
        # The profile is the user's explicit research scope. Do not silently
        # omit a target school merely to reduce a Tavily batch size.
        # Planning has a known checklist and must cover every explicit target.
        # Running it through a model-controlled six-call loop can leave the
        # second school or its materials page unvisited. Interactive free-form
        # consultations still use LLMToolRunner; planning uses the same safe
        # tools deterministically to obtain complete coverage.
        results = []
        for pair in pairs:
            if not pair.program:
                results.append(OfficialResearchResult(
                    unresolved_questions=[f"{pair.school}：请在资料表确认目标项目后再做项目级官网核验"]
                ))
                continue
            results.append(deterministic_program_research(
                self.official_tools, pair.school, pair.program,
                str(profile.planned_enrollment_year or ""), questions))
        result = OfficialResearchResult()
        for item in results:
            result.sources.extend(source for source in item.sources if source.source_id not in {s.source_id for s in result.sources})
            result.requirements.extend(requirement for requirement in item.requirements if requirement not in result.requirements)
            result.unresolved_questions.extend(item.unresolved_questions)
            result.tool_trace.extend(item.tool_trace)
            result.revoked_sources.extend(item.revoked_sources)
        result.unresolved_questions = list(dict.fromkeys(result.unresolved_questions))
        if not result.requirements:
            result.unresolved_questions = list(dict.fromkeys([*result.unresolved_questions, *questions]))
        return result


def _target_pairs(profile: StudentProfile) -> list[TargetProgram]:
    pairs = list(profile.target_program_choices) if profile.target_program_choices else \
        legacy_target_program_pairs(profile.target_schools, profile.target_programs)[0]
    # Historic form/resume values sometimes held “MSAII, MSML” in one cell.
    # Querying it as one synthetic program defeats identity matching, so split
    # only explicit list separators while keeping one school--program query per
    # resulting program.
    expanded: list[TargetProgram] = []
    for pair in pairs:
        programs = [part.strip() for part in re.split(r"[,，、;；\n]+", pair.program) if part.strip()]
        expanded.extend(TargetProgram(school=pair.school, program=program) for program in programs or [""])
    return expanded


def _validate_planning_article(article: str) -> None:
    # The article is the detailed counterpart to the deterministic task cards.
    # A sentence-length completion must not replace a user's existing plan.
    if len(article.strip()) < 600:
        raise ValueError("planning article is too short")


def planning_error_details(error: str | None) -> tuple[str | None, str | None]:
    """Return a stable UI code and a safe, actionable Chinese explanation."""
    if not error:
        return None, None
    lowered = error.casefold()
    if "too short" in lowered:
        return "response_too_short", "AI 连续两次返回的规划少于 600 个字符，未达到详细文章要求"
    if "planning article quality failed" in lowered:
        details = error.split("planning article quality failed:", 1)[-1].strip()
        return "article_sections_incomplete", f"规划文章章节质量校验失败：{details}"
    if "roadmap skill" in lowered:
        return "skill_load_failed", "路线图文章 Skill 无法读取或格式无效"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout", "AI 服务未在规划时限内返回"
    if "401" in lowered or "403" in lowered or "unauthorized" in lowered or "api key" in lowered:
        return "authentication", "AI 服务拒绝了当前 API Key"
    if "ssl" in lowered or "eof" in lowered or "connection" in lowered or "urlerror" in lowered:
        return "connection", "AI 服务连接中断"
    return "generation_failed", "AI 返回内容无法通过规划质量校验"


class HybridRoadmapPlanner:
    def __init__(self, model_planner: ModelScopeRoadmapPlanner | None = None) -> None:
        self.model_planner = model_planner or ModelScopeRoadmapPlanner()
        self.last_mode = "rule_fallback"
        self.last_error: str | None = None

    def generate(self, profile: StudentProfile, state: UserState, current: Roadmap | None = None,
                 revision_reason: str = "initial_profile") -> Roadmap:
        generated = self.model_planner.generate(profile, state, current, revision_reason)
        if generated is not None:
            self.last_mode, self.last_error = generated.generation_mode, self.model_planner.last_error
            return generated
        self.last_mode, self.last_error = "rule_fallback", self.model_planner.last_error
        return build_roadmap(profile, state, version=(current.version + 1 if current else 1),
                             revision_reason=revision_reason)


def replan_deadline(roadmap: Roadmap, new_deadline: date) -> Roadmap:
    updated = roadmap.model_copy(deep=True)
    updated.version += 1
    updated.revision_reason = f"official deadline changed to {new_deadline.isoformat()}"
    if updated.timeline:
        _set_application_deadline(updated.timeline, new_deadline)
    for milestone in updated.milestones:
        for task in milestone.tasks:
            if task.task_id == "submit_application":
                task.due_date, task.reason, task.source = new_deadline, "根据最新官方项目截止日期调整。", "official program deadline event"
            elif task.task_id == "request_letters":
                task.due_date = _date_with_day(new_deadline.year, max(1, new_deadline.month - 2), 15)
            elif task.task_id == "shortlist_programs":
                task.due_date = _date_with_day(new_deadline.year, max(1, new_deadline.month - 3), 30)
    return updated


def _timeline_to_milestones(timeline: PlanningTimeline) -> list[Milestone]:
    milestones: list[Milestone] = []
    for phase in timeline.phases:
        tasks = [task.model_copy(deep=True) for task in (phase.plan.tasks if phase.plan else [])]
        if phase.phase_id == "background":
            first_by_category: dict[str, PlanTask] = {}
            for task in tasks:
                first_by_category.setdefault(task.category, task)
            for category, task_id in (("academic", "maintain_gpa"), ("language", "language_test"), ("research", "research_progress")):
                if category in first_by_category:
                    first_by_category[category].task_id = task_id
        elif phase.phase_id == "materials" and tasks:
            tasks[0].task_id = "shortlist_programs"
            for task in tasks[1:]:
                if task.task_id == "materials_recommenders":
                    task.task_id = "request_letters"
                    task.depends_on = ["research_progress"]
        elif phase.phase_id == "application" and tasks:
            tasks[0].task_id, tasks[0].depends_on = "submit_application", ["shortlist_programs", "request_letters"]
        milestones.append(Milestone(milestone_id=phase.phase_id, title=phase.title, tasks=tasks))
    return milestones


def _fallback_article(profile: StudentProfile, timeline: PlanningTimeline, fallback_knowledge=None) -> str:
    kb = knowledge_for_profile(profile) or fallback_knowledge
    if kb is None:
        return timeline.support_message
    schools = "、".join(profile.target_schools) or "尚未确定的目标学校"
    programs = "、".join(profile.target_programs) or "尚未确定的具体项目"
    countries = "、".join(profile.target_countries) or "尚未确定的目标国家"
    completed = "、".join(profile.completed_courses) or "尚未填写已修课程"
    skills = "、".join([*profile.skills, *profile.hardware_skills]) or "尚未填写技能"
    experiences = "；".join([*profile.research_experiences, *profile.project_experiences,
        *profile.competition_experiences, *profile.internship_experiences]) or "尚未填写科研、项目、竞赛或实习经历"
    paragraphs = []
    for phase in timeline.phases:
        plan = phase.plan
        tasks = "；".join(
            f"{task.title}（状态：{task.execution_status}；{task.reason}）"
            for task in (plan.tasks if plan else [])
        )
        paragraphs.append(f"【{phase.title}｜{phase.start_date.isoformat()} 至 {phase.end_date.isoformat()}】\n"
            f"这一阶段围绕你的 {kb.domain} 目标处理当前差距。{plan.summary if plan else ''}"
            f"具体行动：{tasks or '按阶段边界补充任务'}。每月检查成绩单、代码仓库、实验记录、CV 条目或申请材料等证据。")
    return (f"一、目标与当前背景\n你目前就读于{profile.school or '未填写学校'}的{profile.major or '未填写专业'}，"
        f"计划申请{countries}的{profile.target_degree or '研究生'}，目标方向是{'、'.join(profile.target_fields) or kb.domain}。"
        f"目标学校/项目为{schools}、{programs}。当前成绩信息：{_score_summary(profile)}；已修课程：{completed}；技能：{skills}；经历：{experiences}。"
        "这些信息用于确定任务优先级，而不是替你虚构竞争力。\n\n"
        f"二、与目标方向的差距\n{kb.domain} 的内部种子知识建议重点检查{'、'.join(kb.prerequisites)}。"
        f"你应把已修课程逐项映射到这些基础，再用{'、'.join(kb.projects)}中的一种形式形成可验证作品。"
        f"当前无法仅凭画像断言你满足{schools}的先修课或分数门槛；具体先修课、语言门槛、学费与截止日期均待项目官网核验。\n\n"
        "三、按确定性时间轴执行\n" + "\n\n".join(paragraphs) +
        "\n\n四、语言与申请事实核验\n" + _language_policy(profile) +
        " 对每个目标项目建立官方核验列：先修课、最低/建议语言分数、GRE 政策、学费、奖学金、截止日期和材料要求；"
        "没有官方证据时保留“待项目官网核验”，不能直接断言满足或不满足。\n\n"
        "五、复盘机制\n每四周更新一次成绩、课程、语言、项目和投递证据。发生考试日期、毕业时间、目标方向或官方截止日期变化时，"
        "只重排受影响阶段；日期边界由时间轴引擎维护，内容生成不能改动时间顺序。")


def _language_policy(profile: StudentProfile) -> str:
    scores = ([f"TOEFL {profile.toefl_score}"] if profile.toefl_score is not None else []) + \
             ([f"IELTS {profile.ielts_score:g}"] if profile.ielts_score is not None else []) + \
             ([f"GRE {profile.gre_score}"] if profile.gre_score is not None else [])
    if scores:
        return f"你已记录{'、'.join(scores)}，先对照每个目标项目的官方门槛，不默认重复考试。"
    if profile.exam_plan:
        return f"你计划在 {profile.exam_plan.next_exam_date.isoformat()} 参加 {profile.exam_plan.exam_type}，该日期已作为旗标写入时间轴。"
    return "当前未记录语言成绩或下一次考试日期，应先做水平诊断，但系统不会虚构考试时间。"


def _score_summary(profile: StudentProfile) -> str:
    if profile.gpa is not None:
        result = f"GPA {profile.gpa:.2f}/4.0"
    elif profile.gpa_raw is not None and profile.gpa_scale is not None:
        result = f"GPA {profile.gpa_raw:g}/{profile.gpa_scale:g}（未强制换算）"
    else:
        result = "GPA 待补充"
    if profile.class_rank:
        result += f"，排名 {profile.class_rank}"
    if profile.toefl_score is not None:
        result += f"，TOEFL {profile.toefl_score}"
    return result


def _goal(profile: StudentProfile) -> str:
    return f"申请 {', '.join(profile.target_countries or ['目标国家'])} {profile.target_degree or '研究生'} {', '.join(profile.target_fields or ['相关工科专业'])}"


def _set_application_deadline(timeline: PlanningTimeline, deadline: date) -> None:
    for event in timeline.events:
        if event.event_id == "application_start":
            event.detail = f"官方截止日期事件更新为 {deadline.isoformat()}"
    for phase in timeline.phases:
        if phase.phase_id == "application":
            phase.end_date = deadline
            if phase.plan:
                for task in phase.plan.tasks:
                    task.due_date = deadline


def _article_timeline_context(timeline: PlanningTimeline) -> dict:
    """Keep the article prompt focused; full task objects add latency but no value."""
    return {
        "domain": timeline.domain,
        "graduation_date": timeline.graduation_date,
        "enrollment_date": timeline.enrollment_date,
        "phases": [
            {
                "id": phase.phase_id,
                "title": phase.title,
                "start": phase.start_date,
                "end": phase.end_date,
                "status": phase.status,
                "execution_status": phase.execution_status,
                "plan_summary": phase.plan.summary if phase.plan else "",
                "tasks": [
                    {"title": task.title, "due_date": task.due_date, "reason": task.reason,
                     "execution_status": task.execution_status, "evidence": task.progress_evidence,
                     "boundary_conflict": task.boundary_conflict, "is_overdue": task.is_overdue}
                    for task in (phase.plan.tasks if phase.plan else [])
                ],
            }
            for phase in timeline.phases
        ],
        "events": [
            {"title": event.title, "date": event.event_date, "kind": event.kind, "status": event.status,
             "execution_status": event.execution_status, "evidence": event.progress_evidence,
             "boundary_conflict": event.boundary_conflict}
            for event in timeline.events
            if event.kind in {"exam", "materials", "application", "offer", "visa", "enrollment"}
        ],
    }


def _needs_reasoning(profile: StudentProfile, timeline: PlanningTimeline) -> bool:
    severe = bool(timeline.enrollment_date and (timeline.enrollment_date - date.today()).days < 300)
    major_domain = identify_domain(profile.major)
    target_domain = identify_domain(profile.target_fields)
    in_scope_transfer = bool(major_domain and target_domain and major_domain != target_domain)
    degree_transfer = "transfer" in (profile.target_degree or "").casefold() or "转学" in (profile.target_degree or "")
    return severe or in_scope_transfer or degree_transfer


def _date_with_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, monthrange(year, month)[1]))
