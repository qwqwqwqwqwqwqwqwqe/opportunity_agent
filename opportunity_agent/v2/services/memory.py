"""The sole preference authority. All operations join the caller's transaction."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..db.models import MemoryAudit, MemoryItem, MemoryOutbox
from ..repositories import VersionConflict
from .memory_contracts import ConsolidationInput, PreferenceSnapshot, PreferenceView

KEYS = {"avoid_gre", "employment_priority", "fallback_country", "budget_preference"}


def explicit_preferences(message: str) -> list[dict]:
    """Positive rule proof, not a model's self-declared source/confidence."""
    from ...turn_understanding import assertion_text
    from ..agents.profile_extraction import evidence_is_other_person
    found = {}
    for clause in re.split(r"[，,。；;\n]", message):
        if re.search(r"可能|也许|大概|不确定|假如|假设|如果|要是|是否|能否|是不是|好吗|怎么办|[?？]|听说|他说|她说|老师|导师|他们|她们|朋友|室友|同学|maybe|probably|\bif\b|[“”\"「」]", clause, re.I):
            continue
        text, _ = assertion_text(clause.strip())
        if not text or evidence_is_other_person(message, text):
            continue
        for pattern, key, value in [
            (r"(?:不想考|不考虑|不接受|排除).{0,10}(?:GRE|required\s*GRE)|(?:需要|要求)\s*GRE.{0,8}(?:不考虑|不接受)", "avoid_gre", True),
            (r"(?<!不)(?:愿意|决定|准备|打算)\s*考\s*GRE|取消.{0,12}(?:不考|不考虑|排除)\s*GRE.{0,8}偏好", "avoid_gre", False),
            (r"更(?:看重|关心|重视)就业|就业优先", "employment_priority", True),
            (r"(?:不再|取消).{0,6}(?:就业优先|优先就业)", "employment_priority", False),
        ]:
            match = re.search(pattern, text, re.I)
            if match:
                found[key] = {"key": key, "value": value, "evidence": text, "confidence": .98, "source": "user_explicit"}
        fallback = re.search(r"(?:备选(?:国家)?|保底(?:国家)?)(?:是|为|选|[:：])?\s*(加拿大|英国|美国|澳大利亚|Canada|UK|US|Australia)", text, re.I)
        if fallback:
            found["fallback_country"] = {"key": "fallback_country", "value": fallback[1], "evidence": text, "confidence": .98, "source": "user_explicit"}
        budget = re.search(r"(?:我的)?预算(?:上限)?(?:是|为|[:：])?\s*(\d+(?:\.\d+)?\s*(?:万|千)?\s*(?:元|人民币|美元|美金|英镑))", text)
        if budget:
            found["budget_preference"] = {"key": "budget_preference", "value": budget[1], "evidence": text, "confidence": .98, "source": "user_explicit"}
    return list(found.values())


def valid_value(key, value):
    if key not in KEYS:
        raise ValueError("unsupported preference key")
    if key in {"avoid_gre", "employment_priority"} and type(value) is not bool:
        raise ValueError("preference requires a boolean")
    if key in {"fallback_country", "budget_preference"}:
        # Existing reviewed candidates can contain a conditional country object.
        if isinstance(value, dict) and key == "fallback_country":
            if not isinstance(value.get("country"), str) or not value["country"].strip():
                raise ValueError("fallback preference requires a country")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError("preference requires a bounded string")
        if len(json.dumps(value, ensure_ascii=False)) > 500:
            raise ValueError("preference value is too long")


def _insert(session, table):
    return (pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert)(table)


def _value(row):
    return row.value.get("value") if isinstance(row.value, dict) else row.value


