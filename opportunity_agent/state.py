from __future__ import annotations

from .models import StudentProfile, UserState, TaskProgress, StageSignal


def derive_state(profile: StudentProfile, progress: list[TaskProgress] | None = None,
                 signals: list[StageSignal] | None = None) -> UserState:
    values = {
        fact.field: fact for fact in profile.facts
        if fact.operation != "remove" and not fact.needs_confirmation and fact.confidence >= 0.75
    }
    year = profile.academic_year
    academic = "unknown" if year is None else ("early_undergraduate" if year <= 2 else "mid_undergraduate" if year == 3 else "senior")
    application = "exploring" if profile.target_degree and profile.target_countries else "unknown"
    has_language_score = profile.toefl_score is not None or profile.ielts_score is not None
    state = UserState(
        academic=academic,
        language="completed" if has_language_score else (("preparing" if bool(values["language_preparation"].value) else "not_started") if "language_preparation" in values else ("not_started" if profile.target_degree else "unknown")),
        research=("exploring" if str(values["research_activity"].value).casefold() in {"none", "no", "false", "暂无", "没有"} else "building") if "research_activity" in values else ("exploring" if profile.target_fields else "unknown"),
        application=application,
        career=profile.current_stage if profile.current_stage in {"internship_search", "full_time_search"} else ("exploring" if profile.career_goal else "unknown"),
        language_evidence="score_recorded" if has_language_score else "preparing" if "language_preparation" in values and bool(values["language_preparation"].value) else "unknown",
    )
    if profile.research_experiences:
        state.research = "building"
    for item in progress or []:
        if item.status not in {"in_progress", "completed"}:
            continue
        if item.category == "research":
            state.research = "building"
        elif item.category == "language_exam":
            if item.status == "completed" and not has_language_score:
                state.language_evidence = "exam_taken"
        elif item.category == "language" and not has_language_score:
            state.language, state.language_evidence = "preparing", "preparing"
        elif item.category == "application":
            state.application = "applying" if item.target_id.startswith("application:") else "preparing"
        elif item.category == "career" and state.career in {"unknown", "exploring"}:
            state.career = "internship_search"
    # Signals express interest/readiness, never completion or achievement.
    latest_signals = {s.stage.upper(): s for s in signals or []}
    for signal in latest_signals.values():
        if signal.strength < 0.75 or signal.direction != "increase":
            continue
        if signal.stage.upper() in {"RESEARCH", "SUMMER_RESEARCH"} and state.research == "unknown":
            state.research = "exploring"
        elif signal.stage.upper() == "APPLICATION" and state.application == "unknown":
            state.application = "exploring"
        elif (signal.stage.upper() == "LANGUAGE_PREPARATION" and not has_language_score
              and "language_preparation" not in values and state.language_evidence == "unknown"):
            state.language, state.language_evidence = "preparing", "preparing"
    return state
