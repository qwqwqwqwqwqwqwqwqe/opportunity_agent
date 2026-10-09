"""Owned, idempotent, field-version checked choices; no model invocation."""
from sqlalchemy import select

from ...models import CandidateFact, StudentProfile
from ..db.models import Profile, ProfileChange, ProfileConflict
from .applications import ApplicationCommandService


class ProfileConflictService:
    def __init__(self, session):
        self.session = session

    async def record(self, user_id: str, run_id: str, candidate: dict) -> ProfileConflict:
        existing = await self.session.scalar(select(ProfileConflict).where(
            ProfileConflict.run_id == run_id, ProfileConflict.field == candidate["field"]))
        if existing:
            return existing
        row = ProfileConflict(user_id=user_id, run_id=run_id, **{
            key: candidate[key] for key in ("field", "old_value", "new_value", "old_source", "new_source",
                                            "new_evidence", "candidate", "expected_version", "status")})
        self.session.add(row)
        await self.session.flush()
        return row

    async def resolve(self, conflict_id: str, user_id: str, choice: str) -> ProfileConflict:
        if choice not in {"new", "old"}:
            raise ValueError("choice must be new or old")
        row = await self.session.scalar(select(ProfileConflict).where(
            ProfileConflict.id == conflict_id, ProfileConflict.user_id == user_id).with_for_update())
        if row is None:
            raise LookupError("conflict not found")
        desired = "resolved_new" if choice == "new" else "resolved_old"
        if row.status != "pending":
            if row.status != desired:
                raise ValueError("conflict already resolved with another choice")
            return row
        profile = await self.session.scalar(select(Profile).where(Profile.user_id == user_id).with_for_update())
        if profile is None:
            raise ValueError("profile not found")
        current = StudentProfile.model_validate({**profile.payload, "user_id": user_id})
        current_value = current.model_dump(mode="json")[row.field]
        if choice == "new" and current_value != row.old_value:
            raise ValueError("profile field changed; refresh conflict before deciding")
        if choice == "new":
            fact = CandidateFact.model_validate(row.candidate)
            confirmed = fact.model_dump(mode="json") | {"source": "user_confirmed", "confidence": 1.0,
                                                        "needs_confirmation": False}
            service = ApplicationCommandService(self.session)
            approval = await service.propose(user_id, "profile.change", {
                "facts": [confirmed], "expected_version": profile.version,
            }, f"conflict:{row.id}", row.run_id, "用户在冲突弹窗中选择使用新信息")
            await service.decide(approval.id, user_id, True)
        else:
            self.session.add(ProfileChange(user_id=user_id, run_id=row.run_id, field=row.field,
                                           before=current_value, after=current_value,
                                           reason="User kept existing value in conflict dialog"))
        row.status = desired
        await self.session.flush()
        return row
