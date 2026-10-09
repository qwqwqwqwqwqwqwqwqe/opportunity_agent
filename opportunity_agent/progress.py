"""Task identity and deterministic projection of user evidence onto the timeline."""
from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from datetime import date

from .models import PlanningTimeline, ProgressUpdate, Roadmap, StudentProfile, TaskProgress


LEGACY_IDS = {
    "maintain_gpa": "background_academic", "language_test": "background_language",
    "research_progress": "background_research_map", "shortlist_programs": "materials_materials",
    "request_letters": "materials_recommenders", "submit_application": "application_materials",
}
PURPOSES = {
    "academic": "AcademicEnhancementSkill", "portfolio": "AcademicEnhancementSkill",
    "language": "LanguageExamSkill", "research_map": "ResearchSummerSkill",
    "outreach": "ResearchSummerSkill", "internship": "EngineeringInternshipSkill",
    "materials": "ApplicationMaterialsSkill", "recommenders": "ApplicationMaterialsSkill",
    "offer_visa": "OfferVisaSkill",
}


def _digest(values: object) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:12]


def bind_timeline_identity(timeline: PlanningTimeline, profile: StudentProfile) -> None:
    goal = [timeline.domain, sorted(profile.target_fields), sorted(profile.target_programs),
            sorted(profile.target_schools), profile.target_degree]
    for phase in timeline.phases:
        for task in phase.plan.tasks if phase.plan else []:
            old_id = LEGACY_IDS.get(task.task_id, task.task_id)
            purpose = old_id.removeprefix(phase.phase_id + "_")
            semantic_goal: object = goal
            if purpose == "language":
                has_scores = any(v is not None for v in (profile.toefl_score, profile.ielts_score, profile.gre_score))
                semantic_goal = [goal, "verify_requirements" if has_scores else "prepare_exam"]
            if not task.progress_key:
                task.progress_key = f"{phase.phase_id}:{PURPOSES.get(purpose, phase.skill_name)}:{purpose}:{_digest(semantic_goal)}"
            task.aliases = sorted(set([*task.aliases, old_id, task.task_id,
                                      *(alias for alias, target in LEGACY_IDS.items() if target == old_id)]))
    for event in timeline.events:
        if event.original_event_date is None:
            event.original_event_date = event.event_date
        if not event.progress_key:
            identity = [event.event_id, event.original_event_date, profile.exam_plan.exam_type if event.kind == "exam" and profile.exam_plan else event.kind]
            event.progress_key = f"event:{event.event_id}:{_digest(identity)}"


def targets(roadmap: Roadmap | None) -> list[dict]:
    if not roadmap or not roadmap.timeline:
        return []
    result = []
    for phase in roadmap.timeline.phases:
        for task in phase.plan.tasks if phase.plan else []:
            result.append(dict(target_id=task.progress_key, target_kind="task", title=task.title,
                               category=task.category, aliases=[task.task_id, *task.aliases],
                               phase_id=phase.phase_id, due_date=task.due_date))
    for event in roadmap.timeline.events:
        # Seasonal labels are context, not executable tasks.
        if event.kind not in {"winter_break", "summer_break"}:
            result.append(dict(target_id=event.progress_key, target_kind="event", title=event.title,
                               category="language_exam" if event.kind == "exam" else event.kind,
                               aliases=[event.event_id], phase_id=event.phase_id, due_date=event.event_date))
    return result


def resolve_targets(update: ProgressUpdate, roadmap: Roadmap | None) -> list[dict]:
    available = [t for t in targets(roadmap) if t["target_kind"] == update.target_kind]
    if update.target_id:
        return [t for t in available if update.target_id == t["target_id"] or update.target_id in t["aliases"]]
    # Only explicit category/purpose aliases are used. Never fuzzy-match a title.
    hints = {"科研": "background_research_map", "研究": "background_research_map", "research": "background_research_map",
             "项目": "background_portfolio", "project": "background_portfolio", "语言": "background_language",
             "language": "background_language", "考试": "next_language_exam", "exam": "next_language_exam",
             "实习": "background_internship", "internship": "background_internship",
             "文书": "materials_materials", "网申": "application_materials"}
    alias = hints.get(update.target_hint.casefold(), update.target_hint)
    return [t for t in available if alias and alias in t["aliases"]]


