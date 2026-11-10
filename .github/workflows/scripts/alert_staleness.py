"""Missing runs (#428): whether a watched context has missed its cadence, a dead-man check. A
push workflow must have a completed run for main's newest commit, a scheduled workflow a recent
run, and a `workflow_run` workflow a run after each completed run of its triggers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from alert_config import Watch
from alert_runs import Run

PUSH_GRACE = timedelta(hours=3)
SCHEDULE_GRACE = timedelta(hours=6)
TRIGGER_GRACE = timedelta(hours=3)


def push_missed(runs: Sequence[Run], head: str, committed: datetime, now: datetime) -> str | None:
    """Why the context missed main's newest commit `head`, or None."""
    if now - committed <= PUSH_GRACE:
        return None
    if any(run.sha == head and run.status == "completed" for run in runs):
        return None
    return f"no completed run for main's newest commit {head[:12]} within 3 hours of it"


def schedule_missed(runs: Sequence[Run], watch: Watch, now: datetime) -> str | None:
    """Why the scheduled context is overdue, or None."""
    limit = timedelta(hours=watch.every_hours) + SCHEDULE_GRACE
    newest = max((run.created_at for run in runs), default=None)
    if newest is not None and now - newest <= limit:
        return None
    seen = "no run" if newest is None else f"its newest run started {newest.isoformat()}"
    return f"{seen}; it runs every {watch.every_hours} hours, with 6 hours of grace"


def trigger_missed(runs: Sequence[Run], triggers: Sequence[Run], now: datetime) -> str | None:
    """Why the `workflow_run` context missed a completed run of its triggers, or None. Only the
    newest trigger run older than the grace is checked: each earlier one was checked before."""
    due = [t for t in triggers if t.status == "completed" and now - t.created_at > TRIGGER_GRACE]
    if not due:
        return None
    trigger = max(due, key=lambda t: t.created_at)
    if any(run.sha == trigger.sha and run.created_at >= trigger.created_at for run in runs):
        return None
    return f"no run after trigger run {trigger.id} (commit {trigger.sha[:12]}) within 3 hours"
