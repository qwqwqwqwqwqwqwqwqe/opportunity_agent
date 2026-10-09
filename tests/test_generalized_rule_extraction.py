from opportunity_agent.profile import HybridFactExtractor, LegacySemanticRuleExtractor
from opportunity_agent.models import StudentProfile


def _by_field(facts):
    return {fact.field: fact for fact in facts}


def test_explicit_multiple_countries_are_all_extracted_and_normalized():
    result = HybridFactExtractor().extract_result(
        "我想去美国或者加拿大读硕士", profile=StudentProfile(user_id="countries")
    )
    facts = _by_field(result.facts)
    assert facts["target_countries"].raw_value == ["美国", "加拿大"]
    assert facts["target_countries"].value == ["US", "Canada"]
    assert facts["target_locations"].value == ["US", "Canada"]


def test_rules_do_not_assume_cs_or_engineering_for_explicit_business_major():
    facts = _by_field(LegacySemanticRuleExtractor().extract("我是商科专业，计划申请加拿大 MBA"))
    assert facts["major"].raw_value == "商科"
    assert facts["major"].value == "商科"
    assert facts["target_countries"].raw_value == ["加拿大"]
    assert facts["target_degree"].value == "MBA"
    assert "target_fields" not in facts


def test_rules_preserve_explicit_nontechnical_career_goal_and_internship_stage():
    facts = _by_field(LegacySemanticRuleExtractor().extract("我想找市场营销实习，不考虑写代码"))
    assert facts["career_goal"].raw_value == "市场营销"
    assert facts["current_stage"].value == "internship_search"
    assert "skills" not in facts


def test_region_does_not_become_a_country():
    facts = _by_field(HybridFactExtractor().extract("我更倾向北美项目"))
    assert facts["target_regions"].value == ["North America"]
    assert "target_countries" not in facts


def test_target_schools_and_program_are_preserved_for_planning():
    result = HybridFactExtractor().extract(
        "我想申请多伦多大学经济学硕士项目，也考虑NUS",
    )
    facts = _by_field(result)
    assert "多伦多大学" in facts["target_schools"].value
    assert "NUS" in facts["target_schools"].value
    assert any("经济学硕士项目" in item for item in facts["target_programs"].value)
