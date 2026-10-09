import json

from opportunity_agent.lifecycle_agent import LifecycleAgent
from opportunity_agent.lifecycle_tools import LifecycleTools
from opportunity_agent.models import CandidateFact, ExtractionResult, InformationNeed, StageSignal
from opportunity_agent.profile import HybridFactExtractor


def candidate(field, value):
    return CandidateFact(field=field, value=value, source="conversation", confidence=0.95)


def test_extraction_result_can_carry_all_channels_in_one_turn():
    result = ExtractionResult(
        intent="prepare_summer_research",
        facts=[candidate("language_preparation", True), candidate("target_fields", ["AI"])],
        stage_signals=[
            StageSignal(stage="LANGUAGE_PREPARATION", direction="decrease", strength=0.95,
                        evidence="我托福考完了，103"),
            StageSignal(stage="SUMMER_RESEARCH", direction="increase", strength=0.9,
                        evidence="接下来准备找美国 AI 暑研"),
        ],
        information_needs=[InformationNeed(field="research_timeline", reason="需要确定暑研申请窗口")],
        should_replan=True,
    )
    assert len(result.facts) == 2
    assert len(result.stage_signals) == 2
    assert result.information_needs[0].field == "research_timeline"
    assert result.should_replan is True


def test_extraction_result_lists_do_not_share_mutable_defaults():
    first, second = ExtractionResult(), ExtractionResult()
    first.facts.append(candidate("gpa", 3.8))
    assert second.facts == []


def test_agent_consumes_unified_result_and_tools_return_json():
    class UnifiedExtractor:
        last_mode = "test"
        last_error = None

        def extract_result(self, _message):
            return ExtractionResult(
                intent="profile_update",
                facts=[
                    candidate("academic_year", 1),
                    candidate("major", "Computer Science"),
                    candidate("target_degree", "MS"),
                    candidate("target_countries", ["US"]),
                    candidate("target_fields", ["AI"]),
                ],
                stage_signals=[StageSignal(stage="APPLICATION", direction="increase", strength=0.7,
                                           evidence="想申请美国 AI 硕士")],
                should_replan=True,
            )

    agent = LifecycleAgent("unified", extractor=UnifiedExtractor())
    payload = LifecycleTools(agent).process_user_message("完整画像")
    assert agent.last_extraction.intent == "profile_update"
    assert payload["extraction_result"]["stage_signals"][0]["stage"] == "APPLICATION"
    json.dumps(payload, ensure_ascii=False)


def test_hybrid_extractor_keeps_legacy_list_api():
    extractor = HybridFactExtractor()
    facts = extractor.extract("我是大一 CS，想申请美国 AI 硕士。")
    result = extractor.extract_result("我是大一 CS，想申请美国 AI 硕士。")
    assert isinstance(facts, list)
    assert {item.field for item in facts} == {item.field for item in result.facts}
