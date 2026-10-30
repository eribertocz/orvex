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

Unattended (P1-21, #69): one scheduler per project (an OS lock held for its lifetime);
`SIGTERM` or Ctrl+C stops it at once between runs (no run or maintenance is decided after a
stop; a run already decided when the stop arrives may still start, and gets the grace period),
and a run in progress may finish within the stop grace period, then is cancelled and rolls back
(maintenance in progress finishes). Exception: Ctrl+C during a load from a source cancels the
load at once. A heartbeat in the project's folder says what it is doing, renewed every minute,
during runs too; problems the scheduler prints are its last decision.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import os
import select
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import IO
from zoneinfo import ZoneInfo

from orvex.server import commands, heartbeat, lake_place, maintenance
from orvex.server.commands import Checked, Selection, project_lock
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
SCHEDULER_LOCK = "scheduler.lock"  # held for the scheduler's lifetime: one per project (#69)
STOP_GRACE_SECONDS = 60.0  # how long a run may finish after a stop before it is cancelled
HEARTBEAT_EVERY = dt.timedelta(minutes=1)


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


def _records(home: Path, place: lake_place.Place) -> _Records:
    """What the run records say (nothing yet when there is no lake or no record)."""
    if not lake_place.exists(home):
        return _Records({}, None, ())
    with project_lock(home):
        lake = open_lake(place.location, commands._limits())
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
    decision: tuple[dt.datetime, str] | None = None  # the last one printed (for the heartbeat)
    zone: str | None = None  # the project's, once it has been loaded
    decided: Callable[[], None] = lambda: None  # called after each printed decision


def schedule(
    folder: Path,
    identity: Identity,
    out: IO[str],
    clock: Clock = _clock,
    sleep: Sleep = time.sleep,
    should_stop: Callable[[], bool] | None = None,
    monotonic: Monotonic = time.monotonic,
    stop_grace: float = STOP_GRACE_SECONDS,
) -> int:
    """Run the due models of a project until stopped (#69 for running unattended).

    `SIGTERM` or Ctrl+C (`SIGINT`) stops it: at once between runs; during a run, the run may
    finish for `stop_grace` seconds, then it is cancelled and rolls back. Exception: Ctrl+C while
    a run loads from a source cancels the load at once (`store.lake._on_ctrl_c`, as for `run`).
    One scheduler per project: a second one exits with `SCHEDULER_ALREADY_RUNNING`. While it
    runs, it keeps a heartbeat (`heartbeat.py`) that the `runs` command shows.
    """
    home = Path(folder).expanduser().absolute() / f".{identity.slug}"
    try:
        with commands.project_lock(home, SCHEDULER_LOCK):
            return _schedule(
                folder, home, identity, out, clock, sleep, should_stop, monotonic, stop_grace
            )
    except commands.ProjectLocked:
        beat = heartbeat.read(home)
        pid = str(beat.pid) if beat is not None else "unknown"
        print(
            commands.Problem(
                "SCHEDULER_ALREADY_RUNNING",
                f".{identity.slug}/{SCHEDULER_LOCK}",
                None,
                f"another scheduler (process {pid}) keeps this project; one per project.",
            ),
            file=out,
            flush=True,
        )
        return 1


def _schedule(
    folder: Path,
    home: Path,
    identity: Identity,
    out: IO[str],
    clock: Clock,
    sleep: Sleep,
    should_stop: Callable[[], bool] | None,
    monotonic: Monotonic,
    stop_grace: float,
) -> int:
    control = commands.RunControl()
    stop_reason: list[str] = []  # set once, by the first stop signal
    requested = threading.Event()  # a stop arrived: the grace period starts
    finished = threading.Event()  # the scheduler is done: the grace period is moot

    # Reentrant: the main thread's handler may run again while the first call holds it.
    once = threading.RLock()

    def stop(number: int, _frame: FrameType | None) -> None:
        # Called by the handler and by the watcher thread: the first call counts.
        with once:
            if stop_reason:
                return
            stop_reason.append(signal.Signals(number).name)
        # Only an event is set here: a handler may run while the main thread is inside
        # `Thread.start()`, so it must not start a thread itself.
        requested.set()

    def grace_then_cancel() -> None:
        """A run in progress may finish within the grace period; then it is cancelled. Between
        runs, the loop sees the stop within a second and nothing is cancelled."""
        while not requested.wait(0.2):
            if finished.is_set():
                return
        if not finished.wait(stop_grace):
            control.cancel()

    def stopped() -> bool:
        return bool(stop_reason) or (should_stop is not None and should_stop())

    state = _State(started=clock())
    beat = heartbeat.Heartbeat(
        pid=os.getpid(),
        started_at=state.started.isoformat(),
        updated_at=state.started.isoformat(),
        state="running",
    )
    writing = threading.Lock()  # the loop and the renewer during a run both write
    failed_write: list[str] = []  # the last write problem printed, printed once until it changes

    def store(changed: heartbeat.Heartbeat) -> None:
        nonlocal beat
        beat = changed
        try:
            heartbeat.write(home, beat)
        except OSError as error:
            # The heartbeat informs; it never stops the scheduler.
            text = f"heartbeat not written: {error.strerror or error}"
            if failed_write != [text]:
                print(text, file=out, flush=True)
                failed_write[:] = [text]
        else:
            failed_write.clear()

    def report(next_at: dt.datetime | None = None, *, deciding: bool = False) -> None:
        with writing:
            decision = state.decision
            store(
                dataclasses.replace(
                    beat,
                    updated_at=clock().isoformat(),
                    last_decision_at=decision[0].isoformat() if decision else None,
                    last_decision=decision[1] if decision else None,
                    # A new decision makes the previous "next at" stale.
                    next_at=(
                        next_at.isoformat()
                        if next_at is not None
                        else None
                        if deciding
                        else beat.next_at
                    ),
                )
            )

    state.decided = lambda: report(deciding=True)

    @contextlib.contextmanager
    def renewing() -> Iterator[None]:
        """Renew the heartbeat every minute while a cycle works (a run may take hours)."""
        done = threading.Event()

        def renew() -> None:
            while not done.wait(HEARTBEAT_EVERY.total_seconds()):
                report()

        renewer = threading.Thread(target=renew, name="schedule-heartbeat", daemon=True)
        renewer.start()
        try:
            yield
        finally:
            done.set()
            renewer.join()

    def pause(seconds: float) -> None:
        """Sleep in short steps, so a stop is noticed within a second; the heartbeat is renewed
        at least once a minute."""
        remaining = seconds
        renewed = monotonic()
        while remaining > 0 and not stopped():
            step = min(1.0, remaining)
            sleep(step)
            remaining -= step
            if monotonic() - renewed >= HEARTBEAT_EVERY.total_seconds():
                report()
                renewed = monotonic()

    grace = threading.Thread(target=grace_then_cancel, name="schedule-grace", daemon=True)
    restore = _watch_signals(stop) if should_stop is None else (lambda: None)
    try:
        grace.start()
        report()
        while not stopped():
            try:
                with renewing():
                    wait = _cycle(folder, identity, out, clock, monotonic, state, control, stopped)
            except KeyboardInterrupt:
                # Ctrl+C during an extraction load: the run recorded it and rolled back.
                if not stop_reason:
                    stop_reason.append(signal.SIGINT.name)
                break
            report(clock() + dt.timedelta(seconds=wait))
            pause(wait)
        reason = stop_reason[0] if stop_reason else "stopped"
        moment = clock()
        state.decided = lambda: None  # the final heartbeat below says it
        _say(state, out, "UTC" if state.zone is None else state.zone, moment, f"stopped ({reason})")
        with writing:
            store(
                dataclasses.replace(
                    beat,
                    updated_at=moment.isoformat(),
                    last_decision_at=moment.isoformat(),
                    last_decision=f"stopped ({reason})",
                    state="stopped",
                    stopped_at=moment.isoformat(),
                    stop_reason=reason,
                    next_at=None,
                )
            )
        return 0
    finally:
        finished.set()
        if grace.is_alive():
            grace.join()
        restore()


def _watch_signals(stop: Callable[[int, FrameType | None], None]) -> Callable[[], None]:
    """`SIGTERM` and `SIGINT` call `stop` at once, even while DuckDB holds the main thread.

    Python runs signal handlers only in the main thread, when it gets control back: a long
    statement would delay the stop, and with it the grace period. So the signals are also
    watched from another thread through Python's wakeup file descriptor, which the C-level
    handler writes to as a signal arrives (the same way as `store.lake._on_ctrl_c`, which
    forwards to this one while a load runs). Returns the restoring function.
    """
    numbers = (signal.SIGINT, signal.SIGTERM)
    previous = {number: signal.getsignal(number) for number in numbers}
    receiver, sender = socket.socketpair()
    sender.setblocking(False)
    finished = threading.Event()
    for number in numbers:
        signal.signal(number, stop)
    earlier = signal.set_wakeup_fd(sender.fileno(), warn_on_full_buffer=False)

    def watch() -> None:
        while not finished.is_set():
            ready, _, _ = select.select([receiver], [], [], 0.2)
            if not ready:
                continue
            with contextlib.suppress(OSError):
                received = receiver.recv(256)
                if earlier != -1:
                    os.write(earlier, received)
                for number in numbers:
                    if number in received:
                        stop(number, None)

    watcher = threading.Thread(target=watch, name="schedule-stop", daemon=True)
    watcher.start()

    def restore() -> None:
        finished.set()
        watcher.join()
        signal.set_wakeup_fd(earlier)
        for number, handler in previous.items():
            signal.signal(number, handler)
        receiver.close()
        sender.close()

    return restore


def _say(state: _State, out: IO[str], zone: str, moment: dt.datetime, text: str) -> None:
    local = moment.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{local} {text}", file=out, flush=True)
    state.decision = (moment, text)
    state.decided()


def _report(out: IO[str], state: _State, lines: tuple[str, ...], moment: dt.datetime) -> float:
    """Print problems once until they change; look again in a minute. The first line becomes
    the heartbeat's last decision, at the time it was first printed (#166), so `runs` shows why
    no model runs."""
    if lines != state.reported:
        for line in lines:
            print(line, file=out, flush=True)
        state.reported = lines
        if lines:
            state.decision = (moment, lines[0])
            state.decided()
    return LOOK_AGAIN.total_seconds()


def _cycle(
    folder: Path,
    identity: Identity,
    out: IO[str],
    clock: Clock,
    monotonic: Monotonic,
    state: _State,
    control: commands.RunControl | None = None,
    stopped: Callable[[], bool] = lambda: False,
) -> float:
    """One decision; returns how long to wait before the next one (seconds).

    A stop that arrives during the cycle (while checking the project or reading the records)
    is seen before a run or maintenance starts: none starts after it.
    """
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
        state.checked, state.fingerprint = commands.check_project(folder, identity), fingerprint
        state.checked_at, state.recheck_at = monotonic(), None
    checked = state.checked
    if checked.problems or checked.project is None or checked.graph is None:
        return _report(out, state, tuple(str(problem) for problem in checked.problems), now)
    project, graph = checked.project, checked.graph
    zone = project.settings.time_zone
    state.zone = zone
    home = project.folder / f".{identity.slug}"
    assert checked.place is not None  # a refused lake location is a problem
    place = checked.place
    try:
        records = _records(home, place)
    except commands.ProjectLocked:
        return _report(out, state, ("waiting: another process holds the project",), now)
    except StoreError as error:
        return _report(out, state, (str(commands._store_problem(error, ".", "run records")),), now)
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
        state.reported = ()  # nothing blocks the scheduler now: a problem that comes back is new
        return _maybe_maintain(
            folder, identity, out, clock, monotonic, state, records, decided, zone, stopped
        )
    if stopped():
        return 0.0
    try:
        with project_lock(home):
            pass  # free now; a process taking it in between makes the run fail as locked
    except commands.ProjectLocked:
        return _report(out, state, ("waiting: another process holds the project",), now)
    state.reported = ()
    for name in decided.models:
        trigger = "full refresh" if name in decided.full_refresh else "freshness"
        _say(state, out, zone, now, f"running {name} ({trigger})")
    for reason in decided.reasons:
        _say(state, out, zone, now, f"due {reason}")
    state.last_run_wall, state.last_run_monotonic = now, monotonic()
    selection = Selection(decided.models, "freshness", decided.full_refresh, control)
    previous_start = records.last_run_start
    if commands.run(folder, identity, out, selection=selection) == 0:
        for name in decided.models:
            state.failures.pop(name, None)
            state.held.pop(name, None)
        return 1.0
    after = clock()
    try:
        latest: _Records | None = _records(home, place)
    except commands.ProjectLocked, StoreError:
        latest = None
    if latest is None or latest.last_run_start in {None, previous_start}:
        # No run was recorded: it did not start (a source down while checking, the project
        # taken by another process…). Not a model's failure: tried again after the minimum
        # interval, the reason printed above by the run.
        _say(state, out, zone, after, "the run did not start; trying again in 5 minutes")
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
            _say(state, out, zone, after, f"{name} failed; retry {count} at {retry:%H:%M}")
        else:
            state.failures.pop(name, None)
            state.held[name] = next_due(by_name[name], after, zone)
            print(f"SCHEDULE_GAVE_UP {name}", file=out, flush=True)
            until = state.held[name].astimezone(ZoneInfo(zone))
            _say(state, out, zone, after, f"{name} waits until {until:%Y-%m-%d %H:%M}")
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
    stopped: Callable[[], bool] = lambda: False,
) -> float:
    """No model is due: run lake maintenance when it is due, else wait for whichever comes
    first."""
    now = clock()
    checked = state.checked
    lake_exists = (
        checked is not None
        and checked.project is not None
        and lake_place.exists(checked.project.folder / f".{identity.slug}")
    )
    if not lake_exists:  # nothing to maintain before the first run
        return max(1.0, (decided.wake_at - now).total_seconds())
    due = records.maintained.due_at()
    waiting = state.maintenance_after is not None and monotonic() < state.maintenance_after
    if (due is None or due <= now) and not waiting:
        if stopped():
            return 0.0
        _say(state, out, zone, now, "running lake maintenance")
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
