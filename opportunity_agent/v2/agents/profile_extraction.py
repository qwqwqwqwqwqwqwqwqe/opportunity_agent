"""Evidence-grounded V2 extraction; candidates never have write authority."""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...conflict_resolver import ProfileConflictResolver
from ...llm_client import LLMClient
from ...models import CandidateFact, StudentProfile
from ...normalizer import ProfileNormalizer
from ...profile import FastExtractor, _extract_experience_facts
from ...turn_understanding import assertion_text, extract_progress_rules


class FactCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    raw_value: Any
    normalized_value: Any = None
    operation: Literal["add", "update", "remove"] = "update"
    statement_kind: Literal["explicit", "correction", "negation", "hypothetical", "question", "uncertain"] = "explicit"
    evidence: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    source: Literal["rule", "llm"]


class PreferenceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Literal["avoid_gre", "employment_priority", "fallback_country", "budget_preference"]
    value: Any
    evidence: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class RejectedKnownFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    evidence: str = Field(min_length=1)
    reason: str = ""


class ExtractedCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[FactCandidate] = Field(default_factory=list)
    preferences: list[PreferenceCandidate] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)
    rejected_known_facts: list[RejectedKnownFact] = Field(default_factory=list)


class ExtractionOutcome(BaseModel):
    extracted_facts: list[FactCandidate] = Field(default_factory=list)
    accepted_facts: list[CandidateFact] = Field(default_factory=list)
    preferences: list[PreferenceCandidate] = Field(default_factory=list)
    progress_updates: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    mode: str = "rule_only"
    semantic_failed: bool = False
    route_path: Literal["rule_only", "llm_only", "hybrid", "reject"] = "rule_only"
    route_reason: str = "explicit rule facts"


class ProfileExtractionRoute(BaseModel):
    """Deterministic minimum path for a Profile message.

    This is intentionally a gate, not an LLM router: a message which can be
    completely and safely handled by high-confidence rules must not pay for a
    model call merely because one happens to be configured.
    """

    path: Literal["rule_only", "llm_only", "hybrid", "reject"]
    reason: str


_SEMANTIC_PROFILE_SIGNAL = re.compile(
    r"(?:主申|想申请|计划申请|准备申请|攻读|想读|读.*(?:硕士|博士)|毕业后.{0,12}(?:希望|想|成为|做)|职业目标|希望做|"
    r"备选|如果.*(?:备选|找不到工作)|不考虑.*(?:国家|英国|美国|加拿大)|优先.*(?:国家|地区))",
    re.I,
)
_FUTURE_ONLY = re.compile(r"(?:可能|也许|以后|未来).{0,18}(?:会做|想做|打算做|计划做)|(?:尚未|还没|没有)开始", re.I)
_CAREER_GOAL = re.compile(r"(?:职业目标|毕业后|未来|以后).{0,24}(?:想|希望|计划|从事).{0,24}(?:工程师|开发|工作|岗位|职业|从事)", re.I)
_OTHER_PERSON = re.compile(r"(?:(?:我(?:的)?)?(?:室友|朋友|同学|导师|同事|哥哥|姐姐)|(?<!其)他(?:的)?|她(?:的)?|\bmy\s+(?:friend|roommate)\b|\bhis\b|\bher\b)", re.I)


def evidence_is_other_person(original: str, evidence: str) -> bool:
    """Check local subject for every occurrence; a current self assertion wins."""
    starts = [m.start() for m in re.finditer(re.escape(evidence), original)]
    for start in starts:
        prefix = re.split(r"[，,；;。\n]", original[:start])[-1] + evidence
        others = list(_OTHER_PERSON.finditer(prefix))
        own = list(re.finditer(r"我(?!的?(?:室友|朋友|同学|导师|同事|哥哥|姐姐))|\b(?:I|my\s+score)\b", prefix, re.I))
        if not others or (own and own[-1].start() > others[-1].start()):
            return False
    return bool(starts)


def is_contextual_profile_answer(original: str, context: dict | None) -> bool:
    if not context:
        return False
    background = str(context.get("summary", "")) + "\n" + "\n".join(
        str(m.get("content", "")) for m in context.get("recent_messages", [])[-4:])
    return bool(re.search(r"托福|雅思|GPA|GRE|TOEFL|IELTS|绩点", background, re.I)
                and re.fullmatch(r"(?:我)?(?:这次(?:是)?|现在(?:是)?|刚考到|刚考了|考了|更正为)?\s*\d+(?:\.\d+)?\s*(?:分)?[。.!！]?", original.strip()))


