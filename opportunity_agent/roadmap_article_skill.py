"""Progressively loaded openJiuwen-style skill for roadmap article generation."""
from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.error import HTTPError, URLError

from pydantic import BaseModel, Field, ValidationError

from .domain_knowledge import assess_prerequisite_coverage, knowledge_for_profile
from .llm_client import LLMClient
from .models import OfficialResearchResult, PlanningTimeline, StudentProfile, UserState


SECTION_TITLES = {
    "current_profile_goal": "一、当前画像与目标",
    "gap_analysis": "二、与目标方向的差距",
    "current_stage_actions": "三、当前阶段行动",
    "academic_research_internship": "四、课程、科研与实习安排",
    "application_materials_timeline": "五、申请材料与时间节点",
    "risks_next_steps": "六、风险、待核验项与下一步",
}
SECTION_MIN_LENGTHS = {
    "current_profile_goal": 100,
    "gap_analysis": 120,
    "current_stage_actions": 140,
    "academic_research_internship": 160,
    "application_materials_timeline": 160,
    "risks_next_steps": 100,
}
MIN_ARTICLE_LENGTH = 900
# This is a safety guardrail, not the desired article length.  A detailed,
# profile-specific six-section roadmap commonly needs 2,000+ Chinese
# characters once it includes coursework, evidence, and dated actions.
MAX_ARTICLE_LENGTH = 4200
# A per-section allocation turns the desired article size into a concrete
# contract.  The sum leaves room for headings and source citations while
# keeping a detailed article comfortably below the hard safety ceiling.
SECTION_MAX_LENGTHS = {
    "current_profile_goal": 650,
    "gap_analysis": 650,
    "current_stage_actions": 650,
    "academic_research_internship": 650,
    "application_materials_timeline": 650,
    "risks_next_steps": 650,
}
TARGET_ARTICLE_LENGTH = 3900
# Reserve time for the concise quality-repair turn.  Without a reservation,
# the first long-form generation could consume the entire deadline and make a
# valid repair practically guaranteed to time out.
REPAIR_TIME_RESERVE_SECONDS = 45
RELEVANT_PROFILE_FIELDS = (
    "school", "major", "academic_year", "degree_years", "graduation_year", "graduation_month",
    "target_countries", "target_regions", "target_schools", "target_programs", "target_program_choices", "target_degree",
    "target_fields", "planned_enrollment_year", "planned_enrollment_month", "gpa", "gpa_raw",
    "gpa_scale", "class_rank", "toefl_score", "ielts_score", "gre_score", "budget", "exam_plan",
    "completed_courses", "skills", "hardware_skills", "research_experiences", "project_experiences",
    "paper_experiences", "competition_experiences", "internship_experiences", "summer_preference",
    "career_goal", "target_locations",
)


class SkillLoadError(ValueError):
    pass


class ArticleQualityError(ValueError):
    def __init__(self, issues: dict[str, str]) -> None:
        self.issues = issues
        super().__init__("planning article quality failed: " + "; ".join(
            f"{SECTION_TITLES.get(key, key)}: {value}" for key, value in issues.items()
        ))


class RoadmapArticleDraft(BaseModel):
    current_profile_goal: str = ""
    gap_analysis: str = ""
    current_stage_actions: str = ""
    academic_research_internship: str = ""
    application_materials_timeline: str = ""
    risks_next_steps: str = ""


class RoadmapArticlePatch(BaseModel):
    sections: dict[str, str] = Field(default_factory=dict)


class RoadmapSkillDocument(BaseModel):
    name: str
    description: str
    body: str
    path: Path


