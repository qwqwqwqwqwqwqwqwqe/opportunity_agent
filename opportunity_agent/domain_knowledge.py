from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import StudentProfile


@dataclass(frozen=True)
class EngineeringDomainKnowledge:
    domain: str
    aliases: tuple[str, ...]
    core_courses: tuple[str, ...]
    prerequisites: tuple[str, ...]
    projects: tuple[str, ...]
    research_competitions: tuple[str, ...]
    job_keywords: tuple[str, ...]
    source: str = "internal_seed"
    confidence: float = 0.86
    verification_note: str = "具体项目先修课、语言门槛、学费与截止日期待项目官网核验。"


DOMAIN_KNOWLEDGE: dict[str, EngineeringDomainKnowledge] = {
    "computer_science": EngineeringDomainKnowledge(
        domain="computer_science",
        aliases=("计算机", "计算机科学", "软件工程", "软件", "网络工程", "数据科学", "cs", "computer science", "computer science / software engineering", "software engineering", "data science"),
        core_courses=("离散数学", "数据结构", "算法", "操作系统", "计算机网络", "数据库", "软件工程", "计算机组成"),
        prerequisites=("离散数学", "数据结构与算法", "操作系统", "计算机网络", "数据库系统"),
        projects=("可复现的系统项目", "带测试与文档的工程作品", "数据库或分布式系统项目"),
        research_competitions=("ACM/ICPC 类算法竞赛", "开源项目贡献", "系统与软件工程科研"),
        job_keywords=("software", "backend", "data", "database", "network", "systems", "软件", "后端"),
    ),
    "artificial_intelligence": EngineeringDomainKnowledge(
        domain="artificial_intelligence",
        aliases=("人工智能", "机器学习", "智能科学", "ai", "artificial intelligence", "machine learning", "deep learning", "计算机视觉", "自然语言处理", "nlp", "cv"),
        core_courses=("微积分", "线性代数", "概率统计", "数值计算", "优化方法", "机器学习", "深度学习", "数据结构", "算法"),
        prerequisites=("线性代数", "概率统计", "微积分", "优化方法", "数据结构与算法", "机器学习"),
        projects=("端到端机器学习项目", "可复现实验与消融分析", "模型部署与评测项目"),
        research_competitions=("Kaggle/天池", "CV/NLP/LLM 科研", "模型评测与系统优化"),
        job_keywords=("ai", "machine learning", "ml", "data scientist", "computer vision", "nlp", "llm", "算法"),
    ),
    "electronic_communications": EngineeringDomainKnowledge(
        domain="electronic_communications",
        aliases=("电子信息", "通信工程", "通信", "电信工程", "微电子", "电子科学", "信号处理", "集成电路", "telecommunication", "communications", "electronic engineering", "electronic information engineering", "electronic and communications engineering", "signal processing", "microelectronics"),
        core_courses=("电路", "模拟电子", "数字电子", "信号与系统", "数字信号处理", "通信原理", "信息论", "电磁场", "嵌入式系统", "FPGA"),
        prerequisites=("电路基础", "信号与系统", "概率统计", "数字信号处理", "通信原理"),
        projects=("SDR 通信链路", "FPGA 信号处理", "嵌入式采集与通信系统"),
        research_competitions=("电子设计竞赛", "通信与信号处理科研", "芯片/FPGA 项目"),
        job_keywords=("embedded", "firmware", "fpga", "rf", "communications", "signal", "hardware", "chip", "通信", "射频"),
    ),
    "automation_control": EngineeringDomainKnowledge(
        domain="automation_control",
        aliases=("自动化", "控制科学", "控制工程", "机器人工程", "机器人", "运动控制", "automation", "automation and control engineering", "control", "robotics engineering", "robotics"),
        core_courses=("电路", "信号与系统", "自动控制原理", "现代控制理论", "数值方法", "传感器与检测", "嵌入式系统", "PLC", "机器人学", "运动控制"),
        prerequisites=("线性代数", "微分方程", "信号与系统", "自动控制原理", "现代控制理论"),
        projects=("移动机器人控制", "传感器融合", "PLC/运动控制系统", "嵌入式闭环控制"),
        research_competitions=("智能车竞赛", "机器人竞赛", "控制与感知科研"),
        job_keywords=("automation", "control", "robotics", "embedded", "plc", "motion", "sensor", "自动化", "控制", "机器人"),
    ),
}

