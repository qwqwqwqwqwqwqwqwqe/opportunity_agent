from __future__ import annotations

from .matcher import match_job_to_user
from .models import JobRecommendation, StudentProfile, UserProfile, UserState
from .domain_knowledge import knowledge_for_profile
from .planning_skills import recommend_engineering_jobs
from .repository import LocalRepository


def recommend_jobs(profile: StudentProfile, state: UserState, repository: LocalRepository,
                   limit: int = 5) -> list[JobRecommendation]:
    """Rank the local job dataset only when the user is actively job hunting."""
    if state.career not in {"internship_search", "full_time_search"}:
        return []
    knowledge = knowledge_for_profile(profile)
    if state.career == "internship_search" and knowledge is not None:
        return recommend_engineering_jobs(profile, knowledge, repository, limit)
    user = UserProfile(
        user_id=profile.user_id,
        major=profile.major or "Unknown",
        degree=profile.target_degree or "Unknown",
        school="Unknown",
        graduation_year=profile.graduation_year or 0,
        skills=profile.skills,
        career_goal=profile.career_goal or (profile.target_fields[0] + " Engineer" if profile.target_fields else "Unknown"),
        target_locations=profile.target_locations or profile.target_countries,
        target_companies=[],
        current_stage=state.career,
        interests=profile.target_fields,
    )
    completeness = sum(bool(value) for value in (profile.skills, profile.career_goal, profile.target_locations or profile.target_countries)) / 3
    ranked: list[JobRecommendation] = []
    for job in repository.list_jobs():
        if state.career == "internship_search" and job.employment_type != "internship":
            continue
        if state.career == "full_time_search" and job.employment_type != "full_time":
            continue
        match = match_job_to_user(user, job)
        if match.score < 0.40:
            continue
        ranked.append(JobRecommendation(
            job_id=job.job_id, company=job.company, title=job.title, location=job.location,
            score=match.score, confidence=round(0.65 + 0.30 * completeness, 2),
            matched_skills=match.matched_skills, missing_skills=match.missing_skills,
            reason=match.reason, source=f"{job.source}:{job.job_id}", should_notify=match.should_notify,
        ))
    return sorted(ranked, key=lambda item: item.score, reverse=True)[:limit]
