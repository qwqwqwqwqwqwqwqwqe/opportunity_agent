"""Database authority and optimistic-write helpers for V2 services."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import AgentEvent, AgentRun, Conversation, Message, Profile, User


class VersionConflict(RuntimeError):
    pass


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_user_by_email(self, email: str) -> User | None:
        return await self.session.scalar(select(User).where(User.email == email.lower()))

    async def get_user(self, user_id: str) -> User | None:
        return await self.session.get(User, user_id)

    async def ensure_profile(self, user_id: str) -> Profile:
        profile = await self.session.scalar(select(Profile).where(Profile.user_id == user_id))
        if profile is None:
            profile = Profile(user_id=user_id, payload={}, version=1)
            self.session.add(profile)
            await self.session.flush()
        return profile

    async def patch_profile(self, user_id: str, payload: dict[str, Any], expected_version: int | None) -> Profile:
        profile = await self.ensure_profile(user_id)
        if expected_version is not None and profile.version != expected_version:
            raise VersionConflict("profile has changed; refresh before retrying")
        statement = update(Profile).where(Profile.id == profile.id)
        if expected_version is not None:
            statement = statement.where(Profile.version == expected_version)
        result = await self.session.execute(statement.values(payload=payload, version=Profile.version + 1))
        if result.rowcount != 1:
            raise VersionConflict("profile has changed; refresh before retrying")
        await self.session.refresh(profile)
        return profile

    async def owned_conversation(self, conversation_id: str, user_id: str) -> Conversation | None:
        return await self.session.scalar(select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id,
        ))

    async def create_run(self, user_id: str, conversation_id: str, message: str, request_id: str) -> AgentRun:
        duplicate = await self.session.scalar(select(AgentRun).where(
            AgentRun.conversation_id == conversation_id, AgentRun.request_id == request_id,
        ))
        if duplicate:
            await self._validate_run_message(duplicate, message)
            return duplicate
        run = AgentRun(user_id=user_id, conversation_id=conversation_id, request_id=request_id,
                       status="queued", graph_state={"request_id": request_id, "message": message})
        try:
            async with self.session.begin_nested():
                self.session.add(run)
                self.session.add(Message(conversation_id=conversation_id, role="user", content=message,
                                         request_id=request_id, status="received"))
                await self.session.flush()
        except IntegrityError:
            duplicate = await self.session.scalar(select(AgentRun).where(
                AgentRun.conversation_id == conversation_id, AgentRun.request_id == request_id))
            if duplicate is None:
                raise
            await self._validate_run_message(duplicate, message)
            return duplicate
        return run

    async def _validate_run_message(self, run: AgentRun, message: str) -> None:
        original = (run.graph_state or {}).get("message")
        if original is None:
            original = await self.session.scalar(select(Message.content).where(
                Message.conversation_id == run.conversation_id,
                Message.request_id == run.request_id, Message.role == "user").limit(1))
        if original != message:
            raise VersionConflict("request_id is already used for a different message")

    async def claim_run(self, run_id: str, token: str, lease_seconds: int = 600) -> bool:
        now = datetime.now(timezone.utc)
        result = await self.session.execute(update(AgentRun).where(
            AgentRun.id == run_id, AgentRun.status == "queued",
        ).values(status="running", execution_token=token,
                 lease_expires_at=now + timedelta(seconds=lease_seconds)))
        return result.rowcount == 1

    async def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> AgentEvent:
        sequence = (await self.session.execute(update(AgentRun).where(AgentRun.id == run_id)
                    .values(event_sequence=AgentRun.event_sequence + 1)
                    .returning(AgentRun.event_sequence))).scalar_one()
        event = AgentEvent(run_id=run_id, sequence=sequence, event_type=event_type, payload=payload)
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_events(self, run_id: str, after_sequence: int = 0) -> list[AgentEvent]:
        return list((await self.session.scalars(select(AgentEvent).where(
            AgentEvent.run_id == run_id, AgentEvent.sequence > after_sequence,
        ).order_by(AgentEvent.sequence))).all())

    async def finalize_run(self, run: AgentRun, state: dict[str, Any], status: str,
                           execution_token: str | None = None) -> None:
        statement = update(AgentRun).where(AgentRun.id == run.id)
        if execution_token is not None:
            statement = statement.where(AgentRun.execution_token == execution_token, AgentRun.status == "running")
        result = await self.session.execute(statement.values(status=status,
            graph_state={**(run.graph_state or {}), **state}, lease_expires_at=None))
        if result.rowcount != 1:
            raise VersionConflict("run execution lease was replaced")
        await self.session.refresh(run)
