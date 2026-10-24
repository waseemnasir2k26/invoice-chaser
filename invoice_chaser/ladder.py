"""Overdue ladder: nudge (day 1) -> firm (7) -> final (14) -> escalation (30)."""
from __future__ import annotations

from datetime import date

STAGES = ("nudge", "firm", "final", "escalation")
DEFAULT_DAYS = (1, 7, 14, 30)


def days_overdue(due: date, today: date) -> int:
    return max(0, (today - due).days)


def stage_for(overdue_days: int, stage_days=DEFAULT_DAYS) -> str | None:
    """Highest stage whose threshold has been reached; None if not yet overdue."""
    stage = None
    for name, threshold in zip(STAGES, stage_days):
        if overdue_days >= threshold:
            stage = name
    return stage
