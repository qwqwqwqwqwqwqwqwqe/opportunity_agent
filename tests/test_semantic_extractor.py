import asyncio
import json

from opportunity_agent.models import CandidateFact, ChatMessage, StudentProfile, UserState
from opportunity_agent.profile import HybridFactExtractor
from opportunity_agent.semantic_extractor import SemanticExtractor


def _result(**overrides):
    value = {
        "intent": "profile_update",
        "facts": [{
            "field": "major",
            "raw_value": "机器人专业",
            "normalized_value": "机器人专业",
            "confidence": 0.91,
            "source": "model",
            "evidence": "机器人专业",
            "operation": "set",
        }],
        "stage_signals": [],
        "information_needs": [],
        "should_replan": True,
    }
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False)


def test_semantic_extractor_receives_profile_state_recent_messages_and_schema():
    payloads = []
    extractor = SemanticExtractor(completion_fn=lambda payload: payloads.append(payload) or _result())
    profile = StudentProfile(user_id="u1", academic_year=3)
    result = asyncio.run(extractor.extract(
        "我是机器人专业",
        profile,
        [ChatMessage(role="user", content="我准备申请硕士")],
        UserState(academic="mid_undergraduate"),
    ))

    request_text = payloads[0]["messages"][1]["content"]
    assert '"academic_year": 3' in request_text
    assert "我准备申请硕士" in request_text
    assert "output_schema" in request_text
    assert result.facts[0].raw_value == "机器人专业"
    assert result.facts[0].source == "conversation"
    assert result.should_replan is True


def test_semantic_extractor_marks_uncertain_explicit_fact_for_confirmation():
    response = _result(facts=[{
        "field": "target_fields", "raw_value": ["人机交互"],
        "normalized_value": ["人机交互"], "confidence": 0.95,
        "source": "model", "evidence": "可能考虑人机交互", "operation": "set",
    }])
    result = SemanticExtractor(completion_fn=lambda _payload: response).extract_sync(
        "我可能考虑人机交互", StudentProfile(user_id="u2"), []
    )
    assert result.facts[0].confidence == 0.69
    assert result.facts[0].needs_confirmation is True


def test_semantic_extractor_preserves_remove_operation_for_negation():
    response = _result(facts=[{
        "field": "target_countries", "raw_value": ["US"], "normalized_value": ["US"],
        "confidence": 0.98, "source": "model", "evidence": "不再考虑美国", "operation": "remove",
    }])
    result = SemanticExtractor(completion_fn=lambda _payload: response).extract_sync(
        "我不再考虑美国", StudentProfile(user_id="u3", target_countries=["US"]), []
    )
    assert result.facts[0].operation == "remove"


def test_missing_api_key_returns_immediate_structured_fallback(monkeypatch):
    monkeypatch.delenv("MODELSCOPE_API_KEY", raising=False)
    result = SemanticExtractor(api_key=None).extract_sync("软件工程", StudentProfile(user_id="u4"), [])
    assert result.facts == []
    assert result.diagnostics.attempts == 0
    assert "not configured" in result.diagnostics.fallback_reason


def test_malformed_json_is_repaired_once():
    outputs = iter(["not-json", _result()])
    payloads = []
    extractor = SemanticExtractor(completion_fn=lambda payload: payloads.append(payload) or next(outputs))
    result = extractor.extract_sync("我是机器人专业", StudentProfile(user_id="repair"), [])
    assert result.facts[0].field == "major"
    assert result.diagnostics.attempts == 2
    assert len(result.diagnostics.malformed_outputs) == 1
    assert "previous output was invalid" in payloads[1]["messages"][-1]["content"]


def test_two_malformed_outputs_fall_back_to_rules_without_breaking_chat():
    outputs = iter(["not-json", "still-not-json"])
    semantic = SemanticExtractor(completion_fn=lambda _payload: next(outputs))
    class SingleRule:
        def extract(self, _message):
            return [CandidateFact(field="major", value="历史学", source="conversation", confidence=0.95)]

    hybrid = HybridFactExtractor(rule_extractor=SingleRule(), model_extractor=semantic)
    result = hybrid.extract_result("请记录我的情况", profile=StudentProfile(user_id="fallback"))
    assert {fact.field for fact in result.facts} == {"major"}
    assert hybrid.last_mode == "rule_fallback"
    assert result.diagnostics.attempts == 2
    assert len(result.diagnostics.malformed_outputs) == 2


def test_business_validation_error_retries_and_never_accepts_invented_evidence():
    invalid = _result(facts=[{
        "field": "major", "raw_value": "量子计算", "normalized_value": "量子计算",
        "confidence": 0.99, "source": "model", "evidence": "用户没有说过的文本",
    }])
    outputs = iter([invalid, invalid])
    result = SemanticExtractor(completion_fn=lambda _payload: next(outputs)).extract_sync(
        "我是软件工程专业", StudentProfile(user_id="evidence"), []
    )
    assert result.facts == []
    assert "evidence" in result.diagnostics.fallback_reason


def test_transport_error_returns_diagnostics_instead_of_raising():
    def timeout(_payload):
        raise TimeoutError("slow upstream")

    result = SemanticExtractor(completion_fn=timeout).extract_sync(
        "我是软件工程专业", StudentProfile(user_id="timeout"), []
    )
    assert result.facts == []
    assert result.diagnostics.attempts == 1
    assert result.diagnostics.fallback_reason == "TimeoutError: slow upstream"
