# Engineering planning timeline

## Scope

The detailed timeline supports only computer science/software/data/networking,
AI/ML, electronic information/communications/microelectronics, and
automation/control/robotics plus embedded and signal-processing neighbours.
An explicit onboarding profile outside these domains is saved, but
`PlanningTimeline.supported` is false and no LLM planning call is made.

## Data flow

```text
POST /api/onboarding
  -> OnboardingProfileInput
  -> explicit CandidateFact values
  -> ProfileNormalizer
  -> ProfileConflictResolver
  -> StudentProfile
  -> domain validation
  -> deterministic timeline skeleton
  -> component PlanningSkill results
  -> Roadmap + PlanningTimeline
```

The browser stores the whole validated response alongside each conversation.
Old snapshots remain readable because all new profile and roadmap fields have
defaults. Editing the form resubmits explicit facts and increments the roadmap
version only when the validated profile changes.

## Deterministic boundaries

`timeline.py` owns graduation, application, holiday, exam and enrollment dates.
LLM output is rejected at component level when a task falls outside its phase.
Past user-supplied exam dates are `overdue`; ordinary past nodes are `history`.
The LLM may describe a phase but cannot move its dates or reorder it.

## Planning components

`PlanningSkill.generate(context) -> SkillPlanResult` is implemented by academic,
language, research/summer, engineering internship, application materials, and
offer/visa components. `TimelineComposerSkill` merges and validates them. Each
component uses Pydantic structured output and falls back independently, so a
single timeout never discards the timeline or other successful components.

All model traffic goes through `LLMClient`. Simple component prompts use
`enable_thinking=false`; the final article enables reasoning only for a severe
time shortage. Offline and failed-online paths use the same timeline schema.

## Knowledge and provenance

`domain_knowledge.py` is an `internal_seed`, not an official program database.
It provides domain aliases, course groups, prerequisites, project ideas,
research/competition directions and job keywords. Every school-specific
prerequisite, language threshold, fee or deadline remains labelled
“待项目官网核验”. Job recommendations use the local demo dataset and expose
matched/missing skills, source, score and confidence.