def route_profile_extraction(original: str, rules: ExtractedCandidates | None = None,
                             context: dict | None = None) -> ProfileExtractionRoute:
    """Choose the least expensive safe extraction path for one message."""

    assertion, was_question = assertion_text(original)
    if not assertion.strip() or (was_question and not re.search(r"(?:考了|考完|出分|成绩是|拿到)\s*\d", assertion, re.I)):
        return ProfileExtractionRoute(path="reject", reason="question_or_hypothetical_has_no_asserted_fact")
    if _FUTURE_ONLY.search(assertion) and not _CAREER_GOAL.search(assertion) and not re.search(r"(?:我有|我做过|我参与|我完成|我发表|我发了)", assertion):
        return ProfileExtractionRoute(path="reject", reason="future_or_unstarted_activity_is_not_history")
    candidates = rules if rules is not None else rule_candidates(original)
    semantic = (bool(_SEMANTIC_PROFILE_SIGNAL.search(assertion) or _CAREER_GOAL.search(assertion))
                or is_contextual_profile_answer(original, context)
                or bool(re.search(r"想研究|研究兴趣|科研方向|研究方向|想当|不准备用|取消.{0,12}偏好", assertion)))
    if semantic and (candidates.facts or candidates.preferences):
        return ProfileExtractionRoute(path="hybrid", reason="rule_facts_plus_natural_language_semantics")
    if semantic:
        return ProfileExtractionRoute(path="llm_only", reason="natural_language_profile_semantics")
    return ProfileExtractionRoute(path="rule_only", reason="rules_cover_explicit_profile_message")


def validate_candidate(candidate: FactCandidate, original: str) -> CandidateFact:
    if candidate.field not in ProfileConflictResolver.PROFILE_FIELDS:
        raise ValueError("unsupported_field")
    if candidate.evidence not in original:
        raise ValueError("evidence_not_in_original")
    if evidence_is_other_person(original, candidate.evidence):
        raise ValueError("fact_belongs_to_other_person")
    if candidate.statement_kind in {"question", "hypothetical"}:
        raise ValueError("not_an_assertion")
    operation = {"add": "append", "update": "set", "remove": "remove"}[candidate.operation]
    default = getattr(StudentProfile(user_id="validation"), candidate.field)
    if operation == "append" and not isinstance(default, list):
        operation = "set"
    value = candidate.normalized_value if candidate.normalized_value is not None else candidate.raw_value
    if candidate.field in {"gpa", "gpa_scale", "toefl_score", "ielts_score", "gre_score", "academic_year"} and isinstance(value, bool):
        raise ValueError("expected_number_not_boolean")
    if candidate.field == "gpa_scale":
        match = re.search(r"(?:/|满分(?:是|为)?)\s*(\d+(?:\.\d+)?)|(?P<scale>\d+(?:\.\d+)?)\s*(?:分制|满分)", candidate.evidence)
        scale = float(match.group(1) or match.group("scale")) if match else next(
            (number for label, number in (("四分制", 4), ("五分制", 5), ("百分制", 100)) if label in candidate.evidence), None)
        if scale is None or float(value) != scale:
            raise ValueError("gpa_scale_requires_current_user_evidence")
    if isinstance(default, list) and not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
        raise ValueError("expected_string_list")
    fact = ProfileNormalizer().normalize_fact(CandidateFact(
        field=candidate.field, raw_value=candidate.raw_value, normalized_value=value,
        operation=operation, confidence=candidate.confidence, source="conversation",
        evidence=candidate.evidence,
        needs_confirmation=candidate.statement_kind == "uncertain" or candidate.confidence < .75,
    ))
    StudentProfile.model_validate({"user_id": "validation", fact.field: fact.value})
    if fact.field == "class_rank":
        match = re.fullmatch(r"(\d+)\s*/\s*(\d+)", str(fact.value))
        if not match or not 1 <= int(match[1]) <= int(match[2]):
            raise ValueError("invalid_rank")
    # Do not map contingent/fallback countries into primary application targets.
    if fact.field in {"target_countries", "target_programs"} and re.search(r"如果|备选|假如|假设|找不到", candidate.evidence):
        raise ValueError("conditional_target_requires_preference")
    if fact.field == "target_programs" and re.search(r"项目", candidate.evidence) and not re.search(
        r"申请|目标|想读|攻读|选校|硕士|博士|学位|apply|degree|master|phd", candidate.evidence, re.I
    ) and re.search(r"我(?:现在)?有|做过|参与|完成|开发|实习|科研", original):
        raise ValueError("experience_is_not_application_target")
    return fact


