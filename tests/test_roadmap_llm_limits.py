from opportunity_agent.config import roadmap_article_max_tokens, roadmap_timeout_seconds


def test_roadmap_llm_defaults_prioritize_reliable_background_generation(monkeypatch):
    monkeypatch.delenv("ROADMAP_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("ROADMAP_ARTICLE_MAX_TOKENS", raising=False)

    assert roadmap_timeout_seconds() == 210
    assert roadmap_article_max_tokens() == 2400


def test_roadmap_llm_limits_are_clamped(monkeypatch):
    monkeypatch.setenv("ROADMAP_LLM_TIMEOUT_SECONDS", "999")
    monkeypatch.setenv("ROADMAP_ARTICLE_MAX_TOKENS", "9999")

    assert roadmap_timeout_seconds() == 360
    assert roadmap_article_max_tokens() == 4000