class RoadmapSkillLoader:
    """Read only allow-listed package skills, lazily and with mtime caching."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path(__file__).resolve().parent / "skills").resolve()
        self._cache: dict[str, tuple[int, RoadmapSkillDocument]] = {}
        self._lock = RLock()
        self.load_count = 0

    def load(self, name: str) -> RoadmapSkillDocument:
        if name != "roadmap_article":
            raise SkillLoadError(f"skill is not allow-listed: {name}")
        path = (self.root / name / "SKILL.md").resolve()
        if self.root not in path.parents:
            raise SkillLoadError("skill path escaped the configured root")
        try:
            stat = path.stat()
        except OSError as exc:
            raise SkillLoadError(f"roadmap skill is unavailable: {exc}") from exc
        with self._lock:
            cached = self._cache.get(name)
            if cached and cached[0] == stat.st_mtime_ns:
                return cached[1]
            try:
                raw = path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise SkillLoadError(f"roadmap skill cannot be read: {exc}") from exc
            document = self._parse(raw, path)
            self._cache[name] = (stat.st_mtime_ns, document)
            self.load_count += 1
            return document

    @staticmethod
    def _parse(raw: str, path: Path) -> RoadmapSkillDocument:
        if len(raw.encode("utf-8")) > 64 * 1024:
            raise SkillLoadError("roadmap skill exceeds 64 KiB")
        if not raw.startswith("---"):
            raise SkillLoadError("roadmap skill front matter is missing")
        parts = raw.split("---", 2)
        if len(parts) != 3:
            raise SkillLoadError("roadmap skill front matter is incomplete")
        metadata: dict[str, str] = {}
        for line in parts[1].splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in {"name", "description"}:
                metadata[key.strip()] = value.strip().strip("'\"")
        body = parts[2].strip()
        if metadata.get("name") != "roadmap_article":
            raise SkillLoadError("roadmap skill name must be roadmap_article")
        if not metadata.get("description") or not body:
            raise SkillLoadError("roadmap skill description or body is empty")
        return RoadmapSkillDocument(path=path, body=body, **metadata)


def build_article_context(profile: StudentProfile, state: UserState, timeline: PlanningTimeline,
                          research: OfficialResearchResult, revision_reason: str,
                          timeline_context: dict[str, Any], today: str) -> dict[str, Any]:
    profile_dump = profile.model_dump(mode="json")
    compact_profile = {field: profile_dump.get(field) for field in RELEVANT_PROFILE_FIELDS
                       if profile_dump.get(field) not in (None, "", [], {})}
    latest: dict[str, Any] = {}
    for fact in profile.facts:
        if fact.confidence < 0.75 or fact.needs_confirmation:
            continue
        latest[fact.field] = {
            "field": fact.field,
            "value": fact.value,
            "operation": fact.operation,
            "source": fact.source,
            "evidence": str(fact.evidence or "")[:500],
        }
    sources = [{
        "source_id": item.source_id,
        "university": item.university,
        "program": item.program,
        "title": item.title,
        "url": item.url,
        "evidence_excerpt": item.evidence_excerpt[:700],
        "retrieved_at": item.retrieved_at.isoformat(),
    } for item in research.sources]
    knowledge = knowledge_for_profile(profile)
    course_assessment = (assess_prerequisite_coverage(profile, knowledge) if knowledge else
                         {"confirmed": [], "not_confirmed": []})
    context = {
        "today": today,
        "revision_reason": revision_reason,
        "profile": compact_profile,
        "latest_accepted_facts": list(latest.values()),
        "state": state.model_dump(mode="json"),
        "timeline": timeline_context,
        "official_requirements": [item.model_dump(mode="json") for item in research.requirements],
        "official_sources": sources,
        "unresolved_official_questions": research.unresolved_questions,
        "course_assessment": {
            **course_assessment,
            "policy": "not_confirmed means the current profile/resume did not explicitly list the course; it never means the student did not take it or must make it up.",
        },
        "article_constraints": article_constraints(),
    }
    # Timeline helpers intentionally retain ``date`` objects for deterministic
    # calculations; the LLM boundary receives JSON-safe ISO strings only.
    return json.loads(json.dumps(context, ensure_ascii=False, default=str))


def article_constraints() -> dict[str, Any]:
    return {
        "language": "zh-CN",
        "total_min_characters": MIN_ARTICLE_LENGTH,
        "target_max_characters": TARGET_ARTICLE_LENGTH,
        "hard_max_characters": MAX_ARTICLE_LENGTH,
        "section_min_characters": SECTION_MIN_LENGTHS,
        "section_max_characters": SECTION_MAX_LENGTHS,
        "response": "JSON only; each section contains body text without headings",
    }


@dataclass
class RoadmapArticleSkill:
    llm_client: LLMClient
    loader: RoadmapSkillLoader | None = None
    timeout_seconds: int = 120
    max_tokens: int = 1800
    last_error: str | None = None

    def __post_init__(self) -> None:
        self.loader = self.loader or RoadmapSkillLoader()

    def generate(self, context: dict[str, Any]) -> str:
        self.last_error = None
        started = time.monotonic()
        document = self.loader.load("roadmap_article")
        repair_used = False
        try:
            draft = self._generate_draft(document, context, started)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            self.last_error = self.llm_client.last_error or f"{type(exc).__name__}: {exc}"
            raise
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError, RuntimeError):
            # A malformed/truncated structured response gets one compact schema repair.
            repair_context = {"mode": "repair_complete_json", "planning_context": context}
            draft = self._call(RoadmapArticleDraft, document.body, repair_context, started, self.max_tokens)
            repair_used = True

        issues = article_issues(draft, context)
        if issues:
            if repair_used:
                raise ArticleQualityError(issues)
            patch_context = {
                "mode": "replace_only_listed_sections_within_hard_character_budgets",
                "issues": issues,
                "required_section_names": list(issues),
                "existing_sections": draft.model_dump(),
                "article_constraints": article_constraints(),
                "planning_context": context,
            }
            patch = self._call(RoadmapArticlePatch, document.body, patch_context, started,
                               max(700, min(self.max_tokens, 1200)))
            values = draft.model_dump()
            for name in issues:
                if name in patch.sections:
                    values[name] = patch.sections[name].strip()
            draft = RoadmapArticleDraft.model_validate(values)
            final_issues = article_issues(draft, context)
            if _has_only_length_issues(final_issues):
                # A gateway may ignore a max-token hint despite returning valid
                # JSON. Keep the user's fresh plan usable by reducing only at
                # sentence boundaries; factual content is never regenerated.
                draft = clamp_draft_to_budget(draft, context)
                final_issues = article_issues(draft, context)
            if final_issues:
                raise ArticleQualityError(final_issues)
        self.last_error = None
        return assemble_article(draft)

    def generate_markdown(self, context: dict[str, Any], *, user_request: str,
                          plan_kind: str = "roadmap") -> str:
        """Generate V2 prose directly while keeping deterministic data outside the LLM."""
        self.last_error = None
        full_roadmap = plan_kind == "roadmap"
        system = (
            "你是留学申请 Planning Agent 的写作组件。根据给定画像、执行进度、确定性时间轴和官网证据，"
            "直接输出简体中文 Markdown 正文。不要输出 JSON、代码块、前言或对系统的解释。"
            "不得改变输入中的日期、分数、任务状态、学校、项目或用户经历。项目截止日期、GRE、语言、"
            "先修课、学费和材料要求只有在 official_requirements 中存在对应字段时才能写成已核实事实。"
            "引用必须使用（来源：source_id），source_id 必须来自 official_sources。资料不足时明确写待核验。"
        )
        if full_roadmap:
            system += (
                "生成完整申请规划，使用清晰的 Markdown 标题覆盖当前画像与目标、差距、当前行动、"
                "课程科研实习、申请材料时间线、风险与下一步。正文应具体、可执行，并与任务进度一致。"
            )
            minimum = 900
        else:
            system += "只回答用户本次专项规划问题；不要扩写成完整申请路线图，也不要假装替换现有计划。"
            minimum = 120
        payload = {
            "plan_kind": plan_kind,
            "user_request": user_request,
            "planning_context": context,
            "article_constraints": {
                **article_constraints(),
                "total_min_characters": minimum,
                "minimum_characters": minimum,
                "format": "markdown",
            },
        }
        text = self.llm_client.generate(
            system=system,
            user=json.dumps(payload, ensure_ascii=False, default=str),
            temperature=0.1,
            max_tokens=self.max_tokens,
            thinking=False,
        ).strip()
        if len(text) < minimum:
            raise ArticleQualityError({"article": f"正文只有 {len(text)} 字符，至少需要 {minimum} 字符"})
        if "```" in text:
            raise ArticleQualityError({"article": "Markdown 正文不得包含代码围栏"})
        allowed = {str(item.get("source_id", "")) for item in context.get("official_sources", [])}
        cited = set(re.findall(r"（来源[：:]\s*([^）]+)）", text))
        unknown = sorted(item for item in cited if item not in allowed)
        if unknown:
            raise ArticleQualityError({"article": "出现未知来源编号：" + "、".join(unknown)})
        if full_roadmap:
            missing_schools = []
            for school in sorted({str(item.get("university", "")).strip()
                                  for item in context.get("official_sources", [])}):
                ids = {str(item.get("source_id", "")) for item in context.get("official_sources", [])
                       if str(item.get("university", "")).strip() == school}
                if school and ids and not (ids & cited):
                    missing_schools.append(school)
            if missing_schools:
                raise ArticleQualityError({"article": "缺少官网来源编号：" + "、".join(missing_schools)})
        return text

    def _generate_draft(self, document: RoadmapSkillDocument, context: dict[str, Any], started: float) -> RoadmapArticleDraft:
        reserve = min(REPAIR_TIME_RESERVE_SECONDS, max(0, self.timeout_seconds - 30))
        return self._call(RoadmapArticleDraft, document.body,
                          {"mode": "generate", "planning_context": context}, started, self.max_tokens,
                          reserve_seconds=reserve)

    def _call(self, model_type, system: str, context: dict[str, Any], started: float, max_tokens: int,
              reserve_seconds: float = 0):
        remaining = self.timeout_seconds - (time.monotonic() - started)
        available = remaining - reserve_seconds
        if available <= 1:
            raise TimeoutError("roadmap article skill exhausted its total time budget")
        client = copy.copy(self.llm_client)
        client.retries = 0
        client.timeout_seconds = max(1, min(client.timeout_seconds, int(available)))
        try:
            return client.generate_structured(model_type, system=_strict_system_prompt(system), context=context,
                                              temperature=0.1, max_tokens=max_tokens, thinking=False)
        finally:
            if client.last_error:
                self.llm_client.last_error = client.last_error


def assemble_article(draft: RoadmapArticleDraft) -> str:
    return "\n\n".join(f"{SECTION_TITLES[name]}\n{getattr(draft, name).strip()}" for name in SECTION_TITLES)


def article_issues(draft: RoadmapArticleDraft, context: dict[str, Any]) -> dict[str, str]:
    issues: dict[str, str] = {}
    for name, minimum in SECTION_MIN_LENGTHS.items():
        length = len(getattr(draft, name).strip())
        if length < minimum:
            issues[name] = f"正文只有 {length} 字符，至少需要 {minimum} 字符"
        maximum = SECTION_MAX_LENGTHS[name]
        if length > maximum:
            issues[name] = f"正文达到 {length} 字符，需要压缩到不超过 {maximum} 字符"
    article_length = len(assemble_article(draft))
    if article_length < MIN_ARTICLE_LENGTH:
        for name in sorted(SECTION_TITLES, key=lambda key: len(getattr(draft, key)))[:2]:
            issues.setdefault(name, f"全文只有 {article_length} 字符，需要扩写到至少 {MIN_ARTICLE_LENGTH} 字符")
    if article_length > MAX_ARTICLE_LENGTH:
        for name in sorted(SECTION_TITLES, key=lambda key: len(getattr(draft, key)), reverse=True)[:2]:
            issues.setdefault(name, f"全文达到 {article_length} 字符，需要压缩到不超过 {MAX_ARTICLE_LENGTH} 字符")
    materials = draft.application_materials_timeline.casefold()
    sources = context.get("official_sources", [])
    source_schools = {str(item.get("university", "")).strip() for item in sources}
    missing = sorted(school for school in source_schools if school and school.casefold() not in materials)
    if missing:
        issues["application_materials_timeline"] = "缺少已有官网证据的目标学校：" + "、".join(missing)
    missing_citations = []
    for school in sorted(source_schools):
        ids = [str(item.get("source_id", "")).strip() for item in sources
               if str(item.get("university", "")).strip() == school and item.get("source_id")]
        if ids and not any(source_id.casefold() in materials for source_id in ids):
            missing_citations.append(school)
    if missing_citations:
        previous = issues.get("application_materials_timeline", "")
        citation_issue = "缺少官网来源编号：" + "、".join(missing_citations)
        issues["application_materials_timeline"] = "；".join(item for item in (previous, citation_issue) if item)
    return issues


def _strict_system_prompt(skill_body: str) -> str:
    budgets = "；".join(f"{name}≤{limit}" for name, limit in SECTION_MAX_LENGTHS.items())
    return skill_body + (
        "\n\n## 强制长度协议（高优先级）\n"
        "必须先规划六个字段的篇幅，再输出 JSON。任何字段不能超过其字符上限；"
        f"字段上限：{budgets}。六段正文总计目标不超过 {TARGET_ARTICLE_LENGTH} 字符，"
        f"绝不能超过 {MAX_ARTICLE_LENGTH} 字符。不要通过重复、罗列官网原文或复述输入来凑篇幅。"
        "当 mode 要求修复时，只返回被列出的字段，且必须用替换后的完整短文本覆盖原字段。"
    )


def _has_only_length_issues(issues: dict[str, str]) -> bool:
    return bool(issues) and all("需要压缩到不超过" in issue for issue in issues.values())


def clamp_draft_to_budget(draft: RoadmapArticleDraft, context: dict[str, Any]) -> RoadmapArticleDraft:
    """Last-resort display guard for a valid but overlong model JSON response."""
    values = draft.model_dump()
    for name, limit in SECTION_MAX_LENGTHS.items():
        values[name] = _truncate_at_sentence(values[name], limit)
    # Keep source identifiers visible after a purely mechanical trim. This is
    # provenance metadata, not a new claim about the university.
    materials = values["application_materials_timeline"]
    source_refs = [(str(item.get("university", "")).strip(), str(item.get("source_id", "")).strip())
                   for item in context.get("official_sources", [])]
    source_refs = [(school, source_id) for school, source_id in source_refs if source_id]
    if source_refs and not all(source_id in materials for _, source_id in source_refs):
        suffix = " 已加载官网依据：" + "；".join(
            f"{school or '目标学校'}（来源：{source_id}）" for school, source_id in source_refs) + "。"
        values["application_materials_timeline"] = _truncate_at_sentence(
            materials, max(SECTION_MIN_LENGTHS["application_materials_timeline"],
                           SECTION_MAX_LENGTHS["application_materials_timeline"] - len(suffix))) + suffix
    return RoadmapArticleDraft.model_validate(values)


def _truncate_at_sentence(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    boundary = max(text.rfind(mark, 0, limit) for mark in "。！？；\n")
    if boundary >= max(1, limit // 2):
        return text[:boundary + 1].strip()
    return text[:max(0, limit - 1)].rstrip() + "…"
