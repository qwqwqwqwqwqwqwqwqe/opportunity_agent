from __future__ import annotations

import os
from pathlib import Path


DEFAULT_API_BASE = "https://yibuapi.com/v1"
DEFAULT_MODEL = "gpt-5.5"
_DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _dotenv_values() -> dict[str, str]:
    """Read the local .env without overriding the shell environment.

    This deliberately supports the small, portable subset needed by the demo:
    blank lines, comments, ``KEY=value``, optional ``export``, and matching
    single or double quotes.  It avoids a runtime dependency solely for four
    configuration values.
    """
    if os.getenv("OPPORTUNITY_AGENT_DISABLE_DOTENV") == "1" or not _DOTENV_PATH.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        lines = _DOTENV_PATH.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return {}
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if item.startswith("export "):
            item = item[7:].lstrip()
        key, separator, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _setting(*names: str) -> str | None:
    """Resolve aliases in priority order, with shell over .env per alias.

    Keeping the aliases together is important during migration: an old
    ``MODELSCOPE_API_KEY`` inherited by Python must not override a deliberately
    configured primary ``LLM_API_KEY`` in this project's .env file.
    """
    values = _dotenv_values()
    for name in names:
        if value := os.getenv(name):
            return value
        if value := values.get(name):
            return value
    return None


def llm_api_key() -> str | None:
    return _setting("LLM_API_KEY", "MODELSCOPE_API_KEY")


def llm_model() -> str:
    return _setting("LLM_MODEL", "MODELSCOPE_MODEL") or DEFAULT_MODEL


def llm_api_base() -> str:
    return _setting("LLM_API_BASE", "MODELSCOPE_API_BASE") or DEFAULT_API_BASE


def synthesizer_config() -> dict[str, int]:
    """Resolve shell/.env settings without capturing them at import time."""
    result = {}
    for key, default, lower, upper in (("TIMEOUT_SECONDS", 60, 1, 180),
                                      ("RETRIES", 1, 0, 1),
                                      ("BUDGET_SECONDS", 90, 1, 180),
                                      ("MAX_TOKENS", 1400, 100, 4000)):
        try:
            result[key] = max(lower, min(upper, int(_setting("SYNTHESIZER_" + key) or default)))
        except ValueError:
            result[key] = default
    return result


def chat_completions_url(value: str | None = None) -> str:
    """Accept either an OpenAI base URL or the complete chat endpoint."""
    base = (value or llm_api_base()).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def roadmap_timeout_seconds() -> int:
    # Article generation runs after the deterministic onboarding response, so
    # it can wait longer without blocking profile collection or chat switching.
    # GPT-class compatible gateways often need more than 120 seconds for a
    # grounded Chinese long-form plan on a cold request.  This budget covers
    # one initial draft and, when necessary, a small structured repair.
    value = _setting("ROADMAP_LLM_TIMEOUT_SECONDS") or "210"
    try:
        return max(45, min(int(value), 360))
    except ValueError:
        return 210


def roadmap_article_max_tokens() -> int:
    # Structured six-section JSON needs room for field names and escaping in
    # addition to the requested detailed Chinese roadmap article.
    value = _setting("ROADMAP_ARTICLE_MAX_TOKENS") or "2400"
    try:
        return max(1000, min(int(value), 4000))
    except ValueError:
        return 2400


def tavily_api_key() -> str | None:
    return _setting("TAVILY_API_KEY")


def official_search_enabled() -> bool:
    return _setting("OFFICIAL_SEARCH_ENABLED") == "1" and bool(tavily_api_key())


def official_cache_ttl_hours() -> int:
    try:
        return max(1, min(int(_setting("OFFICIAL_CACHE_TTL_HOURS") or "168"), 24 * 90))
    except ValueError:
        return 168


def official_tool_max_calls() -> int:
    try:
        return max(1, min(int(_setting("OFFICIAL_TOOL_MAX_CALLS") or "6"), 12))
    except ValueError:
        return 6


def official_research_timeout_seconds() -> int:
    try:
        return max(10, min(int(_setting("OFFICIAL_RESEARCH_TIMEOUT_SECONDS") or "60"), 120))
    except ValueError:
        return 60


def a2a_enabled() -> bool:
    return (_setting("A2A_ENABLED") or "1").strip().casefold() not in {"0", "false", "no", "off"}


def opportunity_a2a_host() -> str:
    return _setting("OPPORTUNITY_A2A_HOST") or "127.0.0.1"


def opportunity_a2a_port() -> int:
    try:
        return max(1024, min(int(_setting("OPPORTUNITY_A2A_PORT") or "8770"), 65535))
    except ValueError:
        return 8770


def opportunity_a2a_url() -> str:
    return _setting("OPPORTUNITY_A2A_URL") or f"http://127.0.0.1:{opportunity_a2a_port()}/a2a/jsonrpc/"


def opportunity_a2a_timeout_seconds() -> int:
    try:
        return max(5, min(int(_setting("OPPORTUNITY_A2A_TIMEOUT_SECONDS") or "45"), 180))
    except ValueError:
        return 45
