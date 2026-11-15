"""The transform contract's scheduling values: a model's run history, the plan of what is due,
and the intervals a scheduler keeps (P1-19), as they cross the contract (#486 G5)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

# Two runs of a project start at least this far apart; a failed model is retried after each
# delay in turn; with nothing due, the scheduler looks again after `LOOK_AGAIN`.
MINIMUM_INTERVAL = dt.timedelta(minutes=5)
RETRY_DELAYS = (dt.timedelta(minutes=5), dt.timedelta(minutes=10), dt.timedelta(minutes=20))
LOOK_AGAIN = dt.timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class History:
    """What the run records say about one model (aware instants; None: never)."""

    last_success_start: dt.datetime | None = None
    last_success_end: dt.datetime | None = None
    last_full_refresh: dt.datetime | None = None  # start of the last successful full refresh


@dataclass(frozen=True, slots=True)
class Plan:
    """The models to run now (run order, upstream included), those to fully refresh (sorted),
    why, and when to look again."""

    models: tuple[str, ...]
    full_refresh: tuple[str, ...]
    reasons: tuple[str, ...]
    wake_at: dt.datetime
    next_run: dt.datetime | None = None  # when the next model is due, while none runs (#257)