_UNKNOWN = {"", "未知", "暂不确定", "不确定", "unknown", "待确认"}

# These aliases are deliberately narrow: they identify the same named course
# across Chinese/English resumes, but never infer that an unlisted course was
# not taken.
_COURSE_ALIASES = {
    "离散数学": ("离散数学", "discrete mathematics"),
    "数据结构": ("数据结构", "data structures"),
    "算法": ("算法", "algorithms"),
    "操作系统": ("操作系统", "operating systems"),
    "计算机网络": ("计算机网络", "computer networks"),
    "数据库系统": ("数据库", "database systems", "databases"),
    "概率统计": ("概率统计", "probability and statistics", "probability statistics"),
    "线性代数": ("线性代数", "linear algebra"),
    "微积分": ("微积分", "calculus"),
    "优化方法": ("优化方法", "optimization methods", "optimization"),
    "机器学习": ("机器学习", "machine learning"),
    "信号与系统": ("信号与系统", "signals and systems"),
    "自动控制原理": ("自动控制原理", "control systems"),
}


def assess_prerequisite_coverage(profile: StudentProfile, knowledge: EngineeringDomainKnowledge) -> dict[str, list[str]]:
    """Compare only confirmed course names; absence means *unconfirmed*, not missing."""
    completed = " ".join(profile.completed_courses).casefold()
    confirmed = {canonical for canonical, aliases in _COURSE_ALIASES.items()
                 if any(alias.casefold() in completed for alias in aliases)}
    confirmed_prerequisites, not_confirmed = [], []
    for prerequisite in knowledge.prerequisites:
        if prerequisite == "数据结构与算法":
            covered = {"数据结构", "算法"}.issubset(confirmed)
        else:
            aliases = _COURSE_ALIASES.get(prerequisite, (prerequisite,))
            covered = any(alias.casefold() in completed for alias in aliases)
        (confirmed_prerequisites if covered else not_confirmed).append(prerequisite)
    return {"confirmed": confirmed_prerequisites, "not_confirmed": not_confirmed}


def identify_domain(*values: str | Iterable[str] | None) -> str | None:
    text_parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text_parts.append(value)
        else:
            text_parts.extend(str(item) for item in value)
    text = " ".join(text_parts).casefold().strip()
    if text in _UNKNOWN or not text:
        return None
    scores = {
        key: sum(1 for alias in knowledge.aliases if alias.casefold() in text)
        for key, knowledge in DOMAIN_KNOWLEDGE.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] else None


def validate_profile_domain(profile: StudentProfile) -> tuple[bool, str | None, str]:
    major = (profile.major or "").strip()
    if major.casefold() in _UNKNOWN:
        return False, None, "当前专业尚未确定，暂不能生成详细工科时间轴。"
    major_domain = identify_domain(major)
    if major_domain is None:
        return False, None, f"当前版本暂不支持“{major or '未填写专业'}”的详细时间轴。"
    target_domain = identify_domain(profile.target_fields)
    if profile.target_fields and target_domain is None:
        return False, None, "目标方向不在计算机与电子信息类工科支持范围内，当前版本不生成跨学科时间轴。"
    domain = target_domain or major_domain
    return True, domain, "已启用计算机与电子信息类工科规划。"


def knowledge_for_profile(profile: StudentProfile) -> EngineeringDomainKnowledge | None:
    supported, domain, _ = validate_profile_domain(profile)
    return DOMAIN_KNOWLEDGE[domain] if supported and domain else None
