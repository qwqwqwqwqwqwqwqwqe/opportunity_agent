"""Chat-only business operations: actual approvals, receipts and persisted state."""
import asyncio
import importlib
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from opportunity_agent.v3.chat_actions import ChatDraft, ChatUnderstanding, build_commands, confirmation, handle_actions
from opportunity_agent.v3.profile_assistant import ProfileAssistant
from opportunity_agent.v3.service import WorkflowService
from opportunity_agent.v2.core import config
from opportunity_agent.v2.db.models import AgentRun
from opportunity_agent.v2.db.session import get_session
from test_v3_workflow import database, execute


class ScriptedModel:
    enabled = True

    def __init__(self, *drafts):
        self.drafts = list(drafts)
        self.contexts = []

    def generate_structured(self, model_type, *, context, **kwargs):
        self.contexts.append(deepcopy(context))
        value = self.drafts.pop(0)
        if isinstance(value, Exception):
            raise value
        if model_type is ChatUnderstanding and 'intent' not in value:
            value = {**value, 'intent':'actions'}
        return model_type.model_validate(value)


def initial(message, snapshot, **extra):
    return {"message":message, "workflow_snapshot":snapshot, "request_id":uuid4().hex,
            "user_id":"u", "conversation_id":"c", "run_id":uuid4().hex, **extra}


@pytest.mark.parametrize("text,expected", [("确认添加", "accept"), ("好的，确认保存！", "accept"),
    ("可以", "accept"), ("取消", "reject"), ("确认添加但日期改到明天", None),
    ("不要确认添加", None), ("网页上写着‘确认添加’", None), ("查询确认添加是什么意思", None)])
def test_confirmation_requires_whole_utterance(text, expected):
    assert confirmation(text) == expected


def test_build_project_requirements_linked_todos_and_calendar():
    draft = ChatDraft.model_validate({"projects":[{"ref":"cmu", "university":"CMU", "name":"MSAII", "cycle":"2027 Fall",
        "deadline_date":"2026-12-15", "requirements":[{"title":"推荐信", "kind":"recommendation", "required_count":3},
        {"title":"语言成绩", "kind":"score", "criteria":{"profile_field":"toefl_score", "minimum":105}}]}],
        "todos":[{"title":"修改 SOP", "scope":"new_project", "project_ref":"cmu", "due_date":"2026-11-01"}],
        "events":[{"title":"语言考试", "date":"2026-11-10", "type":"exam"}]})
    commands, lines, questions = build_commands(draft, {}, "r1")
    assert not questions and len(commands) == 3
    assert commands[1]["payload"]["application_id"] == "$ref:cmu"
    assert commands[1]["payload"]["show_in_calendar"] is True
    assert commands[0]["payload"]["requirements"][0]["materials"] == []
    assert "托福 ≥ 105" in lines[0]


def test_missing_fields_ambiguity_and_no_implicit_year():
    draft = ChatDraft.model_validate({"projects":[{"university":"CMU", "name":"MSAII"}],
        "todos":[{"title":"修改 SOP", "scope":"unresolved"}], "events":[{"title":"见推荐人"}]})
    _, _, questions = build_commands(draft, {}, "r")
    assert len(questions) == 3 and "申请周期" in questions[0]
    assert draft.projects[0].cycle is None


@pytest.mark.parametrize("draft", [
    {"todos":[{"title":"任务", "scope":"application", "application_id":"another-users-id"}]},
    {"todos":[{"title":"任务", "scope":"new_project", "project_ref":"not-in-draft"}]},
    {"projects":[{"ref":"a", "university":"A", "name":"B", "cycle":"2027", "requirements":[{"id":"stolen", "title":"SOP"}]}]},
    {"events":[{"title":"见面", "start_at":"2026-11-02T10:00:00"}]},
])
def test_invalid_association_or_naive_timestamp_cannot_be_proposed(draft):
    with pytest.raises(ValueError):
        build_commands(ChatDraft.model_validate(draft), {}, "r")


