from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .models import (Budget, CandidateFact, ChatMessage, ExamPlan, ExtractionResult, StageSignal,
                     StudentProfile, UserState, legacy_target_program_pairs)
from .normalizer import ProfileNormalizer
from .modelscope_transport import open_modelscope_request
from .semantic_extractor import SemanticExtractor


class FastExtractor:
    """High-precision parser for explicitly labelled numbers and dates only."""

    def extract_result(self, message: str) -> ExtractionResult:
        facts: list[CandidateFact] = []
        stage_signals: list[StageSignal] = []

        year_match = re.search(r"大([一二三四])|本科\s*([1-4一二三四])\s*年级?", message)
        if year_match:
            raw = year_match.group(1) or year_match.group(2)
            normalized = {"一": 1, "二": 2, "三": 3, "四": 4}.get(raw, int(raw) if raw.isdigit() else None)
            facts.append(_fast_fact("academic_year", raw, normalized, year_match.group(0)))

        gpa_match = re.search(r"(?:gpa|绩点)\s*(?:是|为|[:：])?\s*([0-9](?:\.\d+)?)", message, re.IGNORECASE)
        if gpa_match:
            score = float(gpa_match.group(1))
            if 0.0 <= score <= 4.0:
                facts.append(_fast_fact("gpa", gpa_match.group(1), score, gpa_match.group(0)))

        score_specs = (
            ("toefl_score", r"(?:toefl|托福)\s*(?:是|为|考了|[:：])?\s*(\d{1,3})", 0.0, 120.0),
            ("ielts_score", r"(?:ielts|雅思)\s*(?:是|为|考了|[:：])?\s*(\d(?:\.\d+)?)(?![\d.])", 0.0, 9.0),
            ("gre_score", r"(?:gre)\s*(?:是|为|考了|[:：])?\s*(\d{3})", 260.0, 340.0),
        )
        for field, pattern, minimum, maximum in score_specs:
            match = re.search(pattern, message, re.IGNORECASE)
            if not match:
                continue
            score = float(match.group(1))
            if not minimum <= score <= maximum:
                continue
            normalized: int | float = int(score) if score.is_integer() else score
            facts.append(_fast_fact(field, match.group(1), normalized, match.group(0)))
            if field in {"toefl_score", "ielts_score"}:
                stage_signals.append(StageSignal(
                    stage="LANGUAGE_PREPARATION", direction="decrease", strength=0.95,
                    evidence=match.group(0),
                ))

        rank_match = re.search(r"(?:rank|排名)\s*(?:是|为|[:：])?\s*(\d+\s*/\s*\d+|前\s*\d+%?)", message, re.IGNORECASE)
        if not rank_match:
            rank_match = re.search(r"(?<![\d.])(\d+\s*/\s*\d+)(?![\d.])", message)
        if rank_match:
            normalized_rank = rank_match.group(1).replace(" ", "")
            fraction = re.fullmatch(r"(\d+)/(\d+)", normalized_rank)
            valid = not fraction or (int(fraction.group(2)) > 0 and int(fraction.group(1)) <= int(fraction.group(2)))
            if valid:
                facts.append(_fast_fact("class_rank", rank_match.group(1), normalized_rank, rank_match.group(0)))

        graduation_match = re.search(r"(20\d{2})\s*年?(?:本科)?毕业", message)
        if graduation_match:
            facts.append(_fast_fact("graduation_year", graduation_match.group(1), int(graduation_match.group(1)), graduation_match.group(0)))

        date_spans: set[tuple[int, int]] = set()
        date_patterns = (
            r"(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)",
            r"(?<!\d)(20\d{2})/(\d{1,2})/(\d{1,2})(?!\d)",
            r"(?<!\d)(20\d{2})年(\d{1,2})月(\d{1,2})日",
        )
        for pattern in date_patterns:
            for match in re.finditer(pattern, message):
                try:
                    normalized_date = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                except ValueError:
                    continue
                date_spans.add(match.span())
                facts.append(_fast_fact("explicit_date", match.group(0), normalized_date.isoformat(), match.group(0)))

        for match in re.finditer(r"(?<!\d)(20\d{2})(?!\d)", message):
            if any(start <= match.start() and match.end() <= end for start, end in date_spans):
                continue
            if graduation_match and graduation_match.start(1) == match.start(1):
                continue
            facts.append(_fast_fact("mentioned_year", match.group(1), int(match.group(1)), match.group(0)))

        budget_match = re.search(
            r"(?:预算|费用|学费)\s*(?:是|为|[:：])?\s*(\d+(?:\.\d+)?)\s*(万)?\s*(人民币|rmb|cny|美元|美金|usd|元|刀)?",
            message, re.IGNORECASE,
        )
        if budget_match:
            amount = float(budget_match.group(1)) * (10000 if budget_match.group(2) else 1)
            currency_text = (budget_match.group(3) or "").casefold()
            currency = "USD" if currency_text in {"美元", "美金", "usd", "刀"} else "CNY" if currency_text in {"人民币", "rmb", "cny", "元"} else None
            normalized_budget = {"amount": int(amount) if amount.is_integer() else amount, "currency": currency}
            facts.append(_fast_fact("budget", budget_match.group(0), normalized_budget, budget_match.group(0)))

        return ExtractionResult(
            facts=facts,
            stage_signals=stage_signals,
            should_replan=bool(facts),
        )

    def extract(self, message: str) -> list[CandidateFact]:
        return self.extract_result(message).facts


