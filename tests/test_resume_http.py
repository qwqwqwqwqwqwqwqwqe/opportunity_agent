import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
pytest.importorskip("docx", reason="install the resume extra for upload tests")
pytest.importorskip("python_multipart", reason="install the resume extra for upload tests")

from opportunity_agent.conversation_store import ConversationStore
from opportunity_agent.resume_models import ResumeDraft, ResumeFact
from opportunity_agent.resume_service import ResumeImportService
from opportunity_agent.session_service import SessionService
from opportunity_agent.web_app import OpportunityWebHandler
from resume_helpers import document, docx_bytes


class Extractor:
    mode,error="rule",None
    def generate(self,parsed):
        return ResumeDraft(facts=[ResumeFact(field="toefl_score",value=105,confidence=.98,
             evidence="TOEFL: 105",block_ids=["p1"])])


def multipart(fields, filename, data, boundary="resume-test-boundary"):
    parts=[]
    for name,value in fields.items():
        parts.extend([f"--{boundary}\r\n".encode(),
          f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()])
    parts.extend([f"--{boundary}\r\n".encode(),
      f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
      b"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n",
      data,b"\r\n",f"--{boundary}--\r\n".encode()])
    return b"".join(parts),f"multipart/form-data; boundary={boundary}"


@pytest.fixture
def api(tmp_path):
    store=ConversationStore(tmp_path/"conversations.json")
    SessionService(store).import_conversation({"session_id":"one","conversation_title":"One"})
    SessionService(store).import_conversation({"session_id":"two","conversation_title":"Two"})
    service=ResumeImportService(store,parser=lambda path:document("TOEFL: 105"),extractor_factory=Extractor)
    store._resume_service=service
    OpportunityWebHandler.store=store
    server=ThreadingHTTPServer(("127.0.0.1",0),OpportunityWebHandler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield server,store,service
    server.shutdown();server.server_close();thread.join(3);service.close()


def request(server,method,path,body=None,headers=None):
    client=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=5)
    client.request(method,path,body=body,headers=headers or {})
    response=client.getresponse();raw=response.read();client.close()
    return response.status,json.loads(raw) if raw else {}


def test_http_upload_poll_review_and_session_isolation(api):
    server,store,_=api
    body,content_type=multipart({"session_id":"one","request_id":"http-1","enhanced":"false"},
                                "synthetic.docx",docx_bytes())
    status,job=request(server,"POST","/api/resume/imports",body,
                       {"Content-Type":content_type,"Content-Length":str(len(body))})
    assert status==202 and job["status"]=="queued"
    deadline=time.monotonic()+6
    while time.monotonic()<deadline:
        status,current=request(server,"GET",f"/api/resume/imports/{job['import_id']}?session_id=one")
        if current["status"]=="review":break
        time.sleep(.03)
    assert status==200 and current["draft"]["facts"][0]["normalized_value"]==105
    status,error=request(server,"GET",f"/api/resume/imports/{job['import_id']}?session_id=two")
    assert status==400 and "找不到" in error["error"]
    status,listing=request(server,"GET","/api/resume/imports?session_id=one")
    assert status==200 and len(listing["imports"])==1
    assert store.get("one")["state"]["profile"]["toefl_score"] is None
    status,deleted=request(server,"DELETE",f"/api/resume/imports/{job['import_id']}?session_id=one")
    assert status==200 and deleted["deleted"]


def test_http_invalid_upload_and_unknown_operation(api):
    server,_,_=api
    body,content_type=multipart({"session_id":"one","request_id":"bad","enhanced":"false"},"fake.pdf",b"not pdf")
    status,result=request(server,"POST","/api/resume/imports",body,
                           {"Content-Type":content_type,"Content-Length":str(len(body))})
    assert status==400 and "真实" in result["error"]
    payload=json.dumps({"session_id":"one"}).encode()
    status,result=request(server,"POST","/api/resume/imports/nope/unknown",payload,
                           {"Content-Type":"application/json","Content-Length":str(len(payload))})
    assert status==400 and "未知" in result["error"]


def test_static_resume_assets_and_no_store(api):
    server,_,_=api
    client=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=5)
    for path,mime in [("/resume_ui.js","text/javascript"),("/resume.css","text/css")]:
        client.request("GET",path);response=client.getresponse();body=response.read()
        assert response.status==200 and mime in response.getheader("Content-Type")
        assert response.getheader("Cache-Control")=="no-store" and body
    client.close()
