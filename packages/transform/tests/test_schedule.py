"""P1-19: cron expressions in a time zone, and freshness-driven planning."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from orvex.transform.cron import CronError, next_time, parse
from orvex.transform.models import Model, discover_models
from orvex.transform.schedule import (
    MINIMUM_INTERVAL,
    History,
    Plan,
    config_problems,
    plan,
)

NEW_YORK = ZoneInfo("America/New_York")
UTC = dt.UTC


def at(text: str, zone: dt.tzinfo = NEW_YORK) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=zone)


def local(instant: dt.datetime, zone: ZoneInfo = NEW_YORK) -> str:
    return instant.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


@pytest.mark.parametrize(
    "text",
    [
        "61 * * * *",
        "* * * *",
        "0 0 30 2 *",
        "0 24 * * *",
        "5-1 * * * *",
        "*/0 * * * *",
        "x * * * *",
    ],
)
def test_invalid_expressions(text: str) -> None:
    with pytest.raises(CronError):
        parse(text)


def test_daylight_saving_changes() -> None:
    # 2026-03-08: 02:00 → 03:00 in New York; 2026-11-01: 02:00 → 01:00.
    spring = next_time(parse("30 2 * * *"), at("2026-03-07T12:00"), NEW_YORK)
    assert local(spring) == "2026-03-08 03:00 EDT"
    fall = next_time(parse("30 1 * * *"), at("2026-10-31T12:00"), NEW_YORK)
    assert local(fall) == "2026-11-01 01:30 EDT"
    # Not again at the second 01:30 (EST): the next firing is the following day.
    assert local(next_time(parse("30 1 * * *"), fall, NEW_YORK)) == "2026-11-02 01:30 EST"


def test_days() -> None:
    sunday = parse("0 3 * * 0")
    assert sunday == parse("0 3 * * 7")
    assert local(next_time(sunday, at("2026-10-28T00:00"), NEW_YORK)) == "2026-11-01 03:00 EST"
    either = parse("0 0 1,15 * 1")  # the 1st, the 15th and every Monday
    moments, after = [], at("2026-10-31T12:00")
    for _ in range(5):
        after = next_time(either, after, NEW_YORK)
        moments.append(local(after)[:10])
    assert moments == ["2026-11-01", "2026-11-02", "2026-11-09", "2026-11-15", "2026-11-16"]
    leap = parse("0 0 29 2 *")
    assert local(next_time(leap, at("2026-01-01T00:00"), NEW_YORK))[:10] == "2028-02-29"


def models(tmp_path: Path, files: dict[str, str]) -> list[Model]:
    for relative, text in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    found = discover_models(tmp_path, {})
    assert found.problems == ()
    return list(found.models)


def test_sidecar_rules(tmp_path: Path) -> None:
    found = models(
        tmp_path,
        {
            "models/s/a.sql": "SELECT 1",
            "models/s/a.yaml": "version: 1\nfreshness: 4m\n",
            "models/s/b.sql": "SELECT 1",
            "models/s/b.yaml": "version: 1\nfreshness: 5m\n",
            "models/s/c.sql": "SELECT 1",
            "models/s/c.yaml": (
                "version: 1\nfull_refresh: {cron: '0 0 30 2 *', time_zone: Mars/Base}\n"
            ),
        },
    )
    problems = [p for model in found for p in config_problems(model, "America/La_Paz")]
    assert [(p.code.root, p.details.path) for p in problems] == [
        ("FRESHNESS_TOO_SHORT", "/freshness"),
        ("MODEL_CONFIG_INVALID", "/full_refresh/cron"),
        ("MODEL_CONFIG_INVALID", "/full_refresh/time_zone"),
    ]


def project(tmp_path: Path) -> list[Model]:
    return models(
        tmp_path,
        {
            "models/raw/readings.sql": "SELECT 1 AS id",
            "models/marts/fresh.sql": "SELECT * FROM raw.readings",
            "models/marts/fresh.yaml": "version: 1\nfreshness: 10m\n",
            "models/marts/weekly.sql": "SELECT * FROM raw.readings",
            "models/marts/weekly.yaml": (
                "version: 1\nfull_refresh: {cron: '0 3 * * 0', time_zone: America/New_York}\n"
            ),
            "models/marts/idle.sql": "SELECT 1 AS id",
        },
    )


ORDER = ("raw.readings", "marts.fresh", "marts.idle", "marts.weekly")
DEPENDENCIES = {"marts.fresh": ("raw.readings",), "marts.weekly": ("raw.readings",)}


def planned(
    found: list[Model],
    now: dt.datetime,
    histories: dict[str, History] | None = None,
    last_run_start: dt.datetime | None = None,
    retry_at: dt.datetime | None = None,
) -> Plan:
    return plan(
        found,
        ORDER,
        DEPENDENCIES,
        histories or {},
        now,
        "America/La_Paz",
        at("2026-10-26T00:00", UTC),
        last_run_start,
        retry_at,
    )


def test_freshness_accounts_for_the_last_run(tmp_path: Path) -> None:
    found = project(tmp_path)
    histories = {
        "marts.fresh": History(at("2026-10-28T09:58", UTC), at("2026-10-28T10:00", UTC)),
        "marts.weekly": History(last_full_refresh=at("2026-10-25T07:00", UTC)),
    }
    early = planned(found, at("2026-10-28T10:07", UTC), histories=histories)
    assert early.models == ()
    assert early.wake_at == at("2026-10-28T10:08", UTC)
    due = planned(found, at("2026-10-28T10:08", UTC), histories=histories)
    assert due.models == ("raw.readings", "marts.fresh")
    assert due.full_refresh == frozenset()


def test_never_succeeded_is_due_now_and_idle_models_never(tmp_path: Path) -> None:
    found = project(tmp_path)
    first = planned(found, at("2026-10-28T10:00", UTC))
    assert first.models == ("raw.readings", "marts.fresh")
    assert "marts.idle" not in first.models


def test_full_refresh_on_its_cron(tmp_path: Path) -> None:
    found = project(tmp_path)
    histories = {
        "marts.fresh": History(at("2026-11-01T07:58", UTC), at("2026-11-01T08:00", UTC)),
        "marts.weekly": History(last_full_refresh=at("2026-10-25T07:00", UTC)),
    }
    # Sunday 2026-11-01 03:00 EST = 08:00 UTC.
    assert planned(found, at("2026-11-01T07:59", UTC), histories=histories).models == ()
    due = planned(found, at("2026-11-01T08:00", UTC), histories=histories)
    assert due.models == ("raw.readings", "marts.weekly")
    assert due.full_refresh == frozenset({"marts.weekly"})


def test_runs_start_at_least_five_minutes_apart(tmp_path: Path) -> None:
    found = project(tmp_path)
    last = at("2026-10-28T10:00", UTC)
    held = planned(found, at("2026-10-28T10:03", UTC), last_run_start=last)
    assert held.models == ()
    assert held.wake_at == at("2026-10-28T10:04", UTC)
    assert planned(found, last + MINIMUM_INTERVAL, last_run_start=last).models
    retried = planned(
        found,
        at("2026-10-28T10:06", UTC),
        last_run_start=last,
        retry_at=at("2026-10-28T10:10", UTC),
    )
    assert retried.models == ()