def _fast_fact(field: str, raw_value: Any, normalized_value: Any, evidence: str) -> CandidateFact:
    return CandidateFact(
        field=field,
        raw_value=raw_value,
        normalized_value=normalized_value,
        source="conversation",
        confidence=0.99,
        evidence=evidence,
    )


class LegacySemanticRuleExtractor:
    """Evidence-only fallback rules for when the semantic model is unavailable."""

    def extract(self, message: str) -> list[CandidateFact]:
        text = message.casefold()
        facts: list[CandidateFact] = []
        major = _extract_explicit_major(message)
        if major:
            facts.append(CandidateFact(field="major", raw_value=major, normalized_value=major,
                                       source="conversation", confidence=0.90, evidence=major))
        elif "cs" in text or "计算机" in message:
            raw = "计算机" if "计算机" in message else "CS"
            facts.append(CandidateFact(field="major", raw_value=raw, normalized_value="Computer Science", source="conversation", confidence=0.90, evidence=raw))
        countries = _extract_explicit_countries(message)
        if countries:
            evidence = "、".join(countries)
            facts.append(CandidateFact(field="target_countries", raw_value=countries, normalized_value=countries,
                                       source="conversation", confidence=0.88, needs_confirmation="可能" in message, evidence=evidence))
        regions = _extract_explicit_regions(message)
        if regions:
            facts.append(CandidateFact(field="target_regions", raw_value=regions, normalized_value=regions,
                                       source="conversation", confidence=0.88, needs_confirmation="可能" in message, evidence="、".join(regions)))
        schools = _extract_explicit_schools(message)
        if schools:
            facts.append(CandidateFact(field="target_schools", raw_value=schools, normalized_value=schools,
                                       source="conversation", confidence=0.90, evidence="、".join(schools)))
        programs = _extract_explicit_programs(message)
        if programs:
            facts.append(CandidateFact(field="target_programs", raw_value=programs, normalized_value=programs,
                                       source="conversation", confidence=0.86, evidence="、".join(programs)))
        facts.extend(_extract_experience_facts(message))
        degree = _extract_explicit_degree(message)
        if degree:
            raw, normalized = degree
            facts.append(CandidateFact(field="target_degree", raw_value=raw, normalized_value=normalized,
                                       source="conversation", confidence=0.86, evidence=raw))
        if "ai" in text or "人工智能" in message:
            raw = "人工智能" if "人工智能" in message else "AI相关" if "ai相关" in text else "AI"
            facts.append(CandidateFact(field="target_fields", raw_value=[raw], normalized_value=["AI"], source="conversation", confidence=0.82, evidence=raw))
        if "llm" in text and any(word in message for word in ("科研", "研究", "实验室")):
            facts.append(CandidateFact(field="research_activity", value="LLM research", source="conversation", confidence=0.90))
        if "托福" in message or "雅思" in message:
            not_started = any(word in message for word in ("没开始", "没有开始", "还没", "未开始"))
            facts.append(CandidateFact(field="language_preparation", value=not not_started, source="conversation", confidence=0.92, evidence=message))
        if any(word in message for word in ("没有科研", "无科研", "还没做科研", "暂无科研")):
            facts.append(CandidateFact(field="research_activity", value="none", source="conversation", confidence=0.96, evidence=message))
        elif any(word in message for word in ("科研", "研究", "实验室", "论文", "项目")):
            facts.append(CandidateFact(field="research_activity", value=message, source="conversation", confidence=0.90, evidence=message))
        skills = [skill for skill in ("Python", "PyTorch", "LLM", "CUDA", "Java", "Spring", "SQL", "Docker") if skill.casefold() in text]
        if skills:
            facts.append(CandidateFact(field="skills", value=skills, source="conversation", confidence=0.96, evidence=", ".join(skills)))
        career = _extract_explicit_career(message)
        if career:
            facts.append(CandidateFact(field="career_goal", raw_value=career, normalized_value=career,
                                       source="conversation", confidence=0.88, evidence=career))
        elif "ai engineer" in text or "ai工程师" in text or "人工智能工程师" in message:
            raw = "人工智能工程师" if "人工智能工程师" in message else "AI工程师" if "ai工程师" in text else "AI Engineer"
            facts.append(CandidateFact(field="career_goal", raw_value=raw, normalized_value="AI Engineer", source="conversation", confidence=0.94, evidence=raw))
        elif "后端" in message or "backend" in text:
            raw = "后端" if "后端" in message else "Backend"
            facts.append(CandidateFact(field="career_goal", raw_value=raw, normalized_value="Backend Engineer", source="conversation", confidence=0.94, evidence=raw))
        if ("实习" in message and any(word in message for word in ("找", "求", "申请", "投递"))) or "实习求职" in message or "internship search" in text:
            facts.append(CandidateFact(field="current_stage", value="internship_search", source="conversation", confidence=0.96))
        elif "找全职" in message or "秋招" in message or "full-time search" in text:
            facts.append(CandidateFact(field="current_stage", value="full_time_search", source="conversation", confidence=0.96))
        if countries:
            facts.append(CandidateFact(field="target_locations", raw_value=countries, normalized_value=countries,
                                       source="conversation", confidence=0.88, needs_confirmation="可能" in message, evidence="、".join(countries)))
        return facts


