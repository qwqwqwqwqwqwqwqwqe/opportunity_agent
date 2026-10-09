from __future__ import annotations

from typing import Any

from .conflict_resolver import ProfileConflictResolver
from .domain_knowledge import identify_domain, validate_profile_domain
from .models import CandidateFact, ExamPlan, OnboardingProfileInput, StudentProfile, TargetProgram
from .normalizer import ProfileNormalizer


LIST_FIELDS = {
    "target_countries", "target_schools", "target_programs", "target_fields",
    "completed_courses", "skills", "hardware_skills", "research_experiences",
    "competition_experiences", "project_experiences", "paper_experiences",
    "internship_experiences",
}


def onboarding_to_facts(form: OnboardingProfileInput) -> list[CandidateFact]:
    payload = form.model_dump(mode="python")
    facts: list[CandidateFact] = []
    direct_fields = (
        "school", "major", "academic_year", "degree_years", "graduation_year",
        "graduation_month", "target_countries", "target_degree", "target_fields",
        "class_rank", "toefl_score", "ielts_score", "gre_score", "target_schools",
        "target_programs", "budget", "completed_courses", "skills", "hardware_skills",
        "research_experiences", "competition_experiences", "project_experiences",
        "paper_experiences", "internship_experiences", "summer_preference",
        "planned_enrollment_year", "planned_enrollment_month",
    )
    for field in direct_fields:
        value = payload.get(field)
        if value is None or value == "" or value == []:
            continue
        facts.append(CandidateFact(
            field=field, raw_value=value, normalized_value=value,
            source="user_explicit", confidence=1.0, evidence=f"onboarding.{field}",
        ))

    if form.gpa_raw is not None:
        facts.extend([
            CandidateFact(field="gpa_raw", raw_value=form.gpa_raw, normalized_value=form.gpa_raw,
                          source="user_explicit", confidence=1.0, evidence="onboarding.gpa_raw"),
            CandidateFact(field="gpa_scale", raw_value=form.gpa_scale, normalized_value=form.gpa_scale,
                          source="user_explicit", confidence=1.0, evidence="onboarding.gpa_scale"),
        ])
        if form.gpa_scale == 4.0 and 0 <= form.gpa_raw <= 4.0:
            facts.extend([
                CandidateFact(field="gpa", raw_value=form.gpa_raw, normalized_value=form.gpa_raw,
                              source="user_explicit", confidence=1.0, evidence="onboarding.gpa_raw"),
                CandidateFact(field="gpa_4_reference", raw_value=form.gpa_raw, normalized_value=form.gpa_raw,
                              source="system", confidence=1.0, evidence="reliable 4.0 scale identity conversion"),
            ])

    if form.next_exam_date and form.next_exam_type:
        exam_plan = ExamPlan(exam_type=form.next_exam_type, next_exam_date=form.next_exam_date)
        facts.append(CandidateFact(
            field="exam_plan", raw_value=exam_plan.model_dump(mode="json"),
            normalized_value=exam_plan.model_dump(mode="json"), source="user_explicit",
            confidence=1.0, evidence="onboarding.next_exam_date",
        ))
    facts.append(CandidateFact(
        field="onboarding_completed", raw_value=True, normalized_value=True,
        source="user_explicit", confidence=1.0, evidence="onboarding.submit",
    ))
    return facts


def apply_onboarding(
    profile: StudentProfile,
    form: OnboardingProfileInput | dict[str, Any],
    normalizer: ProfileNormalizer | None = None,
    resolver: ProfileConflictResolver | None = None,
) -> StudentProfile:
    validated = form if isinstance(form, OnboardingProfileInput) else OnboardingProfileInput.model_validate(form)
    facts = onboarding_to_facts(validated)
    normalized = (normalizer or ProfileNormalizer()).normalize(facts)
    updated = (resolver or ProfileConflictResolver()).resolve(profile, normalized).profile
    # Pairs are deliberately applied after legacy-list conflict resolution:
    # they are a structured user choice, while the old flat lists remain a
    # compatibility projection for snapshots, prompts and older clients.
    if validated.target_program_choices:
        updated.target_program_choices = [TargetProgram.model_validate(item)
                                          for item in validated.target_program_choices]
        updated.target_schools = [item.school for item in updated.target_program_choices]
        updated.target_programs = list(dict.fromkeys(
            item.program for item in updated.target_program_choices if item.program))
        updated.target_program_mapping_needs_review = False
    supported, domain, _ = validate_profile_domain(updated)
    updated.planning_domain = domain if supported else identify_domain(updated.major)
    return updated
