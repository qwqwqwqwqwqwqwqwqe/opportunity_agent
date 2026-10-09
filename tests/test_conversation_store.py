import shutil
import tempfile
from pathlib import Path

from opportunity_agent.conversation_store import ConversationStore


def test_conversation_store_persists_lists_and_deletes_records():
    directory = Path(tempfile.mkdtemp(prefix="conversation-store-", dir=Path.cwd()))
    try:
        path = directory / "conversations.json"
        store = ConversationStore(path)
        first = {"conversation_messages": [{"role": "user", "content": "第一条"}]}
        second = {"conversation_messages": [{"role": "user", "content": "第二条"}, {"role": "assistant", "content": "回复"}]}

        store.save("first", "第一段对话", first)
        store.save("second", "第二段对话", second)

        summaries = store.list()
        assert {item["session_id"] for item in summaries} == {"first", "second"}
        assert next(item for item in summaries if item["session_id"] == "second")["message_count"] == 2
        assert store.get("first")["state"]["conversation_messages"] == first["conversation_messages"]

        # A new store instance reads the same durable file, as another browser/server request would.
        reloaded = ConversationStore(path)
        assert reloaded.get("second")["title"] == "第二段对话"
        assert reloaded.delete("first") is True
        assert reloaded.get("first") is None
        assert reloaded.delete("missing") is False
    finally:
        shutil.rmtree(directory, ignore_errors=True)
