from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from typing import Any
from urllib.error import HTTPError, URLError

from pydantic import ValidationError

from .models import (
    CandidateFact,
    ChatMessage,
    ExtractionDiagnostics,
    ExtractionResult,
    StudentProfile,
    UserState,
)
from .llm_client import LLMClient


ALLOWED_SEMANTIC_FIELDS = {
    "academic_year", "major", "target_countries", "target_regions", "target_schools", "target_programs",
    "target_degree", "target_fields", "graduation_year", "gpa", "class_rank",
    "research_activity", "language_preparation", "skills", "career_goal",
    "target_locations", "current_stage", "toefl_score", "ielts_score",
    "gre_score", "budget", "explicit_date", "mentioned_year",
    "exam_plan", "completed_courses", "research_experiences", "project_experiences",
    "paper_experiences", "competition_experiences", "internship_experiences",
    "planned_enrollment_year", "planned_enrollment_month", "graduation_month",
}
_UNCERTAIN_MARKERS = ("可能", "也许", "大概", "考虑", "不确定", "maybe", "probably", "perhaps")
Completion = Callable[[dict[str, Any]], str]


class SemanticExtractor:
    """Context-aware semantic extraction backed by an OpenAI-compatible API.

    The asynchronous API is the Task-005 contract. ``extract_sync`` exists only
    to keep the current synchronous CLI and HTTP server compatible.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 25,
        completion_fn: Completion | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.client = llm_client or LLMClient(
            api_key=api_key, model=model, base_url=base_url,
            timeout_seconds=timeout_seconds, retries=0, completion_fn=completion_fn,
        )
        self.api_key = self.client.api_key
        self.model = self.client.model
        self.base_url = self.client.base_url
        self.timeout_seconds = timeout_seconds
        self.completion_fn = completion_fn
        self.last_error: str | None = None
        self.last_latency_ms = 0
        self.last_malformed_outputs: list[str] = []

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    async def extract(
        self,
        message: str,
        profile: StudentProfile,
        recent_messages: Sequence[ChatMessage | dict[str, str]],
        state: UserState | None = None,
    ) -> ExtractionResult:
        return await asyncio.to_thread(self.extract_sync, message, profile, recent_messages, state)

    def extract_sync(
        self,
        message: str,
        profile: StudentProfile,
        recent_messages: Sequence[ChatMessage | dict[str, str]],
        state: UserState | None = None,
        progress_targets: list[dict] | None = None,
    ) -> ExtractionResult:
        started = time.perf_counter()
        self.last_error = None
        self.last_malformed_outputs = []
        if not self.enabled:
            self.last_error = "MODELSCOPE_API_KEY is not configured"
            return self._fallback(started, attempts=0)

        payload = self._payload(message, profile, recent_messages, state)
        if progress_targets:
            payload["messages"][-1]["content"] += "\nValid progress targets: " + json.dumps(progress_targets, ensure_ascii=False, default=str)
        for attempt in (1, 2):
            try:
                content = self._complete(payload)
                result = self._parse_and_validate(content, message)
                self.last_latency_ms = int((time.perf_counter() - started) * 1000)
                result.diagnostics = ExtractionDiagnostics(
                    latency_ms=self.last_latency_ms,
                    attempts=attempt,
                    malformed_outputs=list(self.last_malformed_outputs),
                )
                return result
            except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.last_malformed_outputs.append(_safe_excerpt(locals().get("content", "")))
                if attempt == 1:
                    payload = self._repair_payload(payload, self.last_error)
                    continue
            except (HTTPError, URLError, TimeoutError, OSError, KeyError, IndexError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                return self._fallback(started, attempts=attempt)
        return self._fallback(started, attempts=2)

    def _payload(
        self,
        message: str,
        profile: StudentProfile,
        recent_messages: Sequence[ChatMessage | dict[str, str]],
        state: UserState | None,
    ) -> dict[str, Any]:
        schema = ExtractionResult.model_json_schema()
        context = {
            "current_message": message,
            "profile": profile.model_dump(mode="json", exclude={"facts", "change_history"}),
            "state": state.model_dump(mode="json") if state else None,
            "recent_messages": [
                item.model_dump(mode="json") if isinstance(item, ChatMessage) else item
                for item in recent_messages[-6:]
            ],
        }
        instructions = (
            "Extract only information explicitly supported by current_message. Use profile, state, and "
            "recent_messages only to resolve references, never to invent facts. Preserve unknown majors, "
            "fields, and goals verbatim in raw_value. For corrections or negation use operation remove or "
            "set as appropriate. Uncertain statements must have confidence below 0.70 and "
            "needs_confirmation=true. Evidence must be an exact non-empty substring of current_message. "
            "Return one JSON object matching the supplied schema and no prose. Set should_replan only when "
            "the message contains a meaningful profile or goal change."
            " Classify intent as ask_advice/profile_update/progress_update/schedule_change/mixed/no_change."
            " Questions and hypothetical values are NOT facts. Extract progress_updates for explicit actions only."
            " Use a supplied target_id only when uniquely identified; otherwise leave it null with target_hint."
            " Preserve exact evidence for progress. An exam score does not mean program requirements are met,"
            " nor does it cancel any upcoming exam. Stage signals alone never prove task completion."
            " Distinguish the user's completed projects, internships, research and papers from"
            " target_programs, which is only for degrees or programmes they explicitly want to apply to."
        )
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 1200,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps({"context": context, "output_schema": schema}, ensure_ascii=False)},
            ],
        }
        if "qwen" in self.model.casefold():
            payload["enable_thinking"] = False
        return payload

    def _repair_payload(self, payload: dict[str, Any], error: str) -> dict[str, Any]:
        repaired = {**payload, "messages": list(payload["messages"])}
        repaired["messages"].append({
            "role": "user",
            "content": f"Your previous output was invalid ({_safe_excerpt(error)}). Return corrected JSON only.",
        })
        return repaired

    def _complete(self, payload: dict[str, Any]) -> str:
        return self.client.complete_payload(payload)

    def _parse_and_validate(self, content: str, message: str) -> ExtractionResult:
        parsed = json.loads(_strip_json_fence(content))
        result = ExtractionResult.model_validate(parsed)
        for fact in result.facts:
            if fact.field not in ALLOWED_SEMANTIC_FIELDS:
                raise ValueError(f"unsupported fact field: {fact.field}")
            if not fact.evidence or fact.evidence not in message:
                raise ValueError(f"fact evidence is not present in current_message: {fact.field}")
            fact.source = "conversation"
            if any(marker in fact.evidence.casefold() for marker in _UNCERTAIN_MARKERS):
                fact.confidence = min(fact.confidence, 0.69)
                fact.needs_confirmation = True
        for signal in result.stage_signals:
            if not signal.evidence or signal.evidence not in message:
                raise ValueError(f"stage evidence is not present in current_message: {signal.stage}")
            if any(marker in signal.evidence.casefold() for marker in _UNCERTAIN_MARKERS):
                signal.strength = min(signal.strength, 0.69)
        for update in result.progress_updates:
            if not update.evidence or update.evidence not in message:
                raise ValueError("progress evidence is not present in current_message")
            if any(marker in update.evidence.casefold() for marker in _UNCERTAIN_MARKERS):
                update.confidence, update.needs_confirmation = min(update.confidence, 0.69), True
        return result

    def _fallback(self, started: float, attempts: int) -> ExtractionResult:
        self.last_latency_ms = int((time.perf_counter() - started) * 1000)
        return ExtractionResult(diagnostics=ExtractionDiagnostics(
            latency_ms=self.last_latency_ms,
            attempts=attempts,
            fallback_reason=self.last_error,
            malformed_outputs=list(self.last_malformed_outputs),
        ))


def _strip_json_fence(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline == -1:
            raise ValueError("incomplete JSON fence")
        cleaned = cleaned[first_newline + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def _safe_excerpt(value: Any, limit: int = 240) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")[:limit]