def rule_candidates(original: str) -> ExtractedCandidates:
    result = ExtractedCandidates()
    for clause in re.split(r"[，,；;。\n]+", original):
        clause = clause.strip()
        text, _ = assertion_text(clause)
        if not text:
            continue
        if evidence_is_other_person(original, text):
            continue
        uncertain = bool(re.search(r"可能|也许|大概|不确定|maybe|probably", text, re.I))
        kind = "uncertain" if uncertain else "correction" if re.search(r"现在|刚|改成|更正|其实|不是", text) else "explicit"
        for field, label in [("toefl_score", "托福|TOEFL"), ("ielts_score", "雅思|IELTS"), ("gre_score", "GRE")]:
            match = re.search(rf"(?:{label})\s*(?:现在是|刚考到|刚考了|改成|更正为)\s*(\d+(?:\.\d+)?)", text, re.I)
            if match:
                value = float(match[1])
                result.facts.append(FactCandidate(field=field, raw_value=int(value) if value.is_integer() else value,
                    statement_kind="correction", evidence=match[0], confidence=.99, source="rule"))
        for fact in [*FastExtractor().extract(text), *_extract_experience_facts(text)]:
            if fact.field not in ProfileConflictResolver.PROFILE_FIELDS or not fact.evidence or fact.evidence not in original:
                continue
            result.facts.append(FactCandidate(
                field=fact.field, raw_value=fact.raw_value, normalized_value=fact.value,
                operation="add" if fact.operation == "append" else "update", statement_kind=kind,
                evidence=fact.evidence, confidence=.65 if uncertain else fact.confidence, source="rule",
            ))
        major = re.search(r"(?:我是|我读|我的专业是|专业[:：]?|大[一二三四]\s*)(CS\b|计算机科学|计算机|软件工程)", text, re.I)
        if major:
            result.facts.append(FactCandidate(field="major", raw_value=major[1], evidence=major[0], confidence=.96, source="rule"))
        # Preserve the description, including acronyms. No journal/title guessing.
        if re.search(r"做过|参与过", text) and re.search(r"研究|科研", text) and not any(
            f.field == "research_experiences" and f.evidence == text for f in result.facts
        ):
            result.facts.append(FactCandidate(field="research_experiences", raw_value=[text], operation="add",
                                              evidence=text, confidence=.96, source="rule"))
        for pattern, key, value in [
            (r"不想考\s*GRE|不考虑\s*GRE|required GRE.*不考虑", "avoid_gre", True),
            (r"(?<!不)(?:愿意|决定|准备|打算)\s*考\s*GRE|取消.{0,12}(?:不考|不考虑|排除)\s*GRE.{0,8}偏好", "avoid_gre", False),
            (r"更(?:看重|关心|重视)就业|就业优先", "employment_priority", True),
        ]:
            found = re.search(pattern, text, re.I)
            if found:
                result.preferences.append(PreferenceCandidate(key=key, value=value, evidence=found[0], confidence=.98))
    return result


