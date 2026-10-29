"""P1-17 part 2 (#98): the `{since:…}` value and the query sent to ClickHouse, without a server.

The streaming itself is tested against a live ClickHouse by the server package
(`test_extraction_stream.py`), which may compose the store and the transform module.
"""

from __future__ import annotations

import pytest
from orvex.transform.clickhouse import (
    Column,
    SourceError,
    extraction_query,
    load_columns,
    since_type,
    since_value,
)

# --- `{since:…}` values (no server) ---------------------------------------------------------


def kind(text: str) -> object:
    found = since_type(text)
    assert found is not None
    return found


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("Date", "2024-03-01"),
        ("Date32", "2024-03-01"),
        ("DateTime", "2024-03-01 07:50:00"),  # the server's zone: America/La_Paz (UTC-4)
        ("DateTime('UTC')", "2024-03-01 11:50:00"),
        ("DateTime64(3)", "2024-03-01 07:50:00.123"),
        ("DateTime64(6, 'UTC')", "2024-03-01 11:50:00.123456"),
    ],
)
def test_since_for_each_declared_type(declared: str, expected: str) -> None:
    # Watermark 12:00:00.1234567 UTC is stored with microseconds; minus a 10m lookback.
    watermark = "2024-03-01T12:00:00.123456+00:00"
    found = since_type(declared)
    assert found is not None
    assert since_value(found, watermark, "10m", "America/La_Paz") == expected


def test_since_truncates_and_never_rounds_up() -> None:
    found = since_type("DateTime64(2, 'UTC')")
    assert found is not None
    assert since_value(found, "2024-03-01T12:00:00.999999+00:00", None, "UTC") == (
        "2024-03-01 12:00:00.99"
    )
    seconds = since_type("DateTime('UTC')")
    assert seconds is not None
    assert since_value(seconds, "2024-03-01T12:00:00.999999+00:00", None, "UTC") == (
        "2024-03-01 12:00:00"
    )
    # Dates: the date of the instant in the parameter's zone (UTC-4 is the previous day).
    day = since_type("Date")
    assert day is not None
    assert since_value(day, "2024-03-01T02:00:00+00:00", None, "America/La_Paz") == "2024-02-29"


def test_since_on_first_runs_and_below_the_range_is_the_lowest_value() -> None:
    for declared, lowest in [
        ("Date", "1970-01-01"),
        ("Date32", "1900-01-01"),
        ("DateTime", "1970-01-01 00:00:00"),
        ("DateTime64(6)", "1900-01-01 00:00:00"),
    ]:
        found = since_type(declared)
        assert found is not None
        assert since_value(found, None, "1h", "UTC") == lowest
        assert since_value(found, "-infinity", None, "UTC") == lowest
        assert since_value(found, "1850-01-01T00:00:00+00:00", None, "UTC") == lowest


def test_a_date_watermark_starts_at_midnight_in_the_parameter_zone() -> None:
    found = since_type("DateTime('UTC')")
    assert found is not None
    assert since_value(found, "2024-03-01", "1d", "UTC") == "2024-02-29 00:00:00"


def test_an_infinite_watermark_is_refused() -> None:
    found = since_type("DateTime('UTC')")
    assert found is not None
    with pytest.raises(SourceError) as refused:
        since_value(found, "infinity", None, "UTC")
    assert refused.value.code == "UNSUPPORTED_SOURCE_VALUE"


def test_the_query_wraps_the_sql_unchanged() -> None:
    from orvex.transform.clickhouse import map_type

    def column(name: str, text: str) -> Column:
        mapped = map_type(text)
        assert mapped is not None
        return Column(name, text, mapped)

    sql = "SELECT 1 -- a comment at the end"
    columns = (column("a`b", "Int128"), column("t", "Nullable(String)"))
    query = extraction_query(sql, columns)
    assert f"FROM (\n{sql}\n) AS `__source`" in query
    assert "toString(`__source`.`a\\`b`) AS `a\\`b`" in query
    assert "isValidUTF8(`__source`.`t`)" in query
    assert load_columns(columns) == [
        ("a`b", "HUGEINT"),
        ("t", "VARCHAR"),
        ("__row", "UBIGINT"),
        ("__invalid", "VARCHAR"),
    ]
    clashing = (column("__row", "Int32"), column("__invalid_", "String"))
    assert [name for name, _ in load_columns(clashing)][2:] == ["__row_", "__invalid"]


