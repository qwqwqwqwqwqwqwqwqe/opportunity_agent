from __future__ import annotations

from typing import Any

from .models import CandidateFact


class ProfileNormalizer:
    """Normalize known aliases while preserving the user's exact raw value."""

    _MAJORS = {
        "软件工程": "Computer Science / Software Engineering",
        "软工": "Computer Science / Software Engineering",
        "计算机": "Computer Science",
        "计算机科学": "Computer Science",
        "cs": "Computer Science",
        "网络工程": "Computer Science / Network Engineering",
        "数据科学": "Computer Science / Data Science",
        "人工智能": "Artificial Intelligence",
        "智能科学": "Artificial Intelligence",
        "电子信息": "Electronic Information Engineering",
        "通信工程": "Electronic and Communications Engineering",
        "电信工程": "Electronic and Communications Engineering",
        "微电子": "Microelectronics",
        "自动化": "Automation and Control Engineering",
        "控制科学": "Automation and Control Engineering",
        "机器人工程": "Robotics Engineering",
    }
    _COUNTRIES = {
        "美国": "US", "us": "US", "usa": "US",
        "加拿大": "Canada", "canada": "Canada",
        "新加坡": "Singapore", "singapore": "Singapore",
        "英国": "UK", "uk": "UK",
        "澳大利亚": "Australia", "澳洲": "Australia", "australia": "Australia",
        "新西兰": "New Zealand", "new zealand": "New Zealand",
        "德国": "Germany", "germany": "Germany", "法国": "France", "france": "France",
        "日本": "Japan", "japan": "Japan", "韩国": "South Korea", "korea": "South Korea",
        "中国香港": "Hong Kong", "香港": "Hong Kong", "hong kong": "Hong Kong",
    }
    _REGIONS = {"北美": "North America", "north america": "North America", "欧洲": "Europe", "东亚": "East Asia", "东南亚": "Southeast Asia", "大洋洲": "Oceania"}
    _FIELDS = {
        "ai": "Artificial Intelligence",
        "人工智能": "Artificial Intelligence",
        "机器学习": "Machine Learning",
        "软件工程": "Software Engineering",
        "数据科学": "Data Science",
        "通信工程": "Communications Engineering",
        "通信": "Communications Engineering",
        "信号处理": "Signal Processing",
        "微电子": "Microelectronics",
        "嵌入式": "Embedded Systems",
        "自动化": "Automation and Control",
        "控制科学": "Automation and Control",
        "控制工程": "Automation and Control",
        "机器人": "Robotics",
        "ai相关": "Artificial Intelligence",
        "llm": "Large Language Models",
        "大语言模型": "Large Language Models",
    }
    _CAREERS = {
        "后端": "Backend Engineer", "后端工程师": "Backend Engineer",
        "backend": "Backend Engineer", "backend engineer": "Backend Engineer",
        "ai工程师": "AI Engineer", "人工智能工程师": "AI Engineer",
        "ai engineer": "AI Engineer",
        "咨询": "Consultant", "咨询顾问": "Consultant", "投行": "Investment Banking",
        "市场营销": "Marketing", "产品经理": "Product Manager", "律师": "Lawyer",
        "教师": "Teacher", "记者": "Journalist",
    }

    def normalize_fact(self, fact: CandidateFact) -> CandidateFact:
        normalized = fact.model_copy(deep=True)
        value = fact.normalized_value if fact.normalized_value is not None else fact.raw_value
        if fact.field == "major":
            normalized.normalized_value = self._one(value, self._MAJORS)
        elif fact.field in {"target_countries", "target_locations"}:
            normalized.normalized_value = self._many(value, self._COUNTRIES)
        elif fact.field == "target_regions":
            normalized.normalized_value = self._many(value, self._REGIONS)
        elif fact.field == "target_fields":
            normalized.normalized_value = self._many(value, self._FIELDS)
        elif fact.field == "career_goal":
            normalized.normalized_value = self._one(value, self._CAREERS)
        return normalized

    def normalize(self, facts: list[CandidateFact]) -> list[CandidateFact]:
        return [self.normalize_fact(fact) for fact in facts]

    @staticmethod
    def _one(value: Any, mapping: dict[str, str]) -> Any:
        if not isinstance(value, str):
            return value
        return mapping.get(value.strip().casefold(), value)

    @classmethod
    def _many(cls, value: Any, mapping: dict[str, str]) -> list[Any]:
        values = value if isinstance(value, list) else [value]
        return [cls._one(item, mapping) for item in values]
