"""Disposable browser fixture; synthetic files, mock extraction/cloud, no network LLM."""
import base64
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ["OPPORTUNITY_AGENT_DISABLE_DOTENV"]="1"
for key in ("LLM_API_KEY","MODELSCOPE_API_KEY","MINERU_API_TOKEN"): os.environ.pop(key,None)

from opportunity_agent.conversation_store import ConversationStore
from opportunity_agent.resume_service import ResumeImportService
from opportunity_agent.web_app import OpportunityWebHandler
from resume_helpers import docx_bytes,pdf_bytes,document
from test_resume_service import CompleteExtractor

class Cloud:
    enabled=True
    def parse(self,path,**kwargs): return document("Synthetic cloud document")

with tempfile.TemporaryDirectory(prefix="resume-ui-") as directory:
    store=ConversationStore(Path(directory)/"conversations.json")
    service=ResumeImportService(store,extractor_factory=CompleteExtractor,cloud=Cloud())
    store._resume_service=service
    OpportunityWebHandler.store=store
    server=ThreadingHTTPServer(("127.0.0.1",0),OpportunityWebHandler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    print(json.dumps({"port":server.server_port,"docx":base64.b64encode(docx_bytes()).decode(),
                      "scan":base64.b64encode(pdf_bytes([])).decode()}),flush=True)
    try: sys.stdin.readline()
    finally:
        server.shutdown();server.server_close();thread.join(3);service.close()

