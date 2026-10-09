"""Compatibility boundary: V2 orchestration can still expose V1 openJiuwen tools."""
from __future__ import annotations

from typing import Any


class OpenJiuwenCompatibilityAdapter:
    """Lazy adapter so V2 has no hard runtime dependency on the optional binding."""

    def tool_catalog(self) -> list[dict[str, Any]]:
        return [
            {"name": "lifecycle_profile_update", "mode": "proposal_only", "description": "V1 LifecycleTools compatibility entry."},
            {"name": "official_requirement_lookup", "mode": "read_only", "description": "V1 official research compatibility entry."},
        ]

    def a2a_endpoint_description(self) -> dict[str, str]:
        return {"protocol": "A2A", "role": "legacy profile-agent adapter", "write_policy": "approval required"}
