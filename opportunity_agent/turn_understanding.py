"""Conservative assertion filtering and explicit progress syntax (no domain lexicon)."""
from __future__ import annotations

import re
from datetime import date

from .models import ProgressUpdate


# Do not require a question mark: Chinese users routinely end a factual query
# with a bare “吗”, especially for school requirement lookups.
QUESTION = re.compile(r"[?？]|怎么办|怎么|如何|是否|够吗|可以吗|适合|多少|建议|为什么|能否|查询|查一下|帮我(?:查|看)|(?:吗|么)\s*$")
UNCERTAIN = re.compile(r"可能|也许|大概|考虑|不确定|maybe|perhaps", re.I)


def assertion_text(message: str) -> tuple[str, bool]:
    """Questions/conditions are not user facts, even if they contain score tokens."""
    if re.match(r"\s*(如果|假如|假设|要是|if\b|suppose\b)", message, re.I):
        return "", True
    kept = []
    asking = bool(QUESTION.search(message))
    for clause in re.split(r"[，,；;。！!\n]+", message):
        clause = clause.strip()
        if not clause:
            continue
        if re.match(r"^(如果|假如|假设|要是|if\b|suppose\b)", clause, re.I):
            asking = True
            continue
        if re.search(r"没(?:有)?考(?:到|过)|还没(?:有)?(?:考|出分)|不是\s*\d", clause):
            continue
        if QUESTION.search(clause):
            # Keep only an explicitly achieved score prefix, never a hypothetical score.
            match = re.match(r"(.*?(?:考了|考完了|出分(?:了)?|成绩是|拿到)\s*\d+(?:\.\d+)?(?:分)?)(.*)", clause)
            if match:
                kept.append(match.group(1))
            continue
        kept.append(clause)
    return "，".join(kept), asking


def extract_progress_rules(message: str) -> list[ProgressUpdate]:
    # "不考了，改到……" is one reschedule operation. Applying the cancellation
    # clause first would temporarily cancel the same exam and create a false transition.
    if (re.search(r"考试|托福|雅思|GRE|TOEFL|IELTS", message, re.I)
            and re.search(r"延期|推迟|改期|延到|改到", message)):
        postponed = None
        match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?", message)
        if match:
            try:
                postponed = date(*map(int, match.groups()))
            except ValueError:
                postponed = None
        uncertain = bool(UNCERTAIN.search(message))
        return [ProgressUpdate(
            target_kind="event", target_hint="考试", action="postpone", postponed_to=postponed,
            evidence=message.strip(), confidence=0.6 if uncertain else 0.99,
            needs_confirmation=uncertain or postponed is None,
        )]
    result = []
    for clause in re.split(r"[，,；;。\n]+", message):
        if not clause.strip():
            continue
        cancel = bool(re.search(r"取消|不(?:再)?(?:参加|考|做)|不做了|放弃", clause))
        if not cancel and re.search(r"(?:还|尚)?未|没(?:有)?(?:开始|完成|做完|考完)|不确定是否", clause):
            continue
        action = ("cancel" if cancel else "postpone" if re.search(r"延期|推迟|改期|延到|改到", clause)
                  else "complete" if re.search(r"已?完成|做完|考完|已参加|参加了", clause)
                  else "start" if re.search(r"开始|正在|已经在", clause) else None)
        if not action:
            continue
        kind, hint = "task", ""
        if re.search(r"考试|托福|雅思|GRE|TOEFL|IELTS", clause, re.I):
            kind, hint = ("task", "语言") if re.search(r"备考|准备", clause) and action == "start" else ("event", "考试")
        elif re.search(r"科研|暑研|研究", clause):
            hint = "科研"
        elif "项目" in clause:
            hint = "项目"
        elif "实习" in clause:
            hint = "实习"
        elif "文书" in clause:
            hint = "文书"
        elif "网申" in clause:
            hint = "网申"
        elif not re.search(r"任务|这项|这个|这一项|那项|那个|它", clause):
            continue
        postponed = None
        match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?", clause)
        if match:
            try:
                postponed = date(*map(int, match.groups()))
            except ValueError:
                pass
        uncertain = bool(UNCERTAIN.search(clause))
        result.append(ProgressUpdate(target_kind=kind, target_hint=hint, action=action,
                                     postponed_to=postponed if action == "postpone" else None,
                                     actual_date=postponed if action == "complete" else None,
                                     evidence=clause.strip(), confidence=0.6 if uncertain else 0.99,
                                     needs_confirmation=uncertain))
    return result


def progress_only(message: str, updates: list[ProgressUpdate]) -> bool:
    if not updates:
        return False
    remainder = message
    for update in updates:
        remainder = remainder.replace(update.evidence, "")
    return not remainder.strip(" ，,。；;！!\n")


def facts_cover_message(message: str, facts: list, updates: list[ProgressUpdate]) -> bool:
    """Skip inference only when evidence covers the message, not just one number.

    Legacy research/preparation rules quote entire messages as evidence; those
    broad quotes cannot establish that all the other clauses were understood.
    """
    remainder = message.casefold()
    evidence = [p.evidence for p in updates]
    for fact in facts:
        if fact.field in {"research_activity", "language_preparation"}:
            continue
        evidence.extend(re.split(r"、|,\s*", fact.evidence or ""))
    for part in sorted(evidence, key=len, reverse=True):
        if part:
            remainder = remainder.replace(part.casefold(), "")
    # Grammatical glue only: never discard experience or action vocabulary.
    remainder = re.sub(r"我是|我的|我|本科|专业|预计|目前|现在|想申请|申请|目标|成绩|毕业年份|毕业|年|分|是|为|的|和|或者|以及", "", remainder)
    return not remainder.strip(" \t\r\n，,。；;！!：:、")
