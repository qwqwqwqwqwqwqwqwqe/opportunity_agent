"""Build and label the ten-university Research Agent benchmark.

Drafts are intentionally not gold.  Only ``export`` can produce ``gold.json``
after all required human reviews and evidence judgments are complete.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from ...official_research import classify_program_page
from ..rag.ingest import chunk_sectioned_text
from ..research.rewrite import query_rewrites
from .database import default_eval_database_url
from .corpus import consolidate_sources, deduplicate_documents, page_types, repair_corpus_duplicates


VERSION = "research-real150-v2-e5-sectioned"
CHUNKER_VERSION = "e5-multilingual-small-350-50-sectioned-v2"
TOKENIZER_MODEL = "intfloat/multilingual-e5-small"
GENERATOR_VERSION = "ten-school-cases-v1"
PROGRAM_CLASSIFIER_VERSION = "programme-identity-v4-parent-policy-scope"
AS_OF = "2026-10-06"
INTAKE = "2027 Fall"
DEFAULT_DIR = Path(__file__).resolve().parents[3] / "deliverables" / "research" / "real150"

SCHOOLS = [
    {"code": "cmu", "name": "Carnegie Mellon University", "domain": "cmu.edu",
     "programs": ["Master of Science in Computer Science", "Master of Science in Electrical and Computer Engineering"]},
    {"code": "uiuc", "name": "University of Illinois Urbana-Champaign", "domain": "illinois.edu",
     "programs": ["Master of Computer Science", "Master of Science in Electrical and Computer Engineering"]},
    {"code": "ucsd", "name": "University of California, San Diego", "domain": "ucsd.edu",
     "programs": ["Master of Science in Computer Science and Engineering", "Master of Science in Electrical and Computer Engineering"]},
    {"code": "ucsb", "name": "University of California, Santa Barbara", "domain": "ucsb.edu",
     "programs": ["Master of Science in Computer Science", "Master of Science in Electrical and Computer Engineering"]},
    {"code": "uw", "name": "University of Washington", "domain": "washington.edu",
     "programs": ["Professional Master's Program in Computer Science and Engineering", "Master of Science in Electrical and Computer Engineering"]},
    {"code": "duke", "name": "Duke University", "domain": "duke.edu",
     "programs": ["Master of Science in Computer Science", "Master of Science in Electrical and Computer Engineering"]},
    {"code": "brown", "name": "Brown University", "domain": "brown.edu",
     "programs": ["Master of Science in Computer Science", "Master of Science in Electrical and Computer Engineering"]},
    {"code": "neu", "name": "Northeastern University", "domain": "northeastern.edu",
     "programs": ["Master of Science in Computer Science", "Master of Science in Electrical and Computer Engineering"]},
    {"code": "usc", "name": "University of Southern California", "domain": "usc.edu",
     "programs": ["Master of Science in Computer Science", "Master of Science in Electrical and Computer Engineering"]},
    {"code": "gt", "name": "Georgia Institute of Technology", "domain": "gatech.edu",
     "programs": ["Master of Science in Computer Science", "Master of Science in Electrical and Computer Engineering"]},
]

THEMES = [
    ("multimodal_health", "医学影像与病历文本联合建模", "multimodal learning for healthcare"),
    ("vision_robotics", "视觉感知与机器人导航", "visual perception and robot navigation"),
    ("nlp_llm", "自然语言处理与大语言模型", "natural language processing and large language models"),
    ("recommender", "推荐系统与用户行为建模", "recommender systems and user behavior modeling"),
    ("ml_systems", "大规模训练与分布式机器学习系统", "large-scale training and distributed ML systems"),
    ("security", "系统安全、隐私与可信机器学习", "systems security, privacy, and trustworthy ML"),
    ("hci", "人机交互与无障碍智能产品", "human-computer interaction and accessible intelligent products"),
    ("edge_ai", "端侧推理、嵌入式系统与模型压缩", "edge inference, embedded systems, and model compression"),
    ("signal", "信号处理、通信与数据驱动方法", "signal processing, communications, and data-driven methods"),
    ("architecture", "芯片设计与计算机体系结构", "chip design and computer architecture"),
]

# Curated official fallbacks are attempted before search results when a site's
# ranking repeatedly returns unrelated or blocked pages. They remain subject to
# the same official-domain reader and human programme-scope review.
PAGE_SEEDS = {
    ("uiuc-master-of-computer-science-2027-fall", "requirements"):
        "https://siebelschool.illinois.edu/academics/graduate/professional-mcs/app-info",
    ("uiuc-master-of-computer-science-2027-fall", "research"):
        "https://siebelschool.illinois.edu/research/areas",
    ("ucsb-master-of-science-in-computer-science-2027-fall", "admissions"):
        "https://engage.graddiv.ucsb.edu/portal/programs?cmd=prog&code=CMPSC-MS-0",
    ("uw-professional-master-s-program-in-computer-science-and-engineering-2027-fall", "research"):
        "https://www.cs.washington.edu/research/",
    ("usc-master-of-science-in-electrical-and-computer-engineering-2027-fall", "research"):
        "https://minghsiehece.usc.edu/research/",
    ("usc-master-of-science-in-computer-science-2027-fall", "admissions"):
        "https://viterbigradadmission.usc.edu/programs/masters/msprograms/computer-science/ms-computer-science-/",
    ("usc-master-of-science-in-electrical-and-computer-engineering-2027-fall", "admissions"):
        "https://viterbigradadmission.usc.edu/programs/masters/msprograms/electrical-computer-engineering/ms-electrical-and-computer-engineering/",
    ("gt-master-of-science-in-electrical-and-computer-engineering-2027-fall", "admissions"):
        "https://ece.gatech.edu/future-students/graduate-admissions",
}

# Some schools publish universal Graduate School policies separately from each
# department's programme page. Keep these URLs explicitly tied to the target
# programme and fact they can support; arbitrary university-wide pages are not
# accepted as programme evidence.
DUKE_GRADUATE_POLICY_PAGES = {
    "duke-master-of-science-in-computer-science-2027-fall": [
        {"url": "https://gradschool.duke.edu/admissions/application-deadlines/",
         "page_type": "admissions", "field": "deadline"},
        {"url": "https://gradschool.duke.edu/admissions/application-instructions/gre-scores/",
         "page_type": "requirements", "field": "gre_policy"},
        {"url": "https://gradschool.duke.edu/admissions/application-instructions/english-language-proficiency-test-scores/",
         "page_type": "requirements", "field": "language"},
    ],
    "duke-master-of-science-in-electrical-and-computer-engineering-2027-fall": [
        {"url": "https://gradschool.duke.edu/admissions/application-deadlines/",
         "page_type": "admissions", "field": "deadline"},
        {"url": "https://gradschool.duke.edu/admissions/application-instructions/gre-scores/",
         "page_type": "requirements", "field": "gre_policy"},
        {"url": "https://gradschool.duke.edu/admissions/application-instructions/english-language-proficiency-test-scores/",
         "page_type": "requirements", "field": "language"},
    ],
}


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def program_catalog() -> list[dict]:
    rows = []
    for school_index, school in enumerate(SCHOOLS):
        for program_index, program in enumerate(school["programs"]):
            rows.append({
                "id": f"{school['code']}-{slug(program)}-2027-fall",
                "school_code": school["code"], "university": school["name"],
                "program": program, "intake": INTAKE, "country": "US",
                "official_domain": school["domain"], "delivery": "on_campus",
                "degree_level": "masters", "area": "cs" if program_index == 0 else "ece",
                "split": "dev" if program_index == school_index % 2 else "test",
                "review_status": "pending", "aliases": [],
            })
    return rows


def _school_categories(index: int) -> list[str]:
    return (["sql"] * (3 if index < 5 else 2) + ["rag"] * 3 + ["profile_match"] * 3
            + ["hybrid"] * 3 + ["mcp_web"] * (1 if index < 5 else 2) + ["negative"] * 2)


DEV_EXTRAS = [
    ("sql", "negative"), ("sql", "mcp_web"), ("sql", "negative"), ("sql", "mcp_web"),
    ("sql", "negative"), ("sql", "negative"), ("sql", "mcp_web"), ("sql", "negative"),
    ("mcp_web", "negative"), ("mcp_web", "negative"),
]


def _language_schedule(seed: int) -> list[str]:
    values = ["zh"] * 90 + ["en"] * 30 + ["mixed"] * 30
    random.Random(seed).shuffle(values)
    return values


def _claims(program_id: str, category: str, theme: str) -> list[str]:
    if category == "sql":
        return [f"{program_id}:gre-policy", f"{program_id}:deadline"]
    if category == "mcp_web":
        return [f"{program_id}:latest-gre-policy", f"{program_id}:latest-deadline"]
    if category in {"rag", "profile_match"}:
        return [f"{program_id}:fit:{theme}"]
    if category == "hybrid":
        return [f"{program_id}:gre-policy", f"{program_id}:deadline", f"{program_id}:fit:{theme}"]
    return []


def _query(language: str, category: str, school: dict, program: str, theme: tuple[str, str, str], variant: int) -> str:
    _, zh_topic, en_topic = theme
    short = school["code"].upper()
    if category == "sql":
        zh = f"请核验 {short} 的 {program} 2027 秋季申请截止日期和 GRE 政策。"
        en = f"Verify the Fall 2027 application deadline and GRE policy for {school['name']} {program}."
    elif category == "rag":
        zh = f"{short} 的 {program} 在{zh_topic}方面有哪些课程、实验室或研究方向？"
        en = f"Which courses, labs, or research areas in {school['name']} {program} support {en_topic}?"
    elif category == "profile_match":
        zh = (f"我做过{zh_topic}项目，负责数据处理、模型训练和实验分析，希望硕士阶段继续深入但不想只做通用软件开发。"
              f"{short} 的 {program} 是否有与这段经历直接衔接的课程或研究机会？")
        en = (f"I built a project around {en_topic}, covering data preparation, model training, and experimental analysis. "
              f"What courses or research opportunities in {school['name']} {program} directly match this background?")
    elif category == "hybrid":
        zh = (f"我有{zh_topic}项目经历。判断 {short} 的 {program} 是否适合这个方向，并同时核验 2027 秋季是否不强制 GRE、"
              "截止日期是否晚于 2026 年 11 月 1 日；缺少证据的条件不要推断。")
        en = (f"Given my project experience in {en_topic}, assess whether {school['name']} {program} fits, while verifying "
              "that Fall 2027 does not require the GRE and the deadline is after November 1, 2026. Do not infer missing facts.")
    elif category == "mcp_web":
        zh = f"请重新查看 {short} 官网，核验 {program} 2027 秋季最新的 GRE 政策和申请截止日期。"
        en = f"Recheck the official {school['name']} website for the latest Fall 2027 GRE policy and deadline for {program}."
    else:
        negative = [
            f"{short} 的 {program} 能保证我毕业后进入顶级 AI 公司吗？",
            f"只看学校简称，告诉我这个项目 2028 春季确定的截止日期，不需要引用来源。",
        ][variant % 2]
        zh, en = negative, (f"Does {school['name']} {program} guarantee a job at a leading AI company after graduation?"
                            if variant % 2 == 0 else
                            f"Without citing sources, give the confirmed Spring 2028 deadline for {school['name']} {program}.")
    if language == "zh":
        return zh
    if language == "en":
        return en
    return zh + " Please answer with evidence from the official programme pages."


def generate_draft(seed: int = 1729) -> dict:
    programs = program_catalog()
    by_school_split = {(p["school_code"], p["split"]): p for p in programs}
    languages = _language_schedule(seed)
    cases, global_index = [], 0
    for school_index, school in enumerate(SCHOOLS):
        categories = _school_categories(school_index)
        wanted_dev = Counter(["rag", "profile_match", "hybrid", *DEV_EXTRAS[school_index]])
        assigned = Counter()
        for local_index, category in enumerate(categories):
            split = "dev" if assigned[category] < wanted_dev[category] else "test"
            if split == "dev":
                assigned[category] += 1
            program = by_school_split[(school["code"], split)]
            theme = THEMES[(global_index + school_index) % len(THEMES)]
            language = languages[global_index]
            case_id = f"{school['code']}-{category}-{local_index + 1:02d}"
            outcome = "abstain" if category == "negative" and local_index % 2 == 0 else "needs_user" if category == "negative" else "answer"
            filters = {"school": school["name"], "program": program["program"], "intake": INTAKE}
            cases.append({
                "id": case_id, "group": f"{program['id']}:{category}:{theme[0]}", "split": split,
                "category": category, "query": _query(language, category, school, program["program"], theme, local_index),
                "language": language, "query_origin": "generated_pending_human_review", "generator_version": GENERATOR_VERSION,
                "as_of": AS_OF, "filters": filters, "candidate_program_ids": [program["id"]],
                "profile_context": ({"research_interests": [theme[1]], "research_interests_en": [theme[2]],
                    "experiences": [f"project:{theme[0]}"]} if category in {"profile_match", "hybrid"} else {}),
                "semantic_tags": (["profile-project-fit", "low-lexical-overlap"] if category == "profile_match" else
                    ["hybrid-profile-fit"] if category == "hybrid" else ["explicit-semantic"] if category == "rag" else [])
                    + (["cross-lingual"] if language in {"zh", "mixed"} and category in {"rag", "profile_match", "hybrid"} else []),
                "relevant_ids": [], "relevance_judgments": {}, "gold_programs": [],
                "required_claims": _claims(program["id"], category, theme[0]),
                "expected_route": "rag" if category in {"rag", "profile_match", "negative"} else category,
                "expected_outcome": outcome, "target_count": 0 if category == "negative" else 1,
                "annotation_status": "draft_unreviewed", "query_review_status": "pending",
            })
            global_index += 1
    data = {"version": VERSION, "synthetic": False, "query_source": "generated_pending_human_review",
            "as_of": AS_OF, "generator_seed": seed, "generator_version": GENERATOR_VERSION,
            "programs": programs, "cases": cases, "documents": []}
    validate_draft(data)
    return data


def validate_draft(data: dict) -> dict:
    cases = data.get("cases", [])
    for name in ("sources", "documents"):
        identifiers = [item["id"] for item in data.get(name, []) if item.get("id")]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"Duplicate {name} IDs; run repair-corpus before evaluation")
    as_of = data.get("as_of", "")
    if any(document.get("metadata", {}).get("retrieved_at", "") > as_of
           for document in data.get("documents", [])):
        raise ValueError("Corpus contains a page collected after the simulated query date")
    if len(cases) != 150:
        raise ValueError("Dataset must contain exactly 150 cases")
    expected = {"sql": 25, "rag": 30, "profile_match": 30, "hybrid": 30, "mcp_web": 15, "negative": 20}
    if Counter(c["category"] for c in cases) != expected:
        raise ValueError("Category quotas do not match the locked plan")
    if Counter(c["language"] for c in cases) != {"zh": 90, "en": 30, "mixed": 30}:
        raise ValueError("Language quotas do not match the locked plan")
    if Counter(c["split"] for c in cases) != {"dev": 50, "test": 100}:
        raise ValueError("Split must be 50 dev / 100 test")
    school_counts = Counter(c["filters"]["school"] for c in cases)
    if set(school_counts.values()) != {15} or len(school_counts) != 10:
        raise ValueError("Each of the ten schools must own 15 cases")
    split_by_group, split_by_program = {}, {}
    for case in cases:
        for mapping, key in ((split_by_group, case["group"]),
                             (split_by_program, tuple(case.get("candidate_program_ids", [])))):
            previous = mapping.setdefault(key, case["split"])
            if previous != case["split"]:
                raise ValueError("Question family or programme leaks across splits")
    return {"case_count": 150, "categories": expected, "languages": dict(Counter(c["language"] for c in cases)),
            "splits": dict(Counter(c["split"] for c in cases)), "schools": dict(school_counts)}


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def archive_removed_review_items(root: Path, previous_queue: list[dict], current_queue: list[dict]) -> int:
    """Keep snapshots for Previous when a corpus refresh removes queue entries.

    Archived items are not part of the active review queue or gold export. They
    only let an annotator inspect/correct a stale browser bookmark.
    """
    path = root / "review-archive.json"
    archived = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    records = {row.get("item", {}).get("id"): row for row in archived if row.get("item", {}).get("id")}
    active_ids = {item.get("id") for item in current_queue}
    added = 0
    for item in previous_queue:
        item_id = item.get("id")
        if not item_id or item_id in active_ids or item_id in records:
            continue
        records[item_id] = {"item": copy.deepcopy(item), "archived_at": date.today().isoformat(),
                            "reason": "removed_from_active_review_queue"}
        added += 1
    if added:
        write_json(path, list(records.values()))
    return added


def normalise_document_ids(documents: list[dict]) -> list[dict]:
    """Make chunk identity programme-scoped even when two programmes reuse one department page."""
    seen = set()
    output = []
    for item in documents:
        data = copy.deepcopy(item)
        meta = data.get("metadata", {})
        basis = "|".join([str(meta.get("chunker_version", "legacy")),
                           str(meta.get("tokenizer_model", "")), str(meta.get("tokenizer_revision") or "unversioned"),
                           str(meta.get("program_id", "")), data.get("url", ""),
                           str(meta.get("content_hash", "")), str(meta.get("section_path", "")),
                           str(meta.get("chunk_index", "")), data.get("text", "")])
        identifier = "chunk-" + hashlib.sha256(basis.encode()).hexdigest()[:32]
        if identifier in seen:
            continue
        seen.add(identifier)
        data["id"] = identifier
        output.append(data)
    return output


def _page_type(text: str) -> str:
    lowered = text.casefold()
    if re.search(r"deadline|admission|apply|申请|gre|toefl|ielts", lowered):
        return "admissions"
    if re.search(r"curricul|course|课程", lowered):
        return "curriculum"
    return "research"


def _quote_window(text: str, start: int, end: int, *, before: int = 420, after: int = 520) -> str:
    """Keep evidence readable by starting and ending at a sentence/paragraph boundary."""
    left, right = max(0, start - before), min(len(text), end + after)
    prefix = text[left:start]
    boundary = max(prefix.rfind("\n\n"), prefix.rfind(". "), prefix.rfind("! "), prefix.rfind("? "))
    if boundary >= 0:
        left += boundary + (2 if prefix[boundary:boundary + 2] in {"\n\n", ". ", "! ", "? "} else 1)
    suffix = text[end:right]
    endings = [index for index in (suffix.find("\n\n"), suffix.find(". "), suffix.find("! "), suffix.find("? ")) if index >= 0]
    if endings:
        right = end + min(endings) + 1
    return re.sub(r"\s+", " ", text[left:right]).strip()


def _reconstruct_page_text(documents: list[dict]) -> str:
    """Restore a page from overlapping chunks while retaining section transitions."""
    combined = ""
    previous_section = None
    for document in documents:
        piece = str(document.get("text", ""))
        metadata = document.get("metadata", {})
        section = str(metadata.get("section_path", ""))
        if not combined:
            combined = (f"## {section}\n" if section else "") + piece
            previous_section = section
            continue
        if section != previous_section:
            if section:
                combined += f"\n\n## {section}\n"
            else:
                combined += "\n\n"
            combined += piece
            previous_section = section
            continue
        overlap = 0
        for size in range(min(len(combined), len(piece), 1600), 7, -1):
            if combined.endswith(piece[:size]):
                overlap = size
                break
        combined += piece[overlap:]
    return combined


def _load_e5_tokenizer():
    from ..rag.models import shared_embedder

    embedder = shared_embedder()
    tokenizer = embedder.tokenizer()
    if not getattr(tokenizer, "is_fast", False):
        raise RuntimeError(f"{TOKENIZER_MODEL} fast tokenizer with offset mappings is required")
    return tokenizer, embedder.model_name, embedder.revision


def _documents_for_source(program: dict, source: dict, text: str, tokenizer,
                          tokenizer_model: str, tokenizer_revision: str | None) -> list[dict]:
    body_hash = source.get("content_hash") or hashlib.sha256(text.encode()).hexdigest()
    retrieved_at = source.get("retrieved_at", date.today().isoformat())
    expires_at = (date.fromisoformat(str(retrieved_at)[:10]) + timedelta(days=365)).isoformat()
    title = source.get("title", "")
    temporal_scope = source.get("temporal_scope", "evergreen_pending_review")
    documents = []
    for index, (piece, section_path) in enumerate(chunk_sectioned_text(text, tokenizer=tokenizer)):
        identity = "|".join((CHUNKER_VERSION, tokenizer_model, tokenizer_revision or "unversioned",
                             program["id"], source["url"], body_hash, section_path, str(index), piece))
        chunk_id = "chunk-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        documents.append({
            "id": chunk_id, "source_id": source["id"], "url": source["url"], "title": title,
            "text": piece,
            "metadata": {
                "school": program["university"], "program": program["program"], "program_id": program["id"],
                "intake": INTAKE, "page_type": source.get("page_type", "unknown"), "section_path": section_path,
                **({"page_types": source["page_types"]} if "page_types" in source else {}),
                "page_title": title, "temporal_scope": temporal_scope,
                "program_match": "pending_review", "retrieved_at": retrieved_at, "expires_at": expires_at,
                "content_hash": body_hash, "official_domain": program["official_domain"],
                "chunk_index": index,
                "chunker_version": CHUNKER_VERSION, "tokenizer_mode": "e5",
                "tokenizer_model": tokenizer_model, "tokenizer_revision": tokenizer_revision,
            },
        })
    return documents


def _retokenize_existing_documents(sources: list[dict], documents: list[dict], programs: list[dict],
                                   tokenizer, tokenizer_model: str,
                                   tokenizer_revision: str | None,
                                   program_ids: set[str] | None = None) -> list[dict]:
    """Migrate frozen page snapshots to E5-tokenized, section-aware chunk IDs."""
    programs_by_id = {program["id"]: program for program in programs}
    documents_by_source: dict[str, list[dict]] = defaultdict(list)
    for document in deduplicate_documents(documents):
        documents_by_source[document.get("source_id", "")].append(document)
    output, visited = [], set()
    for source in consolidate_sources(sources):
        source_id = source.get("id")
        prior = documents_by_source.get(source_id, [])
        if not source_id or not prior:
            continue
        visited.add(source_id)
        if program_ids is not None and source.get("program_id") not in program_ids:
            output.extend(prior)
            continue
        if all(item.get("metadata", {}).get("chunker_version") == CHUNKER_VERSION
               and item.get("metadata", {}).get("tokenizer_model") == tokenizer_model
               and item.get("metadata", {}).get("tokenizer_revision") == tokenizer_revision
               for item in prior):
            output.extend(prior)
            continue
        program = programs_by_id.get(source.get("program_id"))
        page_text = _reconstruct_page_text(prior)
        if program and page_text.strip():
            output.extend(_documents_for_source(program, source, page_text, tokenizer,
                                                tokenizer_model, tokenizer_revision))
    output.extend(document for document in documents if document.get("source_id") not in visited)
    return deduplicate_documents(output)


def _fact_candidates(
    text: str, *, target_intake: str | None = None, target_degree: str | None = None,
) -> list[dict]:
    facts = []
    intake_match = re.search(r"20\d{2}", target_intake or "")
    intake_year = int(intake_match.group()) if intake_match else None
    months = {name: index for index, name in enumerate(
        "January February March April May June July August September October November December".split(), 1)}
    dates = re.compile(
        r"(?i)\b(?:(?P<iso_year>20\d{2})-(?P<iso_month>\d{1,2})-(?P<iso_day>\d{1,2})|"
        r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{2}))?)\b")
    degree_patterns = (
        ("doctoral", re.compile(r"(?i)(?<![A-Za-z])(?:Ph\.?\s*D\.?|doctor(?:al|ate)?)")),
        ("masters", re.compile(r"(?i)(?<![A-Za-z])(?:M\.?\s*S\.?|master(?:'s)?(?:\s+of\s+science)?)")),
    )
    target = (target_degree or "").casefold()
    target_kind = "doctoral" if re.search(r"ph\.?\s*d|doctor", target) else (
        "masters" if re.search(r"master|\bms\b", target) else None)
    previous_date_end = 0
    has_degree_scoped_date = False
    for match in dates.finditer(text):
        before = text[max(0, match.start() - 240):match.start()]
        # "Applications open ..." is a date, but not an application deadline.
        opening_marker = re.search(r"\bapplications?\s+(?:will\s+)?opens?\s*:?[\s]*$", before[-120:], re.I)
        if opening_marker:
            previous_date_end = match.end()
            continue

        local_context = before[-240:] + " " + text[match.end():min(len(text), match.end() + 80)]
        if not re.search(r"deadline|due\s+date|apply\s+by|最后期限|截止日期", local_context, re.I):
            previous_date_end = match.end()
            continue

        # Degree labels belong to the following date up to the next date. This
        # prevents a Ph.D. deadline from being attached to an M.S. programme.
        degree_context_start = max(previous_date_end, match.start() - 100)
        degree_context = text[degree_context_start:match.start()]
        labels = [(label.start(), kind) for kind, pattern in degree_patterns
                  for label in pattern.finditer(degree_context)]
        degree_kind = max(labels, default=(0, None), key=lambda item: item[0])[1]
        has_degree_scoped_date = has_degree_scoped_date or degree_kind is not None
        if target_kind and degree_kind and target_kind != degree_kind:
            previous_date_end = match.end()
            continue

        if match.group("iso_year"):
            year, month, day = int(match.group("iso_year")), int(match.group("iso_month")), int(match.group("iso_day"))
        else:
            year = int(match.group("year")) if match.group("year") else intake_year
            month = months[match.group("month").title()]
            day = int(match.group("day"))
        if year is None or (intake_year is not None and year not in {intake_year - 1, intake_year}):
            previous_date_end = match.end()
            continue
        try:
            value = date(year, month, day).isoformat()
        except ValueError:
            previous_date_end = match.end()
            continue
        label_start = max(0, match.start() - 100)
        # Keep the degree label in the evidence quote when the page supplies one.
        if labels:
            label_start = degree_context_start + labels[-1][0]
        quote = re.sub(r"\s+", " ", text[max(0, label_start):match.end()]).strip()
        facts.append({"field": "deadline", "value": value, "quote": quote, "review_status": "pending"})
        previous_date_end = match.end()

    # Some catalogue tables separate the intake, deadline header, and date into
    # adjacent cells. Use this conservative fallback only when the page had no
    # parseable date; never let it override an explicit degree-labelled date.
    if not facts and not has_degree_scoped_date and intake_year is not None:
        table_row = re.compile(
            rf"(?is)\b(?:Fall|Autumn)\s+{intake_year}\b(?:(?!\b(?:Fall|Spring|Summer|Autumn|Winter)\s+20\d{{2}}\b).){{0,120}}?"
            r"\b(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
            r"(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b")
        for match in table_row.finditer(text):
            year = int(match.group(3))
            if year in {intake_year - 1, intake_year}:
                month, day = months[match.group(1).title()], int(match.group(2))
                try:
                    value = date(year, month, day).isoformat()
                except ValueError:
                    continue
                header_start = text.rfind("Final Deadline", max(0, match.start() - 160), match.start())
                quote_start = header_start if header_start >= 0 else match.start()
                quote = re.sub(r"\s+", " ", text[quote_start:match.end()]).strip()
                facts.append({"field": "deadline", "value": value, "quote": quote, "review_status": "pending"})
    for match in re.finditer(r"(?i).{0,220}\bGRE\b.{0,220}", text):
        keyword = re.search(r"(?i)\bGRE\b", match.group(0))
        quote = _quote_window(text, match.start() + keyword.start(), match.start() + keyword.end()) if keyword else match.group(0)
        lowered = quote.casefold()
        # "Applicants who do not submit" commonly follows an optional policy;
        # it is not equivalent to a university refusing GRE scores. Likewise,
        # a ban on the at-home test is not a ban on GRE scores overall.
        if re.search(r"\bgre\b.{0,80}(?:will be |is )?optional|strongly recommended", lowered):
            value = "optional"
        elif re.search(r"(?:does not|do not|doesn't|don't|no longer)\s+require\s+(?:the\s+)?gre\b", lowered):
            value = "not_required"
        elif re.search(r"\bat[ -]?home\b", lowered) and re.search(r"not accepted|will not be considered", lowered):
            value = None
        elif re.search(r"\bgre\b.{0,80}(?:not accepted|will not be considered)", lowered):
            value = "not_accepted"
        elif re.search(r"\bgre\b.{0,80}(?:not required|no longer require)", lowered):
            value = "not_required"
        elif re.search(r"\bgre\b.{0,80}\bwaived\b", lowered):
            value = None if re.search(r"for applicants|who (?:have|are)|if ", lowered) else "not_required"
        elif re.search(r"\bgre\b.{0,80}(?:required|must submit)", lowered):
            value = "required"
        else:
            value = None
        if value:
            facts.append({"field": "gre_policy", "value": value, "quote": quote, "review_status": "pending"})
    language_candidates: dict[str, tuple[int, str]] = {}
    for match in re.finditer(r"(?i)TOEFL|IELTS|Duolingo|English proficiency|English language", text):
        quote = _quote_window(text, match.start(), match.end())
        lowered = quote.casefold()
        conditional_exemption = bool(re.search(r"not required.{0,100}(?:for|if)|(?:waiv|exempt).{0,100}(?:for|if)|unless", lowered))
        if conditional_exemption:
            value = "conditional"
        elif re.search(r"not required|no (?:english )?(?:test|proficiency)|不要求|无需", lowered):
            value = "not_required"
        elif re.search(r"optional|可选", lowered):
            value = "optional"
        elif re.search(r"required|must (?:submit|provide|demonstrate)|need(?:ed)? to (?:submit|provide|demonstrate)|proof of english|demonstrate english|要求|必须", lowered):
            value = "required"
        elif re.search(r"waiv|exempt", lowered):
            value = "conditional"
        else:
            continue
        score = sum(bool(re.search(pattern, lowered)) for pattern in (
            r"required|must|not required|optional|waiv|exempt", r"toefl", r"ielts", r"duolingo",
            r"english proficiency|english language"))
        previous = language_candidates.get(value)
        if previous is None or score > previous[0]:
            language_candidates[value] = (score, quote)
    if language_candidates:
        # A page that explicitly exempts one applicant group while requiring
        # tests for another has a conditional policy, even if the score-table
        # paragraph itself has more keyword matches than the exemption clause.
        priority = {"conditional": 4, "required": 3, "not_required": 2, "optional": 1}
        value, (_, quote) = max(language_candidates.items(), key=lambda item: (priority[item[0]], item[1][0]))
        facts.append({"field": "language", "value": value, "quote": quote, "review_status": "pending"})
    unique = {}
    for fact in facts:
        key = (fact["field"] + "|" + str(fact["value"])) if fact["field"] == "deadline" else (
            fact["field"] + "|" + hashlib.sha256(fact["quote"].encode()).hexdigest())
        previous = unique.get(key)
        # A repeated date may appear in multiple page sections/chunks. Keep the
        # clearest citation, preferring an explicit year and a degree label.
        rank = (bool(re.search(r"\b20\d{2}\b", fact["quote"])),
                bool(re.search(r"(?i)(?:M\.?\s*S\.?|Ph\.?\s*D\.?)", fact["quote"])),
                -len(fact["quote"]))
        previous_rank = ((bool(re.search(r"\b20\d{2}\b", previous["quote"])),
                          bool(re.search(r"(?i)(?:M\.?\s*S\.?|Ph\.?\s*D\.?)", previous["quote"])),
                          -len(previous["quote"])) if previous else None)
        if previous is None or rank > previous_rank:
            unique[key] = fact
    result = []
    for fact in list(unique.values())[:30]:
        fact_id = hashlib.sha256((fact["field"] + fact["quote"]).encode()).hexdigest()[:24]
        result.append({"id": fact_id, **fact})
    return result


def _source_rejection(program: dict, url: str, title: str) -> str | None:
    """Reject editorial pages and another campus before accepting programme evidence."""
    path = urlparse(url).path.casefold()
    if set(path.strip("/").split("/")) & {"news", "events", "blog", "press", "press-releases"}:
        return "editorial_page"
    identity = f"{path} {title}".casefold()
    if program.get("school_code") == "duke" and program.get("id") == (
            "duke-master-of-science-in-computer-science-2027-fall"):
        # Duke's CS department has a separate 4+1 route exclusively for
        # current Duke undergraduates; it is not the regular graduate MSCS.
        if re.search(r"4\s*\+\s*1|undergraduate|duke undergraduates", identity):
            return "wrong_program_or_audience"
    if program.get("school_code") == "uiuc" and program.get("program") == "Master of Computer Science":
        if "chicago" in identity or "online" in identity:
            return "wrong_campus_or_delivery"
    return None


def _parent_policy_pages(program: dict) -> list[dict]:
    return list(DUKE_GRADUATE_POLICY_PAGES.get(program.get("id", ""), []))


def _parent_policy_for_url(program: dict | None, url: str) -> dict | None:
    if not program:
        return None
    return next((item for item in _parent_policy_pages(program) if item["url"] == url), None)


def _parent_policy_for_source(program: dict | None, source: dict) -> dict | None:
    policy = _parent_policy_for_url(program, source.get("url", ""))
    if policy or not program:
        return policy
    # Keep source policy identity across a same-domain HTTP redirect, but only
    # when the original request URL is itself one of our exact curated seeds.
    policy = _parent_policy_for_url(program, source.get("policy_seed_url", ""))
    if not policy or policy["field"] not in source.get("policy_fields", []):
        return None
    parsed = urlparse(source.get("url", ""))
    host = (parsed.hostname or "").casefold()
    domain = program.get("official_domain", "").casefold()
    return policy if parsed.scheme == "https" and (host == domain or host.endswith("." + domain)) else None


def _graduate_policy_facts(program: dict, policy: dict, text: str) -> list[dict]:
    """Extract only the target programme's row from a shared grad-school page."""
    field = policy["field"]
    fact: dict | None = None
    if field == "deadline":
        # Duke's deadline page has separate Ph.D. and Master's tables. Restrict
        # extraction to the Master's table and then match the programme row,
        # so the same discipline's Ph.D. deadline can never leak into an MS.
        section = re.search(r"(?is)Master(?:'|’)?s\s+Deadlines(?P<body>.*?)(?=Spring\s+Semester|$)", text)
        body = section.group("body") if section else ""
        if not body:
            return []
        if "electrical and computer engineering" in program.get("program", "").casefold():
            row_name = r"Electrical\s+and\s+Computer\s+Engineering"
        elif "computer science" in program.get("program", "").casefold():
            row_name = r"Computer\s+Science"
        else:
            return []
        row = re.search(
            rf"(?i)(?P<quote>\b{row_name}\b\s*(?:\|\s*|\t+|\s+)"
            r"(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>20\d{2}))",
            body)
        if row:
            try:
                value = date(int(row.group("year")), int(row.group("month")), int(row.group("day"))).isoformat()
            except ValueError:
                value = None
            if value:
                fact = {"field": "deadline", "value": value,
                        "quote": re.sub(r"\s+", " ", row.group("quote")).strip(),
                        "review_status": "pending"}
    elif field == "gre_policy":
        if "electrical and computer engineering" in program.get("program", "").casefold():
            program_row = re.search(r"(?i)Electrical\s+and\s+Computer\s+Engineering\s*\((?=[^)]*\bMS\b)[^)]*\)", text)
        elif "computer science" in program.get("program", "").casefold():
            program_row = re.search(r"(?i)Computer\s+Science\s*\(MS\)", text)
        else:
            return []
        if program_row:
            headings = [(match.start(), value) for value, pattern in (
                ("required", r"GRE\s+Required"), ("optional", r"GRE\s+Optional"),
                ("not_required", r"GRE\s+Not\s+Required"))
                for match in re.finditer(pattern, text, re.I)]
            preceding = [item for item in headings if item[0] < program_row.start()]
            if preceding:
                value = max(preceding, key=lambda item: item[0])[1]
                heading_text = "GRE " + {"required": "Required", "optional": "Optional",
                                         "not_required": "Not Required"}[value]
                fact = {"field": "gre_policy", "value": value,
                        "quote": f"{heading_text}: {program_row.group(0)}", "review_status": "pending"}
    elif field == "language":
        required_match = re.search(
            r"(?is)[^.!?]*(?:must\s+submit|must\s+provide|are\s+required\s+to\s+submit)"
            r"[^.!?]*(?:TOEFL|IELTS|Duolingo)[^.!?]*[.!?]", text)
        waiver_match = re.search(
            r"(?is)To\s+be\s+eligible\s+for\s+a\s+.{0,160}?waiver,\s*"
            r"you\s+must\s+have\s+[^.!?]*[.!?]", text)
        if required_match:
            quote = re.sub(r"\s+", " ", required_match.group(0)).strip()
            if waiver_match:
                waiver_quote = re.sub(r"\s+", " ", waiver_match.group(0)).strip()
                quote = f"{quote} {waiver_quote}"
            fact = {"field": "language", "value": "conditional", "quote": quote,
                    "review_status": "pending"}
    if not fact:
        return []
    fact["id"] = hashlib.sha256((fact["field"] + fact["quote"]).encode()).hexdigest()[:24]
    return [fact]


