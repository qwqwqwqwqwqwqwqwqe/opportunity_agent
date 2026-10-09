from __future__ import annotations

from .models import Job, MatchResult, UserProfile


NOTIFY_THRESHOLD = 0.70


def _normalise(values: list[str]) -> dict[str, str]:
    return {value.casefold(): value for value in values}


def _goal_matches(career_goal: str, title_and_description: str) -> bool:
    """Avoid treating generic titles such as 'Engineer' as a career-goal match."""
    goal = career_goal.casefold()
    text = title_and_description.casefold()
    if goal in text:
        return True
    domains = (
        "ai", "machine learning", "ml", "backend", "data", "research", "software",
        "embedded", "firmware", "fpga", "communications", "signal", "rf", "hardware",
        "automation", "control", "robotics", "network",
    )
    return any(domain in goal and domain in text for domain in domains)


def match_job_to_user(user: UserProfile, job: Job) -> MatchResult:
    """Return a deterministic and intentionally explainable V1 recommendation score."""
    user_skills = _normalise(user.skills)
    job_skills = _normalise(job.skills)
    matched = [job_skills[key] for key in job_skills if key in user_skills]
    missing = [job_skills[key] for key in job_skills if key not in user_skills]

    skill_score = len(matched) / len(job_skills) if job_skills else 0.0
    score = 0.60 * skill_score
    reasons: list[str] = []
    if matched:
        reasons.append(f"匹配技能：{', '.join(matched)}")

    title_and_description = f"{job.title} {job.description}".casefold()
    goal_match = _goal_matches(user.career_goal, title_and_description)
    if goal_match:
        score += 0.20
        reasons.append(f"岗位与职业目标“{user.career_goal}”相关")

    if any(location.casefold() in job.location.casefold() for location in user.target_locations):
        score += 0.10
        reasons.append(f"地点符合目标：{job.location}")
    if any(company.casefold() == job.company.casefold() for company in user.target_companies):
        score += 0.05
        reasons.append(f"目标公司：{job.company}")
    if user.current_stage == "internship_search" and job.employment_type == "internship":
        score += 0.05
        reasons.append("实习岗位符合当前求职阶段")

    score = round(min(score, 1.0), 2)
    reason = "；".join(reasons) if reasons else "岗位与当前目标和技能的直接关联较弱"
    return MatchResult(
        score=score,
        matched_skills=matched,
        missing_skills=missing,
        reason=reason,
        should_notify=score >= NOTIFY_THRESHOLD,
    )