class MemoryService:
    def __init__(self, session, *, embedder=None):
        self.session, self.embedder = session, embedder

    async def versions(self, user_id):
        rows = (await self.session.scalars(select(MemoryItem).where(MemoryItem.user_id == user_id, MemoryItem.memory_type == "preference"))).all()
        return {row.key: row.version for row in rows}

    async def list_preferences(self, user_id):
        now = datetime.now(timezone.utc)
        return list((await self.session.scalars(select(MemoryItem).where(
            MemoryItem.user_id == user_id, MemoryItem.memory_type == "preference", MemoryItem.active.is_(True),
            or_(MemoryItem.expires_at.is_(None), MemoryItem.expires_at > now)
        ).order_by(MemoryItem.updated_at.desc(), MemoryItem.key))).all())

    @staticmethod
    def view(row, method="rule"):
        return PreferenceView(memory_id=row.id, key=row.key, value=_value(row), source=row.source,
            confidence=row.confidence, version=row.version, evidence=row.evidence,
            source_conversation_id=row.source_conversation_id, source_message_id=row.source_message_id,
            expires_at=row.expires_at, relevance_method=method)

    async def _vector(self, text, kind="query"):
        if self.embedder is None and os.getenv("MEMORY_VECTOR_ENABLED", "0") == "1":
            from ..rag.models import shared_embedder
            self.embedder = shared_embedder()
        if self.embedder is None:
            return None
        try:
            async with asyncio.timeout(3):
                if kind == "passage" and hasattr(self.embedder, "passages"):
                    vector = (await asyncio.to_thread(self.embedder.passages, [text]))[0]
                else:
                    vector = await asyncio.to_thread(self.embedder.embed, text)
            return vector if vector and len(vector) == 384 else None
        except Exception:
            return None

    async def retrieve_preferences(self, user_id, query, conversation_context=None, limit=2):
        try:
            rows = [r for r in await self.list_preferences(user_id) if r.confidence >= .75 and r.key in KEYS]
            context = conversation_context or {}
            topic = r"项目|申请|规划|学校|就业|GRE|预算|备选|program|plan|school|budget|career"
            relevant = bool(re.search(topic, query, re.I))
            if not relevant and re.search(r"继续|之前|刚才|上面|这些|照.{0,8}条件", query):
                relevant = bool(re.search(topic, str(context.get("summary", "")) + str(context.get("recent_messages", [])), re.I))
            if not relevant:
                return PreferenceSnapshot()
            vector = await self._vector(query)
            from ..rag.retrieval import cosine
            cues = {"avoid_gre": r"GRE|项目|申请|学校|program|school", "employment_priority": r"就业|工作|规划|career|plan",
                    "fallback_country": r"国家|备选|保底|学校|country|school", "budget_preference": r"预算|学费|费用|budget|tuition"}
            scored = []
            for row in rows:
                rule = bool(re.search(cues[row.key], query, re.I))
                use_vector = vector is not None and row.embedding is not None and self.embedder.model_name == row.embedding_model
                similarity = cosine(vector, list(row.embedding)) if use_vector else 0
                if rule or similarity >= .5 or (not any(re.search(c, query, re.I) for c in cues.values()) and relevant):
                    scored.append((float(rule)+similarity, row, "vector" if use_vector else "rule"))
            scored.sort(key=lambda x: (-x[0], x[1].key))
            return PreferenceSnapshot(preferences=[self.view(row, method) for _, row, method in scored[:min(2, max(0, limit))]])
        except Exception:
            # Database exceptions are isolated with a savepoint by API callers.
            return PreferenceSnapshot(retrieval_status="unavailable")

    async def _ensure(self, user_id, key):
        await self.session.execute(_insert(self.session, MemoryItem).values(id=uuid4().hex,
            user_id=user_id, memory_type="preference", key=key, value={}, source="unset", confidence=0,
            version=0, active=False).on_conflict_do_nothing(index_elements=["user_id", "memory_type", "key"]))
        row = await self.session.scalar(select(MemoryItem).where(MemoryItem.user_id == user_id,
            MemoryItem.memory_type == "preference", MemoryItem.key == key).with_for_update())
        await self.session.refresh(row)
        return row

    async def put(self, user_id, key, value, *, source, confidence, evidence, event_key,
                  expected_version=None, conversation_id=None, message_id=None, active=True):
        valid_value(key, value)
        old_event = await self.session.scalar(select(MemoryAudit).where(MemoryAudit.user_id == user_id, MemoryAudit.event_key == event_key))
        if old_event:
            if (old_event.snapshot.get("key") != key or old_event.snapshot.get("after", {}).get("value") != {"value": value}
                    or old_event.snapshot.get("expected_version") != expected_version):
                raise VersionConflict("memory request_id reused with different value")
            return await self.session.get(MemoryItem, old_event.memory_id), False
        row = await self._ensure(user_id, key)
        # Recheck after obtaining the row lock for concurrent duplicate requests.
        old_event = await self.session.scalar(select(MemoryAudit).where(MemoryAudit.user_id == user_id, MemoryAudit.event_key == event_key))
        if old_event:
            if (old_event.snapshot.get("after", {}).get("value") != {"value": value}
                    or old_event.snapshot.get("expected_version") != expected_version):
                raise VersionConflict("memory request_id reused with different value")
            return row, False
        await self.session.refresh(row)
        if expected_version is not None and row.version != expected_version:
            raise VersionConflict("memory version conflict")
        before = {"value": row.value, "version": row.version, "active": row.active, "source": row.source}
        vector = await self._vector(evidence, "passage")
        result = await self.session.execute(update(MemoryItem).where(MemoryItem.id == row.id, MemoryItem.version == row.version)
            .values(value={"value": value}, source=source, confidence=confidence, evidence=evidence,
                active=active, version=row.version+1, expires_at=None, source_conversation_id=conversation_id, source_message_id=message_id,
                embedding=vector, embedding_model=self.embedder.model_name if vector else None))
        if result.rowcount != 1:
            raise VersionConflict("memory version conflict")
        await self.session.refresh(row)
        self.session.add(MemoryAudit(user_id=user_id, memory_id=row.id, event_key=event_key,
            snapshot={"key": key, "expected_version": expected_version, "before": before, "after": {"value": row.value, "version": row.version,
                "active": row.active, "source": row.source}, "evidence": evidence, "message_id": message_id}))
        await self.session.flush()
        return row, True

    async def save_explicit(self, user_id, message, request_id, conversation_id, message_id):
        changed = []
        for item in explicit_preferences(message):
            # Request keys are conversation-local; two conversations may reuse one.
            existing = await self._ensure(user_id, item["key"])
            if existing and existing.source_message_id and message_id:
                from ..db.models import Message
                prior = await self.session.get(Message, existing.source_message_id)
                current = await self.session.get(Message, message_id)
                if prior and current and prior.created_at > current.created_at:
                    continue  # A slower old run must not overwrite a newer explicit correction.
            row, updated = await self.put(user_id, **item, event_key=f"explicit:{conversation_id}:{request_id}:{item['key']}",
                conversation_id=conversation_id, message_id=message_id)
            if updated:
                changed.append(self.view(row).model_dump(mode="json"))
        return changed

    async def revoke(self, user_id, key, request_id, expected_version):
        row = await self.session.scalar(select(MemoryItem).where(MemoryItem.user_id == user_id,
            MemoryItem.memory_type == "preference", MemoryItem.key == key))
        if row is None:
            raise LookupError("preference not found")
        row, _ = await self.put(user_id, key, _value(row), source="user_explicit", confidence=1,
            evidence="用户明确撤销偏好", event_key=f"revoke:{request_id}:{key}", expected_version=expected_version, active=False)
        return row

    async def apply_confirmed(self, user_id, body, request_id):
        key, value = body["key"], body["value"]
        expected = body.get("expected_version")
        if expected is None:
            # Legacy approvals must not overwrite a preference written since deployment.
            expected = 0
        return await self.put(user_id, key, value, source="user_confirmed", confidence=1,
            evidence=body["evidence"], event_key=f"approval:{request_id}", expected_version=expected,
            conversation_id=body.get("source_conversation_id"), message_id=body.get("source_message_id"))

    async def propose_inferred(self, user_id, candidate, run_id):
        key, value = candidate["key"], candidate["value"]
        valid_value(key, value)
        row = await self._ensure(user_id, key)
        await self.session.refresh(row)
        if row.version != candidate.get("expected_version", 0) or (row.active and _value(row) == value):
            return None
        from ..db.models import ChangeProposal, ApprovalRequest
        pending = (await self.session.scalars(select(ChangeProposal).where(ChangeProposal.user_id == user_id,
            ChangeProposal.run_id == run_id, ChangeProposal.proposal_type.in_(["preference.change", "change_set"]),
            ChangeProposal.status == "pending"))).all()
        for proposal in pending:
            changes = ([{"type": "preference.change", "payload": proposal.payload}]
                       if proposal.proposal_type == "preference.change" else proposal.payload.get("changes", []))
            if any(c.get("type") == "preference.change" and c.get("payload", {}).get("key") == key for c in changes):
                approval = await self.session.scalar(select(ApprovalRequest).where(
                    ApprovalRequest.proposal_id == proposal.id, ApprovalRequest.status == "pending"))
                if approval:
                    return approval  # The Profile Agent already proposed this key in this run.
        from .applications import ApplicationCommandService
        digest = hashlib.sha256(json.dumps([run_id, key, value], sort_keys=True).encode()).hexdigest()
        return await ApplicationCommandService(self.session).propose(user_id, "preference.change", candidate,
            "memory:"+digest, run_id, "这是一项推断偏好，请确认后保存。")

    async def enqueue(self, payload):
        parsed = ConsolidationInput.model_validate(payload)
        if parsed.completion.get("status") != "PASS":
            return
        await self.session.execute(_insert(self.session, MemoryOutbox).values(id=uuid4().hex,
            run_id=parsed.run_id, user_id=parsed.user_id, job_type="consolidate", payload=parsed.model_dump(mode="json"),
            status="queued", attempts=0, available_at=datetime.now(timezone.utc), result={})
            .on_conflict_do_nothing(index_elements=["run_id", "job_type"]))
