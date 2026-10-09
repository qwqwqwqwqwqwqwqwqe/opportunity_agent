"""Offline evaluator: model calls are injected, so CI uses deterministic mocks."""
from __future__ import annotations

from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import EvaluationCase, EvaluationMetric, EvaluationRun
from .metrics import fact_f1


class EvaluationRunner:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def run_profile_cases(self, name: str, executor: Callable[[dict], Awaitable[dict]]) -> EvaluationRun:
        run = EvaluationRun(name=name, configuration={"suite": "profile"})
        self.session.add(run)
        await self.session.flush()
        cases = list((await self.session.scalars(select(EvaluationCase).where(EvaluationCase.category == "profile", EvaluationCase.active))).all())
        scores: list[float] = []
        for case in cases:
            actual = await executor(case.input)
            scores.append(fact_f1(actual.get("fact_fields", []), case.expected.get("fact_fields", [])))
        self.session.add(EvaluationMetric(run_id=run.id, name="profile_fact_f1", value=sum(scores) / max(1, len(scores)), details={"cases": len(cases)}))
        run.status = "completed"
        await self.session.flush()
        return run
