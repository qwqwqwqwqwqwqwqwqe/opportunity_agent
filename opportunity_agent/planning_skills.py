from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from typing import Protocol

from .domain_knowledge import EngineeringDomainKnowledge, assess_prerequisite_coverage, knowledge_for_profile
from .llm_client import LLMClient
from .matcher import match_job_to_user
from .models import (
    Job, JobRecommendation, PlanTask, PlanningTimeline, SkillPlanResult,
    StudentProfile, UserProfile, UserState,
)
from .repository import LocalRepository
from .progress import bind_timeline_identity


@dataclass
class PlanningContext:
    profile: StudentProfile
    state: UserState
    timeline: PlanningTimeline
    phase_id: str
    phase_start: date
    phase_end: date
    knowledge: EngineeringDomainKnowledge
    repository: LocalRepository
    llm_client: LLMClient | None = None


class PlanningSkill(Protocol):
    name: str

    def generate(self, context: PlanningContext) -> SkillPlanResult: ...


class BasePlanningSkill:
    name = "BasePlanningSkill"

    def generate(self, context: PlanningContext) -> SkillPlanResult:
        fallback = self.fallback(context)
        client = context.llm_client
        if not client or not client.enabled:
            return fallback
        try:
            generated = client.generate_structured(
                SkillPlanResult,
                system=self.system_prompt(),
                context=self.prompt_context(context, fallback),
                max_tokens=1800,
                thinking=False,
            )
            generated.skill_name = self.name
            seed_ids = {task.task_id for task in fallback.tasks}
            if {task.task_id for task in generated.tasks} != seed_ids:
                raise ValueError("model changed deterministic task identities")
            for task in generated.tasks:
                task.progress_key = ""
                task.aliases = []
                task.status = task.execution_status = "planned"
            generated.generation_mode = "qwen"
            generated.fallback_reason = None
            _validate_skill_dates(generated, context.phase_start, context.phase_end)
            return generated
        except Exception as exc:  # Component isolation is intentional.
            fallback.fallback_reason = client.last_error or f"{type(exc).__name__}: {exc}"
            return fallback

    def system_prompt(self) -> str:
        return (
            "你是计算机与电子信息类工科留学规划组件。仅根据输入画像、阶段边界和 internal_seed 生成该阶段任务。"
            "不得编造学校门槛、学费、截止日期或用户经历；学校具体要求必须写明待项目官网核验。"
            "不得改变阶段起止日期。已有语言成绩时先核验要求，不默认要求重考。返回严格匹配 schema 的 JSON。"
        )

    def prompt_context(self, context: PlanningContext, fallback: SkillPlanResult) -> dict:
        return {
            "skill": self.name,
            "phase": {"id": context.phase_id, "start": context.phase_start.isoformat(), "end": context.phase_end.isoformat()},
            "profile": context.profile.model_dump(mode="json", exclude={"facts", "change_history"}),
            "domain_knowledge": context.knowledge.__dict__,
            "deterministic_seed_plan": fallback.model_dump(mode="json"),
        }

    def fallback(self, context: PlanningContext) -> SkillPlanResult:
        raise NotImplementedError


class AcademicEnhancementSkill(BasePlanningSkill):
    name = "AcademicEnhancementSkill"

    def fallback(self, context: PlanningContext) -> SkillPlanResult:
        profile, kb = context.profile, context.knowledge
        coverage = assess_prerequisite_coverage(profile, kb)
        unconfirmed = coverage["not_confirmed"]
        gpa_text = _gpa_text(profile)
        courses = "、".join(unconfirmed[:5] or coverage["confirmed"][:5] or kb.prerequisites[:5])
        project = kb.projects[0]
        course_reason = (f"以下课程尚未在已确认课程记录中出现：{courses}；请先核对成绩单或课程大纲，"
                         "不能据此推断未修或必须补修。") if unconfirmed else (
                             f"已确认课程覆盖：{courses}；下一步保持成绩并沉淀课程项目证据。")
        tasks = [
            _task(f"{context.phase_id}_academic", f"核对{courses}的课程记录并保持{gpa_text}", "academic", context.phase_end,
                  f"目标方向为 {kb.domain}；{course_reason}具体项目先修课待官网核验。", 0.88),
            _task(f"{context.phase_id}_portfolio", f"完成一个{project}", "academic", context.phase_end,
                  f"用可复现代码、实验记录和 README 证明工程能力；建议方向来自 {kb.source}。", 0.84),
        ]
        return SkillPlanResult(skill_name=self.name, title="课程、GPA 与作品背景提升", summary=course_reason + "同时把课程产出转成可展示项目。", tasks=tasks)


