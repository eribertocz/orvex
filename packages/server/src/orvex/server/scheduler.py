"""`schedule`: keep a project's models as fresh as they declare (P1-19).

The loop re-reads the project each cycle (edits apply without a restart), reads the history from
the run records, asks `transform.schedule.plan` what is due, and runs it through the normal
`run` — one run at a time, under the project lock. A failed run is retried after 5, 10 and 20
minutes, then given up until the models' next due time. Every decision is printed.
"""

from __future__ import annotations

import datetime as dt
import signal
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import FrameType
from typing import IO
from zoneinfo import ZoneInfo

from orvex.server import commands
from orvex.server.commands import LAKE_FOLDER, Selection, check_project, project_lock
from orvex.server.identity import Identity
from orvex.store.errors import StoreError
from orvex.store.lake import open_lake
from orvex.transform.models import Model
from orvex.transform.run_records import last_run_request, model_history_request
from orvex.transform.schedule import (
    LOOK_AGAIN,
    MINIMUM_INTERVAL,
    RETRY_DELAYS,
    History,
    duration,
    plan,
)

type Clock = Callable[[], dt.datetime]
type Sleep = Callable[[float], None]


def _clock() -> dt.datetime:
    """The runner's clock (the same one run records use)."""
    return dt.datetime.fromisoformat(commands._now())


def _instant(text: object) -> dt.datetime | None:
    return None if text is None else dt.datetime.fromisoformat(str(text))


def _history(home: Path) -> tuple[dict[str, History], dt.datetime | None]:
    """Each model's history and the project's last run start, from the run records."""
    if not (home / LAKE_FOLDER).exists():
        return {}, None
    with project_lock(home):
        lake = open_lake(commands._location(home), commands._limits())
        try:
            snapshot = lake.current_snapshot().id
            try:
                rows = lake.read(model_history_request(snapshot)).read_all().to_pylist()
                (last,) = lake.read(last_run_request(snapshot)).read_all().to_pylist()
            except StoreError as error:
                if error.code != "STORE_STATEMENT_INVALID":
                    raise
                return {}, None  # no run records yet
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
    return histories, _instant(last["started"])


def _hold(models: Mapping[str, Model], names: tuple[str, ...]) -> dt.timedelta:
    """How long models whose runs were given up wait: their shortest freshness (at least the
    minimum interval), or a day for models that only have a full-refresh cron."""
    found = [
        duration(model.config.freshness.root)
        for name in names
        if (model := models.get(name)) is not None and model.config.freshness is not None
    ]
    return max(MINIMUM_INTERVAL, min(found, default=dt.timedelta(days=1)))


def schedule(
    folder: Path,
    identity: Identity,
    out: IO[str],
    clock: Clock = _clock,
    sleep: Sleep = time.sleep,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Run the due models of a project until stopped (Ctrl+C stops after the current run)."""
    stopping = False

    def stop(_signal: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    def stopped() -> bool:
        return stopping or (should_stop is not None and should_stop())

    previous = signal.signal(signal.SIGINT, stop) if should_stop is None else None
    try:
        return _loop(folder, identity, out, clock, sleep, stopped)
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def _say(out: IO[str], zone: str, moment: dt.datetime, text: str) -> None:
    local = moment.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{local} {text}", file=out, flush=True)


def _loop(
    folder: Path,
    identity: Identity,
    out: IO[str],
    clock: Clock,
    sleep: Sleep,
    stopped: Callable[[], bool],
) -> int:
    started = clock()
    failures = 0
    retry_at: dt.datetime | None = None
    reported: tuple[str, ...] = ()
    while not stopped():
        now = clock()
        checked = check_project(folder)
        if checked.problems or checked.project is None or checked.graph is None:
            lines = tuple(str(problem) for problem in checked.problems)
            if lines != reported:  # printed once until the project changes
                for line in lines:
                    print(line, file=out, flush=True)
                reported = lines
            sleep(LOOK_AGAIN.total_seconds())
            continue
        reported = ()
        project, graph = checked.project, checked.graph
        zone = project.settings.time_zone
        home = project.folder / f".{identity.slug}"
        try:
            histories, last_run = _history(home)
        except commands.ProjectLocked:
            _say(out, zone, now, "waiting: another process holds the project")
            sleep(LOOK_AGAIN.total_seconds())
            continue
        decided = plan(
            checked.discovery.models,
            graph.order,
            graph.dependencies,
            histories,
            now,
            zone,
            started,
            last_run,
            retry_at,
        )
        if not decided.models:
            sleep(max(0.0, (decided.wake_at - now).total_seconds()))
            continue
        trigger = "full_refresh" if decided.full_refresh else "freshness"
        _say(
            out,
            zone,
            now,
            f"running {', '.join(decided.models)} ({trigger}): {'; '.join(decided.reasons)}",
        )
        selection = Selection(decided.models, "freshness", decided.full_refresh)
        if commands.run(folder, identity, out, selection=selection) == 0:
            failures, retry_at = 0, None
            continue
        failures += 1
        now = clock()
        if failures <= len(RETRY_DELAYS):
            retry_at = now + RETRY_DELAYS[failures - 1]
            _say(out, zone, now, f"run failed; retry {failures} at {retry_at.isoformat()}")
            continue
        due = tuple(reason.split(":")[0] for reason in decided.reasons)
        for name in due:
            print(f"SCHEDULE_GAVE_UP {name}", file=out, flush=True)
        by_name = {model.name: model for model in checked.discovery.models}
        failures, retry_at = 0, now + _hold(by_name, due)
    return 0
