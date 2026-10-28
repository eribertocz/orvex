"""P1-19: `schedule` keeps models fresh, never overlaps runs, retries with backoff.

The clock and the sleep are fake: a scenario of hours runs in seconds, and run records carry the
fake times.
"""

from __future__ import annotations

import datetime as dt
import io
import itertools
from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest
from orvex.server import commands, scheduler
from orvex.server.cli import main
from orvex.server.identity import load_identity
from orvex.server.scheduler import schedule
from orvex.store.errors import StoreError

SLUG = load_identity().slug
PROJECT = "version: 1\nname: Test\ntime_zone: UTC\n"
CONNECTIONS = "version: 1\nconnections:\n  feed:\n    type: parquet\n    path: data/f.parquet\n"
START = dt.datetime(2026, 10, 28, 9, 50, tzinfo=dt.UTC)


def write(folder: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def feed(folder: Path, status: str) -> None:
    (folder / "data").mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(
        f"COPY (SELECT 1::BIGINT AS id, '{status}' AS status, "
        "TIMESTAMPTZ '2024-01-01 10:00:00+00' AS read_at) "
        f"TO '{folder / 'data' / 'f.parquet'}'"
    )


class FakeTime:
    def __init__(self, folder: Path, until: dt.datetime) -> None:
        self.now = START
        self.until = until
        self.hooks: list[Callable[[dt.datetime], None]] = []

    def text(self) -> str:
        return self.now.isoformat(timespec="microseconds")

    def sleep(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=max(seconds, 1.0))
        for hook in self.hooks:
            hook(self.now)

    def done(self) -> bool:
        return self.now >= self.until

    def monotonic(self) -> float:
        return (self.now - START).total_seconds()


def scheduled(
    folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    until: dt.datetime,
    *hooks: Callable[[dt.datetime], None],
) -> str:
    fake = FakeTime(folder, until)
    fake.hooks += hooks
    monkeypatch.setattr(commands, "_now", fake.text)
    out = io.StringIO()
    assert (
        schedule(
            folder,
            load_identity(),
            out,
            sleep=fake.sleep,
            should_stop=fake.done,
            monotonic=fake.monotonic,
        )
        == 0
    )
    return out.getvalue()


def records(folder: Path) -> list[tuple[str, str, str, str]]:
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    catalog = folder / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    rows = lake.execute(
        "SELECT model, strftime(timezone('UTC', started_at), '%H:%M'), outcome, trigger "
        "FROM lake._meta.run_records WHERE outcome <> 'not_run' ORDER BY started_at, model"
    ).fetchall()
    lake.close()
    return [(str(a), str(b), str(c), str(d)) for a, b, c, d in rows]


def minutes(text: str) -> int:
    hours, mins = text.split(":")
    return int(hours) * 60 + int(mins)


def assert_five_minutes_apart(found: list[tuple[str, str, str, str]]) -> None:
    by_model: dict[str, list[int]] = {}
    for model, started, _, _ in found:
        by_model.setdefault(model, []).append(minutes(started))
    for model, starts in by_model.items():
        gaps = [b - a for a, b in itertools.pairwise(starts)]
        assert all(gap >= 5 for gap in gaps), (model, starts)


def test_freshness_too_short_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "models/s/m.sql": "SELECT 1 AS id",
            "models/s/m.yaml": "version: 1\nfreshness: 4m\n",
        },
    )
    assert main(["validate", str(tmp_path)]) == 1
    assert capsys.readouterr().out.startswith("FRESHNESS_TOO_SHORT models/s/m.yaml")


def test_models_are_kept_fresh_without_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(tmp_path, "valid")
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/raw/f.sql": "SELECT * FROM feed.data",
            "models/marts/a.sql": "SELECT * FROM raw.f",
            "models/marts/a.yaml": "version: 1\nfreshness: 10m\n",
            "models/marts/b.sql": "SELECT * FROM raw.f",
            "models/marts/b.yaml": "version: 1\nfreshness: 7m\n",
            "models/marts/idle.sql": "SELECT 1 AS n",
        },
    )
    out = scheduled(tmp_path, monkeypatch, START + dt.timedelta(minutes=40))
    found = records(tmp_path)
    assert "marts.idle" not in {model for model, *_ in found}
    assert {trigger for *_, trigger in found} == {"freshness"}
    assert {"marts.a", "marts.b", "raw.f"} <= {model for model, *_ in found}
    assert_five_minutes_apart(found)
    assert "running raw.f (freshness)" in out
    assert "due marts.b: freshness 7m" in out


