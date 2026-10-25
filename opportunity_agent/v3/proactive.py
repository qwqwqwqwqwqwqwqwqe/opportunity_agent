"""Reserved integration point. No inference or material writes in this release."""
from datetime import datetime
from typing import Literal, Protocol
from pydantic import Field
from .contracts import Contract


class TaskCompletionEvent(Contract):
    event_id: str
    user_id: str
    task_id: str
    task_version: int
    application_id: str | None = None
    requirement_id: str | None = None
    title: str
    completed_at: datetime


class ProactiveMaterialSuggestion(Contract):
    source_event_id: str
    source_task_id: str
    source_task_version: int
    application_id: str
    requirement_id: str
    material_id: str
    expected_material_version: int
    suggested_status: Literal["ready", "submitted"] = "ready"
    reason: str


class MaterialSuggestionProvider(Protocol):
    async def on_task_completed(self, event: TaskCompletionEvent) -> list[ProactiveMaterialSuggestion]: ...


class DisabledMaterialSuggestions:
    async def on_task_completed(self, event: TaskCompletionEvent) -> list[ProactiveMaterialSuggestion]:
        return []
