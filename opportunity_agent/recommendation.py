from __future__ import annotations

from datetime import date

from .models import ExternalEvent, NotificationDecision, StudentProfile, UserState


def decide_deadline_notification(event: ExternalEvent, profile: StudentProfile, state: UserState, today: date) -> NotificationDecision:
    """Separate notification importance from relevance, using deterministic V1 policy."""
    relevance = 1.0 if profile.target_degree and profile.target_countries else 0.25
    urgency = 1.0 if (event.new_deadline - today).days <= 90 else 0.60
    stage_relevance = 1.0 if state.application in {"exploring", "preparing", "applying"} else 0.30
    novelty = 1.0 if event.old_deadline != event.new_deadline else 0.0
    score = round(0.40 * relevance + 0.20 * urgency + 0.15 * novelty + 0.15 * relevance + 0.10 * stage_relevance, 2)
    action = "immediate" if score >= 0.80 else "digest" if score >= 0.60 else "store" if score >= 0.40 else "ignore"
    return NotificationDecision(score=score, action=action, reason=f"{event.program_name} 的官方截止日期发生变化，且与用户申请目标相关。")