def time_status(start: date, end: date, today: date) -> str:
    return "history" if end < today else "current" if start <= today <= end else "upcoming"


def _migrate_season_window(event) -> None:
    """Supply date ranges for timelines persisted before seasonal spans existed.

    Older snapshots only stored the display anchor (Jan/Jul 15).  Treating it
    as a one-day event makes 31 August look historical and lets the broad
    background phase incorrectly become the only current node.
    """
    if event.kind not in {"winter_break", "summer_break"} or (event.start_date and event.end_date):
        return
    anchor = event.original_event_date or event.event_date
    if event.kind == "winter_break":
        event.start_date = date(anchor.year, 1, 1)
        event.end_date = date(anchor.year, 2, monthrange(anchor.year, 2)[1])
    else:
        event.start_date = date(anchor.year, 7, 1)
        event.end_date = date(anchor.year, 8, 31)


def refresh_progress(roadmap: Roadmap | None, records: list[TaskProgress], today: date | None = None) -> None:
    if not roadmap or not roadmap.timeline:
        return
    today = today or date.today()
    by_id = {r.target_id: r for r in records}
    task_by_key = {}
    for phase in roadmap.timeline.phases:
        phase.time_status = time_status(phase.start_date, phase.end_date, today)
        phase.status = phase.time_status
        tasks = phase.plan.tasks if phase.plan else []
        for task in tasks:
            if task.original_due_date is None:
                task.original_due_date = task.due_date
            record = by_id.get(task.progress_key)
            task.execution_status = record.status if record else (task.status if task.status in {"completed", "in_progress", "cancelled"} else "planned")
            task.due_date = record.postponed_to if record and record.postponed_to else task.original_due_date
            task.is_overdue = bool(task.due_date and task.due_date < today and task.execution_status not in {"completed", "cancelled"})
            task.boundary_conflict = bool(task.due_date and not phase.start_date <= task.due_date <= phase.end_date)
            task.progress_evidence = record.evidence if record else ""
            task.progress_confidence = record.confidence if record else 0.0
            task.actual_date = record.actual_date if record else None
            task.risk_status = (
                "schedule_conflict" if task.boundary_conflict else
                "overdue" if task.is_overdue else
                "ahead_of_schedule" if record and record.status == "completed" and record.actual_date
                and task.original_due_date and record.actual_date < task.original_due_date else
                "needs_backfill" if phase.time_status == "history" and record is None else "none"
            )
            task.status = "overdue" if task.is_overdue else task.execution_status
            task_by_key[task.progress_key] = task
        phase.is_overdue = any(t.is_overdue for t in tasks)
        statuses = [t.execution_status for t in tasks]
        phase.execution_status = ("completed" if statuses and all(s in {"completed", "cancelled"} for s in statuses) and "completed" in statuses
                                  else "cancelled" if statuses and all(s == "cancelled" for s in statuses)
                                  else "in_progress" if any(s in {"in_progress", "completed"} for s in statuses) else "planned")
        phase.risk_status = (
            "schedule_conflict" if any(t.boundary_conflict for t in tasks) else
            "needs_backfill" if phase.time_status == "history" and phase.execution_status == "planned" else
            "overdue" if phase.is_overdue else
            "ahead_of_schedule" if phase.time_status == "upcoming" and phase.execution_status == "completed" else
            "none"
        )
    for event in roadmap.timeline.events:
        record = by_id.get(event.progress_key)
        if event.original_event_date is None:
            event.original_event_date = event.event_date
        _migrate_season_window(event)
        event.event_date = record.postponed_to if record and record.postponed_to else event.original_event_date
        if record:
            event.execution_status = record.status
            event.progress_evidence = record.evidence
            event.progress_confidence = record.confidence
            event.actual_date = record.actual_date
        event.time_status = time_status(event.start_date or event.event_date, event.end_date or event.event_date, today)
        event.is_overdue = bool(event.event_date < today and event.execution_status not in {"completed", "cancelled"}
                                and event.kind not in {"winter_break", "summer_break"})
        event.risk_status = (
            "schedule_conflict" if event.boundary_conflict else
            "overdue" if event.is_overdue else
            "ahead_of_schedule" if record and record.status == "completed" and record.actual_date
            and event.original_event_date and record.actual_date < event.original_event_date else
            "needs_backfill" if event.time_status == "history" and record is None else "none"
        )
        event.status = "overdue" if event.is_overdue else event.time_status
        phase = next((p for p in roadmap.timeline.phases if p.phase_id == event.phase_id), None)
        event.boundary_conflict = bool(record and record.postponed_to and phase and not phase.start_date <= event.event_date <= phase.end_date)
    roadmap.timeline.events.sort(key=lambda e: (e.event_date, e.event_id))
    # Keep the legacy projection in sync without replacing its public task IDs.
    for milestone in roadmap.milestones:
        for task in milestone.tasks:
            projected = task_by_key.get(task.progress_key)
            if projected:
                for field in ("status", "execution_status", "due_date", "original_due_date", "is_overdue", "boundary_conflict", "risk_status", "progress_evidence", "progress_confidence", "actual_date"):
                    setattr(task, field, getattr(projected, field))


