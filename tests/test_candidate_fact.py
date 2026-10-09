from opportunity_agent.models import CandidateFact, StudentProfile
from opportunity_agent.profile import apply_facts


def fact(field, raw, normalized=None, operation="set", **kwargs):
    return CandidateFact(
        field=field,
        raw_value=raw,
        normalized_value=normalized,
        operation=operation,
        source="conversation",
        confidence=kwargs.get("confidence", 0.95),
        needs_confirmation=kwargs.get("needs_confirmation", False),
        evidence=kwargs.get("evidence"),
    )


def test_legacy_value_input_and_output_remain_compatible():
    legacy = CandidateFact.model_validate({
        "field": "gpa", "value": 3.9, "source": "conversation", "confidence": 0.99,
    })
    assert legacy.raw_value == 3.9
    assert legacy.normalized_value == 3.9
    assert legacy.value == 3.9
    dumped = legacy.model_dump(mode="json")
    assert dumped["value"] == 3.9
    assert CandidateFact.model_validate(dumped).value == 3.9


def test_raw_and_normalized_values_are_both_preserved():
    candidate = fact("major", "软工", "Software Engineering", evidence="我是软工的")
    assert candidate.raw_value == "软工"
    assert candidate.value == "Software Engineering"
    assert candidate.evidence == "我是软工的"


def test_set_append_and_remove_apply_mechanically():
    profile = StudentProfile(user_id="operations", target_countries=["US"], major="Computer Science")
    profile = apply_facts(profile, [fact("target_countries", "加拿大", ["Canada"], "append")])
    assert profile.target_countries == ["US", "Canada"]
    profile = apply_facts(profile, [fact("target_countries", "美国", ["US"], "remove")])
    assert profile.target_countries == ["Canada"]
    profile = apply_facts(profile, [fact("major", "计算机", "Computer Science", "remove")])
    assert profile.major is None
    assert [item.operation for item in profile.facts] == ["append", "remove", "remove"]


def test_plan_examples_fit_candidate_fact_contract_without_semantic_extraction():
    examples = [
        fact("major", "软工", "Software Engineering", evidence="我是软工的"),
        fact("target_fields", "AI", ["AI"], needs_confirmation=True, confidence=0.6,
             evidence="我以后可能想做 AI"),
        fact("target_countries", "美国", ["US"], operation="remove", evidence="我不考虑美国了"),
        fact("target_countries", "美国加拿大", ["US", "Canada"], operation="set",
             evidence="美国加拿大都可以"),
    ]
    assert [item.operation for item in examples] == ["set", "set", "remove", "set"]
    assert examples[1].needs_confirmation is True
    assert examples[2].raw_value == "美国"
    assert examples[3].value == ["US", "Canada"]
