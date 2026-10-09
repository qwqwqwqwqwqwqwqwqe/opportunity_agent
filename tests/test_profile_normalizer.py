from opportunity_agent.models import CandidateFact, StudentProfile
from opportunity_agent.normalizer import ProfileNormalizer
from opportunity_agent.profile import HybridFactExtractor, apply_facts


def fact(field, value, confidence=0.95, needs_confirmation=False):
    return CandidateFact(
        field=field, raw_value=value, confidence=confidence,
        needs_confirmation=needs_confirmation, source="conversation", evidence=str(value),
    )


def test_major_raw_and_canonical_are_both_preserved():
    result = ProfileNormalizer().normalize_fact(fact("major", "软件工程"))
    assert result.raw_value == "软件工程"
    assert result.normalized_value == "Computer Science / Software Engineering"


def test_region_is_separate_and_is_not_forced_to_a_country():
    normalized = ProfileNormalizer().normalize_fact(fact("target_regions", ["北美"]))
    profile = apply_facts(StudentProfile(user_id="region"), [normalized])
    assert profile.target_regions == ["North America"]
    assert profile.target_countries == []


def test_field_and_career_aliases_have_canonical_values():
    normalizer = ProfileNormalizer()
    field = normalizer.normalize_fact(fact("target_fields", ["AI相关"]))
    career = normalizer.normalize_fact(fact("career_goal", "后端"))
    assert field.raw_value == ["AI相关"]
    assert field.value == ["Artificial Intelligence"]
    assert career.raw_value == "后端"
    assert career.value == "Backend Engineer"


def test_unknown_value_is_not_guessed_and_uncertain_raw_is_untouched():
    original = fact("major", "机器人科学与工程", confidence=0.6, needs_confirmation=True)
    normalized = ProfileNormalizer().normalize_fact(original)
    assert normalized.raw_value == "机器人科学与工程"
    assert normalized.normalized_value == "机器人科学与工程"
    profile = apply_facts(StudentProfile(user_id="unknown"), [normalized])
    assert profile.major is None


def test_hybrid_preserves_legacy_raw_phrase_while_normalizing_profile_value():
    result = HybridFactExtractor().extract_result("我想读AI相关硕士")
    target = next(item for item in result.facts if item.field == "target_fields")
    assert target.raw_value == ["AI相关"]
    assert target.value == ["Artificial Intelligence"]
