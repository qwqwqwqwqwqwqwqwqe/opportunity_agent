"""Bounded terminology expansion that never substitutes the user's constraints."""
import re


def query_rewrites(query, limit=2):
    expansions = ((r"(?<![a-z])AI(?![a-z])|人工智能", "artificial intelligence 机器学习 machine learning"),
                  (r"课程|curriculum", "curriculum courses 课程"),
                  (r"截止|deadline", "application deadline 截止日期"),
                  (r"(?<![a-z])GRE(?![a-z])", "Graduate Record Examination GRE"))
    result = []
    for pattern, words in expansions:
        if re.search(pattern, query, re.I):
            candidate = query + " " + words
            if valid_rewrite(query, candidate):
                result.append(candidate)
    return result[:limit]


def valid_rewrite(original, rewritten):
    # Expansion-only: do not allow replacing, removing or inventing numbers/negations.
    if not rewritten.startswith(original + " "):
        return False
    suffix = rewritten[len(original):]
    return not re.search(r"\d|不要|必须|无需|不要求|not|required|optional|fall|spring", suffix, re.I)
