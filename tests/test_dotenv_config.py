from opportunity_agent import config


def test_dotenv_is_used_when_shell_is_empty_and_shell_wins(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("# local values\nLLM_API_KEY='from-dotenv'\nLLM_API_BASE=https://example.test/v1\nLLM_MODEL=test-model\n", encoding="utf-8")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("OPPORTUNITY_AGENT_DISABLE_DOTENV", raising=False)
    monkeypatch.setattr(config, "_DOTENV_PATH", dotenv)
    assert config.llm_api_key() == "from-dotenv"
    assert config.llm_api_base() == "https://example.test/v1"
    assert config.llm_model() == "test-model"
    monkeypatch.setenv("LLM_API_KEY", "from-shell")
    assert config.llm_api_key() == "from-shell"


def test_primary_dotenv_key_beats_legacy_modelscope_environment(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("LLM_API_KEY=primary-dotenv\n", encoding="utf-8")
    monkeypatch.setattr(config, "_DOTENV_PATH", dotenv)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPPORTUNITY_AGENT_DISABLE_DOTENV", raising=False)
    monkeypatch.setenv("MODELSCOPE_API_KEY", "obsolete-legacy-key")
    assert config.llm_api_key() == "primary-dotenv"


def test_dotenv_can_be_disabled(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("LLM_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.setattr(config, "_DOTENV_PATH", dotenv)
    monkeypatch.setenv("OPPORTUNITY_AGENT_DISABLE_DOTENV", "1")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert config.llm_api_key() is None