_COUNTRY_TERMS = (
    "美国", "加拿大", "英国", "澳大利亚", "澳洲", "新西兰", "新加坡", "德国", "法国", "荷兰", "瑞士",
    "日本", "韩国", "中国香港", "香港", "爱尔兰", "意大利", "西班牙", "US", "USA", "Canada", "UK",
    "Australia", "New Zealand", "Singapore", "Germany", "France", "Japan", "Korea",
)
_REGION_TERMS = ("北美", "欧洲", "东亚", "东南亚", "大洋洲", "North America", "Europe")


def _extract_explicit_countries(message: str) -> list[str]:
    matches: list[tuple[int, str]] = []
    lower = message.casefold()
    for term in _COUNTRY_TERMS:
        start = lower.find(term.casefold())
        if start >= 0:
            matches.append((start, message[start:start + len(term)]))
    selected: list[tuple[int, str]] = []
    occupied: list[tuple[int, int]] = []
    for start, value in sorted(matches, key=lambda item: (item[0], -len(item[1]))):
        end = start + len(value)
        if any(start < existing_end and end > existing_start for existing_start, existing_end in occupied):
            continue
        selected.append((start, value))
        occupied.append((start, end))
    return list(dict.fromkeys(value for _, value in selected))


def _extract_explicit_regions(message: str) -> list[str]:
    lower = message.casefold()
    return [term for term in _REGION_TERMS if term.casefold() in lower]


def _extract_explicit_schools(message: str) -> list[str]:
    schools: list[str] = []
    for match in re.finditer(r"[\u4e00-\u9fff]{2,24}(?:大学|学院)", message):
        value = re.sub(r"^(?:我(?:想|计划|准备)?|想|计划|准备|申请|目标学校(?:是|为)?|梦校(?:是|为)?|考虑|去)+", "", match.group(0))
        if len(value) >= 4:
            schools.append(value)
    english_pattern = r"\b(?:[A-Z][A-Za-z.&'-]*\s+){0,5}(?:University|College)\b|(?<![A-Za-z])(?:MIT|CMU|UCLA|NYU|LSE|NUS|NTU)(?![A-Za-z])"
    schools.extend(match.group(0).strip() for match in re.finditer(english_pattern, message))
    return list(dict.fromkeys(schools))


