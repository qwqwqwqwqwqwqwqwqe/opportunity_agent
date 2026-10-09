import json

from opportunity_agent.conflict_resolver import ProfileConflictResolver
from opportunity_agent.lifecycle_agent import LifecycleAgent
from opportunity_agent.models import CandidateFact, StudentProfile
from opportunity_agent.profile import HybridFactExtractor
from opportunity_agent.semantic_extractor import SemanticExtractor


def fact(field, value, source="conversation", operation="set", confidence=0.95):
    return CandidateFact(
        field=field, raw_value=value, normalized_value=value, source=source,
        operation=operation, confidence=confidence, evidence=str(value),
    )


def test_explicit_user_correction_overwrites_and_records_old_value():
    resolver = ProfileConflictResolver()
    first = resolver.resolve(StudentProfile(user_id="change"), [fact("major", "Computer Science")]).profile
    second = resolver.resolve(first, [fact("major", "Software Engineering")])
    assert second.profile.major == "Software Engineering"
    assert second.decisions[0].status == "applied"
    assert second.profile.change_history[-1].old_value == "Computer Science"
    assert second.profile.change_history[-1].new_value == "Software Engineering"


def test_lower_priority_source_cannot_silently_overwrite_user_fact():
    resolver = ProfileConflictResolver()
    profile = resolver.resolve(StudentProfile(user_id="priority"), [fact("major", "Computer Science")]).profile
    resolution = resolver.resolve(profile, [fact("major", "Physics", source="model")])
    assert resolution.profile.major == "Computer Science"
    assert resolution.decisions[0].status == "confirmation_required"
    assert resolution.profile.facts[-1].needs_confirmation is True


def test_explicit_negation_removes_list_value_and_records_history():
    resolver = ProfileConflictResolver()
    profile = resolver.resolve(StudentProfile(user_id="remove"), [
        fact("target_countries", ["US"]),
    ]).profile
    resolution = resolver.resolve(profile, [
        fact("target_countries", ["US"], operation="remove"),
        fact("target_countries", ["Singapore"], operation="append"),
    ])
    assert resolution.profile.target_countries == ["Singapore"]
    assert [change.operation for change in resolution.profile.change_history[-2:]] == ["remove", "append"]
    json.dumps(resolution.profile.model_dump(mode="json"), ensure_ascii=False)


def test_hybrid_negation_beats_legacy_positive_keyword_end_to_end():
    response = json.dumps({
        "intent": "correct_target_country",
        "facts": [
            {"field": "target_countries", "raw_value": ["美国"], "normalized_value": ["US"],
             "confidence": 0.99, "source": "model", "evidence": "不打算去美国了", "operation": "remove"},
            {"field": "target_countries", "raw_value": ["新加坡"], "normalized_value": ["Singapore"],
             "confidence": 0.99, "source": "model", "evidence": "更想新加坡", "operation": "append"},
        ],
        "stage_signals": [], "information_needs": [], "should_replan": True,
    }, ensure_ascii=False)
    semantic = SemanticExtractor(completion_fn=lambda _payload: response)
    agent = LifecycleAgent("country-agent", extractor=HybridFactExtractor(model_extractor=semantic))
    agent.profile = ProfileConflictResolver().resolve(
        agent.profile, [fact("target_countries", ["US"])]
    ).profile
    agent.on_user_message("我其实不打算去美国了，更想新加坡")
    assert agent.profile.target_countries == ["Singapore"]
    assert not any(
        item.operation == "set" and item.value == ["US"]
        for item in agent.last_extraction.facts if item.field == "target_countries"
    )