def _source_facts(program: dict, url: str, page_type: str, text: str,
                  policy_seed: dict | None = None) -> list[dict]:
    policy = _parent_policy_for_url(program, url) or policy_seed
    if policy:
        return _graduate_policy_facts(program, policy, text)
    if page_type in {"admissions", "requirements"}:
        return _fact_candidates(text, target_intake=INTAKE, target_degree=program.get("degree_level"))
    return []


def _required_page_types(program: dict) -> set[str]:
    required = {"admissions", "curriculum", "research"}
    if program["id"] == "uiuc-master-of-computer-science-2027-fall":
        required.add("requirements")
    return required


def _revalidate_existing_corpus(draft: dict, *, program_ids: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Remove pages that were attached using domain-only matching in older drafts."""
    programs = {item["id"]: item for item in draft.get("programs", [])}
    documents_by_source: dict[str, list[dict]] = defaultdict(list)
    for document in deduplicate_documents(draft.get("documents", [])):
        documents_by_source[document.get("source_id", "")].append(document)
    retained, audits, retained_ids = [], [], set()
    for source in consolidate_sources(draft.get("sources", [])):
        if not source.get("id"):
            audits.append(source)
            continue
        if program_ids is not None and source.get("program_id") not in program_ids:
            # A targeted collection must not rewrite another school's review
            # candidates or frozen source/document snapshot.
            retained.append(source)
            retained_ids.add(source["id"])
            continue
        program = programs.get(source.get("program_id"))
        documents = documents_by_source.get(source["id"], [])
        page_text = _reconstruct_page_text(documents)
        policy = _parent_policy_for_source(program, source)
        if policy:
            match, scope, evidence = "exact", "graduate_school_policy", [
                f"curated Graduate School policy page: {policy['field']}"]
        else:
            match, scope, evidence = classify_program_page(
                program["program"] if program else "", source.get("title", ""), source.get("url", ""), page_text)
        rejection = _source_rejection(program, source.get("url", ""), source.get("title", "")) if program else None
        if program and documents and match == "exact" and not rejection:
            retained.append({**source,
                # Older drafts used heading/keyword matches as facts. Refresh
                # from the frozen body before any human fact review begins.
                "facts": _source_facts(program, source.get("url", ""),
                    next((kind for kind in page_types(source) if kind in {"admissions", "requirements"}),
                         source.get("page_type", "")), page_text, policy),
                "classifier_scope": "graduate_school_policy" if policy else (
                    "department" if program.get("school_code") == "uiuc" and
                    source.get("page_type") == "research" and
                    urlparse(source.get("url", "")).path.startswith("/research/") else scope),
                "classifier_evidence": evidence,
                **({"policy_fields": [policy["field"]]} if policy else {})})
            retained_ids.add(source["id"])
        else:
            audits.append({"program_id": source.get("program_id"), "url": source.get("url"),
                           "title": source.get("title", ""), "status": rejection or "program_mismatch",
                           "rejected_source_id": source.get("id"), "classifier_match": match,
                           "classifier_scope": scope, "classifier_evidence": evidence,
                           "classifier_version": PROGRAM_CLASSIFIER_VERSION})
    documents = [item for item in deduplicate_documents(draft.get("documents", []))
                 if item.get("source_id") in retained_ids]
    return [*retained, *audits], documents


async def collect_corpus(draft: dict, *, max_pages_per_program: int = 5, checkpoint=None,
                         school_codes: set[str] | None = None, force_refresh: bool = False,
                         tokenizer=None) -> dict:
    from ..research.web import TavilyMCP
    known_school_codes = {school["code"] for school in SCHOOLS}
    selected_codes = {code.casefold() for code in school_codes} if school_codes is not None else None
    unknown_codes = (selected_codes or set()) - known_school_codes
    if unknown_codes:
        raise ValueError(f"unknown school code(s): {', '.join(sorted(unknown_codes))}")
    target_programs = [program for program in draft["programs"]
                       if selected_codes is None or program.get("school_code") in selected_codes]
    if selected_codes is not None and not target_programs:
        raise ValueError(f"no programs found for school code(s): {', '.join(sorted(selected_codes))}")
    collection_date = date.today().isoformat()
    if force_refresh and collection_date > str(draft.get("as_of", "")):
        # A refreshed frozen corpus cannot be newer than the simulated query
        # date used by retrieval filters. Advance every query to the new snapshot.
        draft = {**draft, "as_of": collection_date,
                 "cases": [{**case, "as_of": collection_date} for case in draft.get("cases", [])]}
    target_program_ids = {program["id"] for program in target_programs}
    resumable = str(draft.get("corpus_status", "")).startswith("collect") or selected_codes is not None
    sources, documents = _revalidate_existing_corpus(
        draft, program_ids=target_program_ids if selected_codes is not None else None
    ) if resumable else ([], [])
    if tokenizer is None:
        tokenizer, tokenizer_model, tokenizer_revision = _load_e5_tokenizer()
    else:
        tokenizer_model, tokenizer_revision = TOKENIZER_MODEL, "injected-test-tokenizer"
    documents = _retokenize_existing_documents(sources, documents, draft["programs"],
                                               tokenizer, tokenizer_model, tokenizer_revision,
                                               program_ids=target_program_ids if selected_codes is not None else None)
    draft = {**draft, "version": VERSION}
    refresh_sources = ([source for source in sources
                        if source.get("program_id") in target_program_ids and source.get("id")]
                       if force_refresh else [])
    refresh_source_ids = {source["id"] for source in refresh_sources}
    refresh_documents = ([document for document in documents
                          if document.get("source_id") in refresh_source_ids]
                         if force_refresh else [])

    def refresh_identity(source: dict) -> tuple[str, str]:
        fields = source.get("policy_fields", [])
        identity = "policy:" + ",".join(sorted(fields)) if fields else "page:" + str(source.get("page_type", ""))
        return str(source.get("program_id", "")), identity

    if force_refresh:
        if selected_codes is None:
            raise ValueError("force_refresh requires an explicit school_codes filter")
        # Keep audit/rejection records but discard selected schools' active
        # sources so that pages are fetched and parsed again from the network.
        removed_ids = {source["id"] for source in sources
                       if source.get("program_id") in target_program_ids and source.get("id")}
        sources = [source for source in sources if source.get("id") not in removed_ids]
        documents = [document for document in documents if document.get("source_id") not in removed_ids]
    retryable_collection_errors = {"mcp_connect_failed", "program_collection_failed", "mcp_close_failed"}
    sources = [source for source in sources if not (
        source.get("program_id") in target_program_ids and source.get("status") in retryable_collection_errors)]

    def save_checkpoint() -> None:
        if checkpoint is None:
            return
        checkpoint_sources = list(sources)
        checkpoint_documents = list(documents)
        if force_refresh:
            refreshed_keys = {refresh_identity(source) for source in sources
                              if source.get("program_id") in target_program_ids and source.get("id")}
            fallback_sources = [source for source in refresh_sources if refresh_identity(source) not in refreshed_keys]
            fallback_ids = {source["id"] for source in fallback_sources}
            checkpoint_sources.extend(fallback_sources)
            checkpoint_documents.extend(document for document in refresh_documents
                                        if document.get("source_id") in fallback_ids)
        checkpoint(repair_corpus_duplicates({**draft, "corpus_status": "collecting_pending_human_review",
                    "collection_strategy": "one-page-per-type-plus-parent-policy-e5-sectioned-v3",
                    "sources": checkpoint_sources, "documents": checkpoint_documents}))

    # Failed reads are retryable, especially after transport/extractor changes.
    seen_urls = {(item.get("program_id"), item.get("url")) for item in sources if item.get("id") and item.get("url")}
    seen_policy_fields = {(item.get("program_id"), field) for item in sources if item.get("id")
                          for field in item.get("policy_fields", [])}
    rejected_urls = {(item.get("program_id"), item.get("url")) for item in sources
                     if item.get("status") in {"program_mismatch", "editorial_page", "wrong_campus_or_delivery",
                                                "wrong_program_or_audience"} and item.get("url")
                     and item.get("classifier_version") == PROGRAM_CLASSIFIER_VERSION}
    for program in target_programs:
        existing_types = {kind for item in sources
                          if item.get("program_id") == program["id"] and item.get("id")
                          for kind in page_types(item)}
        required_policy_fields = {(program["id"], item["field"]) for item in _parent_policy_pages(program)}
        if _required_page_types(program) <= existing_types and required_policy_fields <= seen_policy_fields:
            continue
        try:
            web_context = TavilyMCP(search_limit=3, page_limit=max_pages_per_program * 5)
            web = await web_context.__aenter__()
        except Exception as exc:
            sources.append({"program_id": program["id"], "status": "mcp_connect_failed", "error": type(exc).__name__})
            save_checkpoint()
            continue
        try:
            queries = [("admissions", f"{program['university']} {program['program']} 2027 Fall admissions GRE deadline")]
            if "requirements" in _required_page_types(program):
                queries.append(("requirements", f"{program['university']} {program['program']} application requirements GRE"))
            queries.extend([
                ("curriculum", f"{program['university']} {program['program']} curriculum courses"),
                ("research", f"{program['university']} {program['program']} research labs faculty"),
            ])
            page_count = 0
            jobs = [(intended_type, query, None) for intended_type, query in queries[:max_pages_per_program]]
            # Shared Graduate School sources supplement department pages; they
            # are fetched independently and never satisfy a department-page
            # search intent just because the page types overlap.
            jobs.extend((item["page_type"], None, item) for item in _parent_policy_pages(program))
            for intended_type, query, policy_seed in jobs:
                if policy_seed and (program["id"], policy_seed["field"]) in seen_policy_fields:
                    continue
                if not policy_seed and intended_type in existing_types:
                    continue
                if policy_seed:
                    candidates = [{"url": policy_seed["url"], "title": "Duke Graduate School policy"}]
                else:
                    try:
                        result = await web.search(query, [program["official_domain"]])
                    except Exception as exc:
                        sources.append({"program_id": program["id"], "query": query, "status": "search_failed",
                                        "error": type(exc).__name__})
                        continue
                    candidates = list(result.get("results", []))
                    seed_url = PAGE_SEEDS.get((program["id"], intended_type))
                    if seed_url:
                        candidates = [{"url": seed_url, "title": "Curated official fallback"}, *candidates]
                for candidate in candidates[:3]:
                    url = candidate.get("url", "")
                    parsed = urlparse(url)
                    host = (parsed.hostname or "").casefold()
                    seen_key = (program["id"], url)
                    if (not url or seen_key in seen_urls or seen_key in rejected_urls or parsed.scheme != "https"
                            or not (host == program["official_domain"] or host.endswith("." + program["official_domain"]))):
                        continue
                    try:
                        page = await web.read(url, [program["official_domain"]])
                    except Exception as exc:
                        try:
                            page = await web.extract(url, [program["official_domain"]])
                            if not page.get("title"):
                                page["title"] = str(candidate.get("title", ""))
                        except Exception as extract_exc:
                            sources.append({"url": url, "program_id": program["id"], "status": "read_failed",
                                "error": type(exc).__name__, "extract_error": type(extract_exc).__name__})
                            seen_urls.add(seen_key)
                            continue
                    # Candidate URLs can redirect to an already collected canonical page.
                    canonical_key = (program["id"], page["url"])
                    seen_urls.add(seen_key)
                    if canonical_key in seen_urls and canonical_key != seen_key:
                        continue
                    seen_urls.add(canonical_key)
                    text = page["text"].strip()
                    if len(text) < 100:
                        continue
                    rejection = _source_rejection(program, page["url"], page.get("title", ""))
                    if rejection:
                        sources.append({"program_id": program["id"], "url": page["url"],
                            "title": page.get("title", ""), "status": rejection,
                            "classifier_version": PROGRAM_CLASSIFIER_VERSION})
                        rejected_urls.add((program["id"], page["url"]))
                        continue
                    if policy_seed:
                        match, scope, match_evidence = "exact", "graduate_school_policy", [
                            f"curated Graduate School policy page: {policy_seed['field']}"]
                    else:
                        match, scope, match_evidence = classify_program_page(
                            program["program"], page.get("title", ""), page["url"], text)
                    if match != "exact":
                        sources.append({"program_id": program["id"], "url": page["url"],
                            "title": page.get("title", ""), "status": "program_mismatch",
                            "classifier_match": match, "classifier_scope": scope,
                            "classifier_evidence": match_evidence,
                            "classifier_version": PROGRAM_CLASSIFIER_VERSION})
                        rejected_urls.add((program["id"], page["url"]))
                        continue
                    body_hash = hashlib.sha256(text.encode()).hexdigest()
                    source_id = "src-" + hashlib.sha256((program["id"] + "|" + page["url"]).encode()).hexdigest()[:24]
                    # Search intent supplies the initial type; a human still verifies page scope before gold export.
                    kind = intended_type
                    temporal = "explicit_2027_fall" if "2027" in text and re.search(r"fall|autumn|秋季", text, re.I) else (
                        "pending_intake_review" if kind in {"admissions", "requirements"} else "evergreen_pending_review")
                    source = {"id": source_id, "program_id": program["id"], "url": page["url"], "title": page["title"],
                              "page_type": kind, "retrieved_at": date.today().isoformat(), "content_hash": body_hash,
                              "temporal_scope": temporal, "program_match": "pending_review",
                              "chunker_version": CHUNKER_VERSION, "tokenizer_model": tokenizer_model,
                              "tokenizer_revision": tokenizer_revision,
                              "facts": _source_facts(program, page["url"], kind, text, policy_seed)}
                    source["classifier_scope"], source["classifier_evidence"] = scope, match_evidence
                    if policy_seed:
                        source["policy_fields"] = [policy_seed["field"]]
                        source["policy_seed_url"] = policy_seed["url"]
                        seen_policy_fields.add((program["id"], policy_seed["field"]))
                    if (program.get("school_code") == "uiuc" and kind == "research"
                            and urlparse(page["url"]).path.startswith("/research/")):
                        source["classifier_scope"] = "department"
                    sources.append(source)
                    documents.extend(_documents_for_source(program, source, text, tokenizer,
                                                           tokenizer_model, tokenizer_revision))
                    page_count += 1
                    break
        except Exception as exc:
            sources.append({"program_id": program["id"], "status": "program_collection_failed",
                            "error": type(exc).__name__})
        finally:
            try:
                await web_context.__aexit__(None, None, None)
            except Exception as exc:
                sources.append({"program_id": program["id"], "status": "mcp_close_failed",
                                "error": type(exc).__name__})
        save_checkpoint()
    if force_refresh:
        # Refresh transactionally: successful pages replace their prior version,
        # while pages that could not be reached remain available. This avoids a
        # transient MCP/network outage erasing a previously collected school.
        refreshed_keys = {refresh_identity(source) for source in sources
                          if source.get("program_id") in target_program_ids and source.get("id")}
        fallback_sources = [source for source in refresh_sources if refresh_identity(source) not in refreshed_keys]
        fallback_ids = {source["id"] for source in fallback_sources}
        sources.extend(fallback_sources)
        documents.extend(document for document in refresh_documents
                         if document.get("source_id") in fallback_ids)
    sources = consolidate_sources(sources)
    documents = deduplicate_documents(documents)
    gaps = {program["id"]: sorted(_required_page_types(program) - {kind for item in sources
            if item.get("program_id") == program["id"] and item.get("id") for kind in page_types(item)})
            for program in draft["programs"]}
    for program in draft["programs"]:
        missing_policies = ["policy:" + item["field"] for item in _parent_policy_pages(program)
                            if (program["id"], item["field"]) not in seen_policy_fields]
        if missing_policies:
            gaps.setdefault(program["id"], []).extend(missing_policies)
    gaps = {key: value for key, value in gaps.items() if value}
    return {**draft, "corpus_status": "collected_with_gaps_pending_human_review" if gaps else "collected_pending_human_review",
            "collection_strategy": "one-page-per-type-plus-parent-policy-e5-sectioned-v3", "collection_gaps": gaps,
            "sources": sources, "documents": documents}


def review_queue(dataset: dict) -> list[dict]:
    queue = []
    for program in dataset.get("programs", []):
        queue.append({"id": "program:" + program["id"], "stage": "program", "payload": program})
    for case in dataset.get("cases", []):
        queue.append({"id": "query:" + case["id"], "stage": "query", "payload": case})
        queue.append({"id": "gold:" + case["id"], "stage": "gold", "payload": {
            "case_id": case["id"], "expected_outcome": case["expected_outcome"],
            "candidate_program_ids": case.get("candidate_program_ids", []),
            "instruction": "接受表示候选项目属于金标准；拒绝表示该题无合格项目。"}})
    for source in dataset.get("sources", []):
        if not source.get("id"):
            continue
        # Source review establishes whether the page itself is eligible. Its
        # extracted facts are only candidates, reviewed later one by one.
        source_payload = {key: value for key, value in source.items() if key != "facts"}
        if source.get("classifier_scope") == "graduate_school_policy":
            instruction = ("这是 Duke Graduate School 的共享政策页，不是项目身份页；确认该政策适用于目标硕士项目和 2027 Fall。"
                           "截止日期只采用 Master's 表，GRE 只采用对应 MS 项目行；英语要求按适用人群及 waiver 条件审核。")
        else:
            instruction = "接受前确认页面属于目标项目；招生页适用于2027 Fall，常青页当前仍适用。"
        source_payload.update({
            "fact_candidate_count": len(source.get("facts", [])),
            "instruction": instruction
        })
        queue.append({"id": "source:" + source["id"], "stage": "source", "payload": source_payload})
        for fact in source.get("facts", []):
            # The current atomic fact is shown separately. Repeating the full
            # facts[] list in its source context made the UI look like it had
            # duplicated the same GRE/deadline fact.
            source_context = {key: value for key, value in source.items() if key != "facts"}
            queue.append({"id": "fact:" + source["id"] + ":" + fact["id"], "stage": "fact",
                          "payload": {"source": source_context, "fact": fact}})
    for answer in dataset.get("answer_candidates", []):
        queue.append({"id": "answer:" + answer["id"], "stage": "answer", "payload": answer})
    return queue


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("command", choices=["collect", "repair-corpus", "generate-cases", "build-pool", "export-llm", "llm-batch", "import-llm", "export-reviewed", "serve", "export", "validate"])
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--max-pages-per-program", type=int, default=5)
    parser.add_argument("--school", action="append", choices=[school["code"] for school in SCHOOLS],
                        help="collect only this school's programs, or incrementally rebuild its candidate pool; may be repeated")
    parser.add_argument("--refresh", action="store_true",
                        help="with --school, re-fetch that school's pages instead of reusing frozen sources")
    parser.add_argument("--database", default=default_eval_database_url())
    parser.add_argument("--fixture-models", action="store_true")
    parser.add_argument("--output", help="export-llm output JSON; defaults to <dir>/llm-evidence-review.json")
    parser.add_argument("--batch-size", type=int, default=10, help="export-llm evidence pairs per request (1-50)")
    parser.add_argument("--input", help="llm-batch input JSON; defaults to <dir>/llm-evidence-review.json")
    parser.add_argument("--batch-id", help="llm-batch ID; defaults to the first batch")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = Path(args.dir)
    draft_path = root / "draft.json"
    if args.command == "generate-cases":
        draft = generate_draft(args.seed)
        if draft_path.exists():
            previous = json.loads(draft_path.read_text(encoding="utf-8"))
            current_programs = {item["id"] for item in draft["programs"]}
            retained_sources = [item for item in previous.get("sources", [])
                                if item.get("program_id") in current_programs]
            retained_source_ids = {item["id"] for item in retained_sources if item.get("id")}
            draft["sources"] = retained_sources
            draft["documents"] = [item for item in previous.get("documents", [])
                                  if item.get("source_id") in retained_source_ids
                                  and item.get("metadata", {}).get("program_id") in current_programs]
            if retained_sources:
                draft["corpus_status"] = "collecting_pending_human_review"
            draft["documents"] = normalise_document_ids(draft.get("documents", []))
        write_json(draft_path, draft)
        queue_path = root / "review-queue.json"
        previous_queue = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else []
        current_queue = review_queue(draft)
        archive_removed_review_items(root, previous_queue, current_queue)
        write_json(queue_path, current_queue)
        result = validate_draft(draft)
    elif args.command == "repair-corpus":
        original = json.loads(draft_path.read_text(encoding="utf-8"))
        repaired = repair_corpus_duplicates(original)
        validate_draft(repaired)
        from shutil import copy2
        backup = root / "draft.before-dedup.json"
        if not backup.exists():
            copy2(draft_path, backup)
        write_json(draft_path, repaired)
        queue_path = root / "review-queue.json"
        queue_backup = root / "review-queue.before-dedup.json"
        if queue_path.exists() and not queue_backup.exists():
            copy2(queue_path, queue_backup)
        previous_queue = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else []
        current_queue = review_queue(repaired)
        archive_removed_review_items(root, previous_queue, current_queue)
        write_json(queue_path, current_queue)
        result = {"sources_removed": len(original.get("sources", [])) - len(repaired["sources"]),
                  "documents_removed": len(original.get("documents", [])) - len(repaired["documents"]),
                  "sources": len(repaired["sources"]), "documents": len(repaired["documents"]),
                  "backup": str(backup)}
    elif args.command == "collect":
        if args.refresh and not args.school:
            parser.error("--refresh requires at least one --school filter")
        draft = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.exists() else generate_draft(args.seed)
        draft = asyncio.run(collect_corpus(draft, max_pages_per_program=args.max_pages_per_program,
            school_codes=set(args.school) if args.school else None, force_refresh=args.refresh,
            checkpoint=lambda current: write_json(draft_path, current)))
        write_json(draft_path, draft)
        queue_path = root / "review-queue.json"
        previous_queue = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else []
        current_queue = review_queue(draft)
        archive_removed_review_items(root, previous_queue, current_queue)
        write_json(queue_path, current_queue)
        result = {"sources": len(draft.get("sources", [])), "documents": len(draft["documents"]),
                  "status": draft["corpus_status"]}
    elif args.command == "build-pool":
        from .research_annotation import build_pool
        result = asyncio.run(build_pool(draft_path, root / "candidate-pool.json", args.database,
            args.fixture_models, school_codes=set(args.school) if args.school else None))
    elif args.command == "export-llm":
        from .research_llm_export import export_llm
        result = export_llm(root, Path(args.output) if args.output else None, batch_size=args.batch_size)
    elif args.command == "llm-batch":
        from .research_llm_export import resolve_llm_batch
        bundle_path = Path(args.input) if args.input else root / "llm-evidence-review.json"
        result = resolve_llm_batch(json.loads(bundle_path.read_text(encoding="utf-8")), args.batch_id)
    elif args.command == "import-llm":
        from .research_llm_review import import_results
        if not args.input:
            parser.error("import-llm requires --input")
        result = import_results(root, Path(args.input).read_bytes())
    elif args.command == "export-reviewed":
        from .research_llm_review import export_results
        output = Path(args.output) if args.output else root / "llm-reviewed-merged.json"
        protected = [root / name for name in ("draft.json", "candidate-pool.json", "llm-evidence-review.json", "annotations.sqlite", "gold.json")]
        if output.resolve() in [p.resolve() for p in protected] or "llm-snapshots" in output.resolve().parts:
            parser.error("Output must not overwrite source data, gold, annotation database or immutable snapshots")
        exported = export_results(root)
        write_json(output, exported)
        result = {"output": str(output), **exported["summary"]}
    elif args.command == "serve":
        from .research_annotation import serve
        serve(root, args.host, args.port)
        return
    elif args.command == "export":
        from .research_annotation import export_gold
        result = export_gold(root)
    else:
        from .research_annotation import validate_workspace, validation_summary
        result = validation_summary(validate_workspace(root))
    if args.command == "llm-batch" and hasattr(sys.stdout, "reconfigure"):
        # Frozen pages contain Unicode outside Windows' GBK code page (e.g. ©).
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
