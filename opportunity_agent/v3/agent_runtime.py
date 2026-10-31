"""Bounded model -> tool -> observation loop shared by read-only agents.

The runtime owns step/time budgets and the invalid-action policy. Tools own
permissions, argument checks and grounding validation, so a runtime never
gains a capability its tool registry does not expose.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any


class Suspend:
    """Returned by a tool that needs explicit user consent before running."""

    def __init__(self, payload: dict):
        self.payload = payload


@dataclass
class Outcome:
    status: str  # finished | suspended
    result: Any = None
    eligible: bool = False
    pending: dict | None = None
    steps: int = 0


async def run_loop(client, tools, *, system, context, action_model, steps, deadline, max_invalid=1,
                   step_timeout=25, max_tokens=3000, on_step=None, trace=None, on_model=None, on_invalid=None):
    """Each step asks the model for one action; finish must pass tools.validate.

    Optional observers only report progress: on_model(step) before a model
    call, on_step(step, tool, arguments, value) after a tool, and
    on_invalid(step, error) when an action or final answer is rejected.
    step_timeout may be a number or a callable(step, context) -> seconds.
    """
    invalid = 0
    for used in range(1, steps+1):
        if time.monotonic() >= deadline:
            raise TimeoutError("ANALYSIS_BUDGET_EXHAUSTED")
        if on_model is not None:
            await on_model(used)
        try:
            diagnostics = []
            started = time.monotonic()
            entry = {"kind":"model_call", "step":used, "status":"started",
                     "model":getattr(client,"model","test"), "diagnostics":diagnostics}
            if trace is not None:
                trace.append(entry)
            timeout = step_timeout(used, context) if callable(step_timeout) else step_timeout
            entry["timeout_seconds"] = timeout
            try:
                action = await asyncio.wait_for(asyncio.to_thread(client.generate_structured, action_model,
                    system=system, context=context, temperature=0, max_tokens=max_tokens, deadline=deadline,
                    diagnostics=diagnostics,allow_format_fallback=False),
                    min(timeout, max(.01,deadline-time.monotonic())))
                entry["status"] = "completed"
            except Exception as exc:
                from ..llm_client import safe_error_details
                entry.update(status="failed", error=safe_error_details(exc))
                raise
            finally:
                entry["latency_ms"] = int((time.monotonic()-started)*1000)
            if action.tool == "finish":
                if action.result is None:
                    raise ValueError("MISSING_RESULT")
                eligible = tools.validate(action.result)
                return Outcome("finished", action.result, eligible, steps=used)
            value = await tools.call(action.tool, action.arguments)
            if isinstance(value, Suspend):
                return Outcome("suspended", pending={**value.payload, "tool":action.tool,
                               "arguments":action.arguments}, steps=used)
            context["observations"].append({"tool":action.tool,"arguments":action.arguments,"result":value})
            if on_step is not None:
                await on_step(used, action.tool, action.arguments, value)
        except (ValueError, KeyError, TypeError) as exc:
            invalid += 1
            if trace is not None:
                # Keep the rejection reason for diagnostics, not only for the model.
                trace.append({"kind":"rejected", "step":used, "error":str(exc)[:150]})
            if on_invalid is not None:
                await on_invalid(used, str(exc)[:150])
            if invalid > max_invalid:
                raise
            context["observations"].append({"error":str(exc)[:150]})
    raise TimeoutError("MODEL_CALL_BUDGET_EXHAUSTED")