def _extract_explicit_programs(message: str) -> list[str]:
    # A project the user built is not a degree programme they want to apply to.
    # Require an explicit application/degree intent; the former catch-all
    # "anything ending in 项目" silently overwrote target_programs.
    patterns = (
        r"(?:申请|目标项目\s*(?:是|为)?|想读|计划读|考虑申请)\s*([^，。；;]{2,60}?(?:硕士|博士|MBA)?项目|[^，。；;]{2,60}?(?:硕士|博士|MBA))",
    )
    values: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, message, re.IGNORECASE):
            value = re.sub(r"^(?:我|想|计划|准备|申请)+", "", match.group(1)).strip()
            if value:
                values.append(value)
    return list(dict.fromkeys(values))


def _extract_experience_facts(message: str) -> list[CandidateFact]:
    """Keep explicitly reported work/research/project history as append-only facts."""
    facts: list[CandidateFact] = []
    for raw_clause in re.split(r"[，,；;。\n]+", message):
        clause = raw_clause.strip()
        if not clause or re.search(r"没有|无|未做|还没|如果|假如|假设|想找|想做|计划|打算|准备|申请|推荐|查询|请问|是否", clause):
            continue
        # A bare topic is not a personal accomplishment.  A preceding "我有"
        # can govern later comma-separated clauses ("一段科研，一个项目").
        asserted = bool(re.search(r"我(?:现在)?(?:有|做过|做了|参与|参加|完成|发了|发表)|(?:一段|一个|一项|两段|两项|两个|[2-9]段|[2-9]个)", clause))
        if not asserted:
            continue
        value = re.sub(r"^(?:我(?:现在)?(?:有|做过|做了|参与了?|参加了?|完成了?)|一段|一个|一项|两段|两项|两个|[2-9]段|[2-9]个)\s*", "", clause).strip()
        if not value:
            continue
        fields: list[str] = []
        if re.search(r"实习|internship", clause, re.IGNORECASE):
            fields.append("internship_experiences")
        if re.search(r"科研|研究|实验室|research", clause, re.IGNORECASE):
            fields.append("research_experiences")
        if re.search(r"(?:发了|发表|录用|录取).{0,24}(?:论文|期刊|会议|IEEE|ACM)|(?:论文).{0,24}(?:发表|录用)", clause, re.IGNORECASE):
            fields.append("paper_experiences")
        if re.search(r"项目|project", clause, re.IGNORECASE) and not re.search(r"目标项目|申请项目", clause):
            fields.append("project_experiences")
        for field in fields:
            facts.append(CandidateFact(field=field, raw_value=[value], normalized_value=[value],
                                       confidence=0.96, source="conversation", evidence=clause,
                                       operation="append"))
    return facts


def _extract_explicit_major(message: str) -> str | None:
    patterns = (
        r"(?:我的|我读的|所学|本科)?\s*专业\s*(?:是|为|[:：])\s*([A-Za-z][A-Za-z /&-]{1,40}|[\u4e00-\u9fff]{2,20})",
        r"(?:我是|我学的是|本科(?:就读|读)?)(?:大[一二三四]|本科[1-4])?\s*([A-Za-z][A-Za-z /&-]{1,40}|[\u4e00-\u9fff]{2,20})专业",
    )
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _extract_explicit_degree(message: str) -> tuple[str, str] | None:
    for raw, normalized in (("博士", "PhD"), ("MBA", "MBA"), ("硕士", "MS"), ("本科", "Bachelor")):
        if raw.casefold() in message.casefold():
            return raw, normalized
    return None


def _extract_explicit_career(message: str) -> str | None:
    patterns = (
        r"(?:想做|想从事|希望从事|职业目标\s*(?:是|为)?|目标岗位\s*(?:是|为)?|想找)\s*([A-Za-z][A-Za-z /&-]{1,40}?|[\u4e00-\u9fff]{2,20}?)(?=(?:实习|全职|工作|岗位|职业|，|。|；|;|$))",
    )
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _requires_semantic_context(message: str) -> bool:
    """Only corrections/negation bypass the deterministic no-LLM fast path."""
    return any(marker in message for marker in ("不再", "不考虑", "改成", "改为", "其实不", "撤销", "不要"))


# Public V1 name retained for callers and browser snapshot compatibility.
ProfileExtractor = LegacySemanticRuleExtractor