def migrate_progress(roadmap: Roadmap | None, profile: StudentProfile, records: list[TaskProgress]) -> None:
    if not roadmap or not roadmap.timeline:
        return
    bind_timeline_identity(roadmap.timeline, profile)
    available = targets(roadmap)
    for milestone in roadmap.milestones:
        for task in milestone.tasks:
            matches = [t for t in available if task.task_id in t["aliases"]]
            if len(matches) == 1:
                task.progress_key = matches[0]["target_id"]
            if task.status in {"completed", "in_progress", "cancelled"} and task.progress_key and not any(r.target_id == task.progress_key for r in records):
                records.append(TaskProgress(target_id=task.progress_key, title=task.title, category=task.category,
                                            status=task.status, evidence="旧会话中已保存的任务状态", source_event_id="legacy"))
    for record in records:
        if not any(t["target_id"] == record.target_id for t in available):
            matches = [t for t in available if record.target_id in t["aliases"]]
            if len(matches) == 1:
                record.target_id = matches[0]["target_id"]
    refresh_progress(roadmap, records)


def inherit_timeline_progress(current: Roadmap | None, timeline: PlanningTimeline) -> None:
    """Project explicit progress onto a new plan *before* the article prompt.

    Canonical keys are the only join: changed goals and new exam appointments
    cannot inherit completion by sharing a title or an old public alias.
    """
    if not current or not current.timeline:
        return
    records = []
    for phase in current.timeline.phases:
        for task in phase.plan.tasks if phase.plan else []:
            if task.progress_key:
                records.append(TaskProgress(target_id=task.progress_key, title=task.title,
                    category=task.category, status=task.execution_status, evidence=task.progress_evidence,
                    source_event_id="current_plan_projection",
                    postponed_to=task.due_date if task.original_due_date and task.due_date != task.original_due_date else None))
    for event in current.timeline.events:
        if event.progress_key:
            records.append(TaskProgress(target_id=event.progress_key, target_kind="event", title=event.title,
                category=event.kind, status=event.execution_status, evidence=event.progress_evidence,
                source_event_id="current_plan_projection",
                postponed_to=event.event_date if event.original_event_date and event.event_date != event.original_event_date else None))
    refresh_progress(Roadmap(user_id=current.user_id, goal=current.goal, milestones=[], timeline=timeline), records)