class LanguageExamSkill(BasePlanningSkill):
    name = "LanguageExamSkill"

    def fallback(self, context: PlanningContext) -> SkillPlanResult:
        p = context.profile
        recorded = []
        if p.toefl_score is not None:
            recorded.append(f"TOEFL {p.toefl_score}")
        if p.ielts_score is not None:
            recorded.append(f"IELTS {p.ielts_score:g}")
        if p.gre_score is not None:
            recorded.append(f"GRE {p.gre_score}")
        due = p.exam_plan.next_exam_date if p.exam_plan else context.phase_end
        if recorded:
            title = f"核验 {' / '.join(recorded)} 是否满足目标项目门槛"
            reason = "用户已有成绩，先逐项核验项目官网要求，不默认重复考试；仅在分数不足或用户主动安排考试时重考。"
        else:
            exam = p.exam_plan.exam_type if p.exam_plan else "TOEFL/IELTS"
            title = f"完成 {exam} 诊断与分阶段备考"
            reason = "当前未记录有效语言成绩；考试日期仅采用用户表单填写值，未填写时不虚构考试日。"
        return SkillPlanResult(
            skill_name=self.name, title="语言考试", summary=reason,
            tasks=[_task(f"{context.phase_id}_language", title, "language", min(max(due, context.phase_start), context.phase_end), reason, 0.92)],
        )


class ResearchSummerSkill(BasePlanningSkill):
    name = "ResearchSummerSkill"

    def fallback(self, context: PlanningContext) -> SkillPlanResult:
        kb, p = context.knowledge, context.profile
        existing = "；".join(p.research_experiences + p.project_experiences) or "尚未记录相关经历"
        keywords = "、".join(kb.research_competitions)
        research_title = f"推进现有研究，并用“{keywords}”校准成果目标" if context.state.research == "building" else f"用“{keywords}”筛选导师与实验室"
        tasks = [
            _task(f"{context.phase_id}_research_map", research_title, "research", context.phase_end,
                  f"现有经历：{existing}。先阅读近两年工作，再判断课题匹配度。", 0.83),
            _task(f"{context.phase_id}_outreach", "准备一页 CV、项目摘要并分批联系导师", "research", context.phase_end,
                  "每封邮件说明具体论文连接、可投入时间和可交付成果；不承诺未具备的技能。", 0.84),
        ]
        return SkillPlanResult(skill_name=self.name, title="暑研与科研准备", summary=f"围绕 {keywords} 建立导师清单和可验证成果目标。", tasks=tasks)


class EngineeringInternshipSkill(BasePlanningSkill):
    name = "EngineeringInternshipSkill"

    def fallback(self, context: PlanningContext) -> SkillPlanResult:
        recommendations = recommend_engineering_jobs(context.profile, context.knowledge, context.repository)
        job_titles = "、".join(f"{item.company} {item.title}" for item in recommendations[:3]) or "当前数据库暂无达到阈值的岗位"
        task = _task(
            f"{context.phase_id}_internship", "按目标工科方向准备简历并分批投递", "career", context.phase_end,
            f"本地岗位库优先结果：{job_titles}。每条推荐均保留匹配技能、缺失技能、来源和置信度。", 0.86,
        )
        return SkillPlanResult(
            skill_name=self.name, title="工科实习匹配", summary=f"推荐范围严格限定为 {context.knowledge.domain} 相关技术岗位。",
            tasks=[task], recommendations=recommendations,
        )


class ApplicationMaterialsSkill(BasePlanningSkill):
    name = "ApplicationMaterialsSkill"

    def fallback(self, context: PlanningContext) -> SkillPlanResult:
        p = context.profile
        targets = "、".join([*p.target_schools, *p.target_programs]) or "目标学校与项目清单"
        application = context.phase_id == "application"
        title = "逐项提交网申并保留材料核验记录" if application else "建立选校表、CV/SOP 素材库并确认推荐人"
        reason = f"围绕 {targets} 执行；具体先修课、语言门槛、学费和截止日期均待项目官网核验。"
        result = SkillPlanResult(
            skill_name=self.name, title="网申材料" if application else "选校与文书准备", summary=reason,
            tasks=[_task(f"{context.phase_id}_materials", title, "application", context.phase_end, reason, 0.9)],
        )
        if not application:
            result.tasks.append(_task(f"{context.phase_id}_recommenders", "确认推荐人并准备推荐材料包", "application",
                                     context.phase_end, "推荐信应以真实课程、科研或项目合作为依据。", 0.9))
        return result


