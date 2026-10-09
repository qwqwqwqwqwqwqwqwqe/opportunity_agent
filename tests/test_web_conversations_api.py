import json
import shutil
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from opportunity_agent.conversation_store import ConversationStore
from opportunity_agent.web_app import OpportunityWebHandler


def _request(port: int, path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urlopen(request, timeout=3) as response:  # noqa: S310 - loopback test server
        return response.status, json.loads(response.read().decode("utf-8"))


def test_conversations_api_import_list_fetch_and_delete():
    directory = Path(tempfile.mkdtemp(prefix="conversation-api-", dir=Path.cwd()))
    original_store, original_sessions = OpportunityWebHandler.store, OpportunityWebHandler.sessions
    OpportunityWebHandler.store = ConversationStore(directory / "conversations.json")
    OpportunityWebHandler.sessions = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), OpportunityWebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        status, imported = _request(port, "/api/conversations/import", "POST", {
            "session_id": "shared-session",
            "conversation_title": "测试会话",
            "messages": [{"role": "assistant", "content": "你好"}, {"role": "user", "content": "第二条"}],
        })
        assert status == 200
        assert imported["conversation_messages"][0]["content"] == "你好"
        message_id = imported["conversation_messages"][0]["message_id"]

        _, listing = _request(port, "/api/conversations")
        assert listing["conversations"][0]["session_id"] == "shared-session"
        _, record = _request(port, "/api/conversations/shared-session")
        assert record["title"] == "测试会话"
        status, message_deleted = _request(port, f"/api/conversations/shared-session/messages/{message_id}", "DELETE")
        assert status == 200 and message_deleted["deleted"] is True
        assert [item["content"] for item in message_deleted["conversation_messages"]] == ["第二条"]
        status, deleted = _request(port, "/api/conversations/shared-session", "DELETE")
        assert status == 200 and deleted["deleted"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        OpportunityWebHandler.store, OpportunityWebHandler.sessions = original_store, original_sessions
        shutil.rmtree(directory, ignore_errors=True)