def test_duplicate_todo_and_missing_calendar_date_require_clarification():
    snapshot = {"tasks":[{"application_id":None,"title":"SOP", "due_date":None, "status":"planned"}]}
    _, _, questions = build_commands(ChatDraft.model_validate({"todos":[{"title":"SOP", "scope":"global"},
        {"title":"考试准备", "scope":"global", "show_in_calendar":True}]}), snapshot, "r")
    assert len(questions) == 2


def test_calendar_visible_todo_is_not_duplicated_as_separate_event():
    commands, _, questions = build_commands(ChatDraft.model_validate({"todos":[{"title":"见推荐人", "scope":"global", "due_date":"2026-11-02"}],
        "events":[{"title":"见推荐人", "date":"2026-11-02"}]}), {}, "r")
    assert not questions and [c["type"] for c in commands] == ["task.create"]


def test_acknowledgement_without_pending_draft_is_normal_chat():
    assert asyncio.run(handle_actions(ScriptedModel(), initial("好的", {}))) is None


@pytest.mark.parametrize('message', ['增加一个cmu msml项目，截止日期11.18', 'CMU MSML，帮我建档',
    '把CMU MSML放进我的申请清单', 'Could you put CMU MSML on my applications list?'])
def test_semantic_creation_without_keyword_gate_never_enters_profile_extraction(message, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Business creation must never enter profile extraction')
    monkeypatch.setattr('opportunity_agent.v3.profile_assistant.ProfileExtractionPipeline.extract', forbidden)
    async def scenario():
        client = ScriptedModel({'intent':'actions', 'scope':'applications', 'reason':'用户要求建立申请项目',
            'projects':[{'university':'CMU','name':'MSML','deadline_text':'11.18'}]})
        result = await ProfileAssistant(client).ainvoke(initial(message, {'today':'2026-10-29'}))
        assert len(client.contexts) == 1
        assert result['intent_recognition']['intent'] == 'actions'
        assert result['completion']['status'] == 'NEED_USER' and not result['proposals']
        assert not result['profile_result']['accepted_facts']
        assert '申请周期' in result['answer'] and '11.18' in result['answer'] and '哪一年' in result['answer']
        project = result['chat_action_draft']['projects'][0]
        assert project['cycle'] is None and project['deadline_date'] is None
    asyncio.run(scenario())


@pytest.mark.parametrize('intent,scope,message', [('query','applications','我增加的项目有多少个？'),
    ('general_chat','general','增加申请项目时会修改画像吗？'), ('query','profile','我的托福是多少？')])
def test_semantic_read_or_consultation_never_extracts_profile(intent, scope, message, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Read-only intent must never enter profile extraction')
    monkeypatch.setattr('opportunity_agent.v3.profile_assistant.ProfileExtractionPipeline.extract', forbidden)
    async def scenario():
        engine, factory, user, _ = await database()
        async with factory() as session:
            snap = await WorkflowService(session).snapshot(user)
        result = await ProfileAssistant(ScriptedModel({'intent':intent,'scope':scope})).ainvoke(initial(message, snap))
        assert not result['proposals'] and result['completion']['status'] == 'COMPLETED'
        assert not result['profile_result']['accepted_facts']
        await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize('response', [ValueError('timeout'), {'intent':'actions'},
    {'intent':'query','scope':'applications','projects':[{'university':'CMU'}]}])
def test_bad_or_empty_semantic_route_cannot_fall_through_to_profile(response, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Failed/empty intent recognition must never enter profile extraction')
    monkeypatch.setattr('opportunity_agent.v3.profile_assistant.ProfileExtractionPipeline.extract', forbidden)
    async def scenario():
        result = await ProfileAssistant(ScriptedModel(response)).ainvoke(initial('增加一个cmu msml项目，截止日期11.18', {'today':'2026-10-29'}))
        assert result['completion']['status'] == 'NEED_USER' and not result['proposals']
        assert not result['profile_result']['accepted_facts']
    asyncio.run(scenario())


def test_multi_turn_draft_is_not_written_and_uses_fixed_current_date():
    async def scenario():
        engine, factory, user, _ = await database()
        async with factory() as session:
            snap = await WorkflowService(session).snapshot(user, "2026-10-29")
        client = ScriptedModel({"projects":[{"university":"CMU", "name":"MSAII"}]},
            {"projects":[{"university":"CMU", "name":"MSAII", "cycle":"2027 Fall"}]})
        assistant = ProfileAssistant(client)
        first = await assistant.ainvoke(initial("帮我添加 CMU MSAII", snap))
        assert first["completion"]["status"] == "NEED_USER" and not first["proposals"]
        second = await assistant.ainvoke(initial("2027 Fall", snap, chat_action_draft=first["chat_action_draft"]))
        assert second["completion"]["status"] == "WAITING_APPROVAL"
        assert client.contexts[-1]["previous_draft"]["projects"][0]["name"] == "MSAII"
        assert client.contexts[-1]["today"] == "2026-10-29"
        assert not snap["applications"]
        await engine.dispose()
    asyncio.run(scenario())


def test_bad_model_keeps_previous_approval_and_no_write_claim():
    async def scenario():
        value = await handle_actions(ScriptedModel({"commands":[{"type":"notification.read"}]}),
            initial("日期改到明天", {"today":"2026-10-29"}, chat_action_draft={"todos":[]}, chat_pending_approval_ids=["prior"]))
        assert value["completion"]["status"] == "NEED_USER" and not value["proposals"]
        assert value["approval_ids"] == ["prior"] and "没有新增数据" in value["answer"]
    asyncio.run(scenario())


@pytest.mark.parametrize('first_text', ['帮我添加 CMU MSAII', '增加一个cmu msml项目，截止日期11.18'])
def test_http_chat_only_create_edit_confirm_cancel_and_ownership(monkeypatch, first_text):
    api = importlib.import_module("opportunity_agent.v2.api.app")
    mode = replace(config.settings, app_mode="applyops")
    monkeypatch.setattr(config, "settings", mode)
    monkeypatch.setattr(api, "settings", mode)
    monkeypatch.setattr(api, "schedule_run", lambda *args: None)
    from opportunity_agent.llm_client import LLMClient
    monkeypatch.setattr(LLMClient, "enabled", property(lambda self: False))
    complete = {"projects":[{"ref":"cmu", "university":"CMU", "name":"MSAII", "cycle":"2027 Fall",
                            "requirements":[{"title":"SOP"}]}],
                "todos":[{"title":"修改 SOP", "scope":"new_project", "project_ref":"cmu", "due_date":"2026-11-01"}],
                "events":[{"title":"见推荐人", "date":"2026-11-02"}]}
    partial = {"projects":[{"university":"CMU", "name":"MSAII"}]}
    if 'msml' in first_text:
        complete['projects'][0].update(name='MSML', deadline_date='2026-11-18')
        partial['projects'][0].update(name='MSML', deadline_text='11.18')
    changed = deepcopy(complete)
    changed["todos"][0]["due_date"] = "2026-11-03"
    client_model = ScriptedModel(partial, complete, changed,
        {"todos":[{"title":"取消的待办", "scope":"global"}]},
        {"todos":[{"title":"原子回滚待办", "scope":"global"}],
         "projects":[{"university":"Another", "name":"Program", "cycle":"2027 Fall"}]})
    monkeypatch.setattr(api, "orchestrator", ProfileAssistant(client_model))

    async def scenario():
        engine, factory, _, _ = await database()
        monkeypatch.setattr(api, "SessionLocal", factory)
        async def session_dep():
            async with factory() as session:
                yield session
        api.app.dependency_overrides[get_session] = session_dep
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
                registered = await client.post("/api/v1/auth/register", json={"email":"actions@test.local", "password":"long-enough-password"})
                owner = registered.json()["user"]["id"]
                conv = (await client.post("/api/v1/conversations", json={"title":"聊天操作"})).json()["id"]

                async def run(text, conversation=conv):
                    request = uuid4().hex
                    started = await client.post(f"/api/v1/conversations/{conversation}/runs", json={"message":text,"request_id":request})
                    assert started.status_code == 202, started.text
                    identity = started.json()["run_id"]
                    await api.execute_run(identity, owner, conversation, text, request)
                    result = (await client.get("/api/v1/runs/"+identity)).json()
                    assert result["status"] == "completed", result
                    return result

                first = await run(first_text)
                assert first["completion"]["status"] == "NEED_USER" and not first["approval_ids"]
                assert first['intent_recognition']['intent'] == 'actions'
                assert not first['profile_result']['accepted_facts']
                second = await run("2027 Fall，项目截止是2026年11月18日，再加一个修改SOP待办，11月1日截止，以及11月2日见推荐人的日程")
                old_approval = second["approval_ids"][0]
                assert (await client.get("/api/v3/dashboard")).json()["applications"] == 0
                revised = await run("把待办截止改为11月3日，其他保持不变")
                new_approval = revised["approval_ids"][0]
                assert new_approval != old_approval
                assert (await client.get("/api/v1/approvals/"+old_approval)).json()["status"] == "rejected"
                other_conv = (await client.post("/api/v1/conversations", json={"title":"另一对话"})).json()["id"]
                elsewhere = await run("确认添加", other_conv)
                assert elsewhere["completion"]["status"] == "NEED_USER"
                saved = await run("确认添加")
                assert saved["completion"]["status"] == "COMPLETED" and "已确认保存" in saved["answer"]
                assert "1个申请项目" in saved["answer"] and "1条待办" in saved["answer"] and "1项日程" in saved["answer"]
                snap = (await client.get("/api/v3/snapshot")).json()
                assert len(snap["applications"]) == len(snap["tasks"]) == len(snap["calendar_events"]) == 1
                assert snap["tasks"][0]["application_id"] == snap["applications"][0]["id"]
                assert snap["tasks"][0]["due_date"] == "2026-11-03"
                calendar = (await client.get("/api/v3/calendar?from=2026-11-01&to=2026-11-04")).json()
                assert len(calendar) == 2
                await run("确认添加")
                await client.post("/api/v1/approvals/"+new_approval+"/accept", json={})
                await client.post("/api/v1/approvals/"+old_approval+"/accept", json={})
                assert len((await client.get("/api/v3/snapshot")).json()["tasks"]) == 1
                cancelled = await run("新增一个全局待办")
                await run("取消")
                assert (await client.get("/api/v1/approvals/"+cancelled["approval_ids"][0])).json()["status"] == "rejected"
                assert len((await client.get("/api/v3/snapshot")).json()["tasks"]) == 1
                pending = await run("添加 Another Program 2027 Fall，并生成一个待办")
                async with factory.begin() as session:
                    await execute(session, owner, "project.save", {"request_id":uuid4().hex, "university":"Another", "name":"Program", "cycle":"2027 Fall"})
                failed = await run("确认添加")
                assert failed["completion"]["status"] == "NEED_USER" and "没有保存" in failed["answer"]
                assert len((await client.get("/api/v3/snapshot")).json()["tasks"]) == 1
                async with factory() as session:
                    run_row = await session.scalar(select(AgentRun).where(AgentRun.id == pending["id"]))
                    assert run_row.user_id == owner
                await client.post("/api/v1/auth/register", json={"email":"other-actions@test.local", "password":"long-enough-password"})
                assert (await client.get("/api/v1/approvals/"+pending["approval_ids"][0])).status_code == 404
                assert (await client.post("/api/v1/approvals/"+pending["approval_ids"][0]+"/accept", json={})).status_code == 404
        finally:
            api.app.dependency_overrides.pop(get_session, None)
            await engine.dispose()
    asyncio.run(scenario())