class OfferVisaSkill(BasePlanningSkill):
    name = "OfferVisaSkill"

    def fallback(self, context: PlanningContext) -> SkillPlanResult:
        enrollment = context.phase_id == "enrollment"
        title = "完成签证、住宿、保险与行前清单" if enrollment else "比较 Offer、资金需求并准备签证材料"
        budget = context.profile.budget
        budget_text = f"{budget.amount:g} {budget.currency}（{budget.period}）" if budget and budget.amount is not None else "预算尚未量化"
        reason = f"当前{budget_text}；所有费用、签证与入学要求以官方信息为准。"
        return SkillPlanResult(
            skill_name=self.name, title="入学准备" if enrollment else "Offer 与签证", summary=reason,
            tasks=[_task(f"{context.phase_id}_offer_visa", title, "application", context.phase_end, reason, 0.9)],
        )


class TimelineComposerSkill:
    name = "TimelineComposerSkill"

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self.llm_client = llm_client
        self.component_errors: dict[str, str] = {}

    def generate(self, profile: StudentProfile, state: UserState, timeline: PlanningTimeline,
                 repository: LocalRepository) -> PlanningTimeline:
        knowledge = knowledge_for_profile(profile)
        if not timeline.supported or knowledge is None:
            return timeline
        jobs: list[tuple[object, BasePlanningSkill, PlanningContext]] = []
        for phase in timeline.phases:
            context = PlanningContext(profile, state, timeline, phase.phase_id, phase.start_date, phase.end_date, knowledge, repository, self.llm_client)
            if phase.kind == "background":
                component_skills: list[BasePlanningSkill] = [AcademicEnhancementSkill(), LanguageExamSkill()]
                if profile.summer_preference in {"research", "both", "unknown"}:
                    component_skills.append(ResearchSummerSkill())
                if profile.summer_preference in {"internship", "both", "unknown"}:
                    component_skills.append(EngineeringInternshipSkill())
                jobs.extend((phase, skill, context) for skill in component_skills)
            elif phase.kind in {"materials", "application"}:
                jobs.append((phase, ApplicationMaterialsSkill(), context))
            else:
                jobs.append((phase, OfferVisaSkill(), context))

        grouped: dict[str, list[SkillPlanResult]] = {phase.phase_id: [] for phase in timeline.phases}
        if self.llm_client and self.llm_client.enabled and len(jobs) > 1:
            # Independent skill calls run concurrently. A slow component no
            # longer multiplies total page latency by the number of phases.
            with ThreadPoolExecutor(max_workers=min(8, len(jobs)), thread_name_prefix="planning-skill") as executor:
                futures = {executor.submit(skill.generate, context): phase for phase, skill, context in jobs}
                for future in as_completed(futures):
                    phase = futures[future]
                    grouped[phase.phase_id].append(future.result())
        else:
            for phase, skill, context in jobs:
                grouped[phase.phase_id].append(skill.generate(context))

        for phase in timeline.phases:
            results = grouped[phase.phase_id]
            if phase.kind == "background":
                # Stable ordering is independent of thread completion order.
                order = {"AcademicEnhancementSkill": 0, "LanguageExamSkill": 1,
                         "ResearchSummerSkill": 2, "EngineeringInternshipSkill": 3}
                results.sort(key=lambda item: order.get(item.skill_name, 99))
                phase.plan = _merge_results("背景提升综合规划", results)
            else:
                phase.plan = results[0] if results else None
            if phase.plan and phase.plan.fallback_reason:
                self.component_errors[phase.phase_id] = phase.plan.fallback_reason
        _deduplicate_and_validate(timeline)
        bind_timeline_identity(timeline, profile)
        return timeline


