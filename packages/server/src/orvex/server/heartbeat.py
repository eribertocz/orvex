"""The scheduler's heartbeat: what the `schedule` command is doing, for `runs` (P1-21, #69).

One small JSON file in the project's `.<slug>/` folder, replaced atomically (written to a
temporary file, then renamed), so a reader never sees half of it. It holds the process id, the
start time, the last decision, when the scheduler looks again, and whether it stopped (and why).
It never holds a secret: decisions name models and times only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

HEARTBEAT_FILE = "scheduler.json"
NOT_RESPONDING = dt.timedelta(minutes=3)  # a running heartbeat older than this: presumed dead


@dataclass(frozen=True, slots=True)
class Heartbeat:
    pid: int
    started_at: str  # ISO 8601 instants, with their offset
    updated_at: str
    state: str  # "running" or "stopped"
    last_decision_at: str | None = None
    last_decision: str | None = None
    next_at: str | None = None
    stopped_at: str | None = None
    stop_reason: str | None = None


def write(home: Path, heartbeat: Heartbeat) -> None:
    """Replace the heartbeat atomically."""
    home.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".scheduler-", suffix=".json", dir=home)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(asdict(heartbeat), handle)
        os.replace(temporary, home / HEARTBEAT_FILE)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read(home: Path) -> Heartbeat | None:
    """The heartbeat, or None when there is none or it cannot be read."""
    try:
        data = json.loads((home / HEARTBEAT_FILE).read_text(encoding="utf-8"))
        return Heartbeat(**data)
    except OSError, ValueError, TypeError:
        return None


def describe(home: Path, zone: str, now: dt.datetime) -> str:
    """One line for the `runs` command, times in the project's zone."""
    beat = read(home)
    if beat is None:
        return "scheduler: has not run"

    def local(text: str | None) -> str:
        if text is None:
            return "-"
        moment = dt.datetime.fromisoformat(text).astimezone(ZoneInfo(zone))
        return moment.strftime("%Y-%m-%d %H:%M:%S")

    if beat.state == "stopped":
        return f"scheduler: stopped at {local(beat.stopped_at)} ({beat.stop_reason or 'stopped'})"
    updated = dt.datetime.fromisoformat(beat.updated_at)
    if now - updated > NOT_RESPONDING:
        return (
            f"scheduler: not responding (process {beat.pid}, last heard at "
            f"{local(beat.updated_at)})"
        )
    decision = (
        f"last decision at {local(beat.last_decision_at)} ({beat.last_decision})"
        if beat.last_decision is not None
        else "no decision yet"
    )
    following = f", next at {local(beat.next_at)}" if beat.next_at is not None else ""
    return f"scheduler: running since {local(beat.started_at)}, {decision}{following}"