class FactExtractor(Protocol):
    def extract(self, message: str) -> list[CandidateFact]: ...


@dataclass
class ModelScopeFactExtractor:
    """OpenAI-compatible ModelScope client that only returns candidate facts.

    A missing key or a transient API error intentionally raises no exception to
    callers: HybridFactExtractor records the fallback and returns rule results.
    """

    api_key: str | None = None
    model: str | None = None
    base_url: str = "https://api-inference.modelscope.cn/v1/chat/completions"
    timeout_seconds: int = 25
    last_error: str | None = None

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.getenv("MODELSCOPE_API_KEY")
        self.model = self.model or os.getenv("MODELSCOPE_MODEL", "Qwen/Qwen3.5-35B-A3B")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def extract(self, message: str) -> list[CandidateFact]:
        if not self.enabled:
            self.last_error = "MODELSCOPE_API_KEY is not configured"
            return []
        prompt = """Extract only facts explicitly stated by the user. Return JSON only:
{"facts":[{"field":"academic_year|major|target_countries|target_degree|target_fields|graduation_year|gpa|class_rank|research_activity|language_preparation|skills|career_goal|target_locations|current_stage","value":"JSON value","confidence":0.0,"needs_confirmation":false,"evidence":"short quote"}]}
Never infer missing information. Use needs_confirmation=true for uncertain language such as 'maybe' or 'probably'."""
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 500,
            "enable_thinking": False,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": message}],
        }
        request = Request(
            self.base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with open_modelscope_request(request, timeout=self.timeout_seconds) as response:
                body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(_strip_json_fence(content))
            facts = [CandidateFact.model_validate({**fact, "source": "model"}) for fact in parsed.get("facts", [])]
            self.last_error = None
            return [fact for fact in facts if fact.field in _ALLOWED_MODEL_FIELDS]
        except (HTTPError, URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []


_ALLOWED_MODEL_FIELDS = {
    "academic_year", "major", "target_countries", "target_degree", "target_fields",
    "graduation_year", "gpa", "class_rank", "research_activity", "language_preparation",
    "skills", "career_goal", "target_locations", "current_stage",
}


def _strip_json_fence(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
    return cleaned


class HybridFactExtractor:
    """Rule facts are authoritative; the model fills only fields rules missed."""

    def __init__(self, rule_extractor: FactExtractor | None = None,
                 model_extractor: ModelScopeFactExtractor | SemanticExtractor | None = None,
                 fast_extractor: FastExtractor | None = None,
                 normalizer: ProfileNormalizer | None = None) -> None:
        self.fast_extractor = fast_extractor or FastExtractor()
        self.rule_extractor = rule_extractor or ProfileExtractor()
        self.model_extractor = model_extractor or SemanticExtractor()
        self.normalizer = normalizer or ProfileNormalizer()
        self.last_mode = "rule"
        self.last_error: str | None = None

    def extract_result(self, message: str, profile: StudentProfile | None = None,
                       state: UserState | None = None,
                       recent_messages: list[ChatMessage] | None = None,
                       progress_targets: list[dict] | None = None) -> ExtractionResult:
        from .turn_understanding import assertion_text, extract_progress_rules, progress_only, facts_cover_message, UNCERTAIN
        message, asking = assertion_text(message)
        if not message:
            self.last_mode, self.last_error = "rule_fast_path", None
            return ExtractionResult(intent="ask_advice" if asking else "no_change")
        progress_updates = extract_progress_rules(message)
        fast_result = self.fast_extractor.extract_result(message)
        fast_facts = fast_result.facts
        fast_fields = {fact.field for fact in fast_facts}
        legacy_facts = [fact for fact in self.rule_extractor.extract(message) if fact.field not in fast_fields]
        rule_facts = [*fast_facts, *legacy_facts]
        if any(update.target_kind == "event" and update.target_hint == "考试" for update in progress_updates):
            rule_facts = [fact for fact in rule_facts if fact.field != "language_preparation"]
        rule_fields = {fact.field for fact in rule_facts}
        # Explicit numerical/date answers and sufficiently rich rule results
        # are already deterministic. Do not spend an LLM request merely to
        # rediscover them; this also keeps the sidebar useful offline.
        if (not _requires_semantic_context(message) and
                (progress_only(message, progress_updates) or
                 bool(rule_facts) and facts_cover_message(message, rule_facts, progress_updates))):
            self.last_mode = "rule_fast_path"
            self.last_error = None
            facts = self.normalizer.normalize(rule_facts)
            if UNCERTAIN.search(message):
                for fact in facts:
                    fact.needs_confirmation, fact.confidence = True, min(fact.confidence, 0.69)
            return ExtractionResult(
                intent="mixed" if asking else "progress_update" if progress_updates else "profile_update",
                facts=facts,
                progress_updates=progress_updates,
                stage_signals=fast_result.stage_signals,
                should_replan=bool(facts),
            )
        semantic_result = self._semantic_result(
            message, profile or StudentProfile(user_id="anonymous"), state,
            recent_messages or [],
            progress_targets,
        )
        semantic_facts = semantic_result.facts
        removal_fields = {fact.field for fact in semantic_facts if fact.operation == "remove"}
        if removal_fields:
            rule_facts = [fact for fact in rule_facts if fact.field not in removal_fields]
            rule_fields = {fact.field for fact in rule_facts}
        supplemental = [
            fact for fact in semantic_facts
            if fact.operation != "set" or fact.field not in rule_fields
        ]
        if semantic_facts or semantic_result.progress_updates:
            self.last_mode = "hybrid"
            self.last_error = None
        else:
            self.last_mode = "rule_fallback"
            self.last_error = self.model_extractor.last_error
        facts = self.normalizer.normalize([*rule_facts, *supplemental])
        if UNCERTAIN.search(message):
            for fact in facts:
                fact.needs_confirmation, fact.confidence = True, min(fact.confidence, 0.69)
        # Explicit rule actions take priority over duplicated semantic actions.
        updates = list(progress_updates)
        for update in semantic_result.progress_updates:
            if not any(rule.action == update.action and
                       (rule.evidence == update.evidence or
                        rule.target_id and rule.target_id == update.target_id or
                        rule.target_hint and rule.target_hint == update.target_hint)
                       for rule in progress_updates):
                updates.append(update)
        return ExtractionResult(
            intent="mixed" if asking else semantic_result.intent or ("progress_update" if updates else "profile_update"),
            progress_updates=updates,
            facts=facts,
            stage_signals=[*fast_result.stage_signals, *semantic_result.stage_signals],
            information_needs=semantic_result.information_needs,
            should_replan=semantic_result.should_replan or bool(facts),
            diagnostics=semantic_result.diagnostics,
        )

    def _semantic_result(self, message: str, profile: StudentProfile,
                         state: UserState | None,
                         recent_messages: list[ChatMessage], progress_targets: list[dict] | None = None) -> ExtractionResult:
        if hasattr(self.model_extractor, "extract_sync"):
            if isinstance(self.model_extractor, SemanticExtractor):
                return self.model_extractor.extract_sync(message, profile, recent_messages, state, progress_targets=progress_targets)
            return self.model_extractor.extract_sync(message, profile, recent_messages, state)
        # Compatibility for Task-001～004 injected extractors and callers.
        return ExtractionResult(facts=self.model_extractor.extract(message))

    def extract(self, message: str) -> list[CandidateFact]:
        """V1 compatibility API; new Agent code consumes ``extract_result``."""
        return self.extract_result(message).facts


def apply_facts(profile: StudentProfile, facts: list[CandidateFact]) -> StudentProfile:
    """Apply only high-confidence facts; keep every fact for auditability."""
    updated = profile.model_copy(deep=True)
    for fact in facts:
        signature = _fact_signature(fact)
        existing = {
            _fact_signature(item)
            for item in updated.facts
        }
        if signature not in existing:
            updated.facts.append(fact)
        if fact.confidence < 0.75 or fact.needs_confirmation:
            continue
        if fact.operation == "remove":
            _remove_profile_value(updated, fact)
            continue
        if fact.operation == "append":
            if fact.field not in _LIST_PROFILE_FIELDS:
                fact.needs_confirmation = True
                continue
            current = list(getattr(updated, fact.field))
            setattr(updated, fact.field, _ordered_unique([*current, *_as_string_list(fact.value)]))
            continue
        if fact.field == "academic_year":
            updated.academic_year = int(fact.value)
        elif fact.field == "school":
            updated.school = str(fact.value)
        elif fact.field == "degree_years":
            updated.degree_years = int(fact.value)
        elif fact.field == "major":
            updated.major = str(fact.value)
        elif fact.field == "target_countries":
            updated.target_countries = _as_string_list(fact.value)
        elif fact.field == "target_regions":
            updated.target_regions = _as_string_list(fact.value)
        elif fact.field == "target_schools":
            updated.target_schools = _as_string_list(fact.value)
        elif fact.field == "target_programs":
            updated.target_programs = _as_string_list(fact.value)
        elif fact.field == "target_degree":
            updated.target_degree = str(fact.value)
        elif fact.field == "target_fields":
            updated.target_fields = _as_string_list(fact.value)
        elif fact.field == "graduation_year":
            updated.graduation_year = int(fact.value)
        elif fact.field == "graduation_month":
            updated.graduation_month = int(fact.value)
        elif fact.field == "planned_enrollment_year":
            updated.planned_enrollment_year = int(fact.value)
        elif fact.field == "planned_enrollment_month":
            updated.planned_enrollment_month = int(fact.value)
        elif fact.field == "gpa":
            try:
                updated.gpa = _parse_gpa(fact.value)
            except ValueError:
                # Keep the raw fact for audit/confirmation, but never let a
                # model-formatted GPA break the whole conversation request.
                fact.needs_confirmation = True
        elif fact.field == "toefl_score":
            updated.toefl_score = int(fact.value)
        elif fact.field == "ielts_score":
            updated.ielts_score = float(fact.value)
        elif fact.field == "gre_score":
            updated.gre_score = int(fact.value)
        elif fact.field == "class_rank":
            updated.class_rank = str(fact.value)
        elif fact.field == "gpa_raw":
            updated.gpa_raw = float(fact.value)
        elif fact.field == "gpa_scale":
            updated.gpa_scale = float(fact.value) if fact.value is not None else None
        elif fact.field == "gpa_4_reference":
            updated.gpa_4_reference = float(fact.value)
        elif fact.field == "skills":
            updated.skills = sorted(set(updated.skills) | set(_as_string_list(fact.value)))
        elif fact.field in {
            "hardware_skills", "completed_courses", "research_experiences",
            "competition_experiences", "project_experiences", "paper_experiences",
            "internship_experiences",
        }:
            setattr(updated, fact.field, _ordered_unique(_as_string_list(fact.value)))
        elif fact.field == "budget":
            updated.budget = Budget.model_validate(fact.value)
        elif fact.field == "exam_plan":
            updated.exam_plan = ExamPlan.model_validate(fact.value)
        elif fact.field == "summer_preference":
            updated.summer_preference = str(fact.value)
        elif fact.field == "onboarding_completed":
            updated.onboarding_completed = bool(fact.value)
        elif fact.field == "planning_domain":
            updated.planning_domain = str(fact.value) if fact.value else None
        elif fact.field == "career_goal":
            updated.career_goal = str(fact.value)
        elif fact.field == "target_locations":
            updated.target_locations = _as_string_list(fact.value)
        elif fact.field == "current_stage":
            updated.current_stage = str(fact.value)
    # A conversational update can still use the legacy flat fields. Do not
    # silently retain an old pair mapping once either side changed; derive only
    # unambiguous mappings and ask the profile form to resolve the rest.
    if any(fact.field in {"target_schools", "target_programs"} and fact.confidence >= .75
           and not fact.needs_confirmation for fact in facts):
        pairs, needs_review = legacy_target_program_pairs(updated.target_schools, updated.target_programs)
        updated.target_program_choices = pairs
        updated.target_program_mapping_needs_review = needs_review
    return updated


def hydrate_score_fields_from_facts(profile: StudentProfile) -> StudentProfile:
    """Upgrade old browser snapshots whose score facts predate profile fields."""
    score_fields = {"toefl_score", "ielts_score", "gre_score"}
    missing = [
        fact for fact in profile.facts
        if fact.field in score_fields and getattr(profile, fact.field) is None
        and fact.operation != "remove" and not fact.needs_confirmation and fact.confidence >= 0.75
    ]
    return apply_facts(profile, missing) if missing else profile


_LIST_PROFILE_FIELDS = {
    "target_countries", "target_regions", "target_schools", "target_programs",
    "target_fields", "skills", "hardware_skills", "completed_courses",
    "research_experiences", "competition_experiences", "project_experiences",
    "paper_experiences", "internship_experiences", "target_locations",
}
_SCALAR_PROFILE_FIELDS = {
    "school", "academic_year", "degree_years", "major", "target_degree",
    "graduation_year", "graduation_month", "planned_enrollment_year",
    "planned_enrollment_month", "gpa", "gpa_raw", "gpa_scale", "gpa_4_reference",
    "class_rank", "toefl_score", "ielts_score", "gre_score", "career_goal",
    "current_stage", "budget", "exam_plan", "summer_preference",
    "onboarding_completed", "planning_domain",
}


def _fact_signature(fact: CandidateFact) -> tuple[str, str, str]:
    raw = json.dumps(fact.raw_value, ensure_ascii=False, sort_keys=True, default=str)
    normalized = json.dumps(fact.normalized_value, ensure_ascii=False, sort_keys=True, default=str)
    return fact.field, fact.operation, f"{raw}|{normalized}"


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _remove_profile_value(profile: StudentProfile, fact: CandidateFact) -> None:
    if fact.field in _LIST_PROFILE_FIELDS:
        removals = {item.casefold() for item in _as_string_list(fact.value)}
        current = getattr(profile, fact.field)
        setattr(profile, fact.field, [item for item in current if item.casefold() not in removals])
        return
    if fact.field not in _SCALAR_PROFILE_FIELDS:
        return
    current = getattr(profile, fact.field)
    if current is None or fact.value is None or str(current).casefold() == str(fact.value).casefold():
        setattr(profile, fact.field, None)


def _parse_gpa(value: Any) -> float:
    """Accept model output such as ``3.9, Rank 5/120`` without crashing."""
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        match = re.search(r"(?<!\d)([0-4](?:\.\d+)?)(?!\d)", str(value))
        if not match:
            raise ValueError(f"could not find a 0-4 GPA in {value!r}")
        parsed = float(match.group(1))
    if not 0.0 <= parsed <= 4.0:
        raise ValueError(f"GPA must be between 0 and 4, got {parsed}")
    return parsed


def _as_string_list(value: Any) -> list[str]:
    """Accept either a JSON array or a single/comma-separated model string."""
    return StudentProfile.normalize_string_lists(value)


def next_profile_question(profile: StudentProfile) -> str | None:
    requirement = next_profile_requirement(profile)
    return requirement[1] if requirement else None


def next_profile_requirement(profile: StudentProfile) -> tuple[str, str] | None:
    fields = (
        ("academic_year", profile.academic_year is None, "你现在是本科第几年？"),
        ("major", profile.major is None, "你的专业是什么？"),
        ("target_degree", profile.target_degree is None, "你计划申请硕士、博士还是本科转学？"),
        ("target_countries", not profile.target_countries, "你目前更倾向哪些目标国家？"),
        ("target_fields", not profile.target_fields, "你更感兴趣的研究或职业方向是什么？"),
    )
    return next(((field, question) for field, missing, question in fields if missing), None)


def next_enrichment_question(profile: StudentProfile) -> str | None:
    requirement = next_enrichment_requirement(profile)
    return requirement[1] if requirement else None


def next_enrichment_requirement(profile: StudentProfile) -> tuple[str, str] | None:
    """Return the remaining facts required before automatic roadmap generation."""
    fields = (
        ("gpa_or_rank", profile.gpa is None and profile.class_rank is None, "你的当前 GPA 或专业排名大约是多少？如果暂时没有，可以回复“暂未确定”。"),
        ("language_preparation", profile.toefl_score is None and profile.ielts_score is None and not any(f.field == "language_preparation" and f.operation != "remove" for f in profile.facts), "你是否已开始准备托福或雅思？目前大约是什么水平或计划何时考试？"),
        ("research_activity", not any(f.field == "research_activity" and f.operation != "remove" for f in profile.facts), "你是否有科研、竞赛或项目经历？尤其是和目标方向相关的经历。"),
        ("graduation_year", profile.graduation_year is None, "你预计哪一年本科毕业？"),
    )
    return next(((field, question) for field, missing, question in fields if missing), None)


def next_roadmap_requirement(profile: StudentProfile) -> tuple[str, str] | None:
    """One ordered onboarding requirement; route generation waits until none remain."""
    return next_profile_requirement(profile) or next_enrichment_requirement(profile)
