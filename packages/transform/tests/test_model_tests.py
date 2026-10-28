"""P1-15: model tests — names, validation rules and the failing-row counts, in plain DuckDB."""

from __future__ import annotations

from pathlib import Path

import duckdb
from orvex.transform.model_tests import name_of, problems, steps
from orvex.transform.models import Model, discover_models

READINGS = [
    ("reading_id", "BIGINT"),
    ("consumer_id", "VARCHAR"),
    ("kwh", "DECIMAL(18,4)"),
    ("read_at", "TIMESTAMP WITH TIME ZONE"),
    ("status", "VARCHAR"),
    ("small", "TINYINT"),
    ("wide", "DECIMAL(38,25)"),
]


def model(tmp_path: Path, tests: str) -> Model:
    folder = tmp_path / "models" / "s"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "m.sql").write_text("SELECT 1 AS reading_id", encoding="utf-8")
    (folder / "m.yaml").write_text(f"version: 1\ntests:\n{tests}", encoding="utf-8")
    found = discover_models(tmp_path, {})
    assert found.problems == (), found.problems
    return found.models[0]


def counts(chosen: Model, connection: duckdb.DuckDBPyConnection) -> list[tuple[str, int]]:
    """Each test's name and failing rows, binding parameters as the store does (typed)."""
    found = []
    for test in steps(chosen, '"s"."m"', READINGS, {"s.c": '"s"."c"', "s.m": '"s"."m"'}):
        sql = test.request.sql
        for parameter in test.request.parameters:
            sql = sql.replace(
                f"${parameter.name}", f"CAST(${parameter.name} AS {parameter.type.root})"
            )
        values = {parameter.name: parameter.value for parameter in test.request.parameters}
        found.append((test.name, connection.execute(sql, values).fetchall()[0][0]))
    return found


WIDE = "9999999999999.9999999999999999999999999"  # the largest DECIMAL(38,25)


def lake() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    connection.execute("CREATE SCHEMA s")
    connection.execute(
        "CREATE TABLE s.m (reading_id BIGINT, consumer_id VARCHAR, kwh DECIMAL(18,4), "
        "read_at TIMESTAMP, status VARCHAR, small TINYINT, wide DECIMAL(38,25))"
    )
    connection.execute("CREATE TABLE s.c (consumer_id VARCHAR)")
    connection.execute("INSERT INTO s.c VALUES ('C1'), ('C2')")
    connection.execute(
        "INSERT INTO s.m VALUES "
        f"(1, 'C1', -0.0001, '2023-12-31 23:59:59', 'valid', 1, '{WIDE}'), "
        "(2, NULL, 0, '2024-01-01', 'estimated', 2, 0), "
        f"(2, 'C9', 99999999999999.9999, NULL, 'voided', 3, '-{WIDE}'), "
        "(3, NULL, 0.1, '2024-02-01', NULL, NULL, 1), "
        "(4, 'C2', NULL, '2024-03-01', 'valid', 127, NULL)"
    )
    return connection


def test_each_kind_counts_its_failing_rows(tmp_path: Path) -> None:
    chosen = model(
        tmp_path,
        "  - unique: [reading_id]\n"
        "  - not_null: [consumer_id, read_at]\n"
        "  - accepted_range: {column: kwh, min: 0, max: 99999999999999.9999}\n"
        "  - accepted_values: {column: status, values: [valid, estimated]}\n"
        "  - relationship: {column: consumer_id, to: s.c, field: consumer_id}\n",
    )
    # The timestamps are plain TIMESTAMP in this local table; the test types do not matter here.
    assert counts(chosen, lake()) == [
        ("unique(reading_id)", 2),
        ("not_null(consumer_id, read_at)", 3),
        ("accepted_range(kwh)", 1),
        ("accepted_values(status)", 1),
        ("relationship(consumer_id → s.c.consumer_id)", 1),
    ]


def test_bounds_are_exact_and_never_floats(tmp_path: Path) -> None:
    chosen = model(
        tmp_path,
        "  - accepted_range: {column: kwh, min: 0.1}\n"  # 0.1 as a float would not equal 0.1
        "  - accepted_range: {column: kwh, max: '99999999999999.9998'}\n"
        "  - accepted_range: {column: kwh, min: 0.00005}\n"  # finer than the column: rounds up
        "  - accepted_range: {column: small, min: 1.5, max: 1000}\n"  # beyond TINYINT: no limit
        "  - accepted_range: {column: small, min: 200}\n",  # above TINYINT: every row fails
    )
    assert [count for _, count in counts(chosen, lake())] == [2, 1, 2, 1, 4]


