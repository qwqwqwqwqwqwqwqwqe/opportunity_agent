"""Idempotent import of V1's conversations.json into an explicitly chosen user."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from .db.models import Conversation, Message, User
from .db.session import SessionLocal, create_schema


async def migrate(path: Path, legacy_owner_email: str) -> dict[str, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    conversations = payload.get("conversations", []) if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    async with SessionLocal.begin() as session:
        owner = await session.scalar(select(User).where(User.email == legacy_owner_email.lower()))
        if not owner:
            raise ValueError("legacy owner must be an existing V2 account")
        imported = skipped = 0
        for item in conversations:
            legacy_id = str(item.get("session_id") or item.get("id") or "")
            if not legacy_id:
                skipped += 1
                continue
            title = str(item.get("title") or item.get("conversation_title") or "新对话")
            # Idempotency keys on the V1 session id, not the title: V1 leaves
            # many sessions titled "新对话", so a title key silently dropped
            # every collision after the first.
            existing = await session.scalar(select(Conversation).where(
                Conversation.user_id == owner.id, Conversation.legacy_session_id == legacy_id))
            if existing:
                skipped += 1
                continue
            conversation = Conversation(user_id=owner.id, title=title, legacy_session_id=legacy_id)
            session.add(conversation)
            await session.flush()
            state = item.get("state", item)
            messages = state.get("conversation_messages", item.get("messages", [])) if isinstance(state, dict) else []
            for message in messages:
                if not isinstance(message, dict) or not message.get("content"):
                    continue
                session.add(Message(conversation_id=conversation.id, role=message.get("role", "user"), content=str(message["content"]),
                                    status="legacy", metadata_json={"legacy_session_id": legacy_id}))
            imported += 1
    return {"imported": imported, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=Path("data/conversations.json"))
    parser.add_argument("--legacy-owner-email", required=True)
    args = parser.parse_args()
    asyncio.run(create_schema())
    print(json.dumps(asyncio.run(migrate(args.path, args.legacy_owner_email)), ensure_ascii=False))


if __name__ == "__main__":
    main()
