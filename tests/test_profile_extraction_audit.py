from pathlib import Path


AUDIT = Path(__file__).resolve().parent.parent / "docs" / "profile_extraction_current.md"


def test_task_001_profile_extraction_audit_is_complete():
    content = AUDIT.read_text(encoding="utf-8")
    for required in (
        "ProfileExtractor",
        "ModelScopeFactExtractor",
        "HybridFactExtractor",
        "academic_year",
        "career_goal",
        "已知失败和误判类型",
        "FastExtractor",
        "LegacySemanticRuleExtractor",
        "ExtractionResult",
    ):
        assert required in content