def test_the_lookback_is_subtracted_from_the_instant_across_daylight_saving() -> None:
    found = since_type("DateTime64(0)")
    assert found is not None
    # Spring forward (New York, 2024-03-10 02:00 EST → 03:00 EDT). 07:15 UTC is 03:15 EDT; two
    # hours earlier is 05:15 UTC, 00:15 EST. Wall-clock arithmetic would give 01:15 EST: a
    # lookback of one hour instead of two.
    assert since_value(found, "2024-03-10T07:15:00+00:00", "2h", "America/New_York") == (
        "2024-03-10 00:15:00"
    )
    # Fall back (2024-11-03 02:00 EDT → 01:00 EST). 06:30 UTC is 01:30 EST; one hour earlier is
    # 05:30 UTC, 01:30 EDT. The text is ambiguous; ClickHouse reads it as the earlier instant
    # (measured on 26.9), so the window is never shorter than the lookback.
    assert since_value(found, "2024-11-03T06:30:00+00:00", "1h", "America/New_York") == (
        "2024-11-03 01:30:00"
    )


def test_the_stream_schema_must_be_the_validated_one() -> None:
    import pyarrow as pa
    from orvex.transform.clickhouse import _schema_problem, map_type

    def column(name: str, text: str) -> Column:
        mapped = map_type(text)
        assert mapped is not None
        return Column(name, text, mapped)

    columns = (
        column("a", "Int32"),
        column("big", "Int128"),
        column("t", "DateTime64(3, 'UTC')"),
        column("d", "Decimal(18, 4)"),
    )
    sent = pa.schema(
        [
            ("a", pa.int32()),
            ("big", pa.string()),
            ("t", pa.timestamp("ms", tz="UTC")),
            ("d", pa.decimal128(18, 4)),
            ("__row", pa.uint64()),
            ("__invalid", pa.string()),
        ]
    )
    assert _schema_problem(sent, columns) is None
    widened = sent.set(0, pa.field("a", pa.int64()))
    assert _schema_problem(widened, columns) == "a arrives as int64, validated as Int32"
    rescaled = sent.set(3, pa.field("d", pa.decimal128(18, 2)))
    assert "d arrives as decimal128(18, 2)" in str(_schema_problem(rescaled, columns))
    renamed = sent.set(1, pa.field("other", pa.string()))
    assert _schema_problem(renamed, columns) == "columns a, other, t, d, __row, __invalid"


def test_the_bound_is_the_value_clickhouse_filters_by() -> None:
    from orvex.transform.clickhouse import since_bound

    def bound(declared: str, text: str, server: str) -> tuple[str, str]:
        found = since_type(declared)
        assert found is not None
        return since_bound(found, text, server)

    # Truncated to whole seconds, as ClickHouse received it: not the watermark's .500.
    assert bound("DateTime64(0, 'UTC')", "2024-01-20 10:00:00", "America/La_Paz") == (
        "2024-01-20T10:00:00+00:00",
        "2024-01-20",
    )
    # A parameter without a zone is in the server's; a date is midnight there.
    assert bound("DateTime", "2024-03-01 07:50:00", "America/La_Paz") == (
        "2024-03-01T11:50:00+00:00",
        "2024-03-01",
    )
    assert bound("Date", "2024-03-01", "America/La_Paz") == (
        "2024-03-01T04:00:00+00:00",
        "2024-03-01",
    )
    assert bound("DateTime64(3)", "2024-03-01 07:50:00.123", "UTC")[0] == (
        "2024-03-01T07:50:00.123000+00:00"
    )
    # An ambiguous autumn time is the earlier instant, as ClickHouse reads it (EDT, -04:00).
    assert bound("DateTime", "2024-11-03 01:30:00", "America/New_York")[0] == (
        "2024-11-03T05:30:00+00:00"
    )


def test_a_date_since_is_midnight_in_the_time_columns_zone() -> None:
    from orvex.transform.clickhouse import date_zone, map_type, since_bound

    day = since_type("Date")
    assert day is not None
    # Server in La Paz (UTC-4), column in UTC: the date is taken, and compared, in UTC.
    assert since_value(day, "2024-02-27T02:00:00+00:00", None, "America/La_Paz", "UTC") == (
        "2024-02-27"
    )
    assert since_bound(day, "2024-02-27", "America/La_Paz", "UTC")[0] == (
        "2024-02-27T00:00:00+00:00"
    )
    # Column in Los Angeles (UTC-8): 06:00 UTC is still 1 March there, so no later row is
    # skipped (the server's zone would give 2 March, read as 08:00 UTC).
    assert since_value(
        day, "2024-03-02T06:00:00+00:00", None, "America/La_Paz", "America/Los_Angeles"
    ) == ("2024-03-01")
    mapped = map_type("DateTime64(3, 'Asia/Tokyo')")
    assert mapped is not None
    columns = (Column("event_time", "DateTime64(3, 'Asia/Tokyo')", mapped),)
    assert date_zone(columns, "EVENT_TIME") == "Asia/Tokyo"
    assert date_zone(columns, None) is None
    plain = map_type("DateTime")
    assert plain is not None
    assert date_zone((Column("t", "DateTime", plain),), "t") is None  # the server's
