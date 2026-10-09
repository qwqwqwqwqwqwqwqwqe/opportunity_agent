import os

import pytest

from opportunity_agent.llm_client import LLMClient


@pytest.mark.skipif(
    os.getenv("RUN_MODELSCOPE_INTEGRATION") != "1" or not (os.getenv("LLM_API_KEY") or os.getenv("MODELSCOPE_API_KEY")),
    reason="set RUN_MODELSCOPE_INTEGRATION=1 and LLM_API_KEY for the live compatible-API test",
)
def test_real_modelscope_completion_when_explicitly_enabled():
    content = LLMClient(timeout_seconds=45, retries=0).generate(
        system="Return the requested token only.", user="只回复 OK",
        temperature=0, max_tokens=16, thinking=False,
    )
    assert "OK" in content.upper()