class ProfileExtractionPipeline:
    SYSTEM = """Extract user facts as strict JSON matching the schema. Read original_message in full.
known_facts are provisional rule candidates: supplement omissions, do not repeat them.
Only extract the user's own facts, never a friend's/roommate's scores. If a known fact
has the wrong subject, negation or hypothetical meaning, return its exact field and
evidence in rejected_known_facts. Explicit future career goals are valid goals, not completed experiences.
Use conversation_context only to resolve references (e.g. which exam '110' refers to).
If the reference is ambiguous, do not guess the field; put the unresolved phrase in unsupported.
Every evidence must be an exact non-empty substring of original_message, never earlier history.
Never invent facts from profile/history. Mark questions, hypotheticals, uncertainty, corrections and negation.
Separate completed projects/research/internships/papers from target_programs (application degrees).
Experiences use operation=add with a list of original descriptions. Preserve natural-language meaning.
target_countries describes explicit primary targets, not conditional fallback countries. Return preferences
separately. fallback_country value is a country-name string (e.g. Canada); preserve the
full fallback condition in evidence. Never extract GPA scale from an assistant question.
Unsupported concepts go in unsupported; do not force them into a different field.
Numeric facts require numeric values, list fields require string lists. Do not infer a score implies eligibility.
Allowed profile fields are provided. source must be llm for your facts. Output JSON only."""

    def __init__(self, client: LLMClient | None = None):
        self.client = client or LLMClient(timeout_seconds=30, retries=1)

    def extract(self, original: str, context: dict | None = None, *, mode: Literal["auto", "rule_only", "llm_only", "hybrid", "reject"] = "hybrid") -> ExtractionOutcome:
        precomputed_rules = rule_candidates(original) if mode == "auto" else None
        route = route_profile_extraction(original, precomputed_rules, context)
        effective_mode = route.path if mode == "auto" else mode
        if effective_mode == "reject":
            return ExtractionOutcome(mode="reject", route_path=route.path, route_reason=route.reason)
        rules = precomputed_rules if precomputed_rules is not None and effective_mode != "llm_only" else (
            rule_candidates(original) if effective_mode != "llm_only" else ExtractedCandidates()
        )
        candidates = list(rules.facts)
        preferences = list(rules.preferences)
        errors: list[str] = []
        actual_mode = "rule_only"
        semantic_failed = False
        if effective_mode not in {"rule_only", "reject"} and self.client.enabled:
            try:
                semantic = self.client.generate_structured(
                    ExtractedCandidates, system=self.SYSTEM,
                    context={"original_message": original,
                             "known_facts": [f.model_dump(mode="json") for f in rules.facts],
                             "allowed_fields": sorted(ProfileConflictResolver.PROFILE_FIELDS),
                             "conversation_context": context or {}},
                    temperature=0, max_tokens=2400,
                )
                vetoes = {(f.field, f.evidence) for f in semantic.rejected_known_facts}
                candidates = [f for f in candidates if (f.field, f.evidence) not in vetoes]
                candidates.extend(f.model_copy(update={"source": "llm"}) for f in semantic.facts)
                preferences.extend(semantic.preferences)
                errors.extend(f"unsupported: {item}" for item in semantic.unsupported)
                actual_mode = effective_mode
            except Exception as exc:
                errors.append(f"llm_unavailable: {type(exc).__name__}")
                semantic_failed = True
        elif effective_mode == "llm_only":
            raise RuntimeError("LLM-only evaluation requires configured LLM credentials")
        elif effective_mode == "hybrid":
            errors.append("llm_not_configured: rule-only fallback")
            semantic_failed = True

        validated: list[tuple[FactCandidate, CandidateFact]] = []
        for candidate in candidates:
            try:
                validated.append((candidate, validate_candidate(candidate, original)))
            except (ValueError, TypeError) as exc:
                errors.append(f"rejected:{candidate.field}:{str(exc).splitlines()[0]}")
        # Scalar conflicts within this message: explicit correction and later
        # evidence win; otherwise high-confidence rules win over model output.
        groups: dict[str, list[tuple[FactCandidate, CandidateFact]]] = {}
        for pair in validated:
            groups.setdefault(pair[0].field, []).append(pair)
        accepted = []
        for pairs in groups.values():
            if all(f.operation == "append" for _, f in pairs):
                seen = set()
                for candidate, fact in sorted(pairs, key=lambda p: (p[0].source != "rule", -p[0].confidence)):
                    values = [x for x in fact.value if x.casefold() not in seen]
                    if values:
                        seen.update(x.casefold() for x in values)
                        accepted.append(fact.model_copy(update={"normalized_value": values}))
            else:
                candidate, fact = max(pairs, key=lambda p: (
                    p[0].statement_kind in {"correction", "negation"},
                    original.rfind(p[0].evidence) if p[0].statement_kind in {"correction", "negation"} else -1,
                    p[0].source == "rule", p[0].confidence,
                    original.rfind(p[0].evidence),
                ))
                accepted.append(fact)
        prefs = {}
        for pref in preferences:
            if pref.evidence not in original:
                errors.append(f"rejected_preference:{pref.key}:evidence_not_in_original")
                continue
            if pref.key in {"avoid_gre", "employment_priority"} and not isinstance(pref.value, bool):
                errors.append(f"rejected_preference:{pref.key}:expected_boolean")
                continue
            if pref.key == "fallback_country":
                country = pref.value.get("country") if isinstance(pref.value, dict) else pref.value
                if not isinstance(country, str) or not country.strip():
                    errors.append("rejected_preference:fallback_country:expected_country")
                    continue
                canonical = ProfileNormalizer().normalize_fact(CandidateFact(
                    field="target_countries", raw_value=[country], source="conversation", evidence=pref.evidence,
                    confidence=pref.confidence)).value[0]
                pref = pref.model_copy(update={"value": {**pref.value, "country": canonical}
                                               if isinstance(pref.value, dict) else canonical})
            prefs[pref.key] = pref
        assertion, _ = assertion_text(original)
        return ExtractionOutcome(extracted_facts=candidates, accepted_facts=accepted,
                                 preferences=list(prefs.values()), errors=errors, mode=actual_mode,
                                 semantic_failed=semantic_failed,
                                 route_path=route.path, route_reason=route.reason,
                                 progress_updates=[p.model_dump(mode="json") for p in extract_progress_rules(assertion)])
