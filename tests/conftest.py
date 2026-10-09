import os
import pytest


def pytest_configure():
    """Keep unit tests offline even when the developer shell has a live key."""
    os.environ["OPPORTUNITY_AGENT_DISABLE_DOTENV"] = "1"
    if os.getenv("RUN_MODELSCOPE_INTEGRATION") != "1" and os.getenv("RUN_RESUME_LLM_INTEGRATION") != "1":
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("MODELSCOPE_API_KEY", None)
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    if os.getenv("RUN_MINERU_INTEGRATION") != "1":
        os.environ.pop("MINERU_API_TOKEN", None)


@pytest.fixture(autouse=True)
def offline_llm_environment(monkeypatch):
    """SDK imports must not rehydrate real .env credentials midway through tests."""
    if os.getenv("RUN_MODELSCOPE_INTEGRATION") == "1" or os.getenv("RUN_RESUME_LLM_INTEGRATION") == "1":
        return
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("MODELSCOPE_API_KEY", raising=False)
    try:
        import dotenv
    except ImportError:
        return
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
