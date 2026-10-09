from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_STORE_PATH = Path(__file__).resolve().parent.parent / "data" / "conversations.json"


class DeletedConversation(ValueError):
    pass


class RevisionConflict(ValueError):
    pass


class ConversationStore:
    """Single-process durable store; never resurrect deleted browser snapshots."""

    def __init__(self, path: Path = DEFAULT_STORE_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.RLock] = {}
        self.inflight: set[tuple[str, str]] = set()

    def session_lock(self, session_id: str):
        with self._lock:
            return self._session_locks.setdefault(session_id, threading.RLock())

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted([self._summary(r) for r in self._read()["conversations"].values()],
                          key=lambda item: item["updated_at"], reverse=True)

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read()["conversations"].get(session_id)

    def is_deleted(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._read()["deleted"]

    def save(self, session_id: str, title: str, state: dict[str, Any],
             expected_revision: int | None = None) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            if session_id in document["deleted"]:
                raise DeletedConversation("会话已删除，请新建对话")
            old = document["conversations"].get(session_id, {})
            revision = old.get("revision", 0)
            if expected_revision is not None and expected_revision != revision:
                raise RevisionConflict("会话已有新变更，请刷新后重试")
            revision += 1
            state = {**state, "state_revision": revision}
            record = {"session_id": session_id, "title": title.strip() or "新对话",
                      "updated_at": datetime.now(timezone.utc).isoformat(), "revision": revision, "state": state}
            document["conversations"][session_id] = record
            self._write(document)
            return record

    def delete(self, session_id: str) -> bool:
        with self.session_lock(session_id), self._lock:
            document = self._read()
            existed = document["conversations"].pop(session_id, None) is not None
            document["deleted"][session_id] = datetime.now(timezone.utc).isoformat()
            self._write(document)
            return existed

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 2, "conversations": {}, "deleted": {}}
        # Never mistake damaged/unreadable data for an empty database.
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("conversations"), dict):
            raise ValueError("会话文件格式异常，请从备份恢复；原文件未被覆盖")
        data.setdefault("deleted", {})
        return data

    def _write(self, document: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and document.get("schema_version", 1) < 2:
            backup = self.path.with_suffix(".v1.bak")
            if not backup.exists():
                shutil.copy2(self.path, backup)
        document["schema_version"] = 2
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    @staticmethod
    def _summary(record: dict[str, Any]) -> dict[str, Any]:
        messages = record.get("state", {}).get("conversation_messages", [])
        return {"session_id": record["session_id"], "title": record.get("title", "新对话"),
                "updated_at": record.get("updated_at", ""), "revision": record.get("revision", 0),
                "message_count": len(messages)}