def test_temporal_bounds_are_quoted_and_cast(tmp_path: Path) -> None:
    chosen = model(
        tmp_path, "  - accepted_range: {column: read_at, min: '2024-01-01 00:00:00+00'}\n"
    )
    (test,) = steps(chosen, '"s"."m"', READINGS, {})
    assert "CAST($p_min AS TIMESTAMP WITH TIME ZONE)" in test.request.sql
    assert [(p.name, p.type.root) for p in test.request.parameters] == [("p_min", "VARCHAR")]


def test_values_travel_as_parameters(tmp_path: Path) -> None:
    chosen = model(
        tmp_path,
        '  - accepted_values: {column: status, values: ["x\' OR 1=1 --"]}\n'
        "  - accepted_range: {column: kwh, min: 5}\n",
    )
    for test in steps(chosen, '"s"."m"', READINGS, {}):
        assert "OR 1=1" not in test.request.sql
        assert "5" not in test.request.sql.replace("$p_", "")
    assert counts(chosen, lake())[0] == ("accepted_values(status)", 4)


def test_rules_need_the_columns(tmp_path: Path) -> None:
    chosen = model(
        tmp_path,
        "  - unique: [nothing]\n"
        "  - accepted_range: {column: status, min: 0}\n"
        "  - accepted_range: {column: kwh}\n"
        "  - accepted_range: {column: kwh, min: 1e5}\n"
        "  - accepted_range: {column: read_at, min: 5}\n"
        "  - accepted_values: {column: status, values: [1, valid]}\n"
        "  - accepted_values: {column: small, values: [no]}\n"
        "  - accepted_values: {column: kwh, values: [1]}\n"
        "  - relationship: {column: consumer_id, to: s.c, field: other}\n",
    )
    outputs = {"s.m": READINGS, "s.c": [("consumer_id", "VARCHAR")]}
    found = problems(chosen, READINGS, outputs, {"s.m": chosen, "s.c": chosen})
    assert [(p.code.root, p.details.path) for p in found] == [
        ("MODEL_CONFIG_INVALID", "/tests/0/unique/0"),
        ("MODEL_CONFIG_INVALID", "/tests/1/accepted_range/column"),
        ("MODEL_CONFIG_INVALID", "/tests/2/accepted_range"),
        ("MODEL_CONFIG_INVALID", "/tests/3/accepted_range/min"),
        ("MODEL_CONFIG_INVALID", "/tests/4/accepted_range/min"),
        ("MODEL_CONFIG_INVALID", "/tests/5/accepted_values/values/0"),
        ("MODEL_CONFIG_INVALID", "/tests/6/accepted_values/values/0"),
        ("MODEL_CONFIG_INVALID", "/tests/7/accepted_values/column"),
        ("MODEL_CONFIG_INVALID", "/tests/8/relationship/field"),
    ]


def test_names(tmp_path: Path) -> None:
    chosen = model(
        tmp_path,
        "  - not_null: [reading_id, consumer_id]\n"
        "  - relationship: {column: consumer_id, to: staging.consumers, field: consumer_id}\n",
    )
    assert [name_of(test) for test in chosen.config.tests or []] == [
        "not_null(reading_id, consumer_id)",
        "relationship(consumer_id → staging.consumers.consumer_id)",
    ]


def test_long_bounds_on_wide_decimals_stay_exact(tmp_path: Path) -> None:
    chosen = model(
        tmp_path,
        "  - accepted_range: {column: wide, max: 1000000000000}\n"  # 25-digit scale: exact
        "  - accepted_range: {column: kwh, max: 1000000000000000000000000}\n"
        # The column's largest value is still inside; one unit of scale less is not.
        "  - accepted_range: {column: wide, max: '9999999999999.9999999999999999999999999'}\n"
        "  - accepted_range: {column: wide, max: '9999999999999.9999999999999999999999998'}\n"
        "  - accepted_range: {column: wide, min: '-9999999999999.9999999999999999999999998'}\n",
    )
    assert problems(chosen, READINGS, {"s.m": READINGS}, {"s.m": chosen}) == []
    assert [count for _, count in counts(chosen, lake())] == [1, 0, 0, 1, 1]


def test_bounds_must_be_unambiguous(tmp_path: Path) -> None:
    chosen = model(
        tmp_path,
        "  - accepted_range: {column: kwh, min: 017}\n"
        "  - accepted_range: {column: read_at, min: 'not a date'}\n"
        "  - accepted_range: {column: read_at, min: '2024-01-01 00:00:00'}\n"
        "  - accepted_range: {column: read_at, min: '2024-01-01T00:00:00+00:00'}\n",
    )
    found = problems(chosen, READINGS, {"s.m": READINGS}, {"s.m": chosen})
    assert [p.details.path for p in found] == [
        "/tests/0/accepted_range/min",
        "/tests/1/accepted_range/min",
        "/tests/2/accepted_range/min",
    ]
