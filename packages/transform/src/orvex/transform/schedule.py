"""Freshness-driven planning (P1-19): which models are due, and when to look again.

Pure functions with the clock passed in: no I/O, so every decision can be tested at any instant.
A project runs one model set at a time, and two runs start at least `MINIMUM_INTERVAL` apart,
so no model — upstream ones included — reads its sources more often than that.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orvex.contracts.model_config.v1 import ModelError
from orvex.transform.cron import CronError, next_time, parse
from orvex.transform.models import Model, problem

MINIMUM_INTERVAL = dt.timedelta(minutes=5)
RETRY_DELAYS = (dt.timedelta(minutes=5), dt.timedelta(minutes=10), dt.timedelta(minutes=20))
LOOK_AGAIN = dt.timedelta(minutes=1)
_DURATION = re.compile(r"([0-9]+)([smhd])")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def duration(text: str) -> dt.timedelta:
    match = _DURATION.fullmatch(text)
    assert match is not None  # the contract's PositiveDuration pattern
    return dt.timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return None


def config_problems(model: Model, project_zone: str) -> list[ModelError]:
    """Scheduling rules of the sidecar: minimum freshness, valid cron and time zone."""
    file = model.config_file or model.sql_file
    found: list[ModelError] = []

    def invalid(code: str, pointer: str, message: str) -> None:
        found.append(
            problem(code, f"{file}: {pointer}: {message}", file, model=model.name, path=pointer)
        )

    freshness = model.config.freshness
    if freshness is not None and duration(freshness.root) < MINIMUM_INTERVAL:
        invalid(
            "FRESHNESS_TOO_SHORT",
            "/freshness",
            f"{freshness.root} is below the minimum of 5m (sources must not be polled harder).",
        )
    refresh = model.config.full_refresh
    if refresh is not None:
        try:
            parse(refresh.cron)
        except CronError as error:
            invalid("MODEL_CONFIG_INVALID", "/full_refresh/cron", str(error))
        if _zone(refresh.time_zone or project_zone) is None:
            invalid(
                "MODEL_CONFIG_INVALID",
                "/full_refresh/time_zone",
                f"{refresh.time_zone} is not an IANA time zone.",
            )
    return found


@dataclass(frozen=True, slots=True)
class History:
    """What the run records say about one model (aware instants; None: never)."""

    last_success_start: dt.datetime | None = None
    last_success_end: dt.datetime | None = None
    last_full_refresh: dt.datetime | None = None  # start of the last successful full refresh


@dataclass(frozen=True, slots=True)
class Plan:
    """The models to run now (run order, upstream included), those to fully refresh, why, and
    when to look again."""

    models: tuple[str, ...]
    full_refresh: frozenset[str]
    reasons: tuple[str, ...]
    wake_at: dt.datetime


def freshness_due(model: Model, history: History, now: dt.datetime) -> dt.datetime | None:
    """When the model must start so its data is never staler than declared (None: no
    freshness). A model that never succeeded is due now."""
    if model.config.freshness is None:
        return None
    if history.last_success_end is None or history.last_success_start is None:
        return now
    took = history.last_success_end - history.last_success_start
    return history.last_success_end + duration(model.config.freshness.root) - took


def refresh_due(
    model: Model, history: History, project_zone: str, since: dt.datetime
) -> dt.datetime | None:
    """The first full-refresh time after the last one (or after `since` when there was none)."""
    refresh = model.config.full_refresh
    if refresh is None:
        return None
    zone = _zone(refresh.time_zone or project_zone)
    assert zone is not None  # checked at validation
    return next_time(parse(refresh.cron), history.last_full_refresh or since, zone)


def closure(name: str, dependencies: Mapping[str, Sequence[str]], known: set[str]) -> set[str]:
    """A model and every model it reads, transitively."""
    found = {name}
    pending = [name]
    while pending:
        for upstream in dependencies.get(pending.pop(), ()):
            if upstream not in found and upstream in known:
                found.add(upstream)
                pending.append(upstream)
    return found


def plan(
    models: Sequence[Model],
    order: Sequence[str],
    dependencies: Mapping[str, Sequence[str]],
    histories: Mapping[str, History],
    now: dt.datetime,
    project_zone: str,
    started: dt.datetime,
    last_run_start: dt.datetime | None,
    held: Mapping[str, dt.datetime] | None = None,
) -> Plan:
    """What to run at `now`.

    `started` is when the scheduler started (the reference for a first full refresh);
    `last_run_start` the start of the project's last run, of any outcome; `held` the models
    that may not run before a time (a failed model waiting for its retry, or given up): a due
    model that is held, or that reads a held model, waits; other due models run without it.
    """
    held = held or {}
    known = {model.name for model in models}
    due: dict[str, str] = {}
    refreshed: set[str] = set()
    upcoming: list[dt.datetime] = [moment for moment in held.values() if moment > now]
    for model in models:
        history = histories.get(model.name, History())
        fresh = freshness_due(model, history, now)
        if fresh is not None:
            if fresh <= now:
                due[model.name] = f"freshness {model.config.freshness.root}"  # type: ignore[union-attr]
            else:
                upcoming.append(fresh)
        refresh = refresh_due(model, history, project_zone, started)
        if refresh is not None:
            if refresh <= now:
                due[model.name] = f"full refresh {model.config.full_refresh.cron}"  # type: ignore[union-attr]
                refreshed.add(model.name)
            else:
                upcoming.append(refresh)
    blocked = {name for name, until in held.items() if until > now}
    runnable = {
        name: reason
        for name, reason in due.items()
        if not closure(name, dependencies, known) & blocked
    }
    earliest = now
    if last_run_start is not None:
        earliest = max(earliest, last_run_start + MINIMUM_INTERVAL)
    if not runnable or earliest > now:
        # Nothing to run yet: look again at the next due time, or within a minute.
        candidates = [earliest] if runnable else [moment for moment in upcoming if moment > now]
        return Plan((), frozenset(), (), min([now + LOOK_AGAIN, *candidates]))
    needed: set[str] = set()
    for name in runnable:
        needed |= closure(name, dependencies, known)
    selected = tuple(name for name in order if name in needed)
    reasons = tuple(f"{name}: {runnable[name]}" for name in selected if name in runnable)
    return Plan(selected, frozenset(refreshed & set(runnable)), reasons, now + LOOK_AGAIN)


def next_due(model: Model, now: dt.datetime, project_zone: str) -> dt.datetime:
    """When a model given up at `now` may be tried again: as if it had just succeeded — its
    next freshness deadline or its next full-refresh time, whichever comes first (at least
    the minimum interval)."""
    moments: list[dt.datetime] = []
    if model.config.freshness is not None:
        moments.append(now + duration(model.config.freshness.root))
    refresh = model.config.full_refresh
    if refresh is not None:
        zone = _zone(refresh.time_zone or project_zone)
        assert zone is not None  # checked at validation
        moments.append(next_time(parse(refresh.cron), now, zone))
    return max(now + MINIMUM_INTERVAL, min(moments, default=now + dt.timedelta(days=1)))