def recommend_engineering_jobs(profile: StudentProfile, knowledge: EngineeringDomainKnowledge,
                               repository: LocalRepository, limit: int = 5) -> list[JobRecommendation]:
    user = UserProfile(
        user_id=profile.user_id, major=profile.major or "Unknown", degree=profile.target_degree or "Unknown",
        school=profile.school or "Unknown", graduation_year=profile.graduation_year or 0,
        skills=[*profile.skills, *profile.hardware_skills],
        career_goal=profile.career_goal or " ".join(knowledge.job_keywords[:3]),
        target_locations=profile.target_locations or profile.target_countries,
        target_companies=[], current_stage="internship_search", interests=profile.target_fields,
    )
    ranked: list[JobRecommendation] = []
    for job in repository.list_jobs():
        if job.employment_type != "internship" or not _job_matches_domain(job, knowledge):
            continue
        match = match_job_to_user(user, job)
        domain_bonus = 0.15
        score = min(1.0, round(match.score + domain_bonus, 2))
        confidence = round(0.68 + 0.08 * bool(profile.skills) + 0.08 * bool(profile.target_fields), 2)
        ranked.append(JobRecommendation(
            job_id=job.job_id, company=job.company, title=job.title, location=job.location,
            score=score, confidence=confidence, matched_skills=match.matched_skills,
            missing_skills=match.missing_skills,
            reason=f"专业域匹配 {knowledge.domain}；{match.reason}", source=f"{job.source}:{job.job_id}",
            should_notify=score >= 0.70,
        ))
    return sorted(ranked, key=lambda item: (item.score, item.confidence), reverse=True)[:limit]


def _job_matches_domain(job: Job, knowledge: EngineeringDomainKnowledge) -> bool:
    if job.domains:
        return knowledge.domain in job.domains
    text = " ".join((job.title, job.description, *job.skills)).casefold()
    return any(_keyword_in_text(keyword, text) for keyword in knowledge.job_keywords)


def _keyword_in_text(keyword: str, text: str) -> bool:
    token = keyword.casefold()
    if token.isascii() and len(token) <= 3:
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text))
    return token in text


def _merge_results(title: str, results: list[SkillPlanResult]) -> SkillPlanResult:
    tasks, recommendations, sources, errors = [], [], [], []
    modes = []
    for result in results:
        tasks.extend(result.tasks)
        recommendations.extend(result.recommendations)
        sources.extend(result.sources)
        modes.append(result.generation_mode)
        if result.fallback_reason:
            errors.append(f"{result.skill_name}: {result.fallback_reason}")
    return SkillPlanResult(
        skill_name="TimelineComposerSkill", title=title,
        summary="\n".join(result.summary for result in results), tasks=tasks,
        recommendations=recommendations, sources=list(dict.fromkeys(sources)),
        confidence=min((result.confidence for result in results), default=0.8),
        generation_mode="qwen" if "qwen" in modes else "rule_fallback",
        fallback_reason="；".join(errors) or None,
    )


def _validate_skill_dates(result: SkillPlanResult, start: date, end: date) -> None:
    for task in result.tasks:
        if task.due_date and not start <= task.due_date <= end:
            raise ValueError(f"task {task.task_id} is outside deterministic phase boundaries")


def _deduplicate_and_validate(timeline: PlanningTimeline) -> None:
    seen: set[str] = set()
    all_ids: set[str] = set()
    for phase in timeline.phases:
        if not phase.plan:
            continue
        unique = []
        for task in phase.plan.tasks:
            if task.task_id in seen:
                continue
            seen.add(task.task_id)
            all_ids.add(task.task_id)
            if task.due_date and not phase.start_date <= task.due_date <= phase.end_date:
                task.due_date = phase.end_date
            unique.append(task)
        phase.plan.tasks = unique
    for phase in timeline.phases:
        if not phase.plan:
            continue
        for task in phase.plan.tasks:
            task.depends_on = [dependency for dependency in task.depends_on if dependency in all_ids and dependency != task.task_id]


def _task(task_id: str, title: str, category: str, due_date: date, reason: str, confidence: float) -> PlanTask:
    return PlanTask(
        task_id=task_id, title=title, category=category, due_date=due_date,
        reason=reason, source="profile + internal_seed", confidence=confidence,
    )


def _gpa_text(profile: StudentProfile) -> str:
    if profile.gpa is not None:
        return f"GPA {profile.gpa:.2f}"
    if profile.gpa_raw is not None and profile.gpa_scale is not None:
        return f"GPA {profile.gpa_raw:g}/{profile.gpa_scale:g}（保留原量表）"
    return "核心课程成绩"
