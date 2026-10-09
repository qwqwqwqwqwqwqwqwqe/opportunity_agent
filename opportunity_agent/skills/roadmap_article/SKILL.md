---
name: roadmap_article
description: Generate a grounded, detailed six-section Chinese study-abroad roadmap article from confirmed profile, timeline, progress, and official evidence.
---

# Roadmap Article Skill

Use this skill only when the product explicitly requests first-time roadmap article generation or manual replanning. Do not use it for ordinary chat, profile collection, timeline refresh, or progress updates.

## Output contract

Return JSON that exactly matches the supplied output schema. Fill all six section fields with正文 only. Do not emit headings, a preface, Markdown fences, the input JSON, or commentary about the task. The host adds the six fixed headings after validation.

Target 1,800–3,600 Chinese characters across all sections. Write concrete paragraphs, not slogans or generic checklists. Be detailed when the confirmed profile, timeline, or official evidence warrants it; do not pad with repeated generic advice.

## Six sections

1. `current_profile_goal`: summarize the confirmed school, major, year, scores, target countries, schools, programs, degree, budget, and intended enrollment that are actually available. Explain how they shape the goal.
2. `gap_analysis`: compare the confirmed background with the target field. Cover course foundations, technical skills, research/project/internship evidence, language/application readiness, and missing evidence. Do not invent a target-school requirement.
3. `current_stage_actions`: use the deterministic current phase and dates. Give ordered, measurable actions, deliverables, and review points. Respect completed, cancelled, postponed, overdue, and ahead-of-schedule states.
4. `academic_research_internship`: use the user's actual completed courses, skills, research, projects, papers, competitions, and internships. Recommend relevant courses, portfolio evidence, research outreach, summer research, or engineering internships for the supported CS/electronic-information domain.
5. `application_materials_timeline`: cover CV, SOP/personal statement, recommendation materials, essays, application submission, offer/visa steps, and deterministic dates. Cover every target university represented in `official_sources`. Summarize evidence in natural language and end the relevant sentence with `（来源：source_id）`.
6. `risks_next_steps`: identify concrete schedule, evidence, workload, budget, or unresolved-official-information risks; then state the next verification and action sequence.

## Grounding and safety

- Use only `profile`, `latest_accepted_facts`, `state`, `timeline`, `official_requirements`, `official_sources`, and `unresolved_official_questions` supplied in the context.
- Never change a date, score, task state, source identifier, school, program, or user experience.
- Never invent coursework, awards, research output, internship duties, admission thresholds, document word limits, tuition, or deadlines.
- `course_assessment.not_confirmed` means only “not explicitly recorded in the current confirmed profile/resume”. It must be described as “待核对成绩单/课程大纲”, never as “未修”“缺失”“需要补齐” or a personal academic gap. Only an explicit user statement or verified program requirement may support a recommendation to take an additional course.
- Existing execution progress has priority over generic advice. Never schedule a completed or cancelled task as unfinished.
- A recorded TOEFL, IELTS, or GRE score must not be described as “not yet taken”. Do not recommend retaking solely from an official minimum score.
- Project-specific GRE, language, prerequisite, deadline, tuition, SOP and short-answer claims require an `official_requirements` entry whose `program_match` is `exact`. A source with `scope=department` or `scope=university_wide` may describe only its stated generic process and must be labelled “院系级” or “学校通用”; it must not close a project-specific pending item.
- Only entries in `unresolved_official_questions` may be described as pending official verification.
- Community knowledge or model memory must not be presented as an official project requirement.
- If information is missing, state precisely what the user should add or verify; do not fill the gap with a guess.
