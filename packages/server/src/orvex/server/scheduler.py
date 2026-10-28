"""`schedule`: keep a project's models as fresh as they declare (P1-19).

The loop asks `transform.schedule.plan` what is due and runs it through the normal `run` — one
run at a time, under the project lock. It checks the project again only when its files change
(checking binds the models against the sources), and it keeps the start of its last run in
memory, on a monotonic clock, as well as in the run records: two runs never start less than five
minutes apart, whatever the records or the system clock say.

A model whose own step failed is held: retried after 5, 10 and 20 minutes, then given up until
its next due time; due models that do not read it keep running. Every decision is printed.

Lake maintenance (P1-20) runs when no model is due: once a day after a success, an hour after a
failure. Model runs keep priority: maintenance only starts between runs.
"""

from __future__ import annotations

import datetime as dt
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import IO
from zoneinfo import ZoneInfo

from orvex.server import commands, maintenance
from orvex.server.commands import LAKE_FOLDER, Checked, Selection, project_lock
from orvex.server.identity import Identity
from orvex.store.errors import StoreError
from orvex.store.lake import open_lake
from orvex.transform.run_records import last_run_request, model_history_request
from orvex.transform.schedule import (
    LOOK_AGAIN,
    MINIMUM_INTERVAL,
    RETRY_DELAYS,
    History,
    Plan,
    closure,
    next_due,
    plan,
)

type Clock = Callable[[], dt.datetime]
type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]

PROJECT_FILES = ("project.yaml", "connections.yaml")


def _clock() -> dt.datetime:
    """The runner's clock (the same one run records use)."""
    return dt.datetime.fromisoformat(commands._now())


def _instant(text: object) -> dt.datetime | None:
    return None if text is None else dt.datetime.fromisoformat(str(text))


@dataclass(frozen=True, slots=True)
class _Records:
    histories: dict[str, History]
    last_run_start: dt.datetime | None
    failed: tuple[str, ...]  # models whose own step failed in the last run
    maintained: maintenance.Last = field(default_factory=maintenance.Last)


def _records(home: Path) -> _Records:
    """What the run records say (nothing yet when there is no lake or no record)."""
    if not (home / LAKE_FOLDER).exists():
        return _Records({}, None, ())
    with project_lock(home):
        lake = open_lake(commands._location(home), commands._limits())
        try:
            maintained = maintenance.last_maintenance(lake)
            snapshot = lake.current_snapshot().id
            try:
                rows = lake.read(model_history_request(snapshot)).read_all().to_pylist()
                (last,) = lake.read(last_run_request(snapshot)).read_all().to_pylist()
            except StoreError as error:
                if error.code != "STORE_STATEMENT_INVALID":
                    raise
                return _Records({}, None, (), maintained)  # no run records yet
        finally:
            lake.close()
    histories = {
        str(row["model"]): History(
            _instant(row["success_start"]),
            _instant(row["success_end"]),
            _instant(row["full_refresh"]),
        )
        for row in rows
    }
    failed = tuple(str(last["failed"]).split(",")) if last["failed"] else ()
    return _Records(histories, _instant(last["started"]), failed, maintained)


def _fingerprint(folder: Path) -> tuple[tuple[str, int, int], ...]:
    """Name, size and modification time of every project file a check reads."""
    paths = [folder / name for name in PROJECT_FILES]
    models = folder / "models"
    if models.is_dir():
        paths += sorted(path for path in models.rglob("*") if path.is_file())
    found = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append((str(path.relative_to(folder)), stat.st_size, stat.st_mtime_ns))
    return tuple(found)


@dataclass(slots=True)
class _State:
    started: dt.datetime
    checked: Checked | None = None
    fingerprint: tuple[tuple[str, int, int], ...] = ()
    last_run_wall: dt.datetime | None = None
    last_run_monotonic: float | None = None
    failures: dict[str, int] = field(default_factory=dict)
    held: dict[str, dt.datetime] = field(default_factory=dict)
    reported: tuple[str, ...] = ()
    checked_at: float = 0.0
    recheck_at: float | None = None
    deferred: set[str] = field(default_factory=set)  # retries already deferred once
    maintenance_after: float | None = None  # monotonic: maintenance did not start, wait


