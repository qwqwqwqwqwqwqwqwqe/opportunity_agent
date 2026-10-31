"""Tests-only ASGI entrypoint; unique SQLite DB and deterministic model.

python -m uvicorn chat_actions_app:app --app-dir tests/browser --port 18035
Never import this module from production or use a production database.
"""
import os
from pathlib import Path
from uuid import uuid4

test_dir = Path(".tmp-workspace-browser")
test_dir.mkdir(exist_ok=True)
os.environ.update(APP_MODE="applyops", AUTO_CREATE_SCHEMA="1", OPPORTUNITY_AGENT_DISABLE_DOTENV="1",
    MEMORY_VECTOR_ENABLED="0", DOMAIN_AGENT_TRANSPORT="local",
    DATABASE_URL="sqlite+aiosqlite:///" + str(test_dir / ("chat-actions-browser-" + uuid4().hex + ".db")).replace("\\", "/"),
    JWT_SECRET="isolated-chat-actions-browser-test-only-secret")

from opportunity_agent.llm_client import LLMClient
LLMClient.enabled = property(lambda self: False)
import opportunity_agent.v2.api.app as api
from opportunity_agent.v3.profile_assistant import ProfileAssistant
from opportunity_agent.v3.chat_search import ChatSearchService, SearchSummary
from opportunity_agent.v3.monitor_web import parse_html


class BrowserModel:
    enabled = True

    def generate_structured(self, model_type, *, context, **kwargs):
        if model_type is SearchSummary:
            block = next(b for b in context['evidence_blocks'] if 'deadline' in b['text'])
            return model_type.model_validate({'claims':[{'text':'测试网页的截止日期是2026年12月15日。','block_id':block['id'],'quote':block['text']}]})
        if context['query'].startswith('帮我查'):
            return model_type.model_validate({'intent':'search','search':{'search_keywords':'CMU MSML admissions','university':'CMU','program':'MSML'}})
        if context["query"] == "取消这条测试待办":
            return model_type.model_validate({"intent":"actions", "todos":[{"title":"取消的待办", "scope":"global"}]})
        return model_type.model_validate({"intent":"actions", "projects":[{"ref":"cmu", "university":"CMU", "name":"MSAII", "cycle":"2027 Fall",
            "requirements":[{"title":"SOP"}, {"title":"推荐信", "kind":"recommendation", "required_count":3}]}],
            "todos":[{"title":"修改 SOP", "scope":"new_project", "project_ref":"cmu", "due_date":"2026-11-03"}],
            "events":[{"title":"见推荐人", "date":"2026-11-02"}]})

    def generate(self, **kwargs):
        return ""


class BrowserDiscovery:
    async def search(self,query,domains):
        return {'results':[{'title':'CMU MSML synthetic browser fixture','url':'https://www.ml.cmu.edu/fixture-program'}]}


class BrowserFetcher:
    async def fetch(self,url,hosts):
        return parse_html('<main><h1>Synthetic test page</h1><p>The application deadline is December 15, 2026. '
                          'This artificial fixture tests website confirmation and must not be treated as an actual school policy.</p></main>',url)


browser_model = BrowserModel()
api.orchestrator = ProfileAssistant(browser_model,ChatSearchService(browser_model,BrowserDiscovery(),BrowserFetcher()))
app = api.app