def test_failed_runs_are_retried_with_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(tmp_path, "voided")
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": "SELECT * FROM feed.data",
            "models/s/m.yaml": (
                "version: 1\nfreshness: 1h\n"
                "tests:\n  - accepted_values: {column: status, values: [valid]}\n"
            ),
        },
    )

    def fix(now: dt.datetime) -> None:
        if now >= START + dt.timedelta(minutes=14):  # after the second failure
            feed(tmp_path, "valid")

    scheduled(tmp_path, monkeypatch, START + dt.timedelta(minutes=30), fix)
    assert [(started, outcome) for _, started, outcome, _ in records(tmp_path)] == [
        ("09:50", "failed"),
        ("09:55", "failed"),  # first retry after 5 minutes
        ("10:05", "success"),  # second retry after 10 more
    ]


def test_a_run_failing_four_times_is_given_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(tmp_path, "voided")
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": "SELECT * FROM feed.data",
            "models/s/m.yaml": (
                "version: 1\nfreshness: 1h\n"
                "tests:\n  - accepted_values: {column: status, values: [valid]}\n"
            ),
        },
    )
    out = scheduled(tmp_path, monkeypatch, START + dt.timedelta(minutes=90))
    starts = [started for _, started, _, _ in records(tmp_path)]
    assert starts[:4] == ["09:50", "09:55", "10:05", "10:25"]
    assert "SCHEDULE_GAVE_UP s.m" in out
    # Given up for its freshness (1 h) before trying again.
    assert all(minutes(start) >= minutes("11:25") for start in starts[4:])


def test_full_refresh_on_its_cron(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    feed(tmp_path, "valid")
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/raw/f.sql": "SELECT * FROM feed.data",
            "models/s/m.sql": "SELECT * FROM raw.f WHERE read_at > $since",
            "models/s/m.yaml": (
                "version: 1\nmaterialized: incremental\nfreshness: 1h\n"
                "incremental: {strategy: append, time_column: read_at, filter: manual}\n"
                "full_refresh: {cron: '0 10 * * *'}\n"
            ),
        },
    )
    scheduled(tmp_path, monkeypatch, START + dt.timedelta(minutes=20))
    # The trigger is per model: the upstream model is rebuilt as usual, not "full refresh".
    assert [(model, started, trigger) for model, started, _, trigger in records(tmp_path)] == [
        ("raw.f", "09:50", "freshness"),
        ("s.m", "09:50", "freshness"),
        ("raw.f", "10:00", "freshness"),
        ("s.m", "10:00", "full_refresh"),
    ]


def test_a_failing_model_does_not_stall_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(tmp_path, "voided")
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/healthy.sql": "SELECT 1 AS n",
            "models/s/healthy.yaml": "version: 1\nfreshness: 10m\n",
            "models/s/broken.sql": "SELECT * FROM feed.data",
            "models/s/broken.yaml": (
                "version: 1\nfreshness: 1h\n"
                "tests:\n  - accepted_values: {column: status, values: [valid]}\n"
            ),
        },
    )
    out = scheduled(tmp_path, monkeypatch, START + dt.timedelta(minutes=60))
    found = records(tmp_path)
    healthy = [(started, outcome) for model, started, outcome, _ in found if model == "s.healthy"]
    # The first run stops at the broken model (it runs first: by name); then the healthy one
    # runs on its own, five minutes later, while the broken one waits for its retry.
    assert healthy[0] == ("09:55", "success")
    assert sum(1 for _, outcome in healthy if outcome == "success") >= 4
    assert "SCHEDULE_GAVE_UP s.broken" in out
    assert "SCHEDULE_GAVE_UP s.healthy" not in out
    assert_five_minutes_apart(found)


def test_idle_scheduling_does_not_touch_the_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(tmp_path, "valid")
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": "SELECT * FROM feed.data",
            "models/s/m.yaml": (
                "version: 1\nfreshness: 1d\n"
                "tests:\n  - accepted_values: {column: status, values: [valid]}\n"
            ),
        },
    )
    checks: list[int] = []
    original = commands.check_project

    def counted(folder: Path) -> commands.Checked:
        checks.append(1)
        return original(folder)

    monkeypatch.setattr(commands, "check_project", counted)
    scheduled(tmp_path, monkeypatch, START + dt.timedelta(hours=3))
    # Once by the scheduler (the files never changed) and once by the only run: three idle
    # hours never bind the models against the sources again.
    assert len(checks) == 2


def test_unreadable_records_never_crash_or_speed_up_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed(tmp_path, "valid")
    write(
        tmp_path,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": "SELECT * FROM feed.data",
            "models/s/m.yaml": "version: 1\nfreshness: 5m\n",
        },
    )
    calls: list[int] = []

    def unreadable(_home: Path) -> object:
        calls.append(1)
        if len(calls) % 2:
            raise StoreError("STORE_EXTENSION_UNAVAILABLE", "unavailable", {})
        return scheduler._Records({}, None, ())  # records say nothing: never ran

    monkeypatch.setattr(scheduler, "_records", unreadable)
    out = scheduled(tmp_path, monkeypatch, START + dt.timedelta(minutes=30))
    assert out.count("STORE_EXTENSION_UNAVAILABLE") >= 1
    assert_five_minutes_apart(records(tmp_path))
