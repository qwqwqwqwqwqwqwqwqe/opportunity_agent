import pytest

from opportunity_agent.matcher import NOTIFY_THRESHOLD, match_job_to_user
from opportunity_agent.repository import LocalRepository


def test_nvidia_job_is_high_match_for_ai_user():
    repo = LocalRepository()
    match = match_job_to_user(repo.get_user("user_001"), repo.get_job("job_nvidia_ai_intern"))
    assert match.score >= NOTIFY_THRESHOLD
    assert match.should_notify is True
    assert match.matched_skills == ["Python", "PyTorch", "LLM"]
    assert match.missing_skills == ["CUDA"]


def test_same_job_is_not_recommended_to_backend_user():
    repo = LocalRepository()
    match = match_job_to_user(repo.get_user("user_002"), repo.get_job("job_nvidia_ai_intern"))
    assert match.score < NOTIFY_THRESHOLD
    assert match.should_notify is False
    assert match.matched_skills == []


def test_company_preference_changes_score():
    repo = LocalRepository()
    user = repo.get_user("user_001").model_copy(update={"target_companies": []})
    preferred = match_job_to_user(repo.get_user("user_001"), repo.get_job("job_nvidia_ai_intern"))
    neutral = match_job_to_user(user, repo.get_job("job_nvidia_ai_intern"))
    assert preferred.score == pytest.approx(neutral.score + 0.05)
