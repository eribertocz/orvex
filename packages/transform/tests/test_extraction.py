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
