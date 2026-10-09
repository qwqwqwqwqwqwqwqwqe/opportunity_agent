"""Offline checks for the reproducible V2 Profile benchmark."""
import json
from pathlib import Path

from opportunity_agent.v2.evaluation.profile_benchmark import _counts, _identities, _score
from opportunity_agent.v2.agents.profile_extraction import route_profile_extraction


ROOT = Path(__file__).resolve().parents[1]


def test_silver_profile_fixture_is_in_required_range_and_has_required_coverage():
    cases = [json.loads(line) for line in (ROOT / "tests/fixtures/profile_extraction_silver_360.jsonl")
             .read_text(encoding="utf-8").splitlines() if line]
    assert len(cases) == 360
    categories = {case["category"] for case in cases}
    assert {"explicit_score", "academic_multi", "experience", "preference", "negative", "correction", "mixed"} <= categories
    assert all("expected_facts" in case and "expected_preferences" in case and "expected_route" in case for case in cases)
    assert {case["expected_route"] for case in cases} == {"rule_only", "llm_only", "hybrid", "reject"}


def test_value_level_metric_counts_lists_and_preferences_separately():
    predicted = _identities([{"field": "research_experiences", "value": ["A", "B"]}])
    expected = _identities([{"field": "research_experiences", "value": ["A", "C"]}])
    score = _score(_counts(predicted, expected))
    assert score == {"tp": 1, "fp": 1, "fn": 1, "precision": .5, "recall": .5, "f1": .5}
    assert _identities([{"key": "avoid_gre", "value": True}], preference=True) == {"avoid_gre=true"}


def test_deterministic_profile_route_gate_avoids_unnecessary_llm_calls():
    assert route_profile_extraction("我的托福是107。").path == "rule_only"
    assert route_profile_extraction("我主申美国的人工智能硕士，毕业后想做AI工程师。").path == "llm_only"
    assert route_profile_extraction("我是大三CS，托福107，主申美国人工智能硕士。").path == "hybrid"
    assert route_profile_extraction("如果托福110能申请吗？").path == "reject"
    assert route_profile_extraction("我更看重毕业后的就业机会，就业优先。").path == "rule_only"
    assert route_profile_extraction("我刚考到110。", context={"recent_messages": [{"role": "assistant", "content": "托福出分了吗"}]}).path == "llm_only"
