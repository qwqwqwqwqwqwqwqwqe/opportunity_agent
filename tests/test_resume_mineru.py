"""MinerU protocol tests are fake HTTP; integration calls are explicit opt-in."""
import io
import json
import os
from pathlib import Path

import pytest

from opportunity_agent.resume_mineru import MinerUParser
from opportunity_agent.resume_extraction import ResumeExtractionSkill
from resume_helpers import document, pdf_bytes, zip_bytes


def test_mineru_signed_upload_never_sends_bearer_to_storage(tmp_path,monkeypatch):
    monkeypatch.setenv("MINERU_API_TOKEN","synthetic-test-token")
    path=tmp_path/"resume.pdf";path.write_bytes(pdf_bytes())
    zip_data=zip_bytes({"resume_content_list.json":'[{"type":"text","text":"Synthetic University","page_idx":0}]'})
    responses=[
        json.dumps({"code":0,"data":{"batch_id":"synthetic-id","file_urls":["https://cdn-mineru.openxlab.org.cn/upload"]}}).encode(),
        b"",
        json.dumps({"code":0,"data":{"extract_result":[{"state":"done","full_zip_url":"https://cdn-mineru.openxlab.org.cn/result.zip"}]}}).encode(),
        zip_data,
    ]
    seen=[]
    class Opener:
        def open(self,req,timeout):
            seen.append(req);return io.BytesIO(responses.pop(0))
    monkeypatch.setattr("opportunity_agent.resume_mineru.build_opener",lambda *a:Opener())
    result=MinerUParser().parse(path)
    assert result.parser=="mineru" and result.page_count==1
    assert [req.get_method() for req in seen]==["POST","PUT","GET","GET"]
    assert "Authorization" in seen[0].headers and "Authorization" in seen[2].headers
    assert "Authorization" not in seen[1].headers and "Authorization" not in seen[3].headers
    assert json.loads(seen[0].data)["files"][0]["name"]=="resume.pdf"


@pytest.mark.parametrize("url",["http://mineru.net/file","https://example.com/file","https://mineru.net@evil.test/file","https://127.0.0.1/file"])
def test_mineru_rejects_untrusted_signed_urls(tmp_path,monkeypatch,url):
    monkeypatch.setenv("MINERU_API_TOKEN","synthetic-test-token")
    path=tmp_path/"resume.pdf";path.write_bytes(pdf_bytes())
    calls=[]
    class Opener:
        def open(self,req,timeout):
            calls.append(req)
            return io.BytesIO(json.dumps({"code":0,"data":{"batch_id":"x","file_urls":[url]}}).encode())
    monkeypatch.setattr("opportunity_agent.resume_mineru.build_opener",lambda *a:Opener())
    with pytest.raises(ValueError,match="许可域名"):
        MinerUParser().parse(path)
    assert len(calls)==1


def test_mineru_cancelled_request_sends_nothing(tmp_path,monkeypatch):
    monkeypatch.setenv("MINERU_API_TOKEN","synthetic-test-token")
    with pytest.raises(ValueError,match="取消"):
        MinerUParser().parse(tmp_path/"not-read.pdf",cancelled=lambda:True)


def test_resume_disables_tls_compatibility_retry_without_affecting_other_callers():
    from opportunity_agent.llm_client import LLMClient
    original=LLMClient()
    skill=ResumeExtractionSkill(original)
    assert original.tls_compatibility_retry is True
    assert skill.client.tls_compatibility_retry is False and skill.client.retries==0


@pytest.mark.skipif(os.getenv("RUN_RESUME_LLM_INTEGRATION")!="1",reason="explicit synthetic resume LLM opt-in required")
def test_real_resume_llm_synthetic_only():
    from opportunity_agent.config import llm_api_key
    if not llm_api_key(): pytest.fail("Set LLM_API_KEY in the test process")
    skill=ResumeExtractionSkill()
    result=skill.generate(document("Synthetic University (fictional), Computer Science.\nGPA: 3.9/4.0\nTOEFL: 105\nResearch project: Signal classification, 2026, Python, accuracy 90%."))
    assert skill.mode=="llm",skill.error
    assert any(f.field=="school" for f in result.facts)
    assert all(f.field not in {"target_countries","target_schools","target_degree"} for f in result.facts)


@pytest.mark.skipif(os.getenv("RUN_MINERU_INTEGRATION")!="1",reason="explicit synthetic file upload opt-in required")
def test_real_mineru_synthetic_file_only(tmp_path):
    if not MinerUParser().enabled: pytest.fail("Set MINERU_API_TOKEN in the test process")
    path=tmp_path/"synthetic-resume.pdf";path.write_bytes(pdf_bytes())
    result=MinerUParser().parse(path)
    assert result.parser=="mineru" and result.blocks
