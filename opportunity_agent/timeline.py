from __future__ import annotations

from calendar import monthrange
from datetime import date

from .domain_knowledge import validate_profile_domain
from .models import PlanningTimeline, StudentProfile, TimelineEvent, TimelinePhase


def build_timeline_skeleton(profile: StudentProfile, today: date | None = None) -> PlanningTimeline:
    today = today or date.today()
    supported, domain, message = validate_profile_domain(profile)
    if not supported or not domain:
        return PlanningTimeline(domain=domain or "unsupported", supported=False, support_message=message)

    graduation_year = profile.graduation_year or _infer_graduation_year(profile, today)
    graduation_month = profile.graduation_month or 6
    graduation_date = date(graduation_year, graduation_month, 30 if graduation_month in {4, 6, 9, 11} else 28)
    enrollment_year = profile.planned_enrollment_year or graduation_year
    enrollment_month = profile.planned_enrollment_month or 9
    enrollment_date = date(enrollment_year, enrollment_month, 1)
    application_year = graduation_year - 1 if enrollment_month >= 7 else graduation_year - 2
    background_end = date(application_year, 8, 31)
    estimated_start_year = graduation_year - (profile.degree_years or 4)
    background_start = min(today, date(max(2020, estimated_start_year), 9, 1))

    specs = [
        ("background", "背景提升", background_start, background_end, "background", "AcademicEnhancementSkill"),
        ("materials", "选校、文书素材与推荐人", date(application_year, 9, 1), date(application_year, 10, 31), "materials", "ApplicationMaterialsSkill"),
        ("application", "网申与材料提交", date(application_year, 10, 1), date(application_year, 12, 31), "application", "ApplicationMaterialsSkill"),
        ("offer_visa", "Offer 比较与签证准备", date(graduation_year, 1, 1), date(graduation_year, 5, 31), "offer_visa", "OfferVisaSkill"),
        ("enrollment", "毕业、签证、住宿与入学", date(graduation_year, 6, 1), enrollment_date, "enrollment", "OfferVisaSkill"),
    ]
    phases = [
        TimelinePhase(
            phase_id=phase_id, title=title, start_date=start, end_date=max(start, end),
            kind=kind, status=_range_status(start, max(start, end), today), skill_name=skill,
        )
        for phase_id, title, start, end, kind, skill in specs
    ]
    events: list[TimelineEvent] = []
    for year in range(today.year - 1, enrollment_year + 1):
        for month, kind, title in ((1, "winter_break", "寒假：暑研/实习准备"), (7, "summer_break", "暑假：暑研或实习")):
            event_date = date(year, month, 15)
            if background_start <= event_date <= enrollment_date:
                start = date(year, month, 1)
                end = date(year, 2, monthrange(year, 2)[1]) if kind == "winter_break" else date(year, 8, 31)
                events.append(_event(f"{kind}_{year}", title, event_date, kind, "background", today,
                                     start_date=start, end_date=end))
    if profile.exam_plan:
        exam = profile.exam_plan
        status = "overdue" if exam.next_exam_date < today else "current" if exam.next_exam_date == today else "upcoming"
        events.append(TimelineEvent(
            event_id="next_language_exam", title=f"{exam.exam_type} 考试",
            event_date=exam.next_exam_date, kind="exam", status=status,
            phase_id="background", detail="来自用户填写的下一次考试日期，不由系统推测。",
            source="user_explicit", confidence=1.0,
        ))
    milestone_events = (
        ("materials_start", "开始准备文书与推荐人", date(application_year, 9, 1), "materials", "materials"),
        ("application_start", "开始网申", date(application_year, 10, 1), "application", "application"),
        ("offer_start", "Offer 比较与补充材料", date(graduation_year, 1, 15), "offer", "offer_visa"),
        ("visa_start", "签证与住宿准备", date(graduation_year, 5, 1), "visa", "offer_visa"),
        ("enrollment", "开始留学", enrollment_date, "enrollment", "enrollment"),
    )
    events.extend(_event(event_id, title, when, kind, phase_id, today) for event_id, title, when, kind, phase_id in milestone_events)
    events.sort(key=lambda item: (item.event_date, item.event_id))
    return PlanningTimeline(
        domain=domain, supported=True, support_message=message,
        graduation_date=graduation_date, enrollment_date=enrollment_date,
        phases=phases, events=events,
    )


def _infer_graduation_year(profile: StudentProfile, today: date) -> int:
    academic_year = profile.academic_year or 1
    degree_years = profile.degree_years or 4
    remaining = max(0, degree_years - academic_year)
    return today.year + remaining + (1 if today.month > 6 else 0)


def _range_status(start: date, end: date, today: date) -> str:
    if end < today:
        return "history"
    if start <= today <= end:
        return "current"
    return "upcoming"


def _event(event_id: str, title: str, when: date, kind: str, phase_id: str, today: date,
           start_date: date | None = None, end_date: date | None = None) -> TimelineEvent:
    start, end = start_date or when, end_date or when
    status = _range_status(start, end, today)
    return TimelineEvent(
        event_id=event_id, title=title, event_date=when, kind=kind,
        status=status, phase_id=phase_id, start_date=start_date, end_date=end_date,
    )
