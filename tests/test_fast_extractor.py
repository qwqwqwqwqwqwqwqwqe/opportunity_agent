from opportunity_agent.profile import (
    FastExtractor,
    HybridFactExtractor,
    LegacySemanticRuleExtractor,
    ProfileExtractor,
)


def by_field(result):
    return {fact.field: fact for fact in result.facts}


def test_fast_extractor_parses_only_explicit_numeric_and_date_structures():
    message = (
        "我本科大二，GPA 3.9，托福103，雅思7.5，GRE 325，排名5/120，"
        "预计2028年毕业，预算50万人民币，截止日期2027-09-01。"
    )
    result = FastExtractor().extract_result(message)
    facts = by_field(result)
    assert facts["academic_year"].value == 2
    assert facts["gpa"].raw_value == "3.9"
    assert facts["gpa"].normalized_value == 3.9
    assert facts["gpa"].evidence == "GPA 3.9"
    assert facts["toefl_score"].value == 103
    assert facts["ielts_score"].value == 7.5
    assert facts["gre_score"].value == 325
    assert facts["class_rank"].value == "5/120"
    assert facts["graduation_year"].value == 2028
    assert facts["budget"].value == {"amount": 500000, "currency": "CNY"}
    assert facts["explicit_date"].value == "2027-09-01"
    assert {signal.stage for signal in result.stage_signals} == {"LANGUAGE_PREPARATION"}
    assert result.should_replan is True


def test_fast_extractor_rejects_out_of_range_or_invalid_values():
    result = FastExtractor().extract_result(
        "GPA 4.8，托福130，雅思10.5，GRE 350，排名120/5，日期2027-02-30"
    )
    fields = {fact.field for fact in result.facts}
    assert fields.isdisjoint({
        "gpa", "toefl_score", "ielts_score", "gre_score", "class_rank", "explicit_date",
    })


def test_fast_extractor_does_not_take_semantic_dictionary_responsibility():
    result = FastExtractor().extract_result("我是软工，考虑美国 AI，以后想做后端")
    assert result.facts == []
    assert result.stage_signals == []


def test_legacy_extractor_name_and_existing_mvp_pipeline_are_compatible():
    assert ProfileExtractor is LegacySemanticRuleExtractor
    result = HybridFactExtractor().extract_result("我是大一 CS，想申请美国 AI 硕士。")
    fields = {fact.field for fact in result.facts}
    assert fields >= {
        "academic_year", "major", "target_degree", "target_countries", "target_fields",
    }
    assert next(fact for fact in result.facts if fact.field == "academic_year").source == "conversation"
