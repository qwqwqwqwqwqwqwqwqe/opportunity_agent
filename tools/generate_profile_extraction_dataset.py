"""Generate the deterministic 360-case V2 profile extraction regression set.

The fixture is deliberately labelled *programmatic / silver*, not human gold.
It is a reviewable starting point for the 300--500 case manual annotation task.
"""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "profile_extraction_silver_360.jsonl"
MANIFEST = ROOT / "tests" / "fixtures" / "profile_extraction_silver_360.manifest.json"


def fact(field: str, value):
    return {"field": field, "value": value}


def preference(key: str, value):
    return {"key": key, "value": value}


def main() -> None:
    cases: list[dict] = []

    def add(category: str, text: str, facts=(), preferences=(), *, route: str, tags=()):
        cases.append({
            "id": f"profile_{len(cases) + 1:03d}", "category": category, "input": text,
            "expected_facts": list(facts), "expected_preferences": list(preferences),
            "expected_route": route, "tags": list(tags),
        })

    # 90 explicit numerical facts: stable rule coverage and Chinese/English variants.
    for index in range(30):
        score = 91 + index
        wording = (f"我的托福是 {score}。" if index % 3 == 0 else
                   f"TOEFL: {score}" if index % 3 == 1 else f"托福考了{score}分")
        add("explicit_score", wording, [fact("toefl_score", score)], route="rule_only")
    for index in range(20):
        score = round(6.0 + index * .15, 1)
        wording = f"我的雅思是 {score}。" if index % 2 else f"IELTS: {score}"
        add("explicit_score", wording, [fact("ielts_score", score)], route="rule_only")
    for index in range(20):
        score = 300 + index * 2
        wording = f"GRE {score}" if index % 2 else f"我的GRE考了{score}。"
        add("explicit_score", wording, [fact("gre_score", score)], route="rule_only")
    for index in range(20):
        score = round(2.7 + index * .06, 2)
        wording = f"GPA {score}" if index % 2 else f"我的绩点为{score}"
        add("explicit_score", wording, [fact("gpa", score)], route="rule_only")

    # 40 multi-fact records.  Values are what the normalizer is expected to expose.
    majors = [("CS", "Computer Science"), ("计算机", "Computer Science"),
              ("软件工程", "Computer Science / Software Engineering"), ("人工智能", "Artificial Intelligence")]
    for index in range(40):
        year = index % 4 + 1
        raw_major, normalized_major = majors[index % len(majors)]
        gpa = round(3.1 + (index % 9) * .1, 1)
        toefl = 96 + index % 20
        add("academic_multi", f"我是大{'一二三四'[year - 1]} {raw_major}，GPA {gpa}，托福 {toefl}。",
            [fact("academic_year", year), fact("major", normalized_major), fact("gpa", gpa), fact("toefl_score", toefl)], route="rule_only")

    # 20 rank and graduation messages, including spacing variants.
    for index in range(20):
        rank, total, graduation = index % 18 + 1, 80 + index * 3, 2026 + index % 4
        separator = " / " if index % 2 else "/"
        add("rank_graduation", f"我的排名是 {rank}{separator}{total}，预计{graduation}年本科毕业。",
            [fact("class_rank", f"{rank}/{total}"), fact("graduation_year", graduation)], route="rule_only")

    # 40 natural-language target/career records.  These are intentionally LLM-heavy.
    countries = [("美国", "US"), ("加拿大", "Canada"), ("英国", "UK"), ("新加坡", "Singapore")]
    fields = [("人工智能", "Artificial Intelligence"), ("机器学习", "Machine Learning"),
              ("数据科学", "Data Science"), ("软件工程", "Software Engineering")]
    careers = [("AI工程师", "AI Engineer"), ("后端工程师", "Backend Engineer")]
    for index in range(40):
        raw_country, country = countries[index % len(countries)]
        raw_field, target_field = fields[index % len(fields)]
        raw_career, career = careers[index % len(careers)]
        base = [fact("target_countries", [country]), fact("target_fields", [target_field]),
                fact("target_degree", "MS"), fact("career_goal", career)]
        fallback_raw, _ = countries[(index + 1) % len(countries)]
        if index % 4 == 0:
            text = f"我主申{raw_country}的{raw_field}硕士，毕业后希望做{raw_career}。"
            prefs = []
        elif index % 4 == 1:
            text = f"虽然有人推荐{fallback_raw}，但我不考虑去那里，仍主申{raw_country}的{raw_field}硕士；毕业后想做{raw_career}。"
            prefs = []
        elif index % 4 == 2:
            text = f"我主申{raw_country}的{raw_field}硕士，毕业后想做{raw_career}；只有预算不足时才把{fallback_raw}作为备选。"
            prefs = [preference("fallback_country", fallback_raw)]
        else:
            text = f"不是申请{fallback_raw}，而是想读{raw_country}的{raw_field}硕士；我未来希望成为{raw_career}。"
            prefs = []
        add("natural_target", text, base, prefs, route="llm_only", tags=["negation" if index % 4 in {1, 3} else "conditional" if index % 4 == 2 else "natural"])

    # 40 appended experience records; text itself is the expected list element.
    internships = ["华为实习", "字节跳动后端实习", "腾讯云实习", "小米算法实习", "银行科技实习",
                   "创业公司实习", "阿里云实习", "京东数据实习", "美团开发实习", "网易实习"]
    researches = ["图神经网络科研", "大模型科研", "多模态研究", "数据库系统研究", "机器人科研",
                  "强化学习研究", "计算机视觉科研", "NLP研究", "分布式系统研究", "芯片设计科研"]
    papers = ["一篇IEEE论文发表", "一篇ACM论文录用", "一篇中文核心论文发表", "一篇会议论文发表", "一篇期刊论文录用",
              "一篇AI论文发表", "一篇系统论文录用", "一篇CV论文发表", "一篇NLP论文录用", "一篇数据库论文发表"]
    projects = ["Agent项目", "RAG项目", "推荐系统项目", "分布式存储项目", "视觉识别项目",
                "大模型应用项目", "网页爬虫项目", "移动端项目", "机器人项目", "云原生项目"]
    for value in internships:
        add("experience", f"我有{value}。", [fact("internship_experiences", [value])], route="rule_only")
    for value in researches:
        add("experience", f"我参与了{value}。", [fact("research_experiences", [value])], route="rule_only")
    for value in papers:
        add("experience", f"我有{value}。", [fact("paper_experiences", [value])], route="rule_only")
    for value in projects:
        add("experience", f"我有一个{value}。", [fact("project_experiences", [value])], route="rule_only")

    # 30 preference-only facts, deliberately separate from the Profile fact score.
    for index in range(15):
        add("preference", "我不想考 GRE，优先考虑不要求GRE的项目。", preferences=[preference("avoid_gre", True)], route="rule_only")
    for index in range(15):
        add("preference", "我更看重毕业后的就业机会，就业优先。", preferences=[preference("employment_priority", True)], route="rule_only")

    # 50 negatives: questions, hypotheses, uncertainty, invalid values and contingent targets.
    for index in range(10):
        add("negative", f"托福{90 + index}够申请吗？", route="reject", tags=["question"])
    for index in range(10):
        add("negative", f"如果托福{100 + index}能申请美国项目吗？", route="reject", tags=["hypothetical"])
    for index in range(10):
        add("negative", f"我的GRE是{500 + index}。", route="rule_only", tags=["invalid_value"])
    for index in range(10):
        # This is deliberately not a primary target, but it is a valid
        # preference under the current schema.
        add("conditional_preference", "加拿大只是如果美国毕业以后找不到工作时的备选。",
            preferences=[preference("fallback_country", "加拿大")], route="llm_only", tags=["conditional_target"])
    for index in range(10):
        add("negative", "我可能以后会做一个Agent项目，目前还没有开始。", route="reject", tags=["uncertain"])

    # 30 corrections.  Only the latest corrected score is gold.
    for index in range(15):
        old, new = 90 + index, 105 + index
        add("correction", f"我的托福{old}，现在托福{new}。", [fact("toefl_score", new)], route="rule_only")
    for index in range(15):
        old, new = 300 + index, 320 + index
        add("correction", f"GRE原来{old}，更正为{new}。", [fact("gre_score", new)], route="rule_only")

    # 20 mixed records exercise fact/preference separation and deduplication.
    for index in range(20):
        score = 100 + index
        add("mixed", f"我是大三 CS，托福{score}，做过图神经网络科研，不想考 GRE；虽然英国项目很多，仍主申美国人工智能硕士，只有时间不够才考虑加拿大。",
            [fact("academic_year", 3), fact("major", "Computer Science"), fact("toefl_score", score),
             fact("research_experiences", ["图神经网络科研"]), fact("target_countries", ["US"]),
             fact("target_fields", ["Artificial Intelligence"]), fact("target_degree", "MS")],
            [preference("avoid_gre", True), preference("fallback_country", "加拿大")], route="hybrid", tags=["negation", "conditional"])

    assert len(cases) == 360, len(cases)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases), encoding="utf-8")
    MANIFEST.write_text(json.dumps({
        "name": "profile_extraction_silver_360", "case_count": len(cases),
        "label_source": "programmatic_template_annotation", "human_review": "pending",
        "purpose": "deterministic regression and ablation baseline; not a human Gold Dataset",
        "expected_route_counts": {route: sum(item["expected_route"] == route for item in cases)
                                  for route in sorted({item["expected_route"] for item in cases})},
        "categories": {category: sum(item["category"] == category for item in cases)
                       for category in sorted({item["category"] for item in cases})},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(cases)} cases to {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
