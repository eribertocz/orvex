"""P2-19 (#337): an instant is converted to its declared zone before it is cut into calendar
units (C6), and the engine's zone data is checked against the pinned `tzdata`."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from orvex.semantic import zonedata
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie.reader import read
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.conformance.test_execution import ask, corpus_case, lake_for
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]
CATALOG = {
    "events": {
        "event_id": SourceColumn("BIGINT", False),
        "event_time": SourceColumn("TIMESTAMP WITH TIME ZONE", False),
        "amount": SourceColumn("DECIMAL(18,2)", False),
    }
}


def model(default: str = "America/La_Paz", field_zone: str | None = None) -> CompiledModel:
    """events, with the model's default zone, the instant's own zone, and a business day that
    declares America/New_York over both."""
    instant: Document = {"name": "event_time", "expression": {"sql": "event_time"}}
    if field_zone:
        instant["custom_extensions"] = [
            extension({"element": "field", "time_zone": {"name": field_zone}})
        ]
    document = {
        "version": "0.2.0.dev0",
        "name": "zones",
        "datasets": [
            {
                "name": "events",
                "source": "events",
                "primary_key": ["event_id"],
                "fields": [
                    {"name": "event_id", "expression": {"sql": "event_id"}},
                    instant,
                    {"name": "amount", "expression": {"sql": "amount"}},
                    {
                        "name": "ny_day",
                        "expression": {"sql": "DATE_TRUNC('DAY', event_time)"},
                        "custom_extensions": [
                            extension(
                                {
                                    "element": "field",
                                    "business_time": True,
                                    "time_zone": {"name": "America/New_York"},
                                }
                            )
                        ],
                    },
                ],
            }
        ],
        "metrics": [{"name": "revenue", "expression": {"sql": "SUM(events.amount)"}}],
        "custom_extensions": [extension({"element": "model", "default_time_zone": default})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/zones.ossie.json", VENDORS)
    return compile_model(loaded, CATALOG, VENDORS)


COMPILED = model()


@contextmanager
def lake_of(instants: list[str]) -> Iterator[Lake]:
    with TemporaryDirectory() as folder:
        path = Path(folder) / "events.csv"
        lines = ["event_id,event_time,amount"] + [
            f"{n},{at},1.00" for n, at in enumerate(instants, 1)
        ]
        path.write_text("\n".join(lines) + "\n", "utf-8")
        with ce.case_lake([path], CATALOG) as lake:
            yield lake


def by(unit: str | None, member: str = "events.event_time") -> Document:
    group: Document = {"member": member}
    if unit:
        group["bucket"] = {"unit": unit}
    return {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "revenue"}],
        "group_by": [group],
    }


def buckets(
    instants: list[str],
    unit: str | None,
    compiled: CompiledModel | None = None,
    **kw: Any,  # noqa: ANN401
) -> list[str | None]:
    with lake_of(instants) as lake:
        found = ask(by(unit, **kw), compiled or COMPILED, lake)
    return [r[0] for r in ce.rows_of(found)]


# --- the corpus -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("CASE-012", [["2026-01-01", "150.00"], ["2026-02-01", "70.00"]]),
        ("CASE-019", [["2025-12-31", "0.1000"], ["2026-01-01", "0.3000"]]),
    ],
)
def test_the_corpus_cuts_instants_in_their_declared_zone(case: str, expected: list[Any]) -> None:
    compiled, _, _ = corpus_case(case)
    with lake_for(case) as lake:
        found = ask(ce.QUERIES[case], compiled, lake)
    assert ce.rows_of(found) == expected
    assert found.physical.zones == (("UTC",) if case == "CASE-012" else ("America/La_Paz",))


# --- convert, then truncate -------------------------------------------------------------------

# 02:00 UTC on 1 January 2026 is 22:00 on 31 December 2025 in La Paz (UTC-4): each unit differs.
LOCAL = {
    "day": "2025-12-31",
    "week": "2025-12-29",  # the Monday
    "month": "2025-12-01",
    "quarter": "2025-10-01",
    "year": "2025-01-01",
}


@pytest.mark.parametrize("unit", sorted(LOCAL))
def test_each_unit_is_cut_after_converting_to_the_zone(unit: str) -> None:
    assert buckets(["2026-01-01T02:00:00Z"], unit) == [LOCAL[unit]]
    with lake_of(["2026-01-01T02:00:00Z"]) as lake:
        text = ask(by(unit), COMPILED, lake).physical.sql
    assert f"date_trunc('{unit}', timezone($" in text


# --- which zone -------------------------------------------------------------------------------


def test_the_field_s_own_zone_comes_before_the_model_s() -> None:
    instants = ["2026-01-01T02:00:00Z"]
    assert buckets(instants, "day", model(default="UTC")) == ["2026-01-01"]
    assert buckets(instants, "day", model(default="UTC", field_zone="America/La_Paz")) == [
        "2025-12-31"
    ]


def test_a_business_time_field_applies_its_own_zone() -> None:
    # 04:30 UTC is 23:30 the day before in New York (UTC-5), and 00:30 in La Paz.
    found = buckets(["2026-01-02T04:30:00Z"], None, member="events.ny_day")
    assert found == ["2026-01-01"]
    assert buckets(["2026-01-02T04:30:00Z"], "day") == ["2026-01-02"]


def test_the_session_s_zone_never_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        assert buckets(["2026-01-01T02:00:00Z"], "day") == ["2025-12-31"]
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


# --- edges ------------------------------------------------------------------------------------


def test_local_midnight_the_last_instant_of_a_year_and_29_february() -> None:
    assert buckets(["2026-01-01T04:00:00Z"], "day") == ["2026-01-01"]  # 00:00 in La Paz
    assert buckets(["2026-01-01T03:59:59Z"], "year") == ["2025-01-01"]  # 23:59:59 on 31 Dec
    assert buckets(["2028-03-01T03:00:00Z"], "day") == ["2028-02-29"]


def test_a_week_starts_on_monday_across_a_month_and_a_year() -> None:
    assert buckets(["2026-03-01T15:00:00Z", "2026-02-24T15:00:00Z"], "week") == ["2026-02-23"]
    assert buckets(["2027-01-01T15:00:00Z"], "week") == ["2026-12-28"]


def test_summer_time_changes_in_both_directions() -> None:
    new_york = model(default="America/New_York")
    # Spring forward on 8 March 2026: 01:59:59 EST, then 03:00:00 EDT, one second later.
    assert buckets(["2026-03-08T06:59:59Z", "2026-03-08T07:00:00Z"], "hour", new_york) == [
        "2026-03-08T06:00:00.000000Z",
        "2026-03-08T07:00:00.000000Z",
    ]
    # Fall back on 1 November 2026: 01:30 EDT, then 01:30 EST an hour later: two rows.
    assert buckets(["2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"], "hour", new_york) == [
        "2026-11-01T05:00:00.000000Z",
        "2026-11-01T06:00:00.000000Z",
    ]
    assert buckets(["2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"], "day", new_york) == [
        "2026-11-01"
    ]


def test_an_hour_in_a_half_hour_zone_starts_at_its_local_hour() -> None:
    kolkata = model(default="Asia/Kolkata")  # UTC+5:30
    assert buckets(["2026-01-01T00:10:00Z"], "hour", kolkata) == ["2025-12-31T23:30:00.000000Z"]


def test_an_hour_bucket_keeps_the_instant_type() -> None:
    with lake_of(["2026-01-01T02:00:00Z"]) as lake:
        found = ask(by("hour"), COMPILED, lake)
    column = found.result.envelope()["columns"][0]
    assert column["encoding"] == "TIMESTAMP WITH TIME ZONE"  # an instant, in UTC
    day = ask_column(by("day"))
    assert day["encoding"] == "DATE"


def ask_column(document: Document) -> dict[str, Any]:
    with lake_of(["2026-01-01T02:00:00Z"]) as lake:
        found = ask(document, COMPILED, lake)
    column: dict[str, Any] = found.result.envelope()["columns"][0]
    return column


# --- the engine's zone data -------------------------------------------------------------------


class Reader:
    """A connection: reads through the lake, counting each kind of read, and can disagree."""

    def __init__(self, lake: Lake, different: int = 0) -> None:
        self.lake, self.different = lake, different
        self.checks = 0
        self.data = 0
        self.lock = threading.Lock()

    def read(self, request: Any) -> pa.RecordBatchReader:  # noqa: ANN401
        if request.sql == zonedata.CHECK_SQL:
            with self.lock:
                self.checks += 1
            if self.different:
                table = pa.table({"#different": pa.array([self.different], pa.int64())})
                return pa.RecordBatchReader.from_batches(table.schema, table.to_batches())
        else:
            with self.lock:
                self.data += 1
        return self.lake.read(request)


def ask_with(reader: Reader, zone_model: CompiledModel | None = None) -> Any:  # noqa: ANN401
    return ask(by("day"), zone_model or COMPILED, reader.lake, read=reader.read)


def test_a_disagreeing_engine_refuses_before_reading_and_is_checked_again() -> None:
    with lake_of(["2026-01-01T02:00:00Z"]) as lake:
        reader = Reader(lake, different=3)
        for attempt in (1, 2):
            with pytest.raises(SemanticError) as caught:
                ask_with(reader)
            assert (caught.value.code, dict(caught.value.details)) == (
                "UNSUPPORTED_OPERATION",
                {"operation": "time_zone_data"},
            )
            assert (reader.checks, reader.data) == (attempt, 0)  # never cached as a pass


def test_the_check_runs_once_per_zone_and_connection() -> None:
    with lake_of(["2026-01-01T02:00:00Z"]) as lake:
        reader = Reader(lake)
        ask_with(reader)
        ask_with(reader)
        assert (reader.checks, reader.data) == (1, 2)
        ask_with(reader, model(default="America/New_York"))  # another zone
        assert reader.checks == 2
        other = Reader(lake)  # another connection
        ask_with(other)
        assert other.checks == 1


def test_concurrent_first_questions_check_once() -> None:
    with lake_of(["2026-01-01T02:00:00Z"]) as lake:
        reader = Reader(lake)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                ask_with(reader)
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]  # the lake's capacity
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert errors == []
    assert (reader.checks, reader.data) == (1, 2)


def test_a_new_tzdata_version_checks_again(monkeypatch: pytest.MonkeyPatch) -> None:
    with lake_of(["2026-01-01T02:00:00Z"]) as lake:
        reader = Reader(lake)
        ask_with(reader)
        monkeypatch.setattr(zonedata, "TZDATA_VERSION", "9999z")
        ask_with(reader)
    assert reader.checks == 2


def test_the_engine_agrees_with_the_pinned_tzdata_at_every_change() -> None:
    """The real check, on the real engine, for zones with and without summer time."""
    with lake_of(["2026-01-01T02:00:00Z"]) as lake:
        for zone in ("America/La_Paz", "America/New_York", "Europe/London", "Asia/Kolkata"):
            reader = Reader(lake)
            ask_with(reader, model(default=zone))
            assert reader.checks == 1
    assert len(zonedata.points("America/New_York")) > 400  # both sides of every change


# --- property ---------------------------------------------------------------------------------

ZONES = ["America/La_Paz", "America/New_York", "Asia/Kolkata", "Australia/Lord_Howe"]


def reference(at: datetime, unit: str, zone: str) -> str:
    local = at.astimezone(zonedata.pinned(zone))
    day = local.date()
    starts: dict[str, Callable[[], date]] = {
        "day": lambda: day,
        "week": lambda: day - timedelta(days=day.weekday()),
        "month": lambda: day.replace(day=1),
        "quarter": lambda: day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1),
        "year": lambda: day.replace(month=1, day=1),
    }
    if unit in starts:
        return starts[unit]().isoformat()
    start = local.replace(minute=0, second=0) if unit == "hour" else local.replace(second=0)
    return start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.datetimes(
        min_value=datetime(1971, 1, 1), max_value=datetime(2099, 1, 1), timezones=st.just(UTC)
    ),
    st.sampled_from(["minute", "hour", "day", "week", "month", "quarter", "year"]),
    st.sampled_from(ZONES),
)
def test_buckets_match_python_zoneinfo_with_the_pinned_tzdata(
    at: datetime, unit: str, zone: str
) -> None:
    at = at.replace(microsecond=0)
    found = buckets([at.strftime("%Y-%m-%dT%H:%M:%SZ")], unit, model(default=zone))
    assert found == [reference(at, unit, zone)]
