"""P1-16: reconciliation — expression checks and failing-group counts, in plain DuckDB."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from orvex.transform.models import Model, discover_models
from orvex.transform.reconciliation import common_type, expression, measures_sql, problems, steps

CONNECTIONS = {"feed": "parquet", "ch": "clickhouse"}
RELATION = '"s"."m"'


def model(tmp_path: Path, checks: str) -> Model:
    folder = tmp_path / "models" / "s"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "m.sql").write_text("SELECT 1 AS id", encoding="utf-8")
    (folder / "m.yaml").write_text(f"version: 1\nreconciliation:\n{checks}", encoding="utf-8")
    found = discover_models(tmp_path, {})
    assert found.problems == (), found.problems
    return found.models[0]


def test_only_single_expressions_reach_the_query() -> None:
    assert expression("sum(kwh)") == "sum(kwh)"
    assert expression("count(*)") == "count_star()"
    assert expression("date_trunc('month', read_at)") == "date_trunc('month', read_at)"
    for refused in [
        "sum(kwh)) FROM x --",
        "(SELECT 1)",
        "sum(kwh) OVER ()",
        "$p",
        "sum(kwh), 1",
        "sum(kwh) AS total",
        "sum(other.kwh)",
        "exists (SELECT 1)",
        "*",
        "",
    ]:
        assert expression(refused) is None, refused


def test_static_rules(tmp_path: Path) -> None:
    chosen = model(
        tmp_path,
        "  - {name: a, measure: 'sum(kwh)) FROM x --', source: feed.data}\n"
        "  - {name: a, measure: '(SELECT 1)', source: feed.data}\n"
        "  - {name: b, measure: count(*), group_by: ['1, 2'], source: feed.data}\n"
        "  - {name: c, measure: count(*), source: nowhere.t}\n"
        "  - {name: d, measure: count(*), source: ch.events}\n"
        "  - {name: e, measure: count(*), source: feed.data, tolerance: -1}\n"
        "  - {name: f, measure: count(*), source: feed.data, tolerance: 1e5}\n"
        "  - {name: g, measure: count(*), source: feed.data, tolerance: 0.0001}\n",
    )
    found = problems(chosen, CONNECTIONS)
    assert [(p.code.root, p.details.path) for p in found] == [
        ("MODEL_CONFIG_INVALID", "/reconciliation/0/measure"),
        ("MODEL_CONFIG_INVALID", "/reconciliation/1/name"),
        ("MODEL_CONFIG_INVALID", "/reconciliation/1/measure"),
        ("MODEL_CONFIG_INVALID", "/reconciliation/2/group_by/0"),
        ("MODEL_REFERENCE_UNKNOWN", "/reconciliation/3/source"),
        ("MODEL_REFERENCE_UNKNOWN", "/reconciliation/4/source"),
        ("MODEL_CONFIG_INVALID", "/reconciliation/5/tolerance"),
        ("MODEL_CONFIG_INVALID", "/reconciliation/6/tolerance"),
    ]


def lake() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    for schema in ("s", "feed"):
        connection.execute(f"CREATE SCHEMA {schema}")
    rows = (
        "('2024-01-05'::DATE, 1.0000), ('2024-01-20'::DATE, 2.0001), "
        "('2024-02-01'::DATE, 3.0000), (NULL, 4.0000), ('2024-03-01'::DATE, 5.0000)"
    )
    connection.execute(
        "CREATE TABLE feed.data AS SELECT d AS read_at, CAST(k AS DECIMAL(18,4)) AS kwh "
        f"FROM (VALUES {rows}) AS t(d, k)"
    )
    return connection


def failing(chosen: Model, connection: duckdb.DuckDBPyConnection) -> list[tuple[str, int]]:
    differences = []
    for check in chosen.config.reconciliation or []:
        rows = connection.execute(f"DESCRIBE {measures_sql(check, RELATION)}").fetchall()
        differences.append(common_type(str(rows[0][1]), str(rows[1][1])))
        assert all(rows[i][1] == rows[i + 1][1] for i in range(2, len(rows), 2))
    found = []
    for planned in steps(chosen, RELATION, differences):
        sql = planned.request.sql
        for parameter in planned.request.parameters:
            sql = sql.replace(
                f"${parameter.name}", f"CAST(${parameter.name} AS {parameter.type.root})"
            )
        values = {parameter.name: parameter.value for parameter in planned.request.parameters}
        found.append((planned.name, connection.execute(sql, values).fetchall()[0][0]))
    return found


MONTHLY = "measure: sum(kwh), group_by: [\"date_trunc('month', read_at)\"], source: feed.data"


def test_groups_compare_exactly_with_the_tolerance(tmp_path: Path) -> None:
    chosen = model(
        tmp_path,
        f"  - {{name: exact, {MONTHLY}}}\n"
        f"  - {{name: loose, {MONTHLY}, tolerance: 0.0001}}\n"
        f"  - {{name: finer, {MONTHLY}, tolerance: 0.00009}}\n"
        "  - {name: total, measure: count(*), source: feed.data}\n",
    )
    connection = lake()
    connection.execute("CREATE TABLE s.m AS SELECT * FROM feed.data")
    assert failing(chosen, connection) == [("exact", 0), ("loose", 0), ("finer", 0), ("total", 0)]
    # January off by 0.0001.
    connection.execute("UPDATE s.m SET kwh = 2.0000 WHERE kwh = 2.0001")
    assert failing(chosen, connection) == [("exact", 1), ("loose", 0), ("finer", 1), ("total", 0)]


def test_missing_groups_and_null_groups(tmp_path: Path) -> None:
    chosen = model(tmp_path, f"  - {{name: monthly, {MONTHLY}}}\n")
    connection = lake()
    # March missing from the model; the NULL month exists on both sides with equal sums.
    connection.execute(
        "CREATE TABLE s.m AS SELECT * FROM feed.data WHERE read_at IS DISTINCT FROM '2024-03-01'"
    )
    assert failing(chosen, connection) == [("monthly", 1)]
    connection.execute("INSERT INTO s.m VALUES ('2024-03-01', 5.0000)")
    assert failing(chosen, connection) == [("monthly", 0)]
    # The NULL month now exists only in the source.
    connection.execute("DELETE FROM s.m WHERE read_at IS NULL")
    assert failing(chosen, connection) == [("monthly", 1)]


def test_values_travel_as_parameters(tmp_path: Path) -> None:
    chosen = model(
        tmp_path, "  - {name: total, measure: count(*), source: feed.data, tolerance: 7}\n"
    )
    connection = lake()
    connection.execute("CREATE TABLE s.m AS SELECT * FROM feed.data")
    (check,) = steps(chosen, '"s"."m"', ["HUGEINT"])
    assert "7" not in check.request.sql
    assert [(p.name, p.type.root, p.value) for p in check.request.parameters] == [
        ("p_tolerance", "HUGEINT", "7")
    ]


def test_measures_must_aggregate_and_groups_must_not(tmp_path: Path) -> None:
    connection = lake()
    connection.execute("CREATE TABLE s.m AS SELECT * FROM feed.data")
    for checks in [
        "  - {name: raw, measure: kwh, source: feed.data}\n",
        "  - {name: raw, measure: kwh, group_by: [read_at], source: feed.data}\n",
        "  - {name: agg, measure: count(*), group_by: ['sum(kwh)'], source: feed.data}\n",
    ]:
        chosen = model(tmp_path / str(len(checks)), checks)
        (check,) = chosen.config.reconciliation or []
        with pytest.raises(duckdb.BinderException):
            connection.execute(f"DESCRIBE {measures_sql(check, RELATION)}")


def test_unsigned_measures_compare_both_ways(tmp_path: Path) -> None:
    chosen = model(tmp_path, "  - {name: top, measure: max(id), source: feed.ids}\n")
    connection = lake()
    connection.execute("CREATE TABLE feed.ids AS SELECT CAST(7 AS UBIGINT) AS id")
    connection.execute("CREATE TABLE s.m AS SELECT CAST(6 AS UBIGINT) AS id")
    assert failing(chosen, connection) == [("top", 1)]
    connection.execute("UPDATE s.m SET id = 8")
    assert failing(chosen, connection) == [("top", 1)]
    connection.execute("UPDATE s.m SET id = 7")
    assert failing(chosen, connection) == [("top", 0)]


def test_differences_never_overflow(tmp_path: Path) -> None:
    connection = lake()
    for kind, low, high in [
        ("TINYINT", "-100", "100"),
        ("BIGINT", "-9000000000000000000", "9000000000000000000"),
        (
            "HUGEINT",
            "-170141183460469231731687303715884105727",
            "170141183460469231731687303715884105727",
        ),
        (
            "DECIMAL(38,0)",
            "-99999999999999999999999999999999999999",
            "99999999999999999999999999999999999999",
        ),
    ]:
        chosen = model(tmp_path / kind[:5], "  - {name: top, measure: max(v), source: feed.v}\n")
        connection.execute(
            f"CREATE OR REPLACE TABLE feed.v AS SELECT CAST('{high}' AS {kind}) AS v"
        )
        connection.execute(f"CREATE OR REPLACE TABLE s.m AS SELECT CAST('{low}' AS {kind}) AS v")
        assert failing(chosen, connection) == [("top", 1)], kind
        connection.execute("CREATE OR REPLACE TABLE s.m AS SELECT * FROM feed.v")
        assert failing(chosen, connection) == [("top", 0)], kind


def test_measures_of_different_scales_are_not_rounded(tmp_path: Path) -> None:
    connection = lake()
    for model_type, model_value, source_type, source_value, tolerance, expected in [
        ("DECIMAL(18,4)", "1.0049", "DECIMAL(18,2)", "1.00", "0", 1),
        ("DECIMAL(18,4)", "1.0040", "DECIMAL(18,2)", "1.00", "0.001", 1),
        ("DECIMAL(18,4)", "1.0040", "DECIMAL(18,2)", "1.00", "0.004", 0),
        ("DECIMAL(38,4)", "1.4000", "DECIMAL(38,0)", "1", "0", 1),
        ("HUGEINT", "-1", "UHUGEINT", "340282366920938463463374607431768211455", "0", 1),
        ("BIGINT", "5", "UBIGINT", "5", "0", 0),
    ]:
        folder = tmp_path / f"{model_type}{source_type}{tolerance}".replace("(", "").replace(
            ")", ""
        )
        chosen = model(
            folder,
            f"  - {{name: top, measure: max(v), source: feed.v, tolerance: '{tolerance}'}}\n",
        )
        connection.execute(
            f"CREATE OR REPLACE TABLE feed.v AS SELECT CAST('{source_value}' AS {source_type}) AS v"
        )
        connection.execute(
            f"CREATE OR REPLACE TABLE s.m AS SELECT CAST('{model_value}' AS {model_type}) AS v"
        )
        assert failing(chosen, connection) == [("top", expected)], (
            model_type,
            source_type,
            tolerance,
        )


def test_group_types_are_bound_on_both_sides(tmp_path: Path) -> None:
    chosen = model(
        tmp_path, "  - {name: by_meter, measure: sum(kwh), group_by: [meter], source: feed.m}\n"
    )
    connection = lake()
    connection.execute("CREATE TABLE feed.m AS SELECT 1 AS meter, 5.0000::DECIMAL(18,4) AS kwh")
    connection.execute(
        "CREATE TABLE s.m AS SELECT * FROM (VALUES ('1', 5.0000::DECIMAL(18,4)), "
        "('01', 5.0000::DECIMAL(18,4))) AS t(meter, kwh)"
    )
    (check,) = chosen.config.reconciliation or []
    rows = connection.execute(f"DESCRIBE {measures_sql(check, RELATION)}").fetchall()
    # The model groups a VARCHAR and the source an INTEGER: refused at validation.
    assert (rows[2][1], rows[3][1]) == ("VARCHAR", "INTEGER")
