"""Command proposal gate.  Only an accepted approval writes task/application state."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (Application, ApplicationPlan, ApplicationTask, ApprovalRequest, ChangeProposal,
                         Profile, ProfileChange, ProfileFact, TaskProgress, MemoryItem, AgentRun)
from ...models import StudentProfile
from ...conflict_resolver import ProfileConflictResolver


class ApplicationCommandService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def propose(self, user_id: str, proposal_type: str, payload: dict[str, Any], request_id: str,
                      run_id: str | None = None, reason: str = "") -> ApprovalRequest:
        digest = hashlib.sha256(json.dumps({"user": user_id, "type": proposal_type, "payload": payload, "request": request_id}, sort_keys=True, default=str).encode()).hexdigest()
        old = await self.session.scalar(select(ChangeProposal).where(ChangeProposal.idempotency_key == digest))
        if old:
            approval = await self.session.scalar(select(ApprovalRequest).where(ApprovalRequest.proposal_id == old.id))
            if approval:
                return approval
        proposal = ChangeProposal(user_id=user_id, run_id=run_id, proposal_type=proposal_type,
                                  payload=payload, reason=reason, idempotency_key=digest)
        self.session.add(proposal)
        await self.session.flush()
        approval = ApprovalRequest(proposal_id=proposal.id, user_id=user_id, token=secrets.token_urlsafe(32))
        self.session.add(approval)
        await self.session.flush()
        return approval

    async def decide(self, approval_id: str, user_id: str, accept: bool) -> ChangeProposal:
        approval = await self.session.scalar(select(ApprovalRequest).where(
            ApprovalRequest.id == approval_id, ApprovalRequest.user_id == user_id).with_for_update())
        if not approval:
            raise ValueError("approval request not found")
        proposal = await self.session.get(ChangeProposal, approval.proposal_id)
        if not proposal:
            raise ValueError("proposal not found")
        if approval.status != "pending":
            return proposal
        if not accept:
            approval.status, proposal.status = "rejected", "rejected"
            await self.session.flush()
            return proposal
        async with self.session.begin_nested():
            await self._apply(proposal)
            await self.session.flush()
        approval.status, proposal.status = "accepted", "applied"
        await self.session.flush()
        return proposal

    async def _apply(self, proposal: ChangeProposal) -> None:
        if proposal.proposal_type == "change_set":
            changes = proposal.payload.get("changes", [])
            if not changes or not isinstance(changes, list):
                raise ValueError("change_set must contain changes")
            for change in changes:
                await self._apply_one(proposal, change["type"], change["payload"])
            return
        await self._apply_one(proposal, proposal.proposal_type, proposal.payload)

    async def _apply_one(self, proposal: ChangeProposal, proposal_type: str, body: dict[str, Any]) -> None:
        if proposal_type == "preference.change":
            from .memory import MemoryService
            await MemoryService(self.session).apply_confirmed(proposal.user_id, body, proposal.id)
            return
        if proposal_type == "profile.change":
            profile = await self.session.scalar(select(Profile).where(Profile.user_id == proposal.user_id).with_for_update())
            if profile is None:
                profile = Profile(user_id=proposal.user_id, payload={}, version=1)
                self.session.add(profile)
                await self.session.flush()
            if body.get("expected_version") is not None and profile.version != body["expected_version"]:
                raise ValueError("profile version conflict")
            updated = dict(profile.payload or {})
            for fact in body.get("facts", []):
                field, value, operation = fact.get("field"), fact.get("normalized_value", fact.get("raw_value")), fact.get("operation", "set")
                if field not in ProfileConflictResolver.PROFILE_FIELDS:
                    raise ValueError(f"unsupported profile field: {field}")
                before = updated.get(field)
                if operation == "append":
                    existing = before if isinstance(before, list) else []
                    additions = value if isinstance(value, list) else [value]
                    updated[field] = list(dict.fromkeys([*existing, *additions]))
                elif operation == "remove":
                    if isinstance(before, list):
                        targets = set(value if isinstance(value, list) else [value])
                        updated[field] = [item for item in before if item not in targets]
                    elif before == value:
                        updated[field] = None
                else:
                    updated[field] = value
                self.session.add(ProfileFact(user_id=proposal.user_id, field=field, raw_value=fact.get("raw_value"),
                                             normalized_value=value, source=fact.get("source", "agent"),
                                             confidence=float(fact.get("confidence", 0.0)), evidence=fact.get("evidence"),
                                             operation=operation))
                self.session.add(ProfileChange(user_id=proposal.user_id, run_id=proposal.run_id, field=field,
                                               before=before, after=updated.get(field), reason="User accepted agent proposal"))
            if any(fact.get("field") in {"target_schools", "target_programs"} for fact in body.get("facts", [])):
                updated.pop("target_program_choices", None)
            StudentProfile.model_validate({**updated, "user_id": proposal.user_id})
            profile.payload, profile.version = updated, profile.version + 1
            return
        if proposal_type == "application.create":
            if body.get("status", "considering") not in {"considering", "preparing", "submitted", "offered", "rejected", "withdrawn"}:
                raise ValueError("unsupported application status")
            existing = await self.session.scalar(select(Application).where(
                Application.user_id == proposal.user_id, Application.university == body["university"],
                Application.program == body["program"], Application.intake == body.get("intake", "")))
            if existing:
                return
            self.session.add(Application(user_id=proposal.user_id, university=body["university"],
                                         program=body["program"], intake=body.get("intake", ""),
                                         status=body.get("status", "considering"), deadline=body.get("deadline")))
            return
        if proposal_type == "application.change":
            application = await self.session.scalar(select(Application).where(
                Application.id == body["application_id"]).with_for_update())
            if not application or application.user_id != proposal.user_id:
                raise ValueError("application not found")
            if application.version != body.get("expected_version"):
                raise ValueError("application version conflict")
            if body["status"] not in {"considering", "preparing", "submitted", "offered", "rejected", "withdrawn"}:
                raise ValueError("unsupported application status")
            application.status, application.version = body["status"], application.version + 1
            return
        if proposal_type == "plan.replace":
            await self._apply_plan(proposal, body)
            return
        if proposal_type != "task.command":
            raise ValueError(f"unsupported proposal type: {proposal_type}")
        task = await self.session.scalar(select(ApplicationTask).where(
            ApplicationTask.id == body["task_id"]).with_for_update())
        if not task:
            raise ValueError("task not found")
        if task.application_id:
            owner = await self.session.get(Application, task.application_id)
        else:
            owner = await self.session.get(ApplicationPlan, task.plan_id) if task.plan_id else None
        if owner is None or owner.user_id != proposal.user_id:
            raise ValueError("task not found")
        if body.get("expected_status") is not None and task.status != body["expected_status"]:
            raise ValueError("task status conflict")
        action = body["action"]
        states = {"start": "in_progress", "complete": "completed", "postpone": "planned", "cancel": "cancelled", "reset": "planned"}
        if action not in states:
            raise ValueError("unsupported task action")
        task.status = states[action]
        if body.get("due_at"):
            task.due_at = datetime.fromisoformat(body["due_at"])
        task.evidence = body.get("evidence") or task.evidence
        self.session.add(TaskProgress(task_id=task.id, action=action, evidence=body.get("evidence", ""), source="approval"))

    async def _apply_plan(self, proposal: ChangeProposal, body: dict[str, Any]) -> None:
        if proposal.run_id:
            from ..agents.contracts import ResearchResult, research_revision
            run = await self.session.get(AgentRun, proposal.run_id)
            state = run.graph_state if run else {}
            if (state.get("completion") or {}).get("status") in {"FAIL", "PARTIAL", "RETRY"}:
                raise ValueError("plan evidence is incomplete; regenerate the plan")
            expected_revision = body.get("research_revision")
            raw_research = state.get("research_result")
            if expected_revision is not None and expected_revision != research_revision(
                ResearchResult.model_validate(raw_research) if raw_research else None
            ):
                raise ValueError("plan research version conflict")
        profile = await self.session.scalar(select(Profile).where(Profile.user_id == proposal.user_id).with_for_update())
        if not profile or profile.version != body.get("expected_profile_version"):
            raise ValueError("profile version conflict")
        current = await self.session.scalar(select(ApplicationPlan).where(
            ApplicationPlan.user_id == proposal.user_id, ApplicationPlan.status == "active"
        ).order_by(ApplicationPlan.version.desc()).with_for_update())
        current_version = current.version if current else 0
        if current_version != body.get("expected_version"):
            raise ValueError("plan version conflict")
        roadmap = body.get("roadmap")
        if not isinstance(roadmap, dict) or not roadmap.get("timeline"):
            raise ValueError("plan roadmap is incomplete")
        tasks = body.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("plan needs executable tasks")
        old_tasks = {}
        if current:
            for item in (await self.session.scalars(select(ApplicationTask).where(
                ApplicationTask.plan_id == current.id))).all():
                old_tasks[item.stable_key] = item
            current.status = "superseded"
        plan = ApplicationPlan(user_id=proposal.user_id, version=current_version + 1,
                               roadmap=roadmap, status="active",
                               revision_reason=roadmap.get("revision_reason", "user_requested_replan"),
                               source_run_id=proposal.run_id)
        self.session.add(plan)
        await self.session.flush()
        seen = set()
        for row in tasks:
            key = row.get("stable_key")
            if not key or key in seen:
                raise ValueError("plan task stable keys must be unique")
            seen.add(key)
            previous = old_tasks.get(key)
            due = row.get("due_date")
            self.session.add(ApplicationTask(
                plan_id=plan.id, stable_key=key, title=row["title"],
                category=row.get("category", "application"),
                due_at=datetime.fromisoformat(due) if due else None,
                status=previous.status if previous else "planned",
                evidence=previous.evidence if previous else None,
            ))
