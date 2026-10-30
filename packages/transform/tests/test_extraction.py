"""P1-17 part 2 (#98): the `{since:…}` value and the query sent to ClickHouse, without a server.

The streaming itself is tested against a live ClickHouse by the server package
(`test_extraction_stream.py`), which may compose the store and the transform module.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from orvex.transform.clickhouse import (
    Column,
    SourceError,
    extraction_query,
    load_columns,
    since_bound,
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
        # A date is taken in UTC-12 (where midnight comes last): 11:50 UTC is still 29 February.
        ("Date", "2024-02-29"),
        ("Date32", "2024-02-29"),
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
    for declared, lowest, below in [
        ("Date", "1970-01-01", "1969-12-31T00:00:00+00:00"),
        ("Date32", "0000-01-01", None),
        ("DateTime", "1900-01-01 00:00:00", "1969-12-31T23:59:59+00:00"),
        ("DateTime64(6)", "-62167219200", None),
        ("DateTime64(9)", "-9223372036.854775808", "1677-09-21T00:12:43.145224+00:00"),
    ]:
        found = since_type(declared)
        assert found is not None
        assert since_value(found, None, "1h", "UTC") == lowest
        assert since_value(found, "-infinity", None, "UTC") == lowest
        if below is not None:
            assert since_value(found, below, None, "UTC") == lowest
        # Before Python's year 1 (a lookback past it): every type's first value is earlier.
        assert since_value(found, "0001-01-01T00:30:00+00:00", "1h", "UTC") == lowest
        # A first run's boundary for the checks: every row was read.
        assert since_bound(found, lowest, "UTC") == ("-infinity", "-infinity")


def test_since_keeps_values_before_1970_and_before_1000() -> None:
    # #179: a time west of UTC on 1970-01-01 is still in the DateTime range (epoch 0 is
    # 1969-12-31 19:00 in Lima), and a year before 1000 is written in four digits.
    seconds = since_type("DateTime('America/Lima')")
    assert seconds is not None
    assert since_value(seconds, "1970-01-01T01:00:00+00:00", None, "UTC") == "1969-12-31 20:00:00"
    millis = since_type("DateTime64(3, 'UTC')")
    assert millis is not None
    assert since_value(millis, "0850-03-01T12:00:00+00:00", None, "UTC") == (
        "0850-03-01 12:00:00.000"
    )
    days = since_type("Date32")
    assert days is not None
    assert since_value(days, "0850-03-01", None, "UTC") == "0850-03-01"


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
    # A date's boundary is its earliest midnight (UTC+14), whatever the server's zone.
    assert bound("Date", "2024-03-01", "America/La_Paz") == (
        "2024-02-29T10:00:00+00:00",
        "2024-03-01",
    )
    assert bound("DateTime64(3)", "2024-03-01 07:50:00.123", "UTC")[0] == (
        "2024-03-01T07:50:00.123000+00:00"
    )
    # An ambiguous autumn time is the earlier instant, as ClickHouse reads it (EDT, -04:00).
    assert bound("DateTime", "2024-11-03 01:30:00", "America/New_York")[0] == (
        "2024-11-03T05:30:00+00:00"
    )


def test_a_date_since_holds_in_every_zone() -> None:
    from orvex.transform.clickhouse import since_bound

    # ClickHouse compares a date at midnight in the zone of the expression it meets, which the
    # model's SQL decides. The date is taken where midnight comes last (UTC-12), so its midnight
    # anywhere is no later than the watermark; the boundary for checks is where it comes first
    # (UTC+14), no later than ClickHouse's own.
    watermark = dt.datetime(2024, 3, 2, 6, tzinfo=dt.UTC)
    for declared in ("Date", "Date32"):
        found = since_type(declared)
        assert found is not None
        text = since_value(found, watermark.isoformat(), None, "Asia/Tokyo")
        assert text == "2024-03-01"
        instant, day = since_bound(found, text, "Asia/Tokyo")
        assert (instant, day) == ("2024-02-29T10:00:00+00:00", "2024-03-01")
        for zone in (
            "Etc/GMT+12",
            "America/Los_Angeles",
            "UTC",
            "Asia/Tokyo",
            "Pacific/Kiritimati",
        ):
            midnight = dt.datetime(2024, 3, 1, tzinfo=ZoneInfo(zone))
            assert dt.datetime.fromisoformat(instant) <= midnight <= watermark, zone


def test_a_date_boundary_uses_a_fixed_offset_in_every_year() -> None:
    from orvex.transform.clickhouse import since_bound

    found = since_type("Date32")
    assert found is not None
    # UTC+14 whatever the year: Pacific/Kiritimati itself was UTC-10:40 before 1995.
    assert since_bound(found, "1994-06-01", "UTC")[0] == "1994-05-31T10:00:00+00:00"
    assert since_bound(found, "1960-06-01", "UTC")[0] == "1960-05-31T10:00:00+00:00"


def test_a_date_since_is_refused_for_partition_overwrite_on_a_datetime_column(
    tmp_path: Path,
) -> None:
    from orvex.contracts.project.v1 import ClickhouseConnection
    from orvex.transform.clickhouse import map_type, since_column_problems
    from orvex.transform.models import discover_models

    connection = ClickhouseConnection.model_validate(
        {
            "type": "clickhouse",
            "host": "h",
            "port": 8123,
            "database": "d",
            "user": "u",
            "password": "${env:UNUSED}",
            "read_only": True,
        }
    )

    def problems(strategy: str, declared: str, column_type: str) -> list[str]:
        folder = tmp_path / f"{strategy}-{declared}-{column_type}".replace("(", "").replace(")", "")
        (folder / "models" / "raw").mkdir(parents=True)
        (folder / "models" / "raw" / "e.sql").write_text(
            f"SELECT id, month, t FROM events WHERE t > {{since:{declared}}}", encoding="utf-8"
        )
        extra = (
            "  partition_by: [month]\n"
            if strategy == "partition_overwrite"
            else ("  unique_key: [id]\n")
        )
        (folder / "models" / "raw" / "e.yaml").write_text(
            "version: 1\nconnection: ch\nmaterialized: incremental\nincremental:\n"
            f"  strategy: {strategy}\n  time_column: t\n{extra}  filter: manual\n",
            encoding="utf-8",
        )
        found = discover_models(folder, {"ch": connection})
        assert found.problems == ()
        mapped = map_type(column_type)
        assert mapped is not None
        columns = (Column("t", column_type, mapped),)
        return [item.code.root for item in since_column_problems(found.models[0], columns)]

    assert problems("partition_overwrite", "Date", "DateTime64(3, 'UTC')") == ["INVALID_MODEL_SQL"]
    assert problems("partition_overwrite", "Date32", "DateTime") == ["INVALID_MODEL_SQL"]
    # Exact boundaries: a date column, or a time parameter.
    assert problems("partition_overwrite", "Date", "Date") == []
    assert problems("partition_overwrite", "DateTime64(3, 'UTC')", "DateTime64(3, 'UTC')") == []
    # Only the partition check needs the exact boundary.
    assert problems("merge", "Date", "DateTime64(3, 'UTC')") == []


@pytest.mark.parametrize(
    ("strategy", "declared", "column_type", "refused"),
    [
        ("append", "Date", "DateTime('UTC')", True),
        ("append", "Date32", "DateTime64(3, 'UTC')", True),
        ("append", "DateTime('UTC')", "DateTime64(0, 'UTC')", True),
        ("append", "DateTime64(0, 'UTC')", "DateTime64(3, 'UTC')", True),
        ("append", "DateTime64(3, 'UTC')", "Nullable(DateTime64(6, 'UTC'))", True),
        ("append", "Date", "Date", False),
        ("append", "Date", "Date32", False),
        # A date column takes a date parameter only: ClickHouse compares Date32 with a time at
        # midnight in the server's zone (#135, Reviewer pass 1).
        ("append", "DateTime('UTC')", "Date", True),
        ("append", "DateTime('UTC')", "Date32", True),
        ("append", "DateTime64(3, 'UTC')", "Date32", True),
        ("append", "Date32", "Date", False),
        ("append", "DateTime('UTC')", "DateTime('UTC')", False),
        ("append", "DateTime64(0, 'UTC')", "DateTime('UTC')", False),
        ("append", "DateTime64(3, 'UTC')", "DateTime64(3, 'UTC')", False),
        ("append", "DateTime64(6, 'UTC')", "DateTime64(3, 'UTC')", False),
        ("append", "DateTime64(6, 'UTC')", "Nullable(DateTime('UTC'))", False),
        # Strategies that replace re-read rows may read them again.
        ("merge", "Date", "DateTime('UTC')", False),
    ],
)
def test_an_append_since_must_be_as_precise_as_the_time_column(
    tmp_path: Path, strategy: str, declared: str, column_type: str, refused: bool
) -> None:
    # #135: a coarser `{since:…}` is truncated, so an append model would read again, and insert
    # twice, the rows between the truncated value and the watermark.
    from orvex.contracts.project.v1 import ClickhouseConnection
    from orvex.transform.clickhouse import map_type, since_precision_problems
    from orvex.transform.models import discover_models

    connection = ClickhouseConnection.model_validate(
        {
            "type": "clickhouse",
            "host": "h",
            "port": 8123,
            "database": "d",
            "user": "u",
            "password": "${env:UNUSED}",
            "read_only": True,
        }
    )
    (tmp_path / "models" / "raw").mkdir(parents=True)
    (tmp_path / "models" / "raw" / "e.sql").write_text(
        f"SELECT id, t FROM events WHERE t > {{since:{declared}}}", encoding="utf-8"
    )
    unique = "  unique_key: [id]\n" if strategy == "merge" else ""
    (tmp_path / "models" / "raw" / "e.yaml").write_text(
        "version: 1\nconnection: ch\nmaterialized: incremental\nincremental:\n"
        f"  strategy: {strategy}\n  time_column: t\n{unique}  filter: manual\n",
        encoding="utf-8",
    )
    found = discover_models(tmp_path, {"ch": connection})
    assert found.problems == ()
    mapped = map_type(column_type)
    assert mapped is not None
    problems = since_precision_problems(found.models[0], (Column("t", column_type, mapped),))
    assert [item.code.root for item in problems] == (["INVALID_MODEL_SQL"] if refused else [])
    if refused:
        assert "does not match the time_column t" in problems[0].message


@pytest.mark.parametrize(
    ("strategy", "declared", "column_type", "refused"),
    [
        ("merge", "Date", "Date32", True),
        ("append", "Date", "Date32", True),
        ("merge", "DateTime('UTC')", "DateTime64(3, 'UTC')", True),
        ("append", "DateTime('UTC')", "DateTime64(3, 'UTC')", True),
        ("merge", "DateTime('UTC')", "Date32", True),
        ("merge", "Date", "Nullable(DateTime64(6, 'UTC'))", True),
        # A date compared with a time at midnight in a zone west of UTC starts too late.
        ("merge", "Date", "DateTime", True),
        ("merge", "Date", "Nullable(DateTime('America/Lima'))", True),
        ("merge", "Date32", "DateTime64(3, 'UTC')", True),
        ("merge", "Date32", "DateTime64(6)", True),
        # Nanoseconds and other precisions cannot be compared at year 0 (a time column holds
        # at most microseconds, so a `{since:DateTime64(9)}` needs a date or seconds column).
        ("merge", "DateTime64(9, 'UTC')", "DateTime64(6, 'UTC')", True),
        ("merge", "Date32", "Date32", False),
        ("merge", "DateTime64(3, 'UTC')", "DateTime64(3, 'UTC')", False),
        ("merge", "DateTime64(6, 'America/Lima')", "DateTime64(3, 'Etc/GMT-14')", False),
        ("merge", "Date", "Date", False),
        ("merge", "DateTime('UTC')", "DateTime('UTC')", False),
        ("merge", "DateTime('America/Lima')", "DateTime", False),
        ("merge", "DateTime", "Date", False),
        ("merge", "Date32", "DateTime('America/Lima')", False),
        ("merge", "DateTime64(0, 'UTC')", "Date32", False),
        ("merge", "DateTime64(9)", "Date32", True),  # 1677 is after year 0
        ("merge", "DateTime64(9, 'UTC')", "DateTime('UTC')", False),
        ("merge", "DateTime64(3)", "Nullable(DateTime)", False),
    ],
)
def test_a_since_that_cannot_start_at_the_columns_first_value_is_refused(
    tmp_path: Path, strategy: str, declared: str, column_type: str, refused: bool
) -> None:
    # #179: a first run binds the type's lowest value; where it is later than the column's
    # first value in some zone, or cannot be compared with it, older rows would never be loaded.
    # The table was measured on ClickHouse 26.9 (see `_FIRST_RUN_READS`).
    from orvex.contracts.project.v1 import ClickhouseConnection
    from orvex.transform.clickhouse import map_type, since_range_problems
    from orvex.transform.models import discover_models

    connection = ClickhouseConnection.model_validate(
        {
            "type": "clickhouse",
            "host": "h",
            "port": 8123,
            "database": "d",
            "user": "u",
            "password": "${env:UNUSED}",
            "read_only": True,
        }
    )
    (tmp_path / "models" / "raw").mkdir(parents=True)
    (tmp_path / "models" / "raw" / "e.sql").write_text(
        f"SELECT id, t FROM events WHERE t > {{since:{declared}}}", encoding="utf-8"
    )
    unique = "  unique_key: [id]\n" if strategy == "merge" else ""
    (tmp_path / "models" / "raw" / "e.yaml").write_text(
        "version: 1\nconnection: ch\nmaterialized: incremental\nincremental:\n"
        f"  strategy: {strategy}\n  time_column: t\n{unique}  filter: manual\n",
        encoding="utf-8",
    )
    found = discover_models(tmp_path, {"ch": connection})
    assert found.problems == ()
    mapped = map_type(column_type)
    assert mapped is not None
    problems = since_range_problems(found.models[0], (Column("t", column_type, mapped),))
    assert [item.code.root for item in problems] == (["INVALID_MODEL_SQL"] if refused else [])
    if refused:
        assert "cannot start a first run at the oldest value" in problems[0].message


@pytest.mark.parametrize(
    ("declared", "server", "refused"),
    [
        ("DateTime64(3, 'America/New_York')", "UTC", True),
        ("DateTime", "America/New_York", True),  # no zone declared: the server's
        ("DateTime('America/Lima')", "UTC", True),  # daylight saving time in 1986-1994
        ("DateTime64(3, 'UTC')", "America/New_York", False),
        ("DateTime64(3, 'America/La_Paz')", "UTC", False),
        ("DateTime('Etc/GMT+4')", "UTC", False),
        ("DateTime", "UTC", False),
        ("Date", "America/New_York", False),  # a date is taken in a fixed zone already
        ("Date32", "America/New_York", False),
    ],
)
def test_an_append_since_zone_must_keep_one_offset(
    tmp_path: Path, declared: str, server: str, refused: bool
) -> None:
    # #189: ClickHouse reads a repeated local time as its earlier instant.
    from orvex.contracts.project.v1 import ClickhouseConnection
    from orvex.transform.clickhouse import since_zone_problem
    from orvex.transform.models import discover_models

    connection = ClickhouseConnection.model_validate(
        {
            "type": "clickhouse",
            "host": "h",
            "port": 8123,
            "database": "d",
            "user": "u",
            "password": "${env:UNUSED}",
            "read_only": True,
        }
    )
    (tmp_path / "models" / "raw").mkdir(parents=True)
    (tmp_path / "models" / "raw" / "e.sql").write_text(
        f"SELECT id, t FROM events WHERE t > {{since:{declared}}}", encoding="utf-8"
    )
    (tmp_path / "models" / "raw" / "e.yaml").write_text(
        "version: 1\nconnection: ch\nmaterialized: incremental\nincremental:\n"
        "  strategy: append\n  time_column: t\n  filter: manual\n",
        encoding="utf-8",
    )
    found = discover_models(tmp_path, {"ch": connection})
    assert found.problems == ()
    since = since_type(declared)
    assert since is not None
    result = since_zone_problem(found.models[0], since, server)
    assert (result is not None) is refused
    if result is not None:
        assert result.code.root == "INVALID_MODEL_SQL"
        assert "UTC" in result.message


def test_a_clickhouse_join_is_seen_in_the_tree() -> None:
    from orvex.transform.clickhouse import joins

    joined = """SelectWithUnionQuery (children 1)
 ExpressionList (children 1)
  SelectQuery (children 2)
   ExpressionList (children 1)
    Asterisk
   TablesInSelectQuery (children 2)
    TablesInSelectQueryElement (children 1)
     TableExpression (children 1)
      TableIdentifier a
    TablesInSelectQueryElement (children 2)
     TableExpression (children 1)
      TableIdentifier b
     TableJoin
"""
    plain = """SelectWithUnionQuery (children 1)
 ExpressionList (children 1)
  SelectQuery (children 2)
   ExpressionList (children 1)
    Asterisk
   TablesInSelectQuery (children 1)
    TablesInSelectQueryElement (children 1)
     TableExpression (children 1)
      TableIdentifier a
"""
    assert joins(joined)
    assert not joins(plain)
    assert joins("not a tree")  # unreadable: counts as a join