def schedule(
    folder: Path,
    identity: Identity,
    out: IO[str],
    clock: Clock = _clock,
    sleep: Sleep = time.sleep,
    should_stop: Callable[[], bool] | None = None,
    monotonic: Monotonic = time.monotonic,
) -> int:
    """Run the due models of a project until stopped (Ctrl+C stops after the current run)."""
    stopping = False

    def stop(_signal: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    def stopped() -> bool:
        return stopping or (should_stop is not None and should_stop())

    def pause(seconds: float) -> None:
        """Sleep in short steps, so Ctrl+C is noticed within a second."""
        remaining = seconds
        while remaining > 0 and not stopped():
            step = min(1.0, remaining)
            sleep(step)
            remaining -= step

    previous = signal.signal(signal.SIGINT, stop) if should_stop is None else None
    try:
        state = _State(started=clock())
        while not stopped():
            pause(_cycle(folder, identity, out, clock, monotonic, state))
        return 0
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def _say(out: IO[str], zone: str, moment: dt.datetime, text: str) -> None:
    local = moment.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{local} {text}", file=out, flush=True)


def _report(out: IO[str], state: _State, lines: tuple[str, ...]) -> float:
    """Print problems once until they change; look again in a minute."""
    if lines != state.reported:
        for line in lines:
            print(line, file=out, flush=True)
        state.reported = lines
    return LOOK_AGAIN.total_seconds()


def _cycle(
    folder: Path,
    identity: Identity,
    out: IO[str],
    clock: Clock,
    monotonic: Monotonic,
    state: _State,
) -> float:
    """One decision; returns how long to wait before the next one (seconds)."""
    now = clock()
    fingerprint = _fingerprint(folder)
    stale = (
        state.checked is not None
        and bool(state.checked.problems)
        and monotonic() - state.checked_at >= MINIMUM_INTERVAL.total_seconds()
    )
    due_again = state.recheck_at is not None and monotonic() >= state.recheck_at
    if state.checked is None or fingerprint != state.fingerprint or stale or due_again:
        # Checked again when the files change, or every 5 minutes while the check fails (a
        # source may be back), never more often: checking binds models against the sources.
        state.checked, state.fingerprint = commands.check_project(folder), fingerprint
        state.checked_at, state.recheck_at = monotonic(), None
    checked = state.checked
    if checked.problems or checked.project is None or checked.graph is None:
        return _report(out, state, tuple(str(problem) for problem in checked.problems))
    project, graph = checked.project, checked.graph
    zone = project.settings.time_zone
    home = project.folder / f".{identity.slug}"
    try:
        records = _records(home)
    except commands.ProjectLocked:
        return _report(out, state, ("waiting: another process holds the project",))
    except StoreError as error:
        return _report(out, state, (str(commands._store_problem(error, ".", "run records")),))
    state.reported = ()
    last_runs = [moment for moment in (records.last_run_start, state.last_run_wall) if moment]

    def decide(held: dict[str, dt.datetime]) -> Plan:
        return plan(
            checked.discovery.models,
            graph.order,
            graph.dependencies,
            records.histories,
            now,
            zone,
            state.started,
            max(last_runs, default=None),
            held,
        )

    decided = decide(state.held)
    due = {reason.split(":")[0] for reason in decided.reasons}
    recovering = due & set(state.failures)
    healthy = due - recovering
    if recovering and healthy:
        # A model being retried never runs with healthy ones (its failure would roll them back
        # too). Turns alternate: the healthy ones first, then the retries, so neither waits
        # more than one run.
        if recovering <= state.deferred:
            # The models a retry reads run with it: only the other healthy ones wait.
            known = {model.name for model in checked.discovery.models}
            read = set().union(*(closure(n, graph.dependencies, known) for n in recovering))
            waiting = healthy - read
            decided = decide({**state.held, **dict.fromkeys(waiting, now + LOOK_AGAIN)})
            state.deferred -= recovering
        else:
            decided = decide({**state.held, **dict.fromkeys(recovering, now + LOOK_AGAIN)})
            state.deferred |= recovering
    if state.last_run_monotonic is not None:
        # The wall clock may jump; the monotonic clock does not.
        waited = monotonic() - state.last_run_monotonic
        if waited < MINIMUM_INTERVAL.total_seconds():
            return min(LOOK_AGAIN.total_seconds(), MINIMUM_INTERVAL.total_seconds() - waited)
    if not decided.models:
        return _maybe_maintain(
            folder, identity, out, clock, monotonic, state, records, decided, zone
        )
    try:
        with project_lock(home):
            pass  # free now; a process taking it in between makes the run fail as locked
    except commands.ProjectLocked:
        return _report(out, state, ("waiting: another process holds the project",))
    for name in decided.models:
        trigger = "full refresh" if name in decided.full_refresh else "freshness"
        _say(out, zone, now, f"running {name} ({trigger})")
    for reason in decided.reasons:
        _say(out, zone, now, f"due {reason}")
    state.last_run_wall, state.last_run_monotonic = now, monotonic()
    selection = Selection(decided.models, "freshness", decided.full_refresh)
    previous_start = records.last_run_start
    if commands.run(folder, identity, out, selection=selection) == 0:
        for name in decided.models:
            state.failures.pop(name, None)
            state.held.pop(name, None)
        return 1.0
    after = clock()
    try:
        latest: _Records | None = _records(home)
    except commands.ProjectLocked, StoreError:
        latest = None
    if latest is None or latest.last_run_start in {None, previous_start}:
        # No run was recorded: it did not start (a source down while checking, the project
        # taken by another process…). Not a model's failure: tried again after the minimum
        # interval, the reason printed above by the run.
        _say(out, zone, after, "the run did not start; trying again in 5 minutes")
        # Check again then (not before): the cause may be in the project or a source.
        state.recheck_at = monotonic() + MINIMUM_INTERVAL.total_seconds()
        return 1.0
    culprits = latest.failed or decided.models
    by_name = {model.name: model for model in checked.discovery.models}
    for name in culprits:
        if name not in by_name:
            continue  # removed from the project since
        count = state.failures.get(name, 0) + 1
        if count <= len(RETRY_DELAYS):
            state.failures[name] = count
            state.held[name] = after + RETRY_DELAYS[count - 1]
            retry = state.held[name].astimezone(ZoneInfo(zone))
            _say(out, zone, after, f"{name} failed; retry {count} at {retry:%H:%M}")
        else:
            state.failures.pop(name, None)
            state.held[name] = next_due(by_name[name], after, zone)
            print(f"SCHEDULE_GAVE_UP {name}", file=out, flush=True)
            until = state.held[name].astimezone(ZoneInfo(zone))
            _say(out, zone, after, f"{name} waits until {until:%Y-%m-%d %H:%M}")
    return 1.0


def _maybe_maintain(
    folder: Path,
    identity: Identity,
    out: IO[str],
    clock: Clock,
    monotonic: Monotonic,
    state: _State,
    records: _Records,
    decided: Plan,
    zone: str,
) -> float:
    """No model is due: run lake maintenance when it is due, else wait for whichever comes
    first."""
    now = clock()
    checked = state.checked
    lake_exists = (
        checked is not None
        and checked.project is not None
        and (checked.project.folder / f".{identity.slug}" / LAKE_FOLDER).exists()
    )
    if not lake_exists:  # nothing to maintain before the first run
        return max(1.0, (decided.wake_at - now).total_seconds())
    due = records.maintained.due_at()
    waiting = state.maintenance_after is not None and monotonic() < state.maintenance_after
    if (due is None or due <= now) and not waiting:
        _say(out, zone, now, "running lake maintenance")
        if maintenance.maintain(folder, identity, out, "schedule", clock) != 0:
            # Recorded failures wait an hour (the records say so); one that did not start (the
            # project locked by another process…) is tried again after the minimum interval.
            state.maintenance_after = monotonic() + MINIMUM_INTERVAL.total_seconds()
        else:
            state.maintenance_after = None
        return 1.0
    wake = decided.wake_at if due is None or waiting else min(decided.wake_at, due)
    if waiting and state.maintenance_after is not None:
        wake = min(wake, now + dt.timedelta(seconds=state.maintenance_after - monotonic()))
    return max(1.0, (wake - now).total_seconds())
