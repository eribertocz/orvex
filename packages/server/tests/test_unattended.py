"""P1-21 (#69): `schedule` runs unattended — a clean stop, a heartbeat, one per project.

The process tests start the real command and send it real signals. Tests that need a lake run in
CI (DuckLake), as the other scheduler tests do.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import itertools
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import duckdb
import pytest
from orvex.server import commands, heartbeat, lake_place, maintenance, scheduler
from orvex.server.cli import main
from orvex.server.identity import load_identity
from orvex.server.scheduler import SCHEDULER_LOCK, schedule

SLUG = load_identity().slug
PROJECT = "version: 1\nname: Test\ntime_zone: UTC\n"
START = dt.datetime(2026, 10, 28, 9, 50, tzinfo=dt.UTC)
WAIT = 30.0  # the most any process event may take; reaching it fails the test
SLOW = "SELECT sum(hash(a.range + b.range)) AS n FROM range(200000000) AS a, range(1000) AS b"


def write(folder: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def home(folder: Path) -> Path:
    return folder / f".{SLUG}"


def beat(folder: Path) -> dict[str, object]:
    found: dict[str, object] = json.loads((home(folder) / heartbeat.HEARTBEAT_FILE).read_text())
    return found


class Clock:
    """Fake time: the scheduler's minutes pass in a moment, and the heartbeat is read after
    every step (it must always be whole JSON)."""

    def __init__(self, folder: Path, until: dt.datetime) -> None:
        self.now = START
        self.until = until
        self.folder = folder
        self.updates: list[str] = []

    def text(self) -> str:
        return self.now.isoformat(timespec="microseconds")

    def sleep(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=max(seconds, 1.0))
        updated = str(beat(self.folder)["updated_at"])  # whole JSON at any moment
        if not self.updates or self.updates[-1] != updated:
            self.updates.append(updated)

    def done(self) -> bool:
        return self.now >= self.until

    def monotonic(self) -> float:
        return (self.now - START).total_seconds()


def scheduled(folder: Path, monkeypatch: pytest.MonkeyPatch, minutes: int) -> tuple[str, Clock]:
    clock = Clock(folder, START + dt.timedelta(minutes=minutes))
    monkeypatch.setattr(commands, "_now", clock.text)
    out = io.StringIO()
    code = schedule(
        folder,
        load_identity(),
        out,
        sleep=clock.sleep,
        should_stop=clock.done,
        monotonic=clock.monotonic,
    )
    assert code == 0, out.getvalue()
    return out.getvalue(), clock


# --- heartbeat --------------------------------------------------------------------------------


def test_the_heartbeat_is_renewed_every_minute_while_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, {"project.yaml": PROJECT, "models/s/idle.sql": "SELECT 1 AS n"})
    _, clock = scheduled(tmp_path, monkeypatch, minutes=30)
    times = [dt.datetime.fromisoformat(text) for text in clock.updates]
    assert len(times) >= 30
    gaps = [(later - earlier).total_seconds() for earlier, later in itertools.pairwise(times)]
    assert max(gaps) <= 61, max(gaps)
    final = beat(tmp_path)
    assert final["state"] == "stopped"
    assert final["pid"] == os.getpid()


def test_the_heartbeat_holds_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "never-in-the-heartbeat-5Z"
    monkeypatch.setenv("UNATTENDED_TEST_PASSWORD", secret)
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "connections.yaml": (
                "version: 1\nconnections:\n  production:\n    type: postgres\n"
                "    host: 127.0.0.1\n    port: 1\n    database: x\n    user: y\n"
                "    password: ${env:UNATTENDED_TEST_PASSWORD}\n    read_only: true\n"
            ),
            "models/s/m.sql": "SELECT * FROM production.public.t",
            "models/s/m.yaml": "version: 1\nfreshness: 5m\n",
        },
    )
    scheduled(tmp_path, monkeypatch, minutes=12)
    assert secret not in (home(tmp_path) / heartbeat.HEARTBEAT_FILE).read_text()


def record(folder: Path, payload: dict[str, object]) -> None:
    base: dict[str, object] = {
        "pid": 4242,
        "started_at": "2026-10-28T09:00:00+00:00",
        "updated_at": "2026-10-28T09:00:00+00:00",
        "state": "running",
    }
    heartbeat.write(home(folder), heartbeat.Heartbeat(**(base | payload)))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, "scheduler: has not run"),
        (
            {
                "updated_at": "now",
                "last_decision_at": "2026-10-28T09:55:00+00:00",
                "last_decision": "running s.m (freshness)",
                "next_at": "2026-10-28T10:00:00+00:00",
            },
            "scheduler: running since 2026-10-28 09:00:00, last decision at "
            "2026-10-28 09:55:00 (running s.m (freshness)), next at 2026-10-28 10:00:00",
        ),
        (
            {
                "state": "stopped",
                "stopped_at": "2026-10-28T10:05:00+00:00",
                "stop_reason": "SIGTERM",
            },
            "scheduler: stopped at 2026-10-28 10:05:00 (SIGTERM)",
        ),
        (
            {"updated_at": "2026-10-28T09:00:00+00:00"},
            "scheduler: not responding (process 4242, last heard at 2026-10-28 09:00:00)",
        ),
    ],
    ids=["never-run", "running", "stopped", "not-responding"],
)
def test_runs_shows_the_scheduler(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object] | None,
    expected: str,
) -> None:
    write(tmp_path, {"project.yaml": PROJECT})
    now = "2026-10-28T10:10:00+00:00"
    monkeypatch.setattr(commands, "_now", lambda: now)
    if payload is not None:
        record(tmp_path, {key: now if value == "now" else value for key, value in payload.items()})
    assert main(["runs", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == expected


@pytest.mark.parametrize(
    "text",
    [
        "{",
        '{"pid": 1}',
        '{"pid": 1, "started_at": "x", "updated_at": "y", "state": "running"}',
        '{"pid": 1, "started_at": "2026-10-28T09:00:00", "updated_at": '
        '"2026-10-28T09:00:00", "state": "running"}',
    ],
    ids=["not-json", "fields-missing", "not-instants", "no-offset"],
)
def test_runs_shows_an_unreadable_heartbeat_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    write(tmp_path, {"project.yaml": PROJECT, f".{SLUG}/{heartbeat.HEARTBEAT_FILE}": text})
    assert main(["runs", str(tmp_path)]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == "scheduler: unknown (the scheduler.json heartbeat cannot be read)"


def test_a_heartbeat_that_cannot_be_written_does_not_stop_the_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, {"project.yaml": PROJECT, "models/s/idle.sql": "SELECT 1 AS n"})

    def full(_home: Path, _beat: heartbeat.Heartbeat) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(heartbeat, "write", full)
    steps = itertools.count()
    out = io.StringIO()
    code = schedule(
        tmp_path,
        load_identity(),
        out,
        sleep=lambda _seconds: None,
        should_stop=lambda: next(steps) > 200,
    )
    assert code == 0, out.getvalue()
    assert out.getvalue().count("heartbeat not written: No space left on device") == 1
    assert "stopped (stopped)" in out.getvalue()


def test_the_heartbeat_says_a_run_started_and_is_renewed_during_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "models/s/m.sql": "SELECT 1 AS n",
            "models/s/m.yaml": "version: 1\nfreshness: 5m\n",
        },
    )
    monkeypatch.setattr(scheduler, "HEARTBEAT_EVERY", dt.timedelta(seconds=0.05))
    seen: list[dict[str, object]] = []

    def run(*_args: object, **_keywords: object) -> int:
        # A long run: the heartbeat already names it, and keeps being renewed.
        seen.append(beat(tmp_path))
        deadline = time.monotonic() + WAIT
        while beat(tmp_path)["updated_at"] == seen[0]["updated_at"]:
            assert time.monotonic() < deadline, "not renewed during the run"
            time.sleep(0.01)
        seen.append(beat(tmp_path))
        return 0

    monkeypatch.setattr(commands, "run", run)
    out = io.StringIO()
    code = schedule(tmp_path, load_identity(), out, should_stop=lambda: len(seen) == 2)
    assert code == 0, out.getvalue()
    during, renewed = seen
    # The decision lines printed when the run started; the last of them is in the heartbeat.
    assert "running s.m (freshness)" in out.getvalue()
    assert str(during["last_decision"]).startswith("due s.m"), during
    assert during["next_at"] is None  # the earlier "next at" no longer holds
    assert during["state"] == renewed["state"] == "running"
    assert renewed["last_decision"] == during["last_decision"]


# --- a stop during a cycle starts nothing --------------------------------------------------------


def test_a_stop_while_checking_the_project_starts_no_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "models/s/m.sql": "SELECT 1 AS n",
            "models/s/m.yaml": "version: 1\nfreshness: 5m\n",
        },
    )
    stop: list[str] = []
    check = commands.check_project

    def checking(*args: object, **keywords: object) -> commands.Checked:
        stop.append("SIGTERM")  # arrives while the project is being checked
        return check(*args, **keywords)  # type: ignore[arg-type]

    def run(*_args: object, **_keywords: object) -> int:
        pytest.fail("a run started after the stop")

    monkeypatch.setattr(commands, "check_project", checking)
    monkeypatch.setattr(commands, "run", run)
    out = io.StringIO()
    assert schedule(tmp_path, load_identity(), out, should_stop=lambda: bool(stop)) == 0
    assert "running" not in out.getvalue().replace("stopped", "")
    assert beat(tmp_path)["state"] == "stopped"


def test_a_stop_while_reading_the_records_starts_no_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, {"project.yaml": PROJECT, "models/s/idle.sql": "SELECT 1 AS n"})
    stop: list[str] = []

    def records(*_args: object) -> object:
        stop.append("SIGTERM")  # arrives while the run records are read
        return scheduler._Records({}, None, ())  # maintenance never ran: it is due

    def maintain(*_args: object, **_keywords: object) -> int:
        pytest.fail("maintenance started after the stop")

    monkeypatch.setattr(scheduler, "_records", records)
    monkeypatch.setattr(lake_place, "exists", lambda _home: True)
    monkeypatch.setattr(maintenance, "maintain", maintain)
    out = io.StringIO()
    assert schedule(tmp_path, load_identity(), out, should_stop=lambda: bool(stop)) == 0
    assert "lake maintenance" not in out.getvalue()


# --- one scheduler per project ------------------------------------------------------------------


def test_a_second_scheduler_is_refused(tmp_path: Path) -> None:
    write(tmp_path, {"project.yaml": PROJECT})
    record(tmp_path, {})
    out = io.StringIO()
    with commands.project_lock(home(tmp_path), SCHEDULER_LOCK):
        assert schedule(tmp_path, load_identity(), out, should_stop=lambda: True) == 1
    assert out.getvalue().startswith("SCHEDULER_ALREADY_RUNNING ")
    assert "process 4242" in out.getvalue()


class Process:
    """The real `schedule` command, its output read line by line in the background."""

    def __init__(self, folder: Path, *extra: str) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "orvex.server", "schedule", str(folder), *extra],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.seen: list[str] = []
        assert self.process.stdout is not None
        stream = self.process.stdout
        threading.Thread(target=self._read, args=(stream,), daemon=True).start()

    def _read(self, stream: object) -> None:
        with contextlib.suppress(ValueError, OSError):  # closed by `kill`
            for line in stream:  # type: ignore[attr-defined]
                self.lines.put(line)

    def wait_for(self, text: str) -> None:
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=deadline - time.monotonic())
            except queue.Empty:
                break
            self.seen.append(line)
            if text in line:
                return
        pytest.fail(f"never printed {text!r}: {self.seen}")

    def signal(self, number: int) -> None:
        self.process.send_signal(number)

    def finish(self) -> int:
        code = self.process.wait(WAIT)
        while not self.lines.empty():
            self.seen.append(self.lines.get())
        return code

    def kill(self) -> None:
        """End the process if it still runs, and close its output."""
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(WAIT)
        if self.process.stdout is not None:
            self.process.stdout.close()


def started(folder: Path, *extra: str) -> Process:
    """A scheduler that has written its first heartbeat."""
    process = Process(folder, *extra)
    deadline = time.monotonic() + WAIT
    path = home(folder) / heartbeat.HEARTBEAT_FILE
    while not path.exists():
        if time.monotonic() > deadline or process.process.poll() is not None:
            process.kill()
            pytest.fail("the scheduler wrote no heartbeat")
        time.sleep(0.05)
    return process


def test_a_killed_scheduler_releases_the_project(tmp_path: Path) -> None:
    write(tmp_path, {"project.yaml": PROJECT})
    first = started(tmp_path)
    try:
        first.process.send_signal(signal.SIGKILL)
        first.process.wait(WAIT)
    finally:
        first.kill()
    out = io.StringIO()
    assert schedule(tmp_path, load_identity(), out, should_stop=lambda: True) == 0, out.getvalue()


# --- a clean stop ------------------------------------------------------------------------------


def test_sigterm_between_runs_stops_at_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, {"project.yaml": PROJECT, "models/s/idle.sql": "SELECT 1 AS n"})
    process = started(tmp_path)
    try:
        signal_sent = time.monotonic()
        process.signal(signal.SIGTERM)
        assert process.finish() == 0, process.seen
        assert time.monotonic() - signal_sent < 2.0
    finally:
        process.kill()
    found = beat(tmp_path)
    assert (found["state"], found["stop_reason"]) == ("stopped", "SIGTERM")
    assert any("stopped (SIGTERM)" in line for line in process.seen)
    assert main(["runs", str(tmp_path)]) == 0
    assert "(SIGTERM)" in capsys.readouterr().out.splitlines()[0]


def lake_records(folder: Path) -> tuple[list[tuple[str, str, str | None]], int]:
    """The run records of `s.slow`, and whether its table exists (1) or not (0)."""
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    catalog = home(folder) / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    rows = lake.execute(
        "SELECT model, outcome, error_code FROM lake._meta.run_records "
        "WHERE model = 's.slow' ORDER BY started_at"
    ).fetchall()
    tables = lake.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = 'lake' "
        "AND schema_name = 's' AND table_name = 'slow'"
    ).fetchall()
    lake.close()
    found = [(str(a), str(b), None if c is None else str(c)) for a, b, c in rows]
    return found, int(tables[0][0])


def slow_project(folder: Path, sql: str) -> None:
    write(
        folder,
        {
            "project.yaml": PROJECT,
            "models/s/slow.sql": sql,
            "models/s/slow.yaml": "version: 1\nfreshness: 5m\n",
        },
    )


def test_sigterm_during_a_run_that_finishes_commits_it(tmp_path: Path) -> None:
    # A few seconds of work (about 8 on two cores): still running when the signal arrives.
    slow_project(
        tmp_path,
        "SELECT sum(hash(a.range + b.range)) AS n FROM range(200000000) AS a, range(5) AS b",
    )
    process = started(tmp_path, "--stop-grace", "60")
    try:
        process.wait_for("running s.slow")
        signal_sent = time.monotonic()
        process.signal(signal.SIGTERM)
        assert process.finish() == 0, process.seen
        assert time.monotonic() - signal_sent > 1.0  # it waited for the run, then stopped
    finally:
        process.kill()
    assert lake_records(tmp_path) == ([("s.slow", "success", None)], 1)
    assert beat(tmp_path)["stop_reason"] == "SIGTERM"


def test_sigterm_during_a_run_past_the_grace_period_cancels_it(tmp_path: Path) -> None:
    slow_project(tmp_path, SLOW)
    process = started(tmp_path, "--stop-grace", "1")
    try:
        process.wait_for("running s.slow")
        signal_sent = time.monotonic()
        process.signal(signal.SIGTERM)
        assert process.finish() == 0, process.seen
        assert time.monotonic() - signal_sent < 15.0
    finally:
        process.kill()
    # Rolled back: nothing of it was committed, and the table does not exist.
    assert lake_records(tmp_path) == ([("s.slow", "failed", "STORE_CANCELLED")], 0)
    assert beat(tmp_path)["stop_reason"] == "SIGTERM"


# --- the P1 exit criterion: loads from PostgreSQL every 5 minutes, no duplicates, no gaps ------

PASSWORD_ENV = "STORE_TEST_POSTGRES_PASSWORD"


def postgres_settings() -> dict[str, str]:
    connection = os.environ.get("STORE_TEST_POSTGRES")
    if not connection or not os.environ.get(PASSWORD_ENV):
        if os.environ.get("CI"):
            pytest.fail(f"STORE_TEST_POSTGRES and {PASSWORD_ENV} are required in CI")
        pytest.skip("no PostgreSQL configured for this run")
    return dict(pair.split("=", 1) for pair in connection.split())


def test_incremental_loads_from_postgres_every_five_minutes_without_duplicates_or_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = postgres_settings()
    schema = f"unattended_{os.getpid()}_{int(time.time() * 1000) % 100000}"
    admin = duckdb.connect()
    admin.install_extension("postgres")
    admin.load_extension("postgres")
    password = os.environ[PASSWORD_ENV].replace("'", "''")
    admin.execute(f"CREATE SECRET (TYPE postgres, PASSWORD '{password}')")
    target = " ".join(f"{key}={value}" for key, value in settings.items())
    admin.execute(f"ATTACH '{target}' AS pg (TYPE postgres)")

    def source(sql: str) -> None:
        admin.execute(f"CALL postgres_execute('pg', '{sql.replace(chr(39), chr(39) * 2)}')")

    source(f"CREATE SCHEMA {schema}")
    source(
        f"CREATE TABLE {schema}.readings (reading_id bigint PRIMARY KEY, kwh numeric(18,4), "
        "read_at timestamptz NOT NULL)"
    )
    next_id = [1]

    def insert(moment: dt.datetime, late: bool) -> None:
        # Two new readings a minute old, and (after the first cycle) one late reading 12
        # minutes old: within the model's 30-minute lookback.
        times = [moment - dt.timedelta(minutes=1)] * 2
        if late:
            times.append(moment - dt.timedelta(minutes=12))
        values = ", ".join(
            f"({next_id[0] + index}, {next_id[0] + index}.5, '{at.isoformat()}')"
            for index, at in enumerate(times)
        )
        next_id[0] += len(times)
        source(f"INSERT INTO {schema}.readings VALUES {values}")

    try:
        insert(START, late=False)
        write(
            tmp_path,
            {
                "project.yaml": PROJECT,
                "connections.yaml": (
                    "version: 1\nconnections:\n  production:\n    type: postgres\n"
                    f"    host: {settings['host']}\n    port: {settings.get('port', '5432')}\n"
                    f"    database: {settings['dbname']}\n    user: {settings['user']}\n"
                    f"    password: ${{env:{PASSWORD_ENV}}}\n    read_only: true\n"
                    f"    schema: {schema}\n"
                ),
                "models/s/readings.sql": (
                    "SELECT reading_id, kwh, read_at FROM production.readings "
                    "WHERE read_at > $since"
                ),
                "models/s/readings.yaml": (
                    "version: 1\nmaterialized: incremental\nfreshness: 5m\n"
                    "incremental: {strategy: merge, unique_key: [reading_id], "
                    "time_column: read_at, lookback: 30m, filter: manual}\n"
                ),
            },
        )
        clock = Clock(tmp_path, START + dt.timedelta(minutes=60))
        inserted_until = START + dt.timedelta(minutes=50)  # the last runs load the last rows
        boundary = [START + dt.timedelta(minutes=2)]
        original = clock.sleep

        def sleep(seconds: float) -> None:
            original(seconds)
            while clock.now >= boundary[0] and boundary[0] <= inserted_until:
                insert(boundary[0], late=True)
                boundary[0] += dt.timedelta(minutes=5)

        monkeypatch.setattr(commands, "_now", clock.text)
        out = io.StringIO()
        code = schedule(
            tmp_path,
            load_identity(),
            out,
            sleep=sleep,
            should_stop=clock.done,
            monotonic=clock.monotonic,
        )
        assert code == 0, out.getvalue()
        # Every 5 minutes over the hour: the first at the start, then never more than a
        # minute late, the last one in the hour's last ten minutes.
        starts = [
            dt.datetime.fromisoformat(line[:19]).replace(tzinfo=dt.UTC)
            for line in out.getvalue().splitlines()
            if line[20:].startswith("running s.readings")
        ]
        assert starts[0] == START, out.getvalue()
        gaps = [(later - earlier).total_seconds() for earlier, later in itertools.pairwise(starts)]
        assert all(300 <= gap <= 360 for gap in gaps), (gaps, out.getvalue())
        assert starts[-1] >= START + dt.timedelta(minutes=50), out.getvalue()

        expected = admin.execute(
            f"SELECT reading_id FROM pg.{schema}.readings ORDER BY reading_id"
        ).fetchall()
        lake = duckdb.connect()
        lake.execute("INSTALL ducklake")
        lake.execute("LOAD ducklake")
        catalog = home(tmp_path) / "lake" / "catalog.ducklake"
        lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
        loaded = lake.execute(
            "SELECT reading_id FROM lake.s.readings ORDER BY reading_id"
        ).fetchall()
        distinct = lake.execute(
            "SELECT count(*) = count(DISTINCT reading_id) FROM lake.s.readings"
        ).fetchall()
        lake.close()
        assert len(expected) > 30
        assert distinct == [(True,)]  # no duplicates
        assert loaded == expected  # no gaps, late rows included
    finally:
        source(f"DROP SCHEMA {schema} CASCADE")
        admin.close()
